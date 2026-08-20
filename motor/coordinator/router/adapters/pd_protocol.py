# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Stateless native-engine protocol mapping for PD requests."""

import hashlib
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from motor.common.constants import CHAT_COMPLETION_PREFIX, COMPLETION_PREFIX, COMPLETION_SUFFIX


class CoordinationMode(str, Enum):
    HANDOFF = "handoff"
    BOOTSTRAP = "bootstrap"
    TRIGGER = "trigger"


def trim_vllm_engine_request_id(request_id: str) -> str:
    """Strip vLLM/OpenAI prefixes so Coordinator can look up the original req_id."""
    value = str(request_id or "").strip()
    if value.startswith(CHAT_COMPLETION_PREFIX):
        return value.removeprefix(CHAT_COMPLETION_PREFIX)
    if value.startswith(COMPLETION_PREFIX) and value.endswith(COMPLETION_SUFFIX):
        return value.removeprefix(COMPLETION_PREFIX).removesuffix(COMPLETION_SUFFIX)
    return value


@dataclass(frozen=True)
class EngineEndpointMetadata:
    host: str
    bootstrap_port: int | None = None


@dataclass(frozen=True)
class LegContext:
    engine_request_id: str
    pair_id: str
    attempt_seq: int
    api: str
    endpoint: EngineEndpointMetadata
    peer_endpoint: EngineEndpointMetadata | None = None


@dataclass(frozen=True)
class EngineRequest:
    api: str
    body: dict[str, Any]


@dataclass(frozen=True)
class PrefillMetadata:
    handoff_ticket: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None


class EngineProtocolError(RuntimeError):
    """A native engine response or request cannot satisfy the PD contract."""

    def __init__(self, *, engine_type: str, phase: str, message: str) -> None:
        self.engine_type = engine_type
        self.phase = phase
        self.message = message
        super().__init__(f"{engine_type} {phase} protocol error: {message}")


class PDProtocolAdapter(Protocol):
    engine_type: str
    coordination_mode: CoordinationMode
    internal_response_fields: frozenset[str]

    def build_prefill_request(
        self,
        request: Mapping[str, Any],
        context: LegContext,
    ) -> EngineRequest: ...

    def parse_prefill_response(
        self,
        response: dict[str, Any],
    ) -> PrefillMetadata: ...

    def build_decode_request(
        self,
        request: Mapping[str, Any],
        context: LegContext,
        prefill: PrefillMetadata | None,
    ) -> EngineRequest: ...

    def inject_request_id(self, body: dict[str, Any], request_id: str) -> None: ...

    def build_abort_request(self, context: LegContext) -> EngineRequest | None: ...


class VllmProtocolAdapter:
    engine_type = "vllm"
    coordination_mode = CoordinationMode.HANDOFF
    internal_response_fields = frozenset({"kv_transfer_params"})

    def build_prefill_request(
        self,
        request: Mapping[str, Any],
        context: LegContext,
    ) -> EngineRequest:
        body = deepcopy(dict(request))
        self.inject_request_id(body, context.engine_request_id)
        body["stream"] = False
        body["max_tokens"] = 1
        body["min_tokens"] = 1
        body.pop("stream_options", None)
        if "max_completion_tokens" in body:
            body["max_completion_tokens"] = 1
        body["kv_transfer_params"] = {
            "do_remote_decode": True,
            "do_remote_prefill": False,
            "remote_engine_id": None,
            "remote_block_ids": None,
            "remote_host": None,
            "remote_port": None,
        }
        return EngineRequest(api=context.api, body=body)

    def parse_prefill_response(
        self,
        response: dict[str, Any],
    ) -> PrefillMetadata:
        kv_params = response.get("kv_transfer_params")
        if not isinstance(kv_params, dict) or not kv_params:
            raise EngineProtocolError(
                engine_type=self.engine_type,
                phase="prefill",
                message="Missing kv_transfer_params",
            )
        if kv_params.get("do_remote_prefill") is not True:
            raise EngineProtocolError(
                engine_type=self.engine_type,
                phase="prefill",
                message="do_remote_prefill must be true",
            )
        usage = response.get("usage")
        return PrefillMetadata(
            handoff_ticket=deepcopy(kv_params),
            usage=deepcopy(usage) if isinstance(usage, dict) else None,
        )

    def build_decode_request(
        self,
        request: Mapping[str, Any],
        context: LegContext,
        prefill: PrefillMetadata | None,
    ) -> EngineRequest:
        if prefill is None or not prefill.handoff_ticket:
            raise EngineProtocolError(
                engine_type=self.engine_type,
                phase="decode",
                message="Missing handoff ticket",
            )
        body = deepcopy(dict(request))
        self.inject_request_id(body, context.engine_request_id)
        body["kv_transfer_params"] = deepcopy(prefill.handoff_ticket)
        return EngineRequest(api=context.api, body=body)

    def inject_request_id(self, body: dict[str, Any], request_id: str) -> None:
        body.pop("rid", None)
        body["request_id"] = request_id

    def build_trigger_decode_request(
        self,
        request: Mapping[str, Any],
        context: LegContext,
        metaserver_url: str,
    ) -> EngineRequest:
        body = deepcopy(dict(request))
        self.inject_request_id(body, context.engine_request_id)
        body["kv_transfer_params"] = {
            "do_remote_decode": False,
            "do_remote_prefill": True,
            "metaserver": metaserver_url,
        }
        return EngineRequest(api=context.api, body=body)

    def build_trigger_prefill_request(
        self,
        request: Mapping[str, Any],
        context: LegContext,
        kv_transfer_params: Mapping[str, Any],
    ) -> EngineRequest:
        body = deepcopy(dict(request))
        self.inject_request_id(body, context.engine_request_id)
        body["stream"] = False
        body["max_tokens"] = 1
        body["min_tokens"] = 1
        body.pop("stream_options", None)
        if "max_completion_tokens" in body:
            body["max_completion_tokens"] = 1
        params = deepcopy(dict(kv_transfer_params))
        params["do_remote_decode"] = True
        params["do_remote_prefill"] = False
        params.pop("metaserver", None)
        body["kv_transfer_params"] = params
        return EngineRequest(api=context.api, body=body)

    def build_abort_request(self, context: LegContext) -> EngineRequest | None:
        del context


