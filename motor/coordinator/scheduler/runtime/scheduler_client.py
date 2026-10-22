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

from motor.common.logger import get_logger
from motor.common.resources.endpoint import Endpoint, Workload, WorkloadAction
from motor.common.resources.instance import Instance, PDRole
from motor.config.coordinator import (
    KV_AFFINITY_MODE_UNIFIED,
    KV_AFFINITY_MODES,
    KvAffinityConfig,
)
from motor.coordinator.domain import (
    InstanceReadiness,
    UpdateWorkloadParams,
    readiness_from_instances,
)
from motor.coordinator.domain.cache_declaration import (
    CacheDeclarationTable,
    cache_lifecycle_sweep_interval_s,
    create_cache_declaration_table_from_env,
)
from motor.coordinator.domain.scheduling_pin import (
    resolve_pinned_instance,
    select_endpoint_for_instance,
)
from motor.coordinator.domain.workload_calculator import (
    calculate_committed_workload,
    calculate_demand_workload,
)
from motor.coordinator.fault_tolerance.precision.streak_result import (
    PrecisionStreakResult,
)
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.scheduler.allocate_arbitration import (
    ArbitrationContext,
    log_load_balance_all_candidates,
    select_authoritative_allocate_candidate,
    select_valid_candidate,
)
from motor.coordinator.scheduler.policy.dynamic_bucket import (
    DynamicBucketPolicy,
    DynamicBucketSelector,
    get_request_token_length,
    make_dynamic_bucket_allocation_workload,
)
from motor.coordinator.scheduler.policy.kv_cache_affinity import KvCacheAffinityPolicy
from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy
from motor.coordinator.scheduler.policy.round_robin import RoundRobinPolicy
from motor.coordinator.scheduler.runtime.dp_stats import DpStatsLogger
from motor.coordinator.scheduler.runtime.kv_usage import (
    KvUsageShmReader,
    get_endpoint_kv_cache_usage,
    set_kv_usage_reader,
    update_kv_usage_cache,
)
from motor.coordinator.scheduler.runtime.session_affinity import (
    NEW_PIN_OUTCOMES,
    PLACEMENT_BALANCED,
    SessionAffinityTable,
    create_session_affinity_from_env,
    resolve_session_pin,
    select_balanced_instance,
    session_affinity_placement_mode,
)
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    AGENT_STATUS_TOPIC,
    CANDIDATE_POLICY_DYNAMIC_BUCKET,
    CANDIDATE_POLICY_KV_CACHE_AFFINITY,
    CANDIDATE_POLICY_LOAD_BALANCE,
    CANDIDATE_POLICY_ROUND_ROBIN,
    CIRCUIT_BREAKER_TOPIC,
    INSTANCE_CHANGE_TOPIC,
    SchedulerRequest,
    SchedulerRequestType,
    SchedulerResponse,
    SchedulerResponseType,
    ZMQMessageSerializer,
    pack_send_frames,
    unpack_recv_payload,
)

logger = get_logger(__name__)

# Callback signature: receives active endpoint list [(ip, port), ...], returns None
OnInstanceRefreshedCallback = Callable[[list[tuple[str, str]]], Awaitable[None]]

# Number of affinity-ranked candidates a prefill request proposes to the scheduler. The scheduler
# re-picks among them by its authoritative workload ledger, spreading bursts across the top few.
_AFFINITY_CANDIDATE_TOPK = 3
_MAX_CAS_ALLOCATE_ATTEMPTS = 64
# Upper bound on declarations released per keeper tick: a burst of simultaneous
# expiries releases in batches instead of one long event-loop stall.
_SWEEP_RELEASE_BATCH = 64


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
        )
        if cached_instance.gathered_workload is None:
            cached_instance.gathered_workload = Workload()
        cached_instance.gathered_workload.active_tokens += active_tokens - old_workload.active_tokens

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

