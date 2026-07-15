# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Async Scheduler client (zmq.asyncio, works with AsyncSchedulerServer)."""

import asyncio
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable

import msgspec
import zmq

from motor.common.resources.dispatch import (
    has_compatible_dispatch_pair,
)
from motor.common.resources.instance import Instance, PDRole
from motor.common.resources.endpoint import Endpoint, Workload
from motor.coordinator.domain import (
    InstanceReadiness,
    UpdateWorkloadParams,
    readiness_from_instances,
)
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    SchedulerRequest,
    SchedulerResponse,
    SchedulerRequestType,
    SchedulerResponseType,
    INSTANCE_CHANGE_TOPIC,
    CIRCUIT_BREAKER_TOPIC,
    CANDIDATE_POLICY_LOAD_BALANCE,
    CANDIDATE_POLICY_ROUND_ROBIN,
    CANDIDATE_POLICY_KV_CACHE_AFFINITY,
    pack_send_frames,
    unpack_recv_payload,
    ZMQMessageSerializer,
)
from motor.common.logger import get_logger
from motor.config.coordinator import (
    KV_AFFINITY_MODE_UNIFIED,
    KV_AFFINITY_MODES,
)
from motor.coordinator.fault_tolerance.precision.streak_result import (
    PrecisionStreakResult,
)
from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy
from motor.coordinator.scheduler.policy.round_robin import RoundRobinPolicy
from motor.coordinator.scheduler.policy.kv_cache_affinity import KvCacheAffinityPolicy
from motor.coordinator.domain.workload_calculator import calculate_demand_workload
from motor.coordinator.domain.scheduling_pin import (
    resolve_pinned_instance,
    select_endpoint_for_instance,
)
from motor.coordinator.models.request import RequestInfo

logger = get_logger(__name__)

# Callback signature: receives active endpoint list [(ip, port), ...], returns None
OnInstanceRefreshedCallback = Callable[[list[tuple[str, str]]], Awaitable[None]]

# Number of affinity-ranked candidates a prefill request proposes to the scheduler. The scheduler
# re-picks among them by its authoritative workload ledger, spreading bursts across the top few.
_AFFINITY_CANDIDATE_TOPK = 3


class SchedulerRequestFailureReason(str, Enum):
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    DISCONNECTED = "disconnected"
    TRANSPORT_ERROR = "transport_error"
    NO_RESPONSE = "no_response"


@dataclass(frozen=True)
class SchedulerRequestResult:
    response: SchedulerResponse | None = None
    failure_reason: SchedulerRequestFailureReason | None = None
    error: str | None = None


def _collect_active_endpoints_from_cache(
    cache: "_SchedulerInstanceCache",
) -> list[tuple[str, str]]:
    """
    Extract status=normal (ip, business_port) from SchedulerInstanceCache.
    Keeps endpoints whose status is normal.
    """
    endpoints: list[tuple[str, str]] = []
    for role in (PDRole.ROLE_E, PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_U):
        for inst in cache.get_instances(role):
            if not inst or not inst.endpoints:
                continue
            for pod_eps in (inst.endpoints or {}).values():
                for ep in (pod_eps or {}).values():
                    status_val = ep.status.value if hasattr(ep.status, "value") else str(ep.status)
                    if status_val == "normal":
                        endpoints.append((ep.ip, str(ep.business_port)))
    return endpoints


def _instance_to_dict(instance: Instance | None) -> dict:
    """Instance -> dict for ZMQ (model_dump)."""
    return instance.model_dump(mode="json") if instance else {}


def _instance_from_dict(data: dict) -> Instance | None:
    """Dict -> Instance for ZMQ (model_validate)."""
    if not data:
        return None
    try:
        return Instance.model_validate(data)
    except Exception as e:
        logger.error("Failed to deserialize instance: %s", e, exc_info=True)
        return None


def _endpoint_from_dict(data: dict) -> Endpoint | None:
    """Dict -> Endpoint for ZMQ (model_validate)."""
    if not data:
        return None
    try:
        return Endpoint.model_validate(data)
    except Exception as e:
        logger.error("Failed to deserialize endpoint: %s", e, exc_info=True)
        return None


class _SchedulerInstanceCache:
    """
    Instance cache with lock-free reads, incremental role updates, and workload patch from shm.
    """

    def __init__(self):
        self._instance_cache: dict[PDRole, list[Instance]] = {
            PDRole.ROLE_E: [],
            PDRole.ROLE_P: [],
            PDRole.ROLE_D: [],
            PDRole.ROLE_U: [],
        }
        self._instance_map: dict[PDRole, dict[int, Instance]] = {
            PDRole.ROLE_E: {},
            PDRole.ROLE_P: {},
            PDRole.ROLE_D: {},
            PDRole.ROLE_U: {},
        }
        self._endpoint_map: dict[tuple[int, int], Endpoint] = {}
        self._lock = asyncio.Lock()

    def get_instances(self, role: PDRole) -> list[Instance]:
        return self._instance_cache.get(role, [])

    async def replace_all(self, role: PDRole, instances: list[Instance]) -> None:
        """Update cache for one role only; incremental map update to reduce lock hold time."""
        async with self._lock:
            self._apply_role_under_lock(role, instances)

    def patch_workload_from_shm(
        self,
        instance_id: int,
        endpoint_id: int,
        role: PDRole,
        active_tokens: float,
        active_kv_cache: float,
    ) -> None:
        """Patch single endpoint workload from shared memory. Skip if not in cache."""
        role_map = self._instance_map.get(role) or {}
        cached_instance = role_map.get(instance_id)
        if not cached_instance:
            return
        cached_endpoint = self._endpoint_map.get((instance_id, endpoint_id))
        if not cached_endpoint:
            return
        old_workload = cached_endpoint.workload or Workload()
        cached_endpoint.workload = Workload(
            active_tokens=active_tokens,
            active_kv_cache=active_kv_cache,
        )
        if cached_instance.gathered_workload is None:
            cached_instance.gathered_workload = Workload()
        cached_instance.gathered_workload.active_tokens += active_tokens - old_workload.active_tokens
        cached_instance.gathered_workload.active_kv_cache += active_kv_cache - old_workload.active_kv_cache

    def _apply_role_under_lock(self, role: PDRole, instances: list[Instance]) -> None:
        """Update cache and maps for one role. Must be called with _lock held."""
        old_ids_role = set((self._instance_map.get(role) or {}).keys())
        self._instance_cache[role] = instances
        self._instance_map[role] = {inst.id: inst for inst in instances}
        for key in list(self._endpoint_map.keys()):
            if key[0] in old_ids_role:
                del self._endpoint_map[key]
        for inst in instances:
            if inst.endpoints:
                for pod_eps in (inst.endpoints or {}).values():
                    for ep in (pod_eps or {}).values():
                        self._endpoint_map[(inst.id, ep.id)] = ep

    @staticmethod
    def _role_of(inst: Instance) -> PDRole | None:
        role = getattr(inst, "role", None)
        if isinstance(role, PDRole):
            return role
        if role is None:
            return None
        normalized_role = str(role).strip().lower()
        # "hybrid" predates PDRole.ROLE_U ("union").  The enum itself handles
        # all canonical roles and the historical "both" alias, including future
        # values added to PDRole.
        if normalized_role == "hybrid":
            return PDRole.ROLE_U
        try:
            return PDRole(normalized_role)
        except ValueError:
            return None

    async def apply_add(self, instances: list[Instance]) -> bool:
        """Incrementally upsert instances (from a PUB ADD delta), keeping each role list sorted by
        id, so a worker patches its cache on a topology change without a full GET round-trip. An
        ADD may also update an existing instance, so remove its prior role and endpoint entries
        before inserting the replacement. Returns False without mutation when a role is unknown,
        so the caller can fall back to a full refresh instead of accepting an incomplete delta.
        """
        resolved_instances = [(inst, self._role_of(inst)) for inst in instances]
        unknown_instances = [inst for inst, role in resolved_instances if role is None]
        if unknown_instances:
            logger.warning(
                "Rejecting instance ADD delta with unknown role(s); falling back to full refresh: %s",
                [(getattr(inst, "id", None), getattr(inst, "role", None)) for inst in unknown_instances],
            )
            return False
        async with self._lock:
            for inst, role in resolved_instances:
                for existing_role in (PDRole.ROLE_E, PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_U):
                    role_map = self._instance_map.get(existing_role)
                    if role_map and inst.id in role_map:
                        del role_map[inst.id]
                        self._instance_cache[existing_role] = sorted(role_map.values(), key=lambda i: i.id)
                for key in [key for key in self._endpoint_map if key[0] == inst.id]:
                    del self._endpoint_map[key]
                role_map = self._instance_map.setdefault(role, {})
                role_map[inst.id] = inst
                self._instance_cache[role] = sorted(role_map.values(), key=lambda i: i.id)
                if inst.endpoints:
                    for pod_eps in (inst.endpoints or {}).values():
                        for ep in (pod_eps or {}).values():
                            self._endpoint_map[(inst.id, ep.id)] = ep
        return True

    async def apply_remove(self, instances: list[Instance]) -> None:
        """Incrementally drop instances (from a PUB DEL delta) from every role list and the endpoint
        map. Role is searched across all pools so a stale role on the delta cannot orphan an entry.
        """
        async with self._lock:
            for inst in instances:
                iid = inst.id
                for role in (PDRole.ROLE_E, PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_U):
                    role_map = self._instance_map.get(role)
                    if role_map and iid in role_map:
                        del role_map[iid]
                        self._instance_cache[role] = sorted(role_map.values(), key=lambda i: i.id)
                for key in [k for k in self._endpoint_map if k[0] == iid]:
                    del self._endpoint_map[key]


