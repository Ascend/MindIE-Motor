# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE. See the Mulan PSL v2 for more details.

"""
Scheduler process ZMQ communication protocol.
Uses ROUTER/DEALER for multi-process scaling.
Serialization: msgspec.msgpack (high perf, replaces pickle).
Zero-copy: large dict/list in separate frames; main frame holds refs only to reduce buffer copy and memory peak.
"""

from enum import Enum
from typing import Any

import msgspec

from motor.common.logger import get_logger

logger = get_logger(__name__)


class SchedulerRequestType(str, Enum):
    """
    Scheduler request types. Scheduler process uses local InstanceManager for
    read-only queries; no IS_AVAILABLE/GET_ALL_INSTANCES.
    """

    ALLOCATE_ONLY = "allocate_only"  # Worker selects locally; Scheduler only allocates workload
    UPDATE_WORKLOAD = "update_workload"
    GET_AVAILABLE_INSTANCES = "get_available_instances"  # Worker fetches instance list and workload shm name
    REFRESH_INSTANCES = "refresh_instances"
    CONFIRM_SAMPLE = "confirm_sample"  # cross-worker precision sampling exit gate
    RECORD_PRECISION_RESULT = "record_precision_result"  # global consecutive + probing
    FINISH_PRECISION_ACTION = "finish_precision_action"  # clear probing after probe/alarm
    CIRCUIT_BREAKER_REPORT = "circuit_breaker_report"  # worker reports instance failure/success


class SchedulerResponseType(str, Enum):
    """
    Scheduler response types.
    """

    SUCCESS = "success"
    ERROR = "error"


class SchedulerRequest(msgspec.Struct):
    """
    Scheduler request message (msgspec.Struct, native msgpack serialization).
    """

    request_type: str
    request_id: str
    data: dict[str, Any]


class SchedulerResponse(msgspec.Struct):
    """
    Scheduler response message (msgspec.Struct, native msgpack serialization).
    """

    response_type: str
    request_id: str
    data: dict[str, Any] | None = None
    error: str | None = None


# Candidate policy values for ALLOCATE_ONLY request data["candidate_policy"].
CANDIDATE_POLICY_LOAD_BALANCE = "load_balance"
CANDIDATE_POLICY_ROUND_ROBIN = "round_robin"
CANDIDATE_POLICY_KV_CACHE_AFFINITY = "kv_cache_affinity"
KNOWN_CANDIDATE_POLICIES = frozenset(
    {
        CANDIDATE_POLICY_LOAD_BALANCE,
        CANDIDATE_POLICY_ROUND_ROBIN,
        CANDIDATE_POLICY_KV_CACHE_AFFINITY,
    }
)

# Zero-copy: dict/list larger than this (bytes) go in separate frame; main frame stores __ref__ index only
ZERO_COPY_REF_KEY = "__ref__"
DEFAULT_ZERO_COPY_THRESHOLD = 1024

# PUB/SUB topic for instance list change notifications (multipart: [topic, version_bytes])
INSTANCE_CHANGE_TOPIC = b"instances_changed"

# PUB/SUB topic for circuit breaker state change (multipart: [topic, msgpack_payload])
CIRCUIT_BREAKER_TOPIC = b"circuit_breaker"


def _enc_hook(obj: Any) -> Any:
    """Encode: Enum -> .value, Pydantic BaseModel -> dict for compatibility."""
    if isinstance(obj, Enum):
        return obj.value
    # Support Pydantic BaseModel direct serialization (avoid model_dump + encode double serialize)
    if hasattr(obj, 'model_dump'):  # Check if Pydantic BaseModel
        return obj.model_dump(mode='json')
    raise TypeError(f"Object of type {type(obj)} is not supported")


def _replace_large_with_refs(
    obj: Any,
    threshold: int,
    aux_list: list[bytes],
    encoder: msgspec.msgpack.Encoder,
) -> Any:
    """Recursively replace 'large' dict/list in data with {__ref__: index}, append serialized blobs to aux_list."""
    if isinstance(obj, dict):
        if obj.get(ZERO_COPY_REF_KEY) is not None and len(obj) == 1:
            return obj
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                try:
                    blob = encoder.encode(v)
                except Exception as e:
                    logger.warning("ZMQ encode failed for dict/list value, using empty blob: %s", e)
                    blob = b""
                if len(blob) > threshold:
                    aux_list.append(blob)
                    out[k] = {ZERO_COPY_REF_KEY: len(aux_list) - 1}
                else:
                    out[k] = _replace_large_with_refs(v, threshold, aux_list, encoder)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        try:
            blob = encoder.encode(obj)
        except Exception as e:
            logger.warning("ZMQ encode failed for list, using empty blob: %s", e)
            blob = b""
        if len(blob) > threshold:
            aux_list.append(blob)
            return {ZERO_COPY_REF_KEY: len(aux_list) - 1}
        return [_replace_large_with_refs(x, threshold, aux_list, encoder) for x in obj]
    return obj