class SglangProtocolAdapter:
    engine_type = "sglang"
    coordination_mode = CoordinationMode.BOOTSTRAP
    internal_response_fields = frozenset({"bootstrap_host", "bootstrap_port", "bootstrap_room"})

    def build_prefill_request(
        self,
        request: Mapping[str, Any],
        context: LegContext,
    ) -> EngineRequest:
        engine_request = self._build_request(request, context, context.endpoint, phase="prefill")
        engine_request.body["stream"] = False
        engine_request.body.pop("stream_options", None)
        return engine_request

    def parse_prefill_response(
        self,
        response: dict[str, Any],
    ) -> PrefillMetadata:
        usage = response.get("usage")
        return PrefillMetadata(usage=deepcopy(usage) if isinstance(usage, dict) else None)

    def build_decode_request(
        self,
        request: Mapping[str, Any],
        context: LegContext,
        prefill: PrefillMetadata | None,
    ) -> EngineRequest:
        del prefill
        if context.peer_endpoint is None:
            raise EngineProtocolError(
                engine_type=self.engine_type,
                phase="decode",
                message="Missing prefill endpoint metadata",
            )
        return self._build_request(request, context, context.peer_endpoint, phase="decode")

    def inject_request_id(self, body: dict[str, Any], request_id: str) -> None:
        body.pop("request_id", None)
        body["rid"] = request_id

    def build_abort_request(self, context: LegContext) -> EngineRequest | None:
        return EngineRequest(api="abort_request", body={"rid": context.engine_request_id})

    def _build_request(
        self,
        request: Mapping[str, Any],
        context: LegContext,
        prefill_endpoint: EngineEndpointMetadata,
        *,
        phase: str,
    ) -> EngineRequest:
        self._validate_prefill_endpoint(prefill_endpoint, phase=phase)
        body = deepcopy(dict(request))
        self.inject_request_id(body, context.engine_request_id)
        body.update(
            {
                "bootstrap_host": prefill_endpoint.host,
                "bootstrap_port": prefill_endpoint.bootstrap_port,
                "bootstrap_room": self._stable_bootstrap_room(context.pair_id, context.attempt_seq),
            }
        )
        return EngineRequest(api=context.api, body=body)

    @classmethod
    def _validate_prefill_endpoint(cls, endpoint: EngineEndpointMetadata, *, phase: str) -> None:
        if not endpoint.host:
            raise EngineProtocolError(
                engine_type=cls.engine_type,
                phase=phase,
                message="Missing prefill bootstrap host",
            )
        port = endpoint.bootstrap_port
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise EngineProtocolError(
                engine_type=cls.engine_type,
                phase=phase,
                message="Missing or invalid prefill bootstrap port",
            )

    @staticmethod
    def _stable_bootstrap_room(pair_id: str, attempt_seq: int) -> int:
        raw = f"{pair_id}:{attempt_seq}".encode("utf-8")
        digest = hashlib.blake2b(raw, digest_size=8).digest()
        return int.from_bytes(digest, "big") & ((1 << 63) - 1)


ADAPTERS: Mapping[str, PDProtocolAdapter] = MappingProxyType(
    {
        VllmProtocolAdapter.engine_type: VllmProtocolAdapter(),
        SglangProtocolAdapter.engine_type: SglangProtocolAdapter(),
    }
)