class _SchedulerTransport:
    def __init__(
        self,
        scheduler_address: str,
        timeout: float,
        serializer: Any | None = None,
    ) -> None:
        self._scheduler_address = scheduler_address
        self._timeout = timeout
        self._serializer = serializer or ZMQMessageSerializer()
        self._cleanup_delay = timeout * 2

        self._context: zmq.asyncio.Context | None = None
        self._socket: zmq.asyncio.Socket | None = None
        self.connected = False
        self._connect_lock = asyncio.Lock()
        self._pending_requests: dict[str, tuple[asyncio.Event | None, float] | None] = {}
        self._pending_responses: dict[str, SchedulerResponse] = {}
        self._request_lock = asyncio.Lock()
        self._receive_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def connect(self) -> bool:
        async with self._connect_lock:
            if self.connected:
                return True
            try:
                self._context = zmq.asyncio.Context()
                self._socket = self._context.socket(zmq.DEALER)
                self._socket.connect(self._scheduler_address)
                self.connected = True
                self._receive_task = asyncio.create_task(self._receive_loop())
                logger.info("Scheduler transport connected to %s", self._scheduler_address)
                return True
            except Exception as e:
                logger.error("Failed to connect scheduler transport: %s", e, exc_info=True)
                await self._close_connection()
                return False

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        await self._close_connection()

    async def send_request(self, request: SchedulerRequest) -> SchedulerResponse | None:
        result = await self.send_request_result(request)
        return result.response

    async def send_request_result(self, request: SchedulerRequest) -> SchedulerRequestResult:
        if not self.connected or not self._socket:
            logger.error("Scheduler transport not connected")
            return SchedulerRequestResult(failure_reason=SchedulerRequestFailureReason.DISCONNECTED)
        event = asyncio.Event()
        request_timestamp = time.time()
        async with self._request_lock:
            self._pending_requests[request.request_id] = (event, request_timestamp)
        log_req_id = (request.data or {}).get("req_id") or request.request_id
        logger.debug(
            "Scheduler request sent request_type=%s req_id=%s",
            request.request_type,
            log_req_id,
        )
        try:
            # serialize_request is synchronous (msgspec, no await), so the single event loop already
            # runs it atomically; a lock around no-await code is never contended. Same for decode below.
            serialized = self._serializer.serialize_request(request)
            await self._socket.send_multipart(pack_send_frames([b""], serialized))
            try:
                await asyncio.wait_for(event.wait(), timeout=self._timeout)
            except asyncio.TimeoutError:
                elapsed_ms = (time.time() - request_timestamp) * 1000
                logger.warning(
                    "Scheduler request timeout request_type=%s req_id=%s elapsed_ms=%.1f",
                    request.request_type,
                    log_req_id,
                    elapsed_ms,
                )
                async with self._request_lock:
                    if request.request_id in self._pending_requests:
                        self._pending_requests[request.request_id] = (
                            None,
                            request_timestamp,
                        )
                return SchedulerRequestResult(failure_reason=SchedulerRequestFailureReason.TIMEOUT)
            async with self._request_lock:
                pending_info = self._pending_requests.get(request.request_id)
                if pending_info:
                    pending_event, _ = pending_info
                    if pending_event and pending_event.is_set():
                        response = self._pending_responses.pop(request.request_id, None)
                        self._pending_requests.pop(request.request_id, None)
                    else:
                        response = None
                        self._pending_responses.pop(request.request_id, None)
                        self._pending_requests.pop(request.request_id, None)
                else:
                    response = None
                    self._pending_responses.pop(request.request_id, None)
                elapsed_ms = (time.time() - request_timestamp) * 1000
                logger.debug(
                    "Scheduler request done request_type=%s req_id=%s elapsed_ms=%.1f",
                    request.request_type,
                    log_req_id,
                    elapsed_ms,
                )
                if response is None:
                    return SchedulerRequestResult(failure_reason=SchedulerRequestFailureReason.NO_RESPONSE)
                return SchedulerRequestResult(response=response)
        except asyncio.CancelledError:
            logger.warning(
                "Scheduler request cancelled request_type=%s req_id=%s",
                request.request_type,
                log_req_id,
            )
            async with self._request_lock:
                self._pending_requests.pop(request.request_id, None)
                self._pending_responses.pop(request.request_id, None)
            return SchedulerRequestResult(failure_reason=SchedulerRequestFailureReason.CANCELLED)
        except Exception as e:
            elapsed_ms = (time.time() - request_timestamp) * 1000
            logger.error(
                "Scheduler request error request_type=%s req_id=%s elapsed_ms=%.1f error=%s",
                request.request_type,
                log_req_id,
                elapsed_ms,
                e,
                exc_info=True,
            )
            async with self._request_lock:
                self._pending_requests.pop(request.request_id, None)
                self._pending_responses.pop(request.request_id, None)
            reason = (
                SchedulerRequestFailureReason.DISCONNECTED
                if isinstance(e, zmq.ZMQError) or not self.connected or not self._socket
                else SchedulerRequestFailureReason.TRANSPORT_ERROR
            )
            return SchedulerRequestResult(failure_reason=reason, error=str(e))

    async def _close_connection(self) -> None:
        async with self._connect_lock:
            self.connected = False
            if self._socket:
                try:
                    self._socket.close()
                except Exception as e:
                    logger.warning("Error closing scheduler transport socket: %s", e)
                self._socket = None
            if self._context:
                try:
                    # term() is synchronous on zmq.asyncio.Context; do not await.
                    self._context.term()
                except Exception as e:
                    logger.warning("Error terminating scheduler transport context: %s", e)
                self._context = None

    async def _receive_loop(self) -> None:
        try:
            while not self._stop_event.is_set() and self.connected and self._socket:
                try:
                    parts = await asyncio.wait_for(
                        self._socket.recv_multipart(),
                        timeout=self._timeout,
                    )
                    if len(parts) < 2:
                        continue
                    # deserialize_response is synchronous; no decode lock needed (see send path).
                    response = self._serializer.deserialize_response(unpack_recv_payload(parts))
                    async with self._request_lock:
                        pending_info = self._pending_requests.get(response.request_id)
                        if pending_info is None:
                            self._pending_responses.pop(response.request_id, None)
                            continue
                        event, req_timestamp = pending_info
                        current_time = time.time()
                        if event is None:
                            if current_time - req_timestamp > self._cleanup_delay:
                                self._pending_requests.pop(response.request_id, None)
                                self._pending_responses.pop(response.request_id, None)
                            else:
                                logger.debug(
                                    "Received delayed response for request %s (timeout: %.3fs)",
                                    response.request_id,
                                    current_time - req_timestamp,
                                )
                        elif not event.is_set():
                            self._pending_responses[response.request_id] = response
                            event.set()
                        else:
                            self._pending_responses.pop(response.request_id, None)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            logger.debug("Scheduler transport receive loop cancelled")
        except Exception as e:
            if not self._stop_event.is_set():
                logger.error("Scheduler transport receive loop error: %s", e, exc_info=True)