# Callback when an out-of-band agent_status event is received from Scheduler PUB
# (multipart: [topic, msgpack_payload]); args: session_id, state.
OnAgentStatusEventNotify = Callable[[str, str], Awaitable[None]]

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
        on_agent_status_event: OnAgentStatusEventNotify | None = None,
    ) -> None:
        self._sub_address = sub_address
        self._on_instance_change = on_instance_change
        self._on_circuit_breaker_change = on_circuit_breaker_change
        self._on_agent_status_event = on_agent_status_event
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
                    elif topic == AGENT_STATUS_TOPIC and self._on_agent_status_event:
                        payload = self._parse_msgpack_frame(frames, 1)
                        if payload and isinstance(payload, dict):
                            await self._on_agent_status_event(
                                str(payload.get("session_id", "")),
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
    prefill_scheduler_type: str | None = None
    decode_scheduler_type: str | None = None
    # Optional default applied to both roles when the per-role fields are unset (tests).
    scheduler_type: str | None = None
    client_index: int = 0
    client_count: int = 1
    endpoint_instance_score_weight: float = 0.05
    dynamic_bucket_short_median: int = 16 * 1024
    dynamic_bucket_long_median: int = 96 * 1024
    dynamic_bucket_border: int = 32 * 1024
    dynamic_bucket_length_scale: float = 1.0
    dynamic_bucket_load_scale: float = 4.0
    dynamic_bucket_short_bucket_count: int = 0
    dynamic_bucket_long_bucket_count: int = 0
    # kv_cache_affinity tunables (see SchedulerConfig.kv_affinity).
    kv_affinity: KvAffinityConfig | None = None
    # Soft-priority admission (Agent Hint `priority`, Phase 3): endpoints whose
    # workload score reaches priority_load_threshold are excluded for NORMAL
    # requests (priority < priority_admission_level); high-priority requests bypass
    # the gate. The gate never empties the pool (availability first); 0 disables.
    priority_load_threshold: int = 0
    priority_admission_level: int = 3
    dp_stats_window: int = 60
    # Each inference worker dumps its request counts every dp_stats_window seconds.
    # Obs/standby (worker_index is None) leave this off.
    log_dp_stats: bool = False
    tls_config: Any | None = None
    on_instance_refreshed: OnInstanceRefreshedCallback | None = None


class AsyncSchedulerClient:
    """
    Fully async Scheduler client (works with AsyncSchedulerServer).
    Implements SchedulingFacade (select_and_allocate, update_workload) for BaseRouter injection.
    """

    # Declared at class scope so instances built without ``__init__`` (tests construct
    # clients with ``__new__`` plus fake attributes) resolve the keeper as disabled
    # instead of raising AttributeError.
    _cache_declarations: CacheDeclarationTable | None = None
    _cache_lifecycle_task: asyncio.Task | None = None

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
        self._dynamic_bucket_selector = DynamicBucketSelector(
            short_bucket_median=config.dynamic_bucket_short_median,
            long_bucket_median=config.dynamic_bucket_long_median,
            bucket_border=config.dynamic_bucket_border,
            length_scale=config.dynamic_bucket_length_scale,
            load_scale=config.dynamic_bucket_load_scale,
            short_bucket_count=config.dynamic_bucket_short_bucket_count,
            long_bucket_count=config.dynamic_bucket_long_bucket_count,
        )
        affinity = config.kv_affinity or KvAffinityConfig()
        mode = str(affinity.mode or KV_AFFINITY_MODE_UNIFIED).lower()
        if mode not in KV_AFFINITY_MODES:
            logger.warning(
                "Invalid kv_affinity.mode %r; expected one of %s. Falling back to %r.",
                affinity.mode,
                KV_AFFINITY_MODES,
                KV_AFFINITY_MODE_UNIFIED,
            )
            mode = KV_AFFINITY_MODE_UNIFIED
        self._kv_affinity_mode = mode
        self._kv_affinity_load_weight = max(0.0, float(affinity.load_weight))
        self._kv_affinity_overlap_credit = max(0.0, float(affinity.overlap_credit))
        self._kv_affinity_prefill_load_scale = max(0.0, float(affinity.prefill_load_scale))
        self._kv_affinity_load_gate_topn = max(0, int(affinity.load_gate_topn))
        self._kv_affinity_w_npu = max(0.0, float(affinity.w_npu))
        self._kv_affinity_w_cpu = max(0.0, float(affinity.w_cpu))
        self._kv_affinity_w_disk = max(0.0, float(affinity.w_disk))
        self._kv_affinity_hit_rate_threshold = min(1.0, max(0.0, float(affinity.hit_rate_threshold)))

        self._dp_stats = DpStatsLogger(window_sec=config.dp_stats_window, worker_index=config.client_index)
        self._log_dp_stats = bool(config.log_dp_stats)
        self._dp_stats_task: asyncio.Task | None = None
        # cache_control lifecycle keeper (design 20261015-03); None keeps it inert.
        self._cache_lifecycle_task: asyncio.Task | None = None

        self._serializer = ZMQMessageSerializer()
        self._transport = _SchedulerTransport(config.scheduler_address, config.timeout, self._serializer)
        self._cache = _SchedulerInstanceCache()
        self._instance_rr_counters: dict[PDRole, int] = {}
        self._endpoint_rr_counters: dict[int, int] = {}
        default_type = config.scheduler_type or "round_robin"
        self._prefill_scheduler_type: str = config.prefill_scheduler_type or default_type
        self._decode_scheduler_type: str = config.decode_scheduler_type or default_type
        self._workload_reader = None
        self._last_instance_version: int | None = None
        self._on_instance_refreshed = config.on_instance_refreshed
        self._cb_blocked_instances: set[int] = set()
        # KV usage 由 Obs 进程的 MetricsCollector 抓取并经 SchedulerServer 发布到共享内存，
        # 本进程只 attach reader 读缓存，调度路径不做网络 I/O。
        self._kv_usage_reader: KvUsageShmReader | None = None
        # 调度指标（cnt/fresh_load/a_tokens）shm 名：SchedulerServer 采集写入，ObsServer 读取
        # 输出 motor: 指标。仅保存名字，供 ObsServer 的 MetricsCollector attach 读取。
        self._sched_metrics_shm_name: str | None = None

        instance_pub = (config.instance_pub_address or "").strip()
        self._push_subscriber = (
            _InstancePushSubscriber(
                instance_pub,
                self._on_instance_change_notify,
                self._on_circuit_breaker_change,
                self._on_agent_status_event,
            )
            if instance_pub
            else None
        )

        # Session affinity (env-gated; None keeps baseline scheduling untouched)
        self._session_affinity: SessionAffinityTable | None = create_session_affinity_from_env()
        # Optional hook for the worker's agent-session state machine: PUB fan-out
        # events drive it so finalize's terminal semantics hold fleet-wide, not
        # only on the worker that received the POST (review 194528015).
        self.agent_status_state_listener: Callable[[str, str], None] | None = None
        # Monotonic counter feeding the balanced-placement tie-break (per worker process).
        self._affinity_placement_seq = 0

        # cache_control declarations (env-gated; None keeps the request path inert).
        # Only an explicit TTL is recorded, so a deployment whose clients never send one
        # behaves exactly as before (design 20261015-03 §1.4.2 / §7 D1).
        self._cache_declarations: CacheDeclarationTable | None = create_cache_declaration_table_from_env()

        # Soft-priority admission gate (Agent Hint `priority`); 0 disables the gate.
        self._priority_load_threshold = max(0, int(config.priority_load_threshold))
        self._priority_admission_level = max(0, int(config.priority_admission_level))

    @property
    def connected(self) -> bool:
        return self._transport.connected

    @property
    def kv_usage_shm_name(self) -> str | None:
        """KV usage shm name this client's reader is attached to, or None before attach."""
        return self._kv_usage_reader.shm_name if self._kv_usage_reader is not None else None

    @property
    def sched_metrics_shm_name(self) -> str | None:
        """Scheduling-metrics shm name published by SchedulerServer, or None before sync."""
        return self._sched_metrics_shm_name

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
            self._start_dp_stats_task()
            self._start_cache_lifecycle_task()
            logger.info("Async scheduler client connected to %s", self.scheduler_address)
        return success

    async def disconnect(self) -> None:
        await self._stop_dp_stats_task()
        await self._stop_cache_lifecycle_task()
        try:
            if self._kv_usage_reader is not None:
                try:
                    self._kv_usage_reader.detach()
                except Exception as e:
                    logger.warning("Failed to detach kv usage reader: %s", e)
                self._kv_usage_reader = None
            if self._push_subscriber:
                await self._push_subscriber.disconnect()
            if self._workload_reader:
                self._workload_reader.detach()
                self._workload_reader = None
        finally:
            # Always close transport so ZMQ context is terminated even if above steps raise.
            await self._transport.disconnect()

    def _start_dp_stats_task(self) -> None:
        """Start this worker's per-DP stats loop (requests + SHM tokens)."""
        if not self._log_dp_stats or self._dp_stats._window_sec <= 0:
            return
        if self._dp_stats_task is not None and not self._dp_stats_task.done():
            return
        self._dp_stats_task = asyncio.create_task(self._dp_stats_loop())

    async def _stop_dp_stats_task(self) -> None:
        task = self._dp_stats_task
        self._dp_stats_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._emit_dp_stats()

    def _record_cache_declaration(self, hint_info: object, session_id: str | None, role: str | None = None) -> None:
        """Record an explicit ``cache_control`` retention (design 20261015-03, step ①).

        Only an explicit TTL enters the lifecycle: ``CacheControl.ttl`` defaults to 300, so
        a bare ``cache_control`` would otherwise look like "keep it 5 minutes" and change
        behaviour for traffic that never asked for retention. Never raises — a bad
        declaration must not fail a request.

        ``role`` is stored with the declaration so the keeper's protection pass can refresh
        that binding (step ②) through the affinity table's existing read path.
        """
        if self._cache_declarations is None or not session_id:
            return
        cache_control = getattr(hint_info, "cache_control", None)
        if cache_control is None or not getattr(cache_control, "ttl_explicit", False):
            return
        declaration = self._cache_declarations.declare(
            session_id,
            getattr(cache_control, "type", ""),
            getattr(cache_control, "ttl", 0),
            msg_offset=getattr(cache_control, "msg_offset", None),
            role=role,
        )
        if declaration is not None:
            # Publish the promise on the shared pin slot (max-merged): the keeper
            # compares the slot's promise against its own declaration so a longer
            # declaration made by another worker defers the release (review 194528478).
            if hasattr(self._session_affinity, "declare_retention") and role:
                try:
                    self._session_affinity.declare_retention(session_id, role, time.time() + declaration.ttl_s)
                except Exception as e:
                    logger.warning("declare_retention failed: session_id=%s err=%s", session_id, e)
            logger.info(
                "cache_control declared: session_id=%s type=%s ttl_s=%d",
                session_id,
                declaration.decl_type,
                declaration.ttl_s,
            )

    def _start_cache_lifecycle_task(self) -> None:
        """Start the cache_control declaration keeper (env-gated; no-op when disabled)."""
        if self._cache_declarations is None:
            return
        if self._cache_lifecycle_task is not None and not self._cache_lifecycle_task.done():
            return
        self._cache_lifecycle_task = asyncio.create_task(self._cache_lifecycle_loop())

    async def _stop_cache_lifecycle_task(self) -> None:
        task = self._cache_lifecycle_task
        self._cache_lifecycle_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _cache_lifecycle_loop(self) -> None:
        """Release affinity pins whose declared retention elapsed (design §2.2, step ①).

        The keeper only ever relaxes bindings the client itself asked to relax; it never
        touches undeclared sessions. A keeper fault is swallowed so it can never affect
        the request path.
        """
        interval = cache_lifecycle_sweep_interval_s()
        logger.info("cache_control lifecycle keeper started: sweep_interval_s=%d", interval)
        while True:
            await asyncio.sleep(interval)
            try:
                self._sweep_cache_declarations()
                self._touch_cache_declarations()
            except Exception as e:  # CancelledError is BaseException: cancellation propagates
                logger.warning("cache_control lifecycle pass failed: %s", e)

    def _sweep_cache_declarations(self) -> None:
        """Release the affinity pins of every elapsed declaration.

        Bounded per tick: each sweep releases at most
        :data:`_SWEEP_RELEASE_BATCH` declarations so a burst of simultaneous
        expiries cannot stall the event loop for the whole batch in one go
        (the rest wait for the next tick). Cross-worker renewals are honored:
        a pin whose ``renewed_at`` is newer than this worker's declaration was
        re-recorded by another worker after we recorded ours, and that worker's
        (longer) declaration governs — we skip the release instead of tearing
        down a binding that is still declared elsewhere.
        """
        declarations = self._cache_declarations
        if declarations is None:
            return
        released = 0
        for declaration in declarations.sweep():
            if released >= _SWEEP_RELEASE_BATCH:
                declarations.requeue(declaration)
                continue
            if self._release_expired_declaration(declaration):
                released += 1
                logger.info(
                    "cache_control retention elapsed; released session pin: session_id=%s type=%s ttl_s=%d dropped=%d",
                    declaration.session_id,
                    declaration.decl_type,
                    declaration.ttl_s,
                    declaration.dropped,
                )
            else:
                # Deferred (renewed by another worker): hand the follow-up over
                # to that worker's keeper — requeuing here would loop forever.
                logger.info(
                    "cache_control retention elapsed but pin renewed later elsewhere; "
                    "deferring release to the renewing worker: session_id=%s type=%s",
                    declaration.session_id,
                    declaration.decl_type,
                )

    def _release_expired_declaration(self, declaration) -> bool:
        """Conditionally release one expired declaration; True when released.

        The declaration ledger is worker-local while the affinity table may be
        shared machine-wide: another worker can declare a longer retention for
        the same session after this worker recorded its (shorter) one. Each
        declaration publishes its wall expiry onto the shared pin slot
        (max-merged), so we defer when the slot holds a promise that outlives
        our own — the worker that made it owns the release. When the slot
        holds no longer promise, this worker's expired declaration governs and
        the release proceeds.
        """
        affinity = self._session_affinity
        if affinity is None:
            declaration.dropped = 0
            return True
        roles = declaration.roles or (None,)
        own_expiry = declaration.wall_created_s + declaration.ttl_s
        for role in roles:
            if not role:
                continue
            if hasattr(affinity, "peek_retention_expires"):
                promise = affinity.peek_retention_expires(declaration.session_id, role)
                if promise > own_expiry + 1.0:
                    return False
            if hasattr(affinity, "peek_renewal"):
                renewal = affinity.peek_renewal(declaration.session_id, role)
                if renewal is not None and renewal > own_expiry + 1.0:
                    # Re-recorded after our promise expired: someone else owns it now.
                    return False
        declaration.dropped = affinity.drop_all_roles(declaration.session_id)
        return True

    def _touch_cache_declarations(self) -> int:
        """Refresh the affinities of still-active declarations (design §2.2, step ②).

        Protection without touching the eviction algorithm: both affinity tables refresh
        recency on a ``get`` hit, so reading the declared bindings each pass keeps them off
        the LRU eviction end. Only sessions that explicitly declared a retention are
        touched, so undeclared traffic keeps today's behaviour. Returns the hit count for
        diagnostics.

        Cost: one ``get`` per (declaration, role); the shared table takes its file lock per
        call, so this is bounded by ``max_declarations`` and runs off the request path.
        """
        declarations = self._cache_declarations
        affinity = self._session_affinity
        if declarations is None or affinity is None:
            return 0
        touched = 0
        for declaration in declarations.active():
            for role in declaration.roles:
                if affinity.get(declaration.session_id, role) is not None:
                    touched += 1
        if touched:
            logger.debug("cache_control protection refreshed %d declared bindings", touched)
        return touched

    async def _dp_stats_loop(self) -> None:
        """Sleep ``window_sec``, then log each DP's requests and SHM tokens."""
        interval = self._dp_stats._window_sec
        while True:
            await asyncio.sleep(interval)
            self._emit_dp_stats()

    def _emit_dp_stats(self) -> None:
        self._dp_stats.emit_window(self._snapshot_dp_stats())

    def _snapshot_dp_stats(self) -> list[tuple[int, int, float]]:
        """Read current per-DP ``active_tokens`` from schema-4 SHM."""
        reader = self._workload_reader
        native = getattr(reader, "native", None) if reader is not None else None
        if native is None:
            return []
        from motor.coordinator.scheduler.runtime.workload_shm.layout import FLAG_VALID

        try:
            header = native.read_header()
            entries = native.load_entries(int(header.get("entry_count", 0)))
        except Exception as e:
            logger.debug("dp_stats active_tokens snapshot failed: %s", e)
            return []
        snapshots: list[tuple[int, int, float]] = []
        for entry in entries:
            if not (int(entry.get("flags", 0)) & FLAG_VALID):
                continue
            snapshots.append(
                (
                    int(entry["instance_id"]),
                    int(entry["endpoint_id"]),
                    float(entry["active_tokens"]),
                )
            )
        return snapshots

    def _instance_active_tokens(self) -> dict[int, float]:
        """Sum SHM ``active_tokens`` per instance (balanced placement secondary key)."""
        tokens: dict[int, float] = {}
        for instance_id, _endpoint_id, active_tokens in self._snapshot_dp_stats():
            tokens[instance_id] = tokens.get(instance_id, 0.0) + active_tokens
        return tokens

    async def _select_balanced_affinity_instance(
        self,
        role: "PDRole",
        normalized_engine_type: str,
        normalized_dispatch_capability: str,
    ) -> int | None:
        """Balanced new-pin placement: min (pinned sessions, active tokens, seq).

        Candidates carry the same engine-type/dispatch-capability/breaker
        filters the pinned path applies afterwards, so a chosen instance can
        never fail the subsequent pinned resolution. ``None`` (no eligible
        candidate) falls back to the normal policy path.
        """
        pool = await self.get_available_instances(role)
        filtered = self._filter_instances(
            list(pool.values()),
            normalized_engine_type or None,
            normalized_dispatch_capability or None,
        )
        eligible = {c.id for c in filtered if not self.is_instance_blocked(c.id)}
        chosen = select_balanced_instance(
            eligible,
            self._session_affinity.count_pins_by_instance(getattr(role, "value", "")),
            self._instance_active_tokens(),
            self._affinity_placement_seq,
        )
        if chosen is not None:
            self._affinity_placement_seq += 1
        return chosen

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

    def _scheduler_type_for_role(self, role: PDRole | None = None) -> str:
        if role == PDRole.ROLE_D:
            return self._decode_scheduler_type
        return self._prefill_scheduler_type

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
        required_engine_type: str | None = None,
        required_dispatch_capability: str | None = None,
    ) -> tuple[list[tuple[Instance, Endpoint, float]], str]:
        """Select endpoint candidates from cache or fresh instances."""
        cache_role = role if role is not None else PDRole.ROLE_U
        cached_instances = self._filter_instances(
            self._cache.get_instances(cache_role), required_engine_type, required_dispatch_capability
        )
        if cached_instances:
            cached_instances = self._apply_priority_load_gate(cached_instances, req_info, cache_role)
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
                    self._scheduler_type_for_role(cache_role),
                )
                return candidates, candidate_policy
        instances = await self.get_available_instances(role)
        if not instances:
            return [], self._scheduler_type_for_role(cache_role)

        # get_available_instances already wrote sorted list to cache; build sorted list once for this path
        instance_list = self._filter_instances(
            sorted(instances.values(), key=lambda i: i.id), required_engine_type, required_dispatch_capability
        )
        instance_list = self._apply_priority_load_gate(instance_list, req_info, cache_role)
        if not instance_list:
            return [], self._scheduler_type_for_role(cache_role)
        candidates, candidate_policy = self._select_endpoint_candidates_from_list_with_policy(
            instance_list, cache_role, req_info, top_k=top_k
        )
        if candidates:
            logger.debug(
                "Selected %d endpoint candidate(s) from fresh fetch (role=%s, policy=%s)",
                len(candidates),
                role,
                self._scheduler_type_for_role(cache_role),
            )
        return candidates, candidate_policy

    @staticmethod
    def _filter_instances(
        instances: list[Instance],
        required_engine_type: str | None,
        required_dispatch_capability: str | None = None,
    ) -> list[Instance]:
        normalized = str(required_engine_type or "").strip().lower()
        return [
            instance
            for instance in instances
            if (not normalized or str(getattr(instance, "engine_type", "")).strip().lower() == normalized)
            and (
                not required_dispatch_capability
                or required_dispatch_capability in (getattr(instance, "dispatch_capabilities", None) or [])
            )
        ]

    def _affinity_eligible_instance_ids(
        self,
        pool: dict[int, Instance],
        required_engine_type: str | None,
        required_dispatch_capability: str | None,
    ) -> set[int]:
        """Instance ids an affinity pin may resolve to for this request.

        Applies the same engine/dispatch filter as the pinned path and the
        balanced placement, plus circuit-breaker exclusion. An existing pin on
        an instance that no longer passes the filter (e.g. engine type switched)
        then resolves as ``stale`` and the request falls back to policy, instead
        of the pinned path returning None / 503 (review 194976698 — the default
        affinity path previously omitted this filter).
        """
        return {
            candidate.id
            for candidate in self._filter_instances(
                list(pool.values()), required_engine_type, required_dispatch_capability
            )
            if not self.is_instance_blocked(candidate.id)
        }

    def _request_priority(self, req_info: RequestInfo) -> int | None:
        """Hint priority level of the request (Agent Hint `priority`), or None."""
        hint_info = getattr(req_info, "agent_hint_info", None)
        priority = getattr(getattr(hint_info, "priority_control", None), "priority", None)
        return priority if isinstance(priority, int) else None

    def _apply_priority_load_gate(
        self, instances: list[Instance], req_info: RequestInfo, role: PDRole
    ) -> list[Instance]:
        """Exclude overloaded endpoints for normal-priority requests (soft-load gate).

        An instance is overloaded when EVERY endpoint's workload score reaches
        ``priority_load_threshold``. High-priority requests (hint priority >=
        ``priority_admission_level``) bypass the gate; so do disabled gates and
        empty results — the gate must never fail a request (availability first).
        """
        if self._priority_load_threshold <= 0 or not instances:
            return instances
        # WP-E2 hard priority: strict_priority non-None implies it survived the
        # entry trust gate (untrusted claims are stripped there) — bypass fully.
        strict_level = getattr(
            getattr(getattr(req_info, "agent_hint_info", None), "strict_priority", None), "level", None
        )
        if strict_level is not None:
            return instances
        priority = self._request_priority(req_info)
        if priority is not None and priority >= self._priority_admission_level:
            return instances

        def _overloaded(instance: Instance) -> bool:
            endpoints = instance.get_all_endpoints()
            if not endpoints:
                return False
            return all(
                endpoint.workload.calculate_workload_score() >= self._priority_load_threshold for endpoint in endpoints
            )

        kept = [instance for instance in instances if not _overloaded(instance)]
        if len(kept) == len(instances):
            return instances
        if not kept:
            # All candidates are over the gate: availability wins over priority.
            logger.warning(
                "priority load gate kept all instances overloaded req_id=%s role=%s threshold=%s",
                getattr(req_info, "req_id", None),
                getattr(role, "value", role),
                self._priority_load_threshold,
            )
            return instances
        filtered_out = [instance.id for instance in instances if instance not in kept]
        logger.info(
            "priority load gate filtered instances=%s req_id=%s role=%s threshold=%s priority=%s",
            filtered_out,
            getattr(req_info, "req_id", None),
            getattr(role, "value", role),
            self._priority_load_threshold,
            priority,
        )
        return kept

    async def _refresh_cache_from_workload_reader(self, role: PDRole | None = None) -> None:
        """Patch live workload into the local cache and pull a fresh instance list on
        heartbeat-stale or instance-version change.

        Runs before select_and_allocate so each role's selection makes load-aware decisions on
        fresh workload and instance membership. KV cache usage is also synced from the
        scheduler-published shared memory here (read-only, no engine I/O).
        """
        if self._kv_usage_reader:
            try:
                # read_snapshot_ex 区分"发布端清空"与"无新快照"：空快照也会清缓存，
                # 避免引擎重启/指标消失后旧 usage 永久残留。
                changed, snapshot = self._kv_usage_reader.read_snapshot_ex()
                if changed:
                    update_kv_usage_cache(snapshot, allow_clear=True)
            except Exception as e:
                logger.warning("Failed to sync kv usage from shm: %s", e)
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

    @property
    def _scheduler_type(self) -> str:
        """Deprecated alias of prefill type; tests may still assign this to set both roles."""
        return self._prefill_scheduler_type

    @_scheduler_type.setter
    def _scheduler_type(self, value: str) -> None:
        self._prefill_scheduler_type = value
        self._decode_scheduler_type = value

    def _arbitration_context(self, role: PDRole | None = None) -> ArbitrationContext:
        """Worker-local arbitration view: instance cache + circuit-breaker mirror."""

        def _get_available(pool_role: PDRole | None) -> dict[int, Instance]:
            if pool_role is None:
                merged: dict[int, Instance] = {}
                for pdrole in (PDRole.ROLE_E, PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_U):
                    for inst in self._cache.get_instances(pdrole):
                        merged[inst.id] = inst
                return merged
            return {inst.id: inst for inst in self._cache.get_instances(pool_role)}

        return ArbitrationContext(
            get_available_instances=_get_available,
            is_instance_circuit_open=self.is_instance_blocked,
            endpoint_instance_score_weight=self._endpoint_instance_score_weight,
            is_load_balance_scheduler=self._scheduler_type_for_role(role) == "load_balance",
            # 权威重选路径与快路径使用同一 KV usage 数据源，保证 D 实例评分口径一致。
            kv_usage_provider=get_endpoint_kv_cache_usage,
        )

    def _committed_workload_for(
        self,
        role: PDRole,
        candidate_policy: str,
        instance: Instance,
        endpoint: Endpoint,
        demand: Workload,
        matched_tokens_map: dict[tuple[int, int], float],
        isl: float,
    ) -> Workload:
        """Same commit formula the former ALLOCATE_ONLY handler used (R4)."""
        if (
            candidate_policy == CANDIDATE_POLICY_KV_CACHE_AFFINITY
            and isl > 0
            and role in (PDRole.ROLE_P, PDRole.ROLE_U)
        ):
            return calculate_committed_workload(
                role,
                isl,
                matched_tokens=matched_tokens_map.get((instance.id, endpoint.id), 0.0),
            )
        return demand

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
        required_engine_type: str | None = None,
        required_dispatch_capability: str | None = None,
    ) -> tuple[Instance, Endpoint, Workload] | None:
        """Select locally, then CAS-commit on schema-4 SHM (no ALLOCATE_ONLY ZMQ)."""
        from motor.coordinator.scheduler.runtime.workload_shm.layout import FLAG_BLOCKED
        from motor.coordinator.scheduler.runtime.workload_shm.native import (
            STATUS_BLOCKED,
            STATUS_CHANGED,
            STATUS_OK,
            STATUS_SLOT_INVALID,
        )

        role_str = role.value if role is not None else (getattr(PDRole.ROLE_U, "value", "union"))
        await self._refresh_cache_from_workload_reader(role)
        if self._workload_reader is None or self._workload_reader.native is None:
            logger.error(
                "select_and_allocate refused: native workload shm not attached role=%s req_id=%s",
                role_str,
                req_info.req_id,
            )
            return None

        global_affinity = False
        normalized_engine_type = str(required_engine_type or "").strip().lower()
        proposed_instance: Instance
        proposed_endpoint: Endpoint
        normalized_dispatch_capability = str(required_dispatch_capability or "").strip()

        # Session affinity (Phase 2): own-session pin → parent-session pin, with
        # per-role scoping and epoch invalidation. A dropped pin falls back to
        # the normal policy — affinity must never fail a request.
        affinity_session_id: str | None = None
        affinity_parent_session_id: str | None = None
        affinity_epoch: int | None = None
        affinity_outcome = "-"
        target_endpoint_id: int | None = None
        if self._session_affinity is not None:
            hint_info = getattr(req_info, "agent_hint_info", None)
            affinity_session_id = getattr(hint_info, "session_id", None)
            affinity_parent_session_id = getattr(hint_info, "parent_session_id", None)
            affinity_epoch = getattr(getattr(hint_info, "session_hint", None), "epoch", None)

        if (
            target_instance_id is None
            and self._session_affinity is not None
            and (affinity_session_id or affinity_parent_session_id)
        ):
            pool = await self.get_available_instances(role)
            eligible = self._affinity_eligible_instance_ids(
                pool, normalized_engine_type or None, normalized_dispatch_capability or None
            )
            decision = resolve_session_pin(
                self._session_affinity,
                affinity_session_id,
                affinity_parent_session_id,
                affinity_epoch,
                getattr(role, "value", ""),
                eligible,
            )
            affinity_outcome = decision.outcome
            if decision.instance_id is not None:
                target_instance_id = decision.instance_id
                target_endpoint_id = decision.endpoint_id
                logger.info(
                    "session affinity %s session_id=%s parent_session_id=%s instance_id=%s req_id=%s",
                    decision.outcome,
                    affinity_session_id,
                    affinity_parent_session_id,
                    decision.instance_id,
                    req_info.req_id,
                )
            elif decision.outcome in NEW_PIN_OUTCOMES and (session_affinity_placement_mode() == PLACEMENT_BALANCED):
                # Atomic select-and-commit: count-read, selection and pin write
                # happen under one shared-table critical section, so two workers
                # in a fresh burst cannot read the same stale counts and double
                # commit onto one instance (review 194528732).
                pool_balanced = await self.get_available_instances(role)
                eligible_balanced = self._affinity_eligible_instance_ids(
                    pool_balanced, normalized_engine_type or None, normalized_dispatch_capability or None
                )
                if hasattr(self._session_affinity, "record_balanced"):
                    chosen = self._session_affinity.record_balanced(
                        affinity_session_id,
                        eligible_balanced,
                        getattr(role, "value", ""),
                        affinity_epoch,
                        self._instance_active_tokens,
                        self._affinity_placement_seq,
                    )
                    self._affinity_placement_seq += 1
                else:
                    chosen = await self._select_balanced_affinity_instance(
                        role, normalized_engine_type, normalized_dispatch_capability
                    )
                    if chosen is not None:
                        self._session_affinity.record(
                            affinity_session_id,
                            chosen,
                            getattr(role, "value", ""),
                            epoch=affinity_epoch,
                        )
                if chosen is not None:
                    target_instance_id = chosen
                    logger.info(
                        "session affinity %s session_id=%s parent_session_id=%s instance_id=%s "
                        "req_id=%s placement=balanced",
                        decision.outcome,
                        affinity_session_id,
                        affinity_parent_session_id,
                        chosen,
                        req_info.req_id,
                    )
                else:
                    logger.info(
                        "session affinity %s session_id=%s req_id=%s; no balanced candidate; fallback to policy",
                        decision.outcome,
                        affinity_session_id,
                        req_info.req_id,
                    )
            elif decision.outcome in ("stale", "epoch"):
                logger.info(
                    "session affinity %s session_id=%s req_id=%s; fallback to policy",
                    decision.outcome,
                    affinity_session_id,
                    req_info.req_id,
                )

        if target_instance_id is not None:
            instances = await self.get_available_instances(role)
            instances = {
                candidate.id: candidate
                for candidate in self._filter_instances(
                    list(instances.values()),
                    normalized_engine_type or None,
                    normalized_dispatch_capability or None,
                )
            }
            instance = resolve_pinned_instance(instances, target_instance_id)
            if instance is None:
                logger.warning(
                    "Pinned instance_id=%s not available for role=%s req_id=%s",
                    target_instance_id,
                    role_str,
                    req_info.req_id,
                )
                return None
            endpoint = None
            # Review 194528817: multi-endpoint (DP) instances cache per endpoint,
            # so a hit must return to the endpoint that owns the pinned KV —
            # re-picking by load would silently switch DPs and miss. Fall back to
            # the load-based pick only when the recorded endpoint is gone.
            pinned_endpoint_id = target_endpoint_id
            if pinned_endpoint_id is not None:
                pinned_candidate = next(
                    (ep for ep in instance.get_all_endpoints(include_headless=True) if ep.id == pinned_endpoint_id),
                    None,
                )
                if pinned_candidate is not None:
                    endpoint = pinned_candidate
                else:
                    logger.info(
                        "pinned endpoint_id=%s no longer available on instance=%s; re-picking by load",
                        pinned_endpoint_id,
                        instance.id,
                    )
            if endpoint is None:
                endpoint = select_endpoint_for_instance(
                    instance,
                    scheduler_type=self._scheduler_type_for_role(role),
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
            candidate_policy = self._scheduler_type_for_role(role)
            candidate_endpoints = [{"instance_id": instance.id, "endpoint_id": endpoint.id}]
            proposed_instance, proposed_endpoint = instance, endpoint
        else:
            request_top_k = (
                _AFFINITY_CANDIDATE_TOPK
                if (
                    role in _KVA_SELECT_ROLES
                    and self._scheduler_type_for_role(role) == "kv_cache_affinity"
                    and self._kv_affinity_mode != KV_AFFINITY_MODE_UNIFIED
                )
                else 1
            )
            candidates, candidate_policy = await self._select_endpoint_candidates_with_policy(
                req_info,
                role,
                top_k=request_top_k,
                required_engine_type=normalized_engine_type or None,
                required_dispatch_capability=normalized_dispatch_capability or None,
            )
            if not candidates:
                return None
            proposed_instance, proposed_endpoint, _ = candidates[0]
            affinity_debug = getattr(req_info, "kv_affinity_debug", None)
            global_affinity = (
                candidate_policy == CANDIDATE_POLICY_KV_CACHE_AFFINITY
                and isinstance(affinity_debug, dict)
                and any(rec[2] is not None for rec in affinity_debug.values())
            )
            if global_affinity:
                allowed_instance_ids = {
                    candidate.id
                    for candidate in self._filter_instances(
                        self._cache.get_instances(role),
                        normalized_engine_type or None,
                        normalized_dispatch_capability or None,
                    )
                }
                candidate_endpoints = [
                    {
                        "instance_id": ins_id,
                        "endpoint_id": ep_id,
                        "matched_tokens": rec[0],
                        "prefill_cost": rec[2],
                    }
                    for (ins_id, ep_id), rec in affinity_debug.items()
                    if rec[2] is not None and ins_id in allowed_instance_ids
                ]
            elif candidate_policy == CANDIDATE_POLICY_KV_CACHE_AFFINITY and isinstance(affinity_debug, dict):
                candidate_endpoints = []
                for cand_instance, cand_endpoint, _score in candidates:
                    item = {"instance_id": cand_instance.id, "endpoint_id": cand_endpoint.id}
                    rec = affinity_debug.get((cand_instance.id, cand_endpoint.id))
                    if rec is not None:
                        item["matched_tokens"] = rec[0]
                    candidate_endpoints.append(item)
            elif candidate_policy == CANDIDATE_POLICY_DYNAMIC_BUCKET:
                candidate_endpoints = self._get_dynamic_bucket_candidates(
                    role,
                    proposed_instance.id,
                    proposed_endpoint.id,
                    normalized_engine_type or None,
                    normalized_dispatch_capability or None,
                )
            else:
                candidate_endpoints = [
                    {"instance_id": cand_instance.id, "endpoint_id": cand_endpoint.id}
                    for cand_instance, cand_endpoint, _score in candidates
                ]

        scheduler_type = self._scheduler_type_for_role(role)
        if scheduler_type == "round_robin":
            demand = Workload()
        elif scheduler_type == "dynamic_bucket":
            demand = make_dynamic_bucket_allocation_workload(req_info)
        else:
            demand = calculate_demand_workload(role, req_info)
        token_ids = getattr(req_info, "token_ids", None)
        isl = float(len(token_ids)) if isinstance(token_ids, list) and token_ids else 0.0
        candidate_pairs = [(int(item["instance_id"]), int(item["endpoint_id"])) for item in candidate_endpoints]
        affinity_triples = [
            (int(item["instance_id"]), int(item["endpoint_id"]), float(item["prefill_cost"]))
            for item in candidate_endpoints
            if item.get("prefill_cost") is not None
        ]
        matched_tokens_map = {
            (int(item["instance_id"]), int(item["endpoint_id"])): float(item["matched_tokens"])
            for item in candidate_endpoints
            if item.get("matched_tokens") is not None
        }
        proposed = (proposed_instance.id, proposed_endpoint.id)
        excluded: set[tuple[int, int]] = set()
        # Affinity-miss falls back to load_balance with top_k=1. Fast-path would then CAS the
        # worker's stale proposal using a live SHM expected, so concurrent cold prefills pile
        # onto the same endpoint. Re-pick the current global min and CAS against the load that
        # decision saw; a concurrent add then CHANGED-retries onto the next emptiest pair.
        # Pinned allocate must keep the pinned pair, so it stays on the validate-only path.
        use_authoritative = candidate_policy == CANDIDATE_POLICY_LOAD_BALANCE and target_instance_id is None
        native = self._workload_reader.native
        if native is None:
            return None

        for _attempt in range(_MAX_CAS_ALLOCATE_ATTEMPTS):
            # First attempt reuses the candidate-selection refresh above; later retries re-read.
            if _attempt > 0:
                await self._refresh_cache_from_workload_reader(role)
            ctx = self._arbitration_context(role)
            open_pairs = [pair for pair in candidate_pairs if pair not in excluded] or (
                [proposed] if proposed not in excluded else []
            )
            if use_authoritative:
                selected = select_authoritative_allocate_candidate(
                    ctx,
                    proposed,
                    open_pairs,
                    role,
                    candidate_policy,
                    affinity_triples if global_affinity else None,
                    self._kv_affinity_prefill_load_scale if global_affinity else None,
                    self._kv_affinity_load_weight if global_affinity else None,
                    normalized_engine_type or None,
                    excluded=excluded,
                    required_dispatch_capability=normalized_dispatch_capability or None,
                    req_id=req_info.req_id,
                )
            else:
                selected = select_valid_candidate(
                    ctx,
                    proposed,
                    role,
                    normalized_engine_type or None,
                    normalized_dispatch_capability or None,
                )
                if selected is None:
                    selected = select_authoritative_allocate_candidate(
                        ctx,
                        proposed,
                        open_pairs,
                        role,
                        candidate_policy,
                        affinity_triples if global_affinity else None,
                        self._kv_affinity_prefill_load_scale if global_affinity else None,
                        self._kv_affinity_load_weight if global_affinity else None,
                        normalized_engine_type or None,
                        excluded=excluded,
                        required_dispatch_capability=normalized_dispatch_capability or None,
                        req_id=req_info.req_id,
                    )
                    use_authoritative = True
            if selected is None:
                return None
            out_instance, out_endpoint, selected_score = selected
            pair = (out_instance.id, out_endpoint.id)
            if pair in excluded:
                use_authoritative = True
                continue
            committed = self._committed_workload_for(
                role,
                candidate_policy,
                out_instance,
                out_endpoint,
                demand,
                matched_tokens_map,
                isl,
            )
            decision_expected = float(out_endpoint.workload.active_tokens if out_endpoint.workload is not None else 0.0)
            meta = self._workload_reader.entry_meta(out_instance.id, out_endpoint.id)
            if meta is None or int(meta.get("flags", 0)) & FLAG_BLOCKED:
                excluded.add(pair)
                use_authoritative = True
                continue
            status, actual = native.cas_add(
                out_instance.id,
                out_endpoint.id,
                int(meta["generation"]),
                decision_expected,
                float(committed.active_tokens),
                slot=meta.get("slot"),
            )
            if status == STATUS_OK:
                self._cache.patch_workload_from_shm(out_instance.id, out_endpoint.id, role, actual)
                meta["active_tokens"] = actual
                self._dp_stats.record(
                    instance_id=out_instance.id,
                    dp_rank=out_endpoint.id,
                )
                affinity_debug = getattr(req_info, "kv_affinity_debug", None)
                matched_load = (
                    affinity_debug.get((out_instance.id, out_endpoint.id))
                    if (candidate_policy == CANDIDATE_POLICY_KV_CACHE_AFFINITY and isinstance(affinity_debug, dict))
                    else None
                )
                tier_hit = matched_load[3] if matched_load and len(matched_load) > 3 else None
                # KV-affinity hit rate: matched prefix tokens / prompt token ids (kv_cache_affinity
                # only; None for other policies or when token ids are unavailable).
                matched = matched_load[0] if matched_load else None
                token_ids = getattr(req_info, "token_ids", None)
                hit_rate = (
                    matched / len(token_ids)
                    if (matched is not None and isinstance(token_ids, list) and token_ids)
                    else None
                )
                hint_info = getattr(req_info, "agent_hint_info", None)
                hint_priority = getattr(hint_info, "priority_control", None)
                prio = getattr(hint_priority, "priority", None)
                prio_reason = getattr(hint_priority, "reason", None)
                logger.info(
                    "scheduled role=%s req_id=%s input_tokens=%s instance=%s endpoint=%s policy=%s matched=%s "
                    "npu(hbm)=%s cpu(dram)=%s disk=%s load=%s committed=%s score=%s fast_path=%s repicked=%s "
                    "proposed=%s-%s hit_rate=%s prio=%s aff=%s reason=%s sp=%s",
                    role_str,
                    req_info.req_id,
                    int(isl),
                    out_instance.id,
                    out_endpoint.id,
                    candidate_policy,
                    matched,
                    tier_hit[0] if tier_hit else None,
                    tier_hit[1] if tier_hit else None,
                    tier_hit[2] if tier_hit else None,
                    matched_load[1] if matched_load else None,
                    committed.active_tokens,
                    selected_score,
                    not use_authoritative,
                    pair != proposed,
                    proposed_instance.id,
                    proposed_endpoint.id,
                    hit_rate,
                    prio,
                    affinity_outcome,
                    prio_reason,
                    getattr(getattr(hint_info, "strict_priority", None), "level", None),
                )
                if self._session_affinity is not None and affinity_session_id:
                    self._session_affinity.record(
                        affinity_session_id,
                        out_instance.id,
                        getattr(role, "value", ""),
                        epoch=affinity_epoch,
                        endpoint_id=out_endpoint.id,
                    )
                    # Retention is recorded on the same request that establishes the
                    # binding — after the pin exists, so the promise can be written
                    # onto the shared slot (design §2.1; review 194528478).
                    self._record_cache_declaration(hint_info, affinity_session_id, getattr(role, "value", ""))
                return (out_instance, out_endpoint, committed)
            if status == STATUS_CHANGED:
                use_authoritative = True
                continue
            if status in (STATUS_BLOCKED, STATUS_SLOT_INVALID):
                excluded.add(pair)
                use_authoritative = True
                continue
            logger.error(
                "select_and_allocate unexpected cas status=%s role=%s req_id=%s",
                status,
                role_str,
                req_info.req_id,
            )
            return None
        logger.warning(
            "select_and_allocate exhausted CAS retries role=%s req_id=%s",
            role_str,
            req_info.req_id,
        )
        return None

    async def claim_sample(
        self,
        d_instance_id: int,
        now: float,
        interval_seconds: float,
    ) -> bool:
        if not self._transport.connected:
            logger.warning("claim_sample: scheduler transport not connected")
            return False
        request_id = self._next_request_id()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.CONFIRM_SAMPLE,
            request_id=request_id,
            data={
                "p_instance_id": None,
                "d_instance_id": d_instance_id,
                "now": now,
                "interval_seconds": interval_seconds,
            },
        )
        response = await self._transport.send_request(request)
        if response and response.response_type == SchedulerResponseType.SUCCESS:
            return bool((response.data or {}).get("confirmed", False))
        if response:
            logger.warning("claim_sample failed d_instance_id=%s error=%s", d_instance_id, response.error)
        else:
            logger.warning("claim_sample: no response (timeout) d_instance_id=%s", d_instance_id)
        return False

    async def record_precision_result(
        self,
        key: tuple[int | None, int],
        has_issue: bool,
        threshold: int,
        *,
        clear_threshold: int,
        check_valid: bool,
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
                "clear_threshold": clear_threshold,
                "check_valid": check_valid,
            },
        )
        response = await self._transport.send_request(request)
        if response and response.response_type == SchedulerResponseType.SUCCESS:
            data = response.data or {}
            return PrecisionStreakResult(
                skip=bool(data.get("skip", False)),
                threshold_hit=bool(data.get("threshold_hit", False)),
                clear_threshold_hit=bool(data.get("clear_threshold_hit", False)),
                consecutive=int(data.get("consecutive", 0)),
                action_token=data.get("action_token"),
                alarm_moi=data.get("alarm_moi"),
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
        *,
        action_type: str,
        success: bool,
        alarm_moi: str | None = None,
        auto_recovery_cleared: bool = False,
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
                "action_type": action_type,
                "success": success,
                "alarm_moi": alarm_moi,
                "auto_recovery_cleared": auto_recovery_cleared,
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

    async def dismiss_precision_alarm_state(
        self,
        *,
        p_instance_id: int | None,
        d_instance_id: int,
    ) -> bool:
        if not self._transport.connected:
            logger.warning("dismiss_precision_alarm_state: scheduler transport not connected")
            return False
        request_id = self._next_request_id()
        request = SchedulerRequest(
            request_type=SchedulerRequestType.DISMISS_PRECISION_ALARM_STATE,
            request_id=request_id,
            data={
                "p_instance_id": p_instance_id,
                "d_instance_id": d_instance_id,
            },
        )
        response = await self._transport.send_request(request)
        if response and response.response_type == SchedulerResponseType.SUCCESS:
            return bool((response.data or {}).get("dismissed", False))
        if response:
            logger.warning(
                "dismiss_precision_alarm_state failed pd_group=(%s,%s) error=%s",
                p_instance_id,
                d_instance_id,
                response.error,
            )
        else:
            logger.warning(
                "dismiss_precision_alarm_state: no response pd_group=(%s,%s)",
                p_instance_id,
                d_instance_id,
            )
        return False

    async def update_workload(self, params: UpdateWorkloadParams) -> bool:
        """Release path: CAS-sub floor 0 on schema-4 SHM (no UPDATE_WORKLOAD ZMQ)."""
        from motor.coordinator.scheduler.runtime.workload_shm.native import STATUS_OK

        role_str = params.role.value if hasattr(params.role, "value") else str(params.role)
        if self._workload_reader is None or self._workload_reader.native is None:
            logger.error(
                "update_workload refused: native workload shm not attached instance_id=%s endpoint_id=%s req_id=%s",
                params.instance_id,
                params.endpoint_id,
                params.req_id,
            )
            return False
        if params.workload_action != WorkloadAction.RELEASE_TOKENS:
            logger.error(
                "update_workload refused: action=%s is not RELEASE_TOKENS instance_id=%s endpoint_id=%s req_id=%s",
                getattr(params.workload_action, "value", params.workload_action),
                params.instance_id,
                params.endpoint_id,
                params.req_id,
            )
            return False
        meta = self._workload_reader.entry_meta(params.instance_id, params.endpoint_id)
        if meta is None:
            logger.warning(
                "update_workload slot missing instance_id=%s endpoint_id=%s req_id=%s",
                params.instance_id,
                params.endpoint_id,
                params.req_id,
            )
            return False
        delta = abs(float(params.workload_change.active_tokens))
        status, actual = self._workload_reader.native.cas_sub_floor0(
            params.instance_id,
            params.endpoint_id,
            int(meta["generation"]),
            delta,
            slot=meta.get("slot"),
        )
        if status != STATUS_OK:
            logger.warning(
                "cas_sub_floor0 failed status=%s instance_id=%s endpoint_id=%s role=%s req_id=%s",
                status,
                params.instance_id,
                params.endpoint_id,
                role_str,
                params.req_id,
            )
            return False
        try:
            role = params.role if isinstance(params.role, PDRole) else PDRole(params.role)
        except ValueError:
            role = PDRole.ROLE_U
        meta["active_tokens"] = actual
        # CAS already committed above; a cache-patch failure must not turn this into a retry
        # (a second cas_sub_floor0 would subtract the same delta twice).
        try:
            self._cache.patch_workload_from_shm(params.instance_id, params.endpoint_id, role, actual)
        except Exception as e:
            logger.warning(
                "update_workload cache patch failed after CAS success instance_id=%s endpoint_id=%s "
                "role=%s req_id=%s: %s",
                params.instance_id,
                params.endpoint_id,
                role_str,
                params.req_id,
                e,
            )
        return True

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
                    except Exception as e:
                        logger.error(
                            "Workload shm %s attach failed: %s",
                            shm_name,
                            e,
                        )
                        self._workload_reader = None
                    else:
                        self._last_instance_version = None

            kv_usage_shm_name = data.get("kv_usage_shm_name")
            if kv_usage_shm_name:
                need_attach = not self._kv_usage_reader or self._kv_usage_reader.shm_name != kv_usage_shm_name
                if need_attach:
                    if self._kv_usage_reader:
                        self._kv_usage_reader.detach()
                    self._kv_usage_reader = KvUsageShmReader(kv_usage_shm_name)
                    try:
                        self._kv_usage_reader.attach()
                    except FileNotFoundError:
                        logger.debug(
                            "KV usage shm %s not ready, will retry on next get_available_instances",
                            kv_usage_shm_name,
                        )
                        self._kv_usage_reader = None
                    else:
                        # 注册模块级 reader：调度热路径的 get_endpoint_kv_cache_usage
                        # 按需从共享内存拉新快照（MetricsCollector 每轮采集后推送）。
                        set_kv_usage_reader(self._kv_usage_reader)

            sched_metrics_shm_name = data.get("sched_metrics_shm_name")
            if sched_metrics_shm_name:
                self._sched_metrics_shm_name = sched_metrics_shm_name

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
            logger.error("Failed to get available instances: %s", response.error)
        return {}

    async def get_instance_status(self) -> dict[str, Any] | None:
        """Fetch the authoritative all-pool instance status from the Mgmt process."""
        request = SchedulerRequest(
            request_type=SchedulerRequestType.GET_INSTANCE_STATUS,
            request_id=self._next_request_id(),
            data={},
        )
        response = await self._transport.send_request(request)
        if response and response.response_type == SchedulerResponseType.SUCCESS:
            return response.data
        return None

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

    def _instances_from_cache(self, role: PDRole | None = None) -> dict[int, Instance]:
        if role is not None:
            return {inst.id: inst for inst in self._cache.get_instances(role)}
        instances: dict[int, Instance] = {}
        for cached_role in (PDRole.ROLE_E, PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_U):
            for inst in self._cache.get_instances(cached_role):
                instances[inst.id] = inst
        return instances

    async def get_local_instances(self, role: PDRole | None = None) -> dict[int, Instance]:
        """Return cached instances; RPC warm-up only when the local view is empty."""
        instances = self._instances_from_cache(role)
        if instances:
            return instances
        try:
            await self.get_available_instances(None)
        except Exception as e:
            logger.debug("get_local_instances: warm-up fetch failed: %s", e)
        return self._instances_from_cache(role)

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

    async def _on_agent_status_event(self, session_id: str, state: str) -> None:
        """Apply an out-of-band agent_status fan-out from Scheduler PUB (Phase 4).

        The fan-out drives two things: the worker's local session state machine
        (via ``agent_status_state_listener``, so finalize's terminal semantics —
        idempotent finalize, no re-entry into running — hold on every worker,
        not only the one that received the POST), and the affinity/declaration
        release for ``finalize``. Other states are informational (T-A2/A5
        engine KV actions are gated on the engine capability conclusion and are
        not wired here). Never fails scheduling: a lost PUB event only leaves
        the pin for the normal stale/epoch/LRU fallback to clean up.
        """
        if not session_id or not state:
            return
        listener = getattr(self, "agent_status_state_listener", None)
        if listener is not None:
            try:
                listener(session_id, state)
            except Exception as e:
                logger.warning(
                    "agent status state listener failed: session_id=%s state=%s err=%s", session_id, state, e
                )
        if state != "finalize":
            logger.debug(
                "agent status event (observability only): session_id=%s state=%s",
                session_id,
                state,
            )
            return
        # finalize releases the retention immediately, so the keeper must not act on it
        # again later (design 20261015-03 §2.3).
        if self._cache_declarations is not None:
            self._cache_declarations.drop(session_id)
        if self._session_affinity is None:
            return
        dropped = self._session_affinity.drop_all_roles(session_id)
        if dropped:
            logger.info(
                "agent status finalize released session pins: session_id=%s dropped=%d",
                session_id,
                dropped,
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
            # Success reports fire on every completed inference — keep them at
            # DEBUG so healthy traffic doesn't spam the log; failures stay at
            # WARNING (logged above).
            logger.debug(
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

    def drop_session_pins(self, session_id: str) -> int:
        """Release every role pin for session_id in THIS worker's table (Phase 4).

        Local half of the finalize cleanup; the fleet-wide half goes through
        SchedulerServer PUB (``report_agent_status``). Returns the number of
        entries removed (0 when affinity is disabled or the session has no
        pin here) so the caller can log the release.
        """
        if self._cache_declarations is not None:
            self._cache_declarations.drop(session_id)
        if self._session_affinity is None:
            return 0
        return self._session_affinity.drop_all_roles(session_id)

    async def report_agent_status(self, session_id: str, state: str) -> None:
        """Relay an out-of-band agent_status event to SchedulerServer (Phase 4).

        Fire-and-forget like ``report_cb_event``: the finalize fan-out releases
        session pins fleet-wide (WP-A T-A4); delivery is best-effort (a lost
        event only leaves the pin for the normal stale/epoch/LRU fallback —
        affinity never fails a request).
        """
        if not self._transport.connected:
            logger.warning(
                "agent status relay dropped (transport disconnected): session_id=%s state=%s",
                session_id,
                state,
            )
            return
        request = SchedulerRequest(
            request_type=SchedulerRequestType.AGENT_STATUS_NOTIFY,
            request_id=str(uuid.uuid4()),
            data={
                "session_id": session_id,
                "state": state,
            },
        )

        def _on_agent_status_send_done(fut):
            if fut.cancelled():
                return
            try:
                fut.result()
            except Exception as err:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "agent status notify send failed: session_id=%s state=%s error=%s",
                    session_id,
                    state,
                    err,
                )

        task = asyncio.create_task(self._transport.send_request(request))
        task.add_done_callback(_on_agent_status_send_done)

    def _select_endpoint_candidates_from_list_with_policy(
        self,
        instances: list[Instance],
        role: PDRole,
        req_info: RequestInfo,
        top_k: int = 1,
    ) -> tuple[list[tuple[Instance, Endpoint, float]], str]:
        if not instances:
            return [], self._scheduler_type_for_role(role)
        st = self._scheduler_type_for_role(role)
        if st == "load_balance":
            candidates = self._select_endpoint_candidates_by_load_balance(
                instances, role, top_k, req_id=req_info.req_id
            )
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
                    w_npu=self._kv_affinity_w_npu,
                    w_cpu=self._kv_affinity_w_cpu,
                    w_disk=self._kv_affinity_w_disk,
                    hit_rate_threshold=self._kv_affinity_hit_rate_threshold,
                    top_k=max(1, top_k),
                )
                if ranked:
                    return ranked, CANDIDATE_POLICY_KV_CACHE_AFFINITY
                if ranked is None:
                    logger.warning("kv_cache_affinity unavailable (no conductor match), falling back to load_balance")
            candidates = self._select_endpoint_candidates_by_load_balance(
                instances, role, top_k, req_id=req_info.req_id
            )
            if candidates:
                return candidates, CANDIDATE_POLICY_LOAD_BALANCE
            logger.warning("load_balance unavailable, falling back to round-robin")
        elif st == "dynamic_bucket":
            if role in (PDRole.ROLE_E, PDRole.ROLE_P):
                candidates = self._select_endpoint_candidates_by_load_balance(instances, role, top_k)
                if candidates:
                    return candidates, CANDIDATE_POLICY_LOAD_BALANCE
                logger.warning("dynamic_bucket load_balance unavailable for role=%s", role)
            else:
                req_length = get_request_token_length(req_info)
                eligible_instances = [instance for instance in instances if not self.is_instance_blocked(instance.id)]
                if req_length is not None:
                    selected = DynamicBucketPolicy.select_endpoint_from_list(
                        eligible_instances,
                        req_length,
                        self._dynamic_bucket_selector,
                    )
                    if selected is not None:
                        instance, endpoint = selected
                        return [(instance, endpoint, 0.0)], CANDIDATE_POLICY_DYNAMIC_BUCKET
                logger.warning(
                    "dynamic_bucket unavailable req_id=%s, falling back to load_balance",
                    req_info.req_id,
                )
                candidates = self._select_endpoint_candidates_by_load_balance(instances, role, top_k)
                if candidates:
                    return candidates, CANDIDATE_POLICY_LOAD_BALANCE
                logger.warning("dynamic_bucket unavailable, falling back to round-robin")
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
        selected = self._select_endpoint_for_instance(selected_instance, role)
        if not selected:
            return [], CANDIDATE_POLICY_ROUND_ROBIN
        instance, endpoint = selected
        return [(instance, endpoint, 0.0)], CANDIDATE_POLICY_ROUND_ROBIN

    def _select_endpoint_for_instance(
        self, instance: Instance, role: PDRole | None = None
    ) -> tuple[Instance, Endpoint] | None:
        if not instance:
            return None
        all_endpoints = instance.get_all_endpoints()
        if not all_endpoints:
            return None
        st = self._scheduler_type_for_role(role)
        if st in ("load_balance", "kv_cache_affinity", "dynamic_bucket"):
            ep = LoadBalancePolicy.select_endpoint_from_instance(instance)
            if ep:
                return (instance, ep)
            return (instance, all_endpoints[0])
        ep = RoundRobinPolicy.select_endpoint_from_instance(
            instance, self._endpoint_rr_counters, is_blocked=self.is_instance_blocked
        )
        return (instance, ep) if ep else None

    def _get_dynamic_bucket_candidates(
        self,
        role: PDRole,
        instance_id: int,
        endpoint_id: int,
        required_engine_type: str | None = None,
        required_dispatch_capability: str | None = None,
    ) -> list[dict[str, int]]:
        """Return every endpoint in the selected bucket for fresh CAS re-selection."""
        role_instances = self._filter_instances(
            self._cache.get_instances(role),
            required_engine_type,
            required_dispatch_capability,
        )
        candidates = DynamicBucketPolicy.build_bucket_endpoints(
            [instance for instance in role_instances if not self.is_instance_blocked(instance.id)],
            self._dynamic_bucket_selector.short_bucket_count,
            self._dynamic_bucket_selector.long_bucket_count,
        )
        selected_bucket = next(
            (item.bucket for item in candidates if item.instance.id == instance_id and item.endpoint.id == endpoint_id),
            None,
        )
        if selected_bucket is None:
            return [{"instance_id": instance_id, "endpoint_id": endpoint_id}]
        return [
            {"instance_id": item.instance.id, "endpoint_id": item.endpoint.id}
            for item in candidates
            if item.bucket == selected_bucket
        ]

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
        *,
        req_id: str | None = None,
    ) -> list[tuple[Instance, Endpoint, float]]:
        # Equal-score (tie) requests are broken uniformly at random by the policy itself, so
        # equal-load requests spread across nodes without a per-client rotation offset.
        log_load_balance_all_candidates(
            instances,
            role,
            req_id,
            is_blocked=self.is_instance_blocked,
            instance_score_weight=self._endpoint_instance_score_weight,
            exclude_highest_kv_usage=(role == PDRole.ROLE_D),
            kv_usage_provider=get_endpoint_kv_cache_usage if role == PDRole.ROLE_D else None,
        )
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            instances,
            role,
            top_k=max(1, top_k),
            instance_score_weight=self._endpoint_instance_score_weight,
            is_blocked=self.is_instance_blocked,
            # D 实例：评分上叠加 KV usage 惩罚项，避免调度到 KV cache 快满的实例。
            exclude_highest_kv_usage=(role == PDRole.ROLE_D),
            kv_usage_provider=get_endpoint_kv_cache_usage if role == PDRole.ROLE_D else None,
        )
        return [(candidate.instance, candidate.endpoint, candidate.score) for candidate in candidates]