def _resolve_refs(obj: Any, aux_buffers: list[bytes], decoder: msgspec.msgpack.Decoder) -> Any:
    """Recursively replace {__ref__: index} in decoded result with decoded aux_buffers[index]."""
    if isinstance(obj, dict):
        if set(obj.keys()) == {ZERO_COPY_REF_KEY} and isinstance(obj.get(ZERO_COPY_REF_KEY), int):
            idx = obj[ZERO_COPY_REF_KEY]
            if 0 <= idx < len(aux_buffers):
                decoded = decoder.decode(aux_buffers[idx])
                return _resolve_refs(decoded, aux_buffers, decoder)
        return {k: _resolve_refs(v, aux_buffers, decoder) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_refs(x, aux_buffers, decoder) for x in obj]
    return obj


class ZMQMessageSerializer:
    """
    ZMQ message serializer (msgspec.msgpack + zero-copy large objects).
    """

    def __init__(self, zero_copy_threshold: int = DEFAULT_ZERO_COPY_THRESHOLD) -> None:
        self._encoder = msgspec.msgpack.Encoder(enc_hook=_enc_hook)
        self._request_decoder = msgspec.msgpack.Decoder(SchedulerRequest)
        self._response_decoder = msgspec.msgpack.Decoder(SchedulerResponse)
        self._generic_decoder = msgspec.msgpack.Decoder()
        self._zero_copy_threshold = zero_copy_threshold

    def serialize_request(self, request: SchedulerRequest) -> bytes | list[bytes]:
        """Serialize request. Returns single bytes or [main_buf, *aux_buffers] for multipart send."""
        aux: list[bytes] = []
        data = _replace_large_with_refs(request.data, self._zero_copy_threshold, aux, self._encoder)
        modified = SchedulerRequest(request_type=request.request_type, request_id=request.request_id, data=data)
        main_buf = self._encoder.encode(modified)
        if not aux:
            return main_buf
        return [main_buf] + aux

    def deserialize_request(self, data: bytes | list[bytes]) -> SchedulerRequest:
        """Deserialize request. data may be single bytes or multipart list [main_buf, *aux]."""
        if isinstance(data, bytes):
            return self._request_decoder.decode(data)
        main_buf = data[0]
        aux = data[1:]
        obj = self._request_decoder.decode(main_buf)
        if not aux:
            return obj
        resolved_data = _resolve_refs(obj.data, aux, self._generic_decoder)
        return SchedulerRequest(request_type=obj.request_type, request_id=obj.request_id, data=resolved_data)

    def serialize_response(self, response: SchedulerResponse) -> bytes | list[bytes]:
        """Serialize response. Returns single bytes or [main_buf, *aux_buffers]."""
        aux: list[bytes] = []
        data = response.data
        if data is not None:
            data = _replace_large_with_refs(data, self._zero_copy_threshold, aux, self._encoder)
        modified = SchedulerResponse(
            response_type=response.response_type,
            request_id=response.request_id,
            data=data,
            error=response.error,
        )
        main_buf = self._encoder.encode(modified)
        if not aux:
            return main_buf
        return [main_buf] + aux

    def deserialize_response(self, data: bytes | list[bytes]) -> SchedulerResponse:
        """Deserialize response. data may be single bytes or multipart list."""
        if isinstance(data, bytes):
            return self._response_decoder.decode(data)
        main_buf = data[0]
        aux = data[1:]
        obj = self._response_decoder.decode(main_buf)
        if not aux:
            return obj
        resolved_data = _resolve_refs(obj.data, aux, self._generic_decoder) if obj.data else None
        return SchedulerResponse(
            response_type=obj.response_type,
            request_id=obj.request_id,
            data=resolved_data,
            error=obj.error,
        )


def pack_send_frames(prefix: list[bytes], payload: bytes | list[bytes]) -> list[bytes]:
    """Pack serialized result into ZMQ send frames. payload is single frame or zero-copy [main, *aux]."""
    if isinstance(payload, list):
        return prefix + payload
    return prefix + [payload]


def unpack_recv_payload(parts: list[bytes], payload_start: int = 1) -> bytes | list[bytes]:
    """Extract payload from recv_multipart parts: single frame returns bytes, multi returns list[bytes]."""
    if len(parts) <= payload_start:
        return b""
    if len(parts) == payload_start + 1:
        return parts[payload_start]
    return parts[payload_start:]