# Callback when instance list change is received from Scheduler PUB; args: instance_version, optional
# incremental delta ({"event": "add"|"del", "instances": [...]}) or None for version-only messages.
OnInstanceChangeNotify = Callable[[int | None, dict | None], Awaitable[None]]

# Callback when circuit breaker state change is received from Scheduler PUB
OnCircuitBreakerChangeNotify = Callable[[int, str], Awaitable[None]]
# args: instance_id, state ("open"|"closed")

# ZMQ PUB does not queue; SUB must be ready before PUB sends. Short delay after connect.
_INSTANCE_PUB_SUB_SETTLE_MS = 150
# Roles that should use kv_cache_affinity scheduling.
_KVA_SELECT_ROLES = frozenset({PDRole.ROLE_P, PDRole.ROLE_U})


class _InstancePushSubscriber:
    """
    SUB socket that listens for Scheduler PUB notifications.

    Instance-change messages trigger instance cache refresh.
    Uses its own ZMQ context to avoid coupling with DEALER transport.
    """

    def __init__(
        self,
        sub_address: str,
        on_instance_change: OnInstanceChangeNotify,
        on_circuit_breaker_change: OnCircuitBreakerChangeNotify | None = None,
    ) -> None:
        self._sub_address = sub_address
        self._on_instance_change = on_instance_change
        self._on_circuit_breaker_change = on_circuit_breaker_change
        self._context: zmq.asyncio.Context | None = None
        self._socket: zmq.asyncio.Socket | None = None
        self._stop_event = asyncio.Event()
        self._recv_task: asyncio.Task | None = None

    async def connect(self) -> bool:
        # Idempotent: if already connected or half-closed, disconnect first so recv_loop can run again.
        if self._recv_task or self._socket or self._context:
            await self.disconnect()
        self._stop_event.clear()
        try:
            self._context = zmq.asyncio.Context()
            self._socket = self._context.socket(zmq.SUB)
            self._socket.connect(self._sub_address)
            self._socket.subscribe(b"")
            # ZMQ PUB does not buffer; allow connection to settle so we don't miss the next message.
            await asyncio.sleep(_INSTANCE_PUB_SUB_SETTLE_MS / 1000.0)
            self._recv_task = asyncio.create_task(self._recv_loop())
            logger.info("Instance push SUB connected to %s", self._sub_address)
            return True
        except Exception as e:
            logger.warning("Failed to connect instance push SUB to %s: %s", self._sub_address, e)
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        self._stop_event.set()
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None
        if self._socket:
            try:
                self._socket.close()
            except Exception as e:
                logger.debug("Error closing instance push SUB socket: %s", e)
            self._socket = None
        if self._context:
            try:
                # term() is synchronous on zmq.asyncio.Context; do not await.
                self._context.term()
            except Exception as e:
                logger.debug("Error terminating instance push context: %s", e)
            self._context = None

    async def _recv_loop(self) -> None:
        try:
            while not self._stop_event.is_set() and self._socket:
                try:
                    frames = await self._socket.recv_multipart()
                    topic = frames[0] if frames else b""
                    if topic == INSTANCE_CHANGE_TOPIC:
                        version = self._parse_int_frame(frames, 1)
                        delta = self._parse_msgpack_frame(frames, 2)
                        await self._on_instance_change(version, delta)
                    elif topic == CIRCUIT_BREAKER_TOPIC and self._on_circuit_breaker_change:
                        payload = self._parse_msgpack_frame(frames, 1)
                        if payload and isinstance(payload, dict):
                            await self._on_circuit_breaker_change(
                                int(payload.get("instance_id", 0)),
                                str(payload.get("state", "")),
                            )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning("Instance push SUB recv/notify error: %s", e)
                    await asyncio.sleep(1.0)  # Avoid tight loop on persistent errors
        except asyncio.CancelledError:
            logger.debug("Instance push SUB recv loop cancelled")
        except Exception as e:
            if not self._stop_event.is_set():
                logger.error("Instance push SUB recv loop error: %s", e, exc_info=True)

    @staticmethod
    def _parse_int_frame(frames: list[bytes], index: int) -> int | None:
        """Parse int from a multipart frame."""
        if len(frames) <= index:
            return None
        try:
            return int(frames[index].decode())
        except (ValueError, UnicodeDecodeError):
            return None

    @staticmethod
    def _parse_msgpack_frame(frames: list[bytes], index: int) -> dict | None:
        """Parse msgpack-encoded dict from a multipart frame."""
        if len(frames) <= index:
            return None
        try:
            return msgspec.msgpack.decode(frames[index])
        except Exception:
            return None


@dataclass
class SchedulerClientConfig:
    """
    Config for AsyncSchedulerClient (G.FNM.03: encapsulate many related args).
    """

    scheduler_address: str = "ipc:///tmp/scheduler_frontend"
    instance_pub_address: str = ""  # SUB to Scheduler PUB for instance-change push; empty disables
    timeout: float = 5.0
    reconnect_interval: float = 5.0
    scheduler_type: str | None = None
    client_index: int = 0
    client_count: int = 1
    endpoint_instance_score_weight: float = 0.05
    # kv_cache_affinity tunables (see SchedulerConfig). mode = "unified" | "load_gated".
    kv_affinity_mode: str = KV_AFFINITY_MODE_UNIFIED
    kv_affinity_load_weight: float = 1.0
    kv_affinity_overlap_credit: float = 1.0
    kv_affinity_prefill_load_scale: float = 1.0
    # Load-gated affinity: keep only the N least-loaded endpoints, then pick the best prefix
    # match among them. 0 disables it (uses the unified-score / legacy path instead).
    kv_affinity_load_gate_topn: int = 0
    tls_config: Any | None = None
    on_instance_refreshed: OnInstanceRefreshedCallback | None = None


class AsyncSchedulerClient:
    """
    Fully async Scheduler client (works with AsyncSchedulerServer).
    Implements SchedulingFacade (select_and_allocate, update_workload) for BaseRouter injection.
    """

    def __init__(self, config: SchedulerClientConfig):
        self.scheduler_address = config.scheduler_address
        self.timeout = config.timeout
        self._client_index = max(0, config.client_index)
        self._client_count = max(1, config.client_count)
        # Per-request ids: one-time full-uuid prefix + monotonic counter, avoiding a uuid4() per call.
        # Correctness only needs client-local uniqueness -- the transport matches replies in its own
        # _pending_requests dict keyed by request_id, on its own DEALER socket; the scheduler only
        # echoes it back. The full 128-bit prefix keeps ids effectively globally unique anyway, so
        # cross-process log/trace correlation stays unambiguous.
        self._request_id_prefix = uuid.uuid4().hex
        self._request_seq = 0
        self._endpoint_instance_score_weight = max(0.0, config.endpoint_instance_score_weight)
        mode = str(config.kv_affinity_mode or KV_AFFINITY_MODE_UNIFIED).lower()
        if mode not in KV_AFFINITY_MODES:
            logger.warning(
                "Invalid kv_affinity_mode %r; expected one of %s. Falling back to %r.",
                config.kv_affinity_mode,
                KV_AFFINITY_MODES,
                KV_AFFINITY_MODE_UNIFIED,
            )
            mode = KV_AFFINITY_MODE_UNIFIED
        self._kv_affinity_mode = mode
        self._kv_affinity_load_weight = max(0.0, config.kv_affinity_load_weight)
        self._kv_affinity_overlap_credit = max(0.0, config.kv_affinity_overlap_credit)
        self._kv_affinity_prefill_load_scale = max(0.0, config.kv_affinity_prefill_load_scale)
        self._kv_affinity_load_gate_topn = max(0, int(config.kv_affinity_load_gate_topn))

        self._serializer = ZMQMessageSerializer()
        self._transport = _SchedulerTransport(config.scheduler_address, config.timeout, self._serializer)
        self._cache = _SchedulerInstanceCache()
        self._instance_rr_counters: dict[PDRole, int] = {}
        self._endpoint_rr_counters: dict[int, int] = {}
        self._scheduler_type: str = config.scheduler_type or "round_robin"
        self._workload_reader = None
        self._last_instance_version: int | None = None
        self._on_instance_refreshed = config.on_instance_refreshed
        self._cb_blocked_instances: set[int] = set()

        instance_pub = (config.instance_pub_address or "").strip()
        self._push_subscriber = (
            _InstancePushSubscriber(
                instance_pub,
                self._on_instance_change_notify,
                self._on_circuit_breaker_change,
            )
            if instance_pub
            else None
        )

    @property
    def connected(self) -> bool:
        return self._transport.connected

    async def connect(self) -> bool:
        success = await self._transport.connect()
        if success:
            await self._init_cache()
        if success and self._push_subscriber:
            sub_ok = await self._push_subscriber.connect()
            if sub_ok:
                # Initial sync after SUB is ready (covers any message lost during connect).
                try:
                    await self.get_available_instances(None)
                except Exception as e:
                    logger.debug("Post-SUB connect sync failed: %s", e)
            else:
                logger.debug("Instance push SUB disabled; cache will refresh on next request/shm")
        if success:
            logger.info("Async scheduler client connected to %s", self.scheduler_address)
        return success

    async def disconnect(self) -> None:
        try:
            if self._push_subscriber:
                await self._push_subscriber.disconnect()
            if self._workload_reader:
                self._workload_reader.detach()
                self._workload_reader = None
        finally:
            # Always close transport so ZMQ context is terminated even if above steps raise.
            await self._transport.disconnect()

    async def _send_request_result(self, request: SchedulerRequest) -> SchedulerRequestResult:
        if hasattr(type(self._transport), "send_request_result"):
            return await self._transport.send_request_result(request)
        response = await self._transport.send_request(request)
        if response is None:
            return SchedulerRequestResult(failure_reason=SchedulerRequestFailureReason.NO_RESPONSE)
        return SchedulerRequestResult(response=response)

    def _next_request_id(self) -> str:
        """Cheap monotonic request id (client-local uniqueness is all the transport needs)."""
        self._request_seq += 1
        return f"{self._request_id_prefix}-{self._request_seq}"

    async def _select_endpoint_candidates(
        self,
        req_info: RequestInfo,
        role: PDRole | None = None,
        top_k: int = 1,
    ) -> list[tuple[Instance, Endpoint, float]]:
        candidates, _ = await self._select_endpoint_candidates_with_policy(req_info, role, top_k)
        return candidates

    async def _select_endpoint_candidates_with_policy(
        self,
        req_info: RequestInfo,
        role: PDRole | None = None,
        top_k: int = 1,
    ) -> tuple[list[tuple[Instance, Endpoint, float]], str]:
        """Select endpoint candidates from cache or fresh instances."""
        cache_role = role if role is not None else PDRole.ROLE_U
        cached_instances = self._cache.get_instances(cache_role)
        if cached_instances:
            # Cache stores instances sorted by id (see replace_all call sites); use as-is for RR
            candidates, candidate_policy = self._select_endpoint_candidates_from_list_with_policy(
                cached_instances, cache_role, req_info, top_k=top_k
            )
            if candidates:
                logger.debug(
                    "Selected %d endpoint candidate(s) from cache (role=%s, policy=%s)",
                    len(candidates),
                    role,
                    self._scheduler_type,
                )
                return candidates, candidate_policy
        instances = await self.get_available_instances(role)
        if not instances:
            return [], self._scheduler_type or CANDIDATE_POLICY_ROUND_ROBIN

        # get_available_instances already wrote sorted list to cache; build sorted list once for this path
        instance_list = sorted(instances.values(), key=lambda i: i.id)
        candidates, candidate_policy = self._select_endpoint_candidates_from_list_with_policy(
            instance_list, cache_role, req_info, top_k=top_k
        )
        if candidates:
            logger.debug(
                "Selected %d endpoint candidate(s) from fresh fetch (role=%s, policy=%s)",
                len(candidates),
                role,
                self._scheduler_type,
            )
        return candidates, candidate_policy

    async def _refresh_cache_from_workload_reader(self, role: PDRole | None = None) -> None:
        """Patch live workload into the local cache and pull a fresh instance list on
        heartbeat-stale or instance-version change.

        Runs before select_and_allocate so each role's selection makes load-aware decisions on
        fresh workload and instance membership.
        """
        if not self._workload_reader:
            return
        current_version, heartbeat_stale = self._workload_reader.read_and_patch_cache(self._cache, role=role)
        if heartbeat_stale:
            await self._pull_instances_and_notify(current_version, "stale heartbeat")
        elif current_version is not None:
            if self._last_instance_version is not None and current_version != self._last_instance_version:
                await self._pull_instances_and_notify(current_version, "version change")
            else:
                self._last_instance_version = current_version

    async def _pull_instances_and_notify(self, current_version, reason: str) -> None:
        """Pull a fresh instance list; on success update the version and fire the refresh callback."""
        try:
            await self.get_available_instances(None)
        except Exception as e:
            logger.warning("Failed to refresh instances on %s: %s", reason, e)
            return
        self._last_instance_version = current_version
        await self._notify_instance_refreshed()

    async def _notify_instance_refreshed(self) -> None:
        """Fire the instance-refresh callback with the current active endpoints.

        Always fires when a callback is registered -- an empty list is meaningful: a full drain
        must still notify downstream (e.g. so the HTTP client pool prunes clients for endpoints that
        went away) instead of leaking them until the next non-empty refresh.
        """
        if not self._on_instance_refreshed:
            return
        active_endpoints = _collect_active_endpoints_from_cache(self._cache)
        try:
            await self._on_instance_refreshed(active_endpoints)
        except Exception as e:
            logger.warning("on_instance_refreshed callback failed: %s", e)

    async def select_and_allocate(
        self,
        role: "PDRole",
        req_info: RequestInfo,
        *,
        target_instance_id: int | None = None,
    ) -> tuple[Instance, Endpoint, Workload] | None:
        """Select instance locally + ALLOCATE_ONLY RPC. Allocation workload is decided here (RR=zero, LB=demand)."""
        role_str = role.value if role is not None else (getattr(PDRole.ROLE_U, "value", "union"))

        await self._refresh_cache_from_workload_reader(role)

        # Set in the kv_cache_affinity unified branch below: forward every endpoint's
        # affinity-discounted prefill cost so the scheduler re-ranks globally by fresh load.
        global_affinity = False

        if target_instance_id is not None:
            instances = await self.get_available_instances(role)
            instance = resolve_pinned_instance(instances, target_instance_id)
            if instance is None:
                logger.warning(
                    "Pinned instance_id=%s not available for role=%s req_id=%s",
                    target_instance_id,
                    role_str,
                    req_info.req_id,
                )
                return None
            endpoint = select_endpoint_for_instance(
                instance,
                scheduler_type=self._scheduler_type or "round_robin",
                endpoint_rr_counters=self._endpoint_rr_counters,
                is_blocked=self.is_instance_blocked,
            )
            if endpoint is None:
                logger.warning(
                    "No endpoint on pinned instance_id=%s role=%s req_id=%s",
                    target_instance_id,
                    role_str,
                    req_info.req_id,
                )
                return None
            candidate_policy = self._scheduler_type or CANDIDATE_POLICY_ROUND_ROBIN
            candidate_endpoints = [{"instance_id": instance.id, "endpoint_id": endpoint.id}]
        else:
            # Unified affinity forwards EVERY endpoint (with prefill_cost) for the scheduler's global
            # re-rank, so the worker only needs its own top-1 locally. Only load_gated still proposes
            # a fixed ranked alternate set the scheduler picks among, so it keeps the topK.
            request_top_k = (
                _AFFINITY_CANDIDATE_TOPK
                if (
                    role in _KVA_SELECT_ROLES
                    and (self._scheduler_type or "") == "kv_cache_affinity"
                    and self._kv_affinity_mode != KV_AFFINITY_MODE_UNIFIED
                )
                else 1
            )
            (
                candidates,
                candidate_policy,
            ) = await self._select_endpoint_candidates_with_policy(req_info, role, top_k=request_top_k)
            if not candidates:
                return None
            instance, endpoint, _ = candidates[0]
            # kv_cache_affinity unified mode: forward EVERY scored endpoint with its
            # affinity-discounted prefill cost so the scheduler re-ranks all of them by its own
            # fresh load ledger (prefill_load_scale*prefill_cost + load_weight*fresh_load) -- a
            # global selection, no fixed top-k. Other policies/modes forward the worker's ranked
            # alternates (best-first) for the scheduler's existing re-pick.
            affinity_debug = getattr(req_info, "kv_affinity_debug", None)
            global_affinity = (
                candidate_policy == CANDIDATE_POLICY_KV_CACHE_AFFINITY
                and isinstance(affinity_debug, dict)
                and any(rec[2] is not None for rec in affinity_debug.values())
            )
            if global_affinity:
                candidate_endpoints = [
                    {
                        "instance_id": ins_id,
                        "endpoint_id": ep_id,
                        "prefill_cost": rec[2],
                    }
                    for (ins_id, ep_id), rec in affinity_debug.items()
                    if rec[2] is not None
                ]
            else:
                candidate_endpoints = [
                    {"instance_id": cand_instance.id, "endpoint_id": cand_endpoint.id}
                    for cand_instance, cand_endpoint, _score in candidates
                ]

        # Allocation workload: RR does not use load, so use zero; LB uses demand for accounting.
        workload = (
            Workload()
            if (self._scheduler_type or "round_robin") == "round_robin"
            else calculate_demand_workload(role, req_info)
        )

        request_id = self._next_request_id()
        workload_sequence = self._workload_reader.last_sequence if self._workload_reader is not None else None
        role_workload_sequence = (
            self._workload_reader.last_sequence_for_role(role) if self._workload_reader is not None else None
        )
        req_data = {
            "instance_id": instance.id,
            "endpoint_id": endpoint.id,
            "candidates": candidate_endpoints,
            "role": role_str,
            "req_id": req_info.req_id,
            "workload_sequence": workload_sequence,
            "role_workload_sequence": role_workload_sequence,
            "instance_version": self._last_instance_version,
            # Workload demand as two raw floats: avoids a pydantic dump here and a model_validate on
            # the scheduler. RR sends 0.0/0.0 (Workload() has no demand) rather than a dumped model.
            "workload_active_tokens": workload.active_tokens,
            "workload_active_kv_cache": workload.active_kv_cache,
            "candidate_policy": candidate_policy,
        }
        if global_affinity:
            # Scalars the scheduler needs to recompute the unified score against its fresh load:
            # combined = prefill_load_scale * prefill_cost + load_weight * fresh_load.
            req_data["prefill_load_scale"] = self._kv_affinity_prefill_load_scale
            req_data["load_weight"] = self._kv_affinity_load_weight
        request = SchedulerRequest(
            request_type=SchedulerRequestType.ALLOCATE_ONLY,
            request_id=request_id,
            data=req_data,
        )
        response = await self._transport.send_request(request)
        if not response or response.response_type != SchedulerResponseType.SUCCESS:
            if response:
                logger.error(
                    "ALLOCATE_ONLY failed: role=%s req_id=%s error=%s",
                    role_str,
                    req_info.req_id,
                    response.error,
                )
            return None
        data = response.data or {}
        instance_data = data.get("instance")
        endpoint_data = data.get("endpoint")
        if not instance_data:
            return None
        out_instance = _instance_from_dict(instance_data)
        if not out_instance:
            return None
        if endpoint_data:
            out_endpoint = _endpoint_from_dict(endpoint_data)
            if out_endpoint:
                # Final, authoritative allocation log (the endpoint the scheduler actually
                # committed, after its fresh-ledger re-pick). This is the one to trust for load
                # distribution -- NOT the per-candidate kv_cache_affinity selection log, which is
                # only the worker's proposal and is emitted at DEBUG. matched/load are the chosen
                # endpoint's KV-affinity prefix hit and load at selection time (kv_cache_affinity
                # only; None otherwise); score/fast_path come from the scheduler's authoritative
                # response; repicked=True means the scheduler moved the request off the worker's
                # top-1 to spread load.
                affinity_debug = getattr(req_info, "kv_affinity_debug", None)
                matched_load = (
                    affinity_debug.get((out_instance.id, out_endpoint.id))
                    if (candidate_policy == CANDIDATE_POLICY_KV_CACHE_AFFINITY and isinstance(affinity_debug, dict))
                    else None
                )
                matched = matched_load[0] if matched_load else None
                sel_load = matched_load[1] if matched_load else None
                repicked = (out_instance.id, out_endpoint.id) != (
                    instance.id,
                    endpoint.id,
                )
                logger.info(
                    "scheduled role=%s req_id=%s instance=%s endpoint=%s policy=%s matched=%s "
                    "load=%s score=%s fast_path=%s repicked=%s proposed=%s-%s",
                    role_str,
                    req_info.req_id,
                    out_instance.id,
                    out_endpoint.id,
                    candidate_policy,
                    matched,
                    sel_load,
                    data.get("selected_score"),
                    data.get("fast_path"),
                    repicked,
                    instance.id,
                    endpoint.id,
                )
                return (out_instance, out_endpoint, workload)
        return None

    async def confirm_sample(
        self,
        key: tuple[int | None, int],
        now: float,
        interval_seconds: float,
    ) -> bool:
        if not self._transport.connected:
            logger.warning("confirm_sample: scheduler transport not connected")
            return False
        request_id = self._next_request_id()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.CONFIRM_SAMPLE,
            request_id=request_id,
            data={
                "p_instance_id": key[0],
                "d_instance_id": key[1],
                "now": now,
                "interval_seconds": interval_seconds,
            },
        )
        response = await self._transport.send_request(request)
        if response and response.response_type == SchedulerResponseType.SUCCESS:
            return bool((response.data or {}).get("confirmed", False))
        if response:
            logger.warning("confirm_sample failed pd_group=%s error=%s", key, response.error)
        else:
            logger.warning("confirm_sample: no response (timeout) pd_group=%s", key)
        return False

    async def record_precision_result(
        self,
        key: tuple[int | None, int],
        has_issue: bool,
        threshold: int,
    ) -> PrecisionStreakResult | None:
        if not self._transport.connected:
            logger.warning("record_precision_result: scheduler transport not connected")
            return None
        request_id = self._next_request_id()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.RECORD_PRECISION_RESULT,
            request_id=request_id,
            data={
                "p_instance_id": key[0],
                "d_instance_id": key[1],
                "has_issue": has_issue,
                "threshold": threshold,
            },
        )
        response = await self._transport.send_request(request)
        if response and response.response_type == SchedulerResponseType.SUCCESS:
            data = response.data or {}
            return PrecisionStreakResult(
                skip=bool(data.get("skip", False)),
                threshold_hit=bool(data.get("threshold_hit", False)),
                consecutive=int(data.get("consecutive", 0)),
                action_token=data.get("action_token"),
            )
        if response:
            logger.warning(
                "record_precision_result failed pd_group=%s error=%s",
                key,
                response.error,
            )
        else:
            logger.warning("record_precision_result: no response pd_group=%s", key)
        return None

    async def finish_precision_action(
        self,
        key: tuple[int | None, int],
        action_token: str,
    ) -> bool:
        if not self._transport.connected:
            logger.warning("finish_precision_action: scheduler transport not connected")
            return False
        request_id = self._next_request_id()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.FINISH_PRECISION_ACTION,
            request_id=request_id,
            data={
                "p_instance_id": key[0],
                "d_instance_id": key[1],
                "action_token": action_token,
            },
        )
        response = await self._transport.send_request(request)
        if response and response.response_type == SchedulerResponseType.SUCCESS:
            return bool((response.data or {}).get("finished", False))
        if response:
            logger.warning(
                "finish_precision_action failed pd_group=%s error=%s",
                key,
                response.error,
            )
        else:
            logger.warning("finish_precision_action: no response pd_group=%s", key)
        return False

    async def update_workload(self, params: UpdateWorkloadParams) -> bool:
        role_str = params.role.value if hasattr(params.role, "value") else str(params.role)
        request_id = self._next_request_id()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.UPDATE_WORKLOAD,
            request_id=request_id,
            data={
                "instance_id": params.instance_id,
                "endpoint_id": params.endpoint_id,
                "role": role_str,
                "req_id": params.req_id,
                "workload_action": params.workload_action.value,
                "workload_change": params.workload_change,
                "operation_id": params.operation_id,
            },
        )

        result = await self._send_request_result(request)
        response = result.response

        if response and response.response_type == SchedulerResponseType.SUCCESS:
            success = (response.data or {}).get("success", False)
            if not success:
                logger.warning(
                    "Update workload returned success=False from scheduler: "
                    "instance_id=%s endpoint_id=%s role=%s req_id=%s action=%s",
                    params.instance_id,
                    params.endpoint_id,
                    role_str,
                    params.req_id,
                    params.workload_action.value,
                )
            return success

        if response:
            logger.error(
                "Failed to update workload: instance_id=%s endpoint_id=%s role=%s req_id=%s error=%s",
                params.instance_id,
                params.endpoint_id,
                role_str,
                params.req_id,
                response.error,
            )
        else:
            logger.error(
                "Update workload got no response reason=%s error=%s: "
                "instance_id=%s endpoint_id=%s role=%s req_id=%s action=%s",
                (result.failure_reason or SchedulerRequestFailureReason.NO_RESPONSE).value,
                result.error,
                params.instance_id,
                params.endpoint_id,
                role_str,
                params.req_id,
                params.workload_action.value,
            )
        return False

    async def get_available_instances(self, role: PDRole | None = None) -> dict[int, Instance]:
        request_id = self._next_request_id()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.GET_AVAILABLE_INSTANCES,
            request_id=request_id,
            data={"role": role.value if hasattr(role, "value") else (str(role) if role else None)},
        )

        response = await self._transport.send_request(request)

        if response and response.response_type == SchedulerResponseType.SUCCESS:
            data = response.data or {}
            instances_data = data.get("instances", [])
            instances = {}
            for inst_data in instances_data:
                instance = _instance_from_dict(inst_data)
                if instance:
                    instances[instance.id] = instance

            shm_name = data.get("workload_shm_name")
            if shm_name:
                need_attach = not self._workload_reader or getattr(self._workload_reader, "_shm_name", None) != shm_name
                if need_attach:
                    if self._workload_reader:
                        self._workload_reader.detach()
                    from motor.coordinator.scheduler.runtime.workload_shm import (
                        WorkloadSharedMemoryReader,
                    )

                    self._workload_reader = WorkloadSharedMemoryReader(shm_name)
                    try:
                        self._workload_reader.attach()
                    except FileNotFoundError:
                        logger.debug(
                            "Workload shm %s not ready, will retry on next get_available_instances",
                            shm_name,
                        )
                        self._workload_reader = None
                    else:
                        self._last_instance_version = None

            # Store sorted by instance.id so round-robin order is stable without sorting on each select.
            # Empty successful responses must also clear stale cache entries.
            if role is not None:
                await self._cache.replace_all(role, sorted(instances.values(), key=lambda i: i.id))
            else:
                role_to_list: dict[PDRole, list] = {
                    PDRole.ROLE_E: [],
                    PDRole.ROLE_P: [],
                    PDRole.ROLE_D: [],
                    PDRole.ROLE_U: [],
                }
                _role_map = {
                    "encode": PDRole.ROLE_E,
                    "prefill": PDRole.ROLE_P,
                    "decode": PDRole.ROLE_D,
                    "union": PDRole.ROLE_U,
                    "both": PDRole.ROLE_U,
                    "hybrid": PDRole.ROLE_U,
                }
                for inst in instances.values():
                    r = getattr(inst, "role", None)
                    if r is None:
                        continue
                    role_enum = _role_map.get(r) if isinstance(r, str) else (r if r in role_to_list else None)
                    if role_enum is not None:
                        role_to_list[role_enum].append(inst)
                for r, lst in role_to_list.items():
                    await self._cache.replace_all(r, sorted(lst, key=lambda i: i.id))

            return instances

        if response:
            logger.error(f"Failed to get available instances: {response.error}")
        return {}

    def _roles_from_cache(self) -> set[PDRole]:
        return {
            role
            for role in (PDRole.ROLE_E, PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_U)
            if self._cache.get_instances(role)
        }

    async def get_available_instance_roles(self) -> set[PDRole]:
        """Return topology roles from the client cache; warm-up fetch once if the cache is cold.

        Router selection (dispatch.handle_request) reads roles before any select_*; without this
        warm-up a cold cache (process start / right after a refresh) would 503 instead of pulling.
        """
        roles = self._roles_from_cache()
        if not roles:
            try:
                await self.get_available_instances(None)
            except Exception as e:
                logger.debug("get_available_instance_roles: warm-up fetch failed: %s", e)
            roles = self._roles_from_cache()
        return roles

    async def has_compatible_pd_pair(self) -> bool:
        """Return whether cached P/D pools contain a compatible pair.

        Assumes the cache was warmed by a preceding get_available_instance_roles in the same
        routing decision; falls back to a warm-up fetch if both pools look empty.
        """
        prefill = self._cache.get_instances(PDRole.ROLE_P)
        decode = self._cache.get_instances(PDRole.ROLE_D)
        if not prefill and not decode:
            try:
                await self.get_available_instances(None)
            except Exception as e:
                logger.debug("has_compatible_pd_pair: warm-up fetch failed: %s", e)
            prefill = self._cache.get_instances(PDRole.ROLE_P)
            decode = self._cache.get_instances(PDRole.ROLE_D)
        return has_compatible_dispatch_pair(prefill, decode)

    async def get_unblocked_instances(self, role: PDRole) -> list[int]:
        """Return instance IDs of the given role that are NOT blocked by circuit breaker."""
        cached = self._cache.get_instances(role)
        if not cached:
            try:
                await self.get_available_instances(None)
            except Exception as e:
                logger.debug("get_unblocked_instances: warm-up fetch failed: %s", e)
            cached = self._cache.get_instances(role)
        return [inst.id for inst in cached if inst.id not in self._cb_blocked_instances]

    async def has_required_instances(self) -> InstanceReadiness:
        """Return InstanceReadiness from cache; warm-up fetch if needed."""

        def _cached_lists() -> tuple[list, list, list, list]:
            return (
                self._cache.get_instances(PDRole.ROLE_E),
                self._cache.get_instances(PDRole.ROLE_P),
                self._cache.get_instances(PDRole.ROLE_D),
                self._cache.get_instances(PDRole.ROLE_U),
            )

        def _status(cached: tuple[list, list, list, list]) -> InstanceReadiness:
            return readiness_from_instances(instance for role_instances in cached for instance in role_instances)

        e_list, p_list, d_list, u_list = _cached_lists()
        status = _status((e_list, p_list, d_list, u_list))
        if status != InstanceReadiness.NONE:
            return status
        try:
            await self.get_available_instances(None)
        except Exception as e:
            logger.debug("has_required_instances: warm-up get_available_instances failed: %s", e)
        return _status(_cached_lists())

    async def get_all_instances(
        self,
    ) -> tuple[dict[int, Instance], dict[int, Instance]]:
        """Interface compat; returns empty (Mgmt process uses local InstanceManager)."""
        return {}, {}

    async def refresh_instances(self, event_type, instances: list[Instance]) -> None:
        request_id = self._next_request_id()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.REFRESH_INSTANCES,
            request_id=request_id,
            data={
                "event_type": event_type.value if hasattr(event_type, "value") else str(event_type),
                "instances": [_instance_to_dict(inst) for inst in instances],
            },
        )

        response = await self._transport.send_request(request)

        if response and response.response_type == SchedulerResponseType.SUCCESS:
            logger.info(f"Successfully refreshed instances: {(response.data or {}).get('message', '')}")
        elif response:
            logger.error(f"Failed to refresh instances: {response.error}")

    async def _on_instance_change_notify(self, version: int | None, delta: dict | None = None) -> None:
        """Called when SUB receives instance-change from Scheduler; dedup by version, then apply the
        incremental ADD/DEL delta when present (no GET), else fall back to a full instance pull.
        """
        if version is not None and self._last_instance_version is not None and version == self._last_instance_version:
            return
        if await self._try_apply_instance_delta(version, delta):
            return
        try:
            await self.get_available_instances(None)
            if version is not None:
                self._last_instance_version = version
            # Remove stale entries for instances that no longer exist in the pool
            # (covers DEL events where no explicit "closed" message is published).
            current_ids = {
                inst.id
                for role in (PDRole.ROLE_E, PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_U)
                for inst in self._cache.get_instances(role)
            }
            self._cb_blocked_instances &= current_ids
            await self._notify_instance_refreshed()
        except Exception as e:
            logger.warning("Instance change notify refresh failed: %s", e)

    async def _try_apply_instance_delta(self, version: int | None, delta: dict | None) -> bool:
        """Patch the local cache from an ADD/DEL PUB delta without a full GET. Returns True on apply.

        Only apply a delta when it is the next contiguous version.  A dropped or reordered PUB
        notification must fall back to a full pull; otherwise accepting a later version would hide
        the gap from the shared-memory version check and leave the cache permanently incomplete.
        """
        if not delta or version is None:
            return False
        if self._last_instance_version is None or version != self._last_instance_version + 1:
            return False
        event = delta.get("event")
        instances_data = delta.get("instances")
        if event not in ("add", "del") or not isinstance(instances_data, list):
            return False
        instances = []
        for instance_data in instances_data:
            instance = _instance_from_dict(instance_data)
            if instance is None:
                return False
            instances.append(instance)
        if not instances:
            return False
        if event == "add":
            if not await self._cache.apply_add(instances):
                return False
        else:
            await self._cache.apply_remove(instances)
            self._cb_blocked_instances -= {inst.id for inst in instances}
        self._last_instance_version = version
        await self._notify_instance_refreshed()
        return True

    async def _on_circuit_breaker_change(self, instance_id: int, state: str) -> None:
        """Update local CB blocked-instance cache when PUB notifies state change."""
        if state == "open":
            self._cb_blocked_instances.add(instance_id)
            logger.warning(
                "Circuit breaker OPEN: instance_id=%d",
                instance_id,
            )
        elif state == "closed":
            self._cb_blocked_instances.discard(instance_id)
            logger.info(
                "Circuit breaker CLOSED: instance_id=%d",
                instance_id,
            )

    def is_instance_blocked(self, instance_id: int) -> bool:
        """Check whether a specific instance is currently blocked by circuit breaker.

        Lock-free read of the local cache: may return a slightly stale value if
        ``_on_circuit_breaker_change`` is modifying the set concurrently.  This is
        acceptable because the cache is best-effort — the authoritative CB state
        lives on SchedulerServer, which performs the final gate.
        """
        return instance_id in self._cb_blocked_instances

    async def report_cb_event(self, instance_id: int, event: str) -> None:
        """Send a circuit-breaker event ("failure" | "success") to SchedulerServer."""
        if not self._transport.connected:
            if event == "failure":
                logger.warning(
                    "CircuitBreaker: transport disconnected, failure report dropped: instance_id=%d",
                    instance_id,
                )
            return
        if event == "failure":
            logger.warning(
                "CircuitBreaker: reporting failure to SchedulerServer: instance_id=%d",
                instance_id,
            )
        elif event == "success":
            logger.info(
                "CircuitBreaker: reporting success to SchedulerServer: instance_id=%d",
                instance_id,
            )
        else:
            return
        request = SchedulerRequest(
            request_type=SchedulerRequestType.CIRCUIT_BREAKER_REPORT,
            request_id=str(uuid.uuid4()),
            data={
                "instance_id": instance_id,
                "event": event,
            },
        )

        def _on_cb_send_done(fut):
            if fut.cancelled():
                return
            try:
                fut.result()
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "CircuitBreaker: CB report send failed: instance_id=%d event=%s error=%s",
                    instance_id,
                    event,
                    err,
                )

        task = asyncio.create_task(self._transport.send_request(request))
        task.add_done_callback(_on_cb_send_done)

    def _select_endpoint_candidates_from_list_with_policy(
        self,
        instances: list[Instance],
        role: PDRole,
        req_info: RequestInfo,
        top_k: int = 1,
    ) -> tuple[list[tuple[Instance, Endpoint, float]], str]:
        if not instances:
            return [], self._scheduler_type or CANDIDATE_POLICY_ROUND_ROBIN
        st = self._scheduler_type or "round_robin"
        if st == "load_balance":
            candidates = self._select_endpoint_candidates_by_load_balance(instances, role, top_k)
            if candidates:
                return candidates, CANDIDATE_POLICY_LOAD_BALANCE
            logger.warning("load_balance failed, falling back to round-robin")
        elif st == "kv_cache_affinity":
            # Affinity ranking applies to KVA-eligible roles only; others fall through to
            # the load_balance -> round_robin chain below.
            if role in _KVA_SELECT_ROLES:
                # Propose the top-k affinity-ranked candidates. The scheduler re-picks among them
                # by its authoritative (fresh) workload ledger, so a burst spreads across the top
                # candidates without a client-local in-flight overlay.
                ranked = KvCacheAffinityPolicy.select_endpoint_candidates_from_list(
                    instances,
                    req_info,
                    mode=self._kv_affinity_mode,
                    overlap_credit=self._kv_affinity_overlap_credit,
                    prefill_load_scale=self._kv_affinity_prefill_load_scale,
                    load_weight=self._kv_affinity_load_weight,
                    load_gate_topn=self._kv_affinity_load_gate_topn,
                    top_k=max(1, top_k),
                )
                if ranked:
                    return ranked, CANDIDATE_POLICY_KV_CACHE_AFFINITY
                logger.warning("kv_cache_affinity unavailable (no conductor match), falling back to load_balance")
            candidates = self._select_endpoint_candidates_by_load_balance(instances, role, top_k)
            if candidates:
                return candidates, CANDIDATE_POLICY_LOAD_BALANCE
            logger.warning("load_balance unavailable, falling back to round-robin")
        # Round-robin path: default policy or load_balance fallback
        if role not in self._instance_rr_counters:
            self._instance_rr_counters[role] = 0
        n = len(instances)
        start_offset = (n * self._client_index) // self._client_count if n else 0
        counter = self._instance_rr_counters[role]
        effective_counter = counter + start_offset
        selected_instance, next_counter = RoundRobinPolicy.select_instance_from_list(instances, effective_counter)
        self._instance_rr_counters[role] = next_counter - start_offset
        if not selected_instance:
            return [], CANDIDATE_POLICY_ROUND_ROBIN
        selected = self._select_endpoint_for_instance(selected_instance)
        if not selected:
            return [], CANDIDATE_POLICY_ROUND_ROBIN
        instance, endpoint = selected
        return [(instance, endpoint, 0.0)], CANDIDATE_POLICY_ROUND_ROBIN

    def _select_endpoint_for_instance(self, instance: Instance) -> tuple[Instance, Endpoint] | None:
        if not instance:
            return None
        all_endpoints = instance.get_all_endpoints()
        if not all_endpoints:
            return None
        st = self._scheduler_type or "round_robin"
        if st in ("load_balance", "kv_cache_affinity"):
            ep = LoadBalancePolicy.select_endpoint_from_instance(instance)
            if ep:
                return (instance, ep)
            return (instance, all_endpoints[0])
        ep = RoundRobinPolicy.select_endpoint_from_instance(
            instance, self._endpoint_rr_counters, is_blocked=self.is_instance_blocked
        )
        return (instance, ep) if ep else None

    async def _init_cache(self) -> None:
        """Load initial instance cache via GET_AVAILABLE_INSTANCES."""
        try:
            await self.get_available_instances(None)
        except Exception as e:
            logger.warning("Failed to initialize instance cache: %s", e, exc_info=True)

    def _select_endpoint_candidates_by_load_balance(
        self,
        instances: list[Instance],
        role: PDRole,
        top_k: int = 1,
    ) -> list[tuple[Instance, Endpoint, float]]:
        n = len(instances)
        start_index = (n * self._client_index) // self._client_count if n else 0
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            instances,
            role,
            top_k=max(1, top_k),
            instance_score_weight=self._endpoint_instance_score_weight,
            start_index=start_index,
            is_blocked=self.is_instance_blocked,
        )
        return [(candidate.instance, candidate.endpoint, candidate.score) for candidate in candidates]
