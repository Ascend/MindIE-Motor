# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Control-plane server owned by the Mgmt process: ZMQ ROUTER + PUB, schema-4 SHM,
circuit breaker, and precision sampling. Infer Workers CAS-commit on SHM; there
is no per-request ALLOCATE / UPDATE RPC and no standalone Scheduler process.
IPC paths remain scheduler_frontend / scheduler_instance_pub (bound by Mgmt).
"""

import asyncio
import os
import time
from multiprocessing import shared_memory
from typing import Awaitable, Callable

import zmq.asyncio
import msgspec

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.http_msg_spec import EventType
from motor.common.resources.instance import PDRole, Instance
from motor.common.logger import get_logger
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain.circuit_breaker import (
    CircuitBreakerManager,
)
from motor.coordinator.models.constants import DEFAULT_REQUEST_ID, REQUEST_ID_KEY
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy
from motor.coordinator.domain.instance_status import build_instance_status_response
from motor.coordinator.scheduler.scheduler import Scheduler
from motor.coordinator.scheduler.runtime.kv_usage import (
    DEFAULT_KV_USAGE_SHM_MAX_ENTRIES,
    KvUsageShmReader,
    KvUsageShmWriter,
    get_endpoint_kv_cache_usage,
    kv_usage_shm_total_size,
    set_kv_usage_reader,
)
from motor.coordinator.scheduler.runtime.scheduler_metrics import (
    DEFAULT_SCHED_METRICS_SHM_MAX_ENTRIES,
    SchedulerMetricsCollector,
    SchedulerMetricsShmWriter,
    _pack_role,
    sched_metrics_shm_total_size,
)
from motor.coordinator.scheduler.runtime.workload_shm import WorkloadSharedMemoryOwner
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    DEFAULT_WORKLOAD_SHM_MAX_ENTRIES,
)
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    SchedulerRequest,
    SchedulerResponse,
    SchedulerRequestType,
    SchedulerResponseType,
    INSTANCE_CHANGE_TOPIC,
    CIRCUIT_BREAKER_TOPIC,
    pack_send_frames,
    unpack_recv_payload,
)

logger = get_logger(__name__)

# Time bound (per phase: connect + first response line) for the pre-recovery
# HTTP health probe: an instance is only re-enabled after it proves ready
# (see _probe_instance / _auto_recover).
_RECOVERY_PROBE_TIMEOUT_SECS = 2.0

# Health-check endpoint served by the vLLM engine on the business port: it
# returns 200 only when the engine is ready to serve (model loaded); during
# loading / not-ready it returns 503. So a 200 answer means the instance is
# not merely reachable but actually usable.
_PROBE_HEALTH_PATH = "/health"


def _create_kv_usage_shared_memory(shm_module, shm_name: str, shm_size: int):
    """Create POSIX kv usage SharedMemory; recover from orphan segment like workload SHM."""
    try:
        return shm_module.SharedMemory(name=shm_name, create=True, size=shm_size)
    except FileExistsError:
        logger.warning(
            "KV usage SHM %s already exists (likely orphan from a prior run or PID reuse); unlinking and recreating",
            shm_name,
        )
        try:
            stale = shm_module.SharedMemory(name=shm_name, create=False)
        except FileNotFoundError:
            return shm_module.SharedMemory(name=shm_name, create=True, size=shm_size)
        try:
            stale.close()
            stale.unlink()
        except Exception as e:
            logger.error("Failed to unlink stale kv usage SHM %s: %s", shm_name, e)
            raise
        return shm_module.SharedMemory(name=shm_name, create=True, size=shm_size)


def _create_sched_metrics_shared_memory(shm_module, shm_name: str, shm_size: int):
    """Create POSIX scheduling-metrics SharedMemory; recover from orphan segment like workload SHM."""
    try:
        return shm_module.SharedMemory(name=shm_name, create=True, size=shm_size)
    except FileExistsError:
        logger.warning(
            "Sched metrics SHM %s already exists (orphan from prior run or PID reuse); unlinking and recreating",
            shm_name,
        )
        try:
            stale = shm_module.SharedMemory(name=shm_name, create=False)
        except FileNotFoundError:
            return shm_module.SharedMemory(name=shm_name, create=True, size=shm_size)
        try:
            stale.close()
            stale.unlink()
        except Exception as e:
            logger.error("Failed to unlink stale sched metrics SHM %s: %s", shm_name, e)
            raise
        return shm_module.SharedMemory(name=shm_name, create=True, size=shm_size)


InstanceRefreshCallback = Callable[[EventType, list[Instance]], None | Awaitable[None]]

# ==================== Serialization (module-level, shared by Server / Broadcaster) ====================


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


# ==================== Request dispatch ====================


class _SchedulerRequestDispatcher:
    """
    Control-plane RPC router: GET_AVAILABLE_INSTANCES, circuit-breaker report,
    and precision sampling. Instance refresh is in-process via apply_refresh.
    """

    def __init__(
        self,
        instance_manager: InstanceManager,
        scheduler: Scheduler,
        config: CoordinatorConfig,
        workload_writer: WorkloadSharedMemoryOwner | None = None,
        on_instance_refresh_done: InstanceRefreshCallback | None = None,
        circuit_breaker_manager: CircuitBreakerManager | None = None,
        pub_socket: zmq.asyncio.Socket | None = None,
        kv_usage_writer: KvUsageShmWriter | None = None,
        sched_metrics_writer: SchedulerMetricsShmWriter | None = None,
    ):
        self._instance_manager = instance_manager
        self._scheduler = scheduler
        self._config = config
        self._workload_writer = workload_writer
        self._kv_usage_writer = kv_usage_writer
        self._sched_metrics_writer = sched_metrics_writer
        self._on_instance_refresh_done = on_instance_refresh_done
        self._cb_manager = circuit_breaker_manager or CircuitBreakerManager(self._config.circuit_config)
        self._pub_socket = pub_socket
        self._recovery_timers: dict[int, asyncio.Task] = {}
        self._workload_commit_lock = asyncio.Lock()
        # instance_id -> BLOCKED state that failed to apply to SHM; drained by _retry_pending_blocked.
        self._pending_blocked: dict[int, bool] = {}
        # Desired PUB state for a set_blocked that has not yet landed on SHM.
        self._pending_blocked_pub: dict[int, str] = {}
        # True when InstanceManager has changes not yet reflected in the SHM snapshot; drained by
        # _retry_dirty_snapshot (also forces a resync on the next apply_refresh).
        self._snapshot_dirty = False

    async def dispatch(self, request: SchedulerRequest) -> SchedulerResponse:
        """Dispatch control-plane request to the appropriate handler."""
        handlers = {
            SchedulerRequestType.GET_AVAILABLE_INSTANCES.value: self._handle_get_available_instances,
            SchedulerRequestType.GET_INSTANCE_STATUS.value: self._handle_get_instance_status,
            SchedulerRequestType.CONFIRM_SAMPLE.value: self._handle_confirm_sample,
            SchedulerRequestType.RECORD_PRECISION_RESULT.value: self._handle_record_precision_result,
            SchedulerRequestType.FINISH_PRECISION_ACTION.value: self._handle_finish_precision_action,
            SchedulerRequestType.DISMISS_PRECISION_ALARM_STATE.value: self._handle_dismiss_precision_alarm_state,
            SchedulerRequestType.CIRCUIT_BREAKER_REPORT.value: self._handle_circuit_breaker_report,
        }
        handler = handlers.get(request.request_type)
        if handler:
            result = handler(request)
            if asyncio.iscoroutine(result):
                return await result
            return result
        return SchedulerResponse(
            response_type=SchedulerResponseType.ERROR,
            request_id=request.request_id,
            error=f"Unknown request type: {request.request_type}",
        )

    def _handle_get_available_instances(self, request: SchedulerRequest) -> SchedulerResponse:
        role_str = request.data.get("role")
        role = PDRole(role_str) if role_str else None
        instances = self._instance_manager.get_available_instances(role)
        instances_data = [_instance_to_dict(inst) for inst in instances.values()]
        data: dict = {
            "instances": instances_data,
        }
        if self._workload_writer:
            data["workload_shm_name"] = self._workload_writer.shm_name
        if self._kv_usage_writer:
            data["kv_usage_shm_name"] = self._kv_usage_writer.shm_name
        if self._sched_metrics_writer:
            data["sched_metrics_shm_name"] = self._sched_metrics_writer.shm_name
        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data=data,
        )

    def _clear_instance_circuit_breaker(self, instance_id: int, shm_closed_ids: list[int]) -> None:
        """Drop Mgmt CB state for a removed instance and mirror CLOSED to SHM/workers (same as DEL)."""
        self._cb_manager.clear_instance(instance_id)
        self._cancel_recovery(instance_id)
        # PUB "closed" too — the worker-side prune is absence-based, so a reused id stays blocked.
        if self._set_blocked(instance_id, False):
            shm_closed_ids.append(instance_id)

    async def _handle_get_instance_status(self, request: SchedulerRequest) -> SchedulerResponse:
        data = await build_instance_status_response(self._instance_manager, self._cb_manager)
        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data=data,
        )

    async def apply_refresh(self, event_type: EventType, instances: list[Instance]) -> bool:
        """Apply an instance-list change locally: IM + SHM snapshot + PUB (no ZMQ REFRESH)."""
        shm_closed_ids: list[int] = []
        async with self._workload_commit_lock:
            changed, set_removed_ids = await self._instance_manager.refresh_instances(event_type, instances)
            if event_type == EventType.SET:
                for iid in set_removed_ids:
                    self._clear_instance_circuit_breaker(iid, shm_closed_ids)
            elif event_type == EventType.DEL:
                for inst in instances:
                    self._clear_instance_circuit_breaker(inst.id, shm_closed_ids)
            if (changed or self._snapshot_dirty) and self._workload_writer:
                self._snapshot_dirty = True  # cleared only after write_snapshot succeeds below
                self._workload_writer.write_snapshot()
                self._snapshot_dirty = False
        if changed:
            if self._on_instance_refresh_done:
                try:
                    result = self._on_instance_refresh_done(event_type, instances)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.warning("Failed to publish instance change: %s", e)
        for iid in shm_closed_ids:
            asyncio.create_task(self._publish_circuit_breaker(iid, "closed"))
        return changed

    def _set_blocked(self, instance_id: int, blocked: bool) -> bool:
        """Mirror circuit-breaker OPEN/CLOSED onto SHM BLOCKED flags (final CAS gate).

        Returns False on native failure instead of silently dropping it; the desired state is
        queued and retried from the heartbeat loop (_retry_pending_blocked). Callers must not
        PUB OPEN/CLOSED until this returns True — SHM BLOCKED is the allocate gate.
        """
        if not self._workload_writer:
            return True
        try:
            self._workload_writer.set_blocked(instance_id, blocked)
            self._pending_blocked.pop(instance_id, None)
            self._pending_blocked_pub.pop(instance_id, None)
            return True
        except Exception as e:
            logger.error(
                "Failed to set_blocked instance_id=%d blocked=%s, will retry: %s",
                instance_id,
                blocked,
                e,
            )
            self._pending_blocked[instance_id] = blocked
            self._pending_blocked_pub[instance_id] = "open" if blocked else "closed"
            return False

    def _retry_dirty_snapshot(self) -> None:
        """Retry a previously failed write_snapshot. Called every heartbeat tick."""
        if not self._snapshot_dirty or not self._workload_writer:
            return
        try:
            self._workload_writer.write_snapshot()
            self._snapshot_dirty = False
        except Exception as e:
            logger.debug("Retry write_snapshot still failing: %s", e)

    def _retry_pending_blocked(self) -> list[tuple[int, str]]:
        """Flush SHM BLOCKED flags that previously failed. Returns (instance_id, state) to PUB."""
        flushed: list[tuple[int, str]] = []
        if not self._pending_blocked or not self._workload_writer:
            return flushed
        for instance_id, blocked in list(self._pending_blocked.items()):
            try:
                self._workload_writer.set_blocked(instance_id, blocked)
                self._pending_blocked.pop(instance_id, None)
                state = self._pending_blocked_pub.pop(instance_id, "open" if blocked else "closed")
                flushed.append((instance_id, state))
            except Exception as e:
                logger.debug("Retry set_blocked instance_id=%d blocked=%s still failing: %s", instance_id, blocked, e)
        return flushed

    async def _retry_pending_blocked_and_publish(self) -> None:
        """Heartbeat drain: land SHM BLOCKED, then PUB so workers see the same transition."""
        for instance_id, state in self._retry_pending_blocked():
            await self._publish_circuit_breaker(instance_id, state)

    async def _handle_confirm_sample(self, request: SchedulerRequest) -> SchedulerResponse:
        """Cross-worker precision sampling admission (per D instance)."""
        data = request.data or {}
        d_instance_id = data.get("d_instance_id")
        now = data.get("now")
        interval_seconds = data.get("interval_seconds")
        if d_instance_id is None or now is None or interval_seconds is None:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error="Missing d_instance_id, now, or interval_seconds in request data",
            )
        try:
            now_f = float(now)
            interval_f = float(interval_seconds)
            d_id = int(d_instance_id)
        except (TypeError, ValueError) as e:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error=f"Invalid confirm_sample fields: {e}",
            )
        confirmed = await self._scheduler.claim_precision_sample(
            d_instance_id=d_id,
            now=now_f,
            interval_seconds=interval_f,
        )
        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data={"confirmed": confirmed},
        )

    async def _handle_record_precision_result(self, request: SchedulerRequest) -> SchedulerResponse:
        data = request.data or {}
        d_instance_id = data.get("d_instance_id")
        has_issue = data.get("has_issue")
        threshold = data.get("threshold")
        clear_threshold = data.get("clear_threshold")
        check_valid = data.get("check_valid")
        if (
            d_instance_id is None
            or has_issue is None
            or threshold is None
            or clear_threshold is None
            or check_valid is None
        ):
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error="Missing d_instance_id, has_issue, threshold, clear_threshold, or check_valid",
            )
        try:
            d_id = int(d_instance_id)
            threshold_i = int(threshold)
            clear_threshold_i = int(clear_threshold)
            has_issue_b = bool(has_issue)
            check_valid_b = bool(check_valid)
        except (TypeError, ValueError) as e:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error=f"Invalid record_precision_result fields: {e}",
            )
        p_raw = data.get("p_instance_id")
        p_id: int | None
        if p_raw is None:
            p_id = None
        else:
            try:
                p_id = int(p_raw)
            except (TypeError, ValueError):
                return SchedulerResponse(
                    response_type=SchedulerResponseType.ERROR,
                    request_id=request.request_id,
                    error="Invalid p_instance_id",
                )
        result = await self._scheduler.record_precision_result(
            p_instance_id=p_id,
            d_instance_id=d_id,
            has_issue=has_issue_b,
            threshold=threshold_i,
            clear_threshold=clear_threshold_i,
            check_valid=check_valid_b,
        )
        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data=result,
        )

    async def _handle_finish_precision_action(self, request: SchedulerRequest) -> SchedulerResponse:
        data = request.data or {}
        d_instance_id = data.get("d_instance_id")
        action_token = data.get("action_token")
        action_type = data.get("action_type", "raise")
        success = data.get("success", True)
        alarm_moi = data.get("alarm_moi")
        auto_recovery_cleared = data.get("auto_recovery_cleared", False)
        if d_instance_id is None or not action_token:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error="Missing d_instance_id or action_token in request data",
            )
        try:
            d_id = int(d_instance_id)
        except (TypeError, ValueError) as e:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error=f"Invalid finish_precision_action fields: {e}",
            )
        p_raw = data.get("p_instance_id")
        p_id: int | None
        if p_raw is None:
            p_id = None
        else:
            try:
                p_id = int(p_raw)
            except (TypeError, ValueError):
                return SchedulerResponse(
                    response_type=SchedulerResponseType.ERROR,
                    request_id=request.request_id,
                    error="Invalid p_instance_id",
                )
        ok = await self._scheduler.finish_precision_action(
            p_instance_id=p_id,
            d_instance_id=d_id,
            action_token=str(action_token),
            action_type=str(action_type),
            success=bool(success),
            alarm_moi=str(alarm_moi) if alarm_moi is not None else None,
            auto_recovery_cleared=bool(auto_recovery_cleared),
        )
        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data={"finished": ok},
        )

    async def _handle_dismiss_precision_alarm_state(self, request: SchedulerRequest) -> SchedulerResponse:
        data = request.data or {}
        d_instance_id = data.get("d_instance_id")
        if d_instance_id is None:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error="Missing d_instance_id in request data",
            )
        try:
            d_id = int(d_instance_id)
        except (TypeError, ValueError) as e:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error=f"Invalid dismiss_precision_alarm_state fields: {e}",
            )
        p_raw = data.get("p_instance_id")
        p_id: int | None
        if p_raw is None:
            p_id = None
        else:
            try:
                p_val = int(p_raw)
                p_id = p_val if p_val > 0 else None
            except (TypeError, ValueError):
                return SchedulerResponse(
                    response_type=SchedulerResponseType.ERROR,
                    request_id=request.request_id,
                    error="Invalid p_instance_id",
                )
        ok = await self._scheduler.dismiss_precision_alarm_state(
            p_instance_id=p_id,
            d_instance_id=d_id,
        )
        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data={"dismissed": ok},
        )

    async def _handle_circuit_breaker_report(self, request: SchedulerRequest) -> SchedulerResponse:
        instance_id = request.data.get("instance_id")
        event = request.data.get("event")

        if instance_id is None:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error="Missing instance_id in circuit breaker report",
            )
        instance_id = int(instance_id)

        if event == "failure":
            should_trip, timeout = self._cb_manager.process_failure(instance_id)
            if should_trip:
                shm_ok = self._set_blocked(instance_id, True)
                self._schedule_recovery(instance_id, timeout)
                if shm_ok:
                    await self._publish_circuit_breaker(instance_id, "open")
        elif event == "success":
            recovered = self._cb_manager.process_success(instance_id)
            if recovered:
                shm_ok = self._set_blocked(instance_id, False)
                self._cancel_recovery(instance_id)
                if shm_ok:
                    await self._publish_circuit_breaker(instance_id, "closed")
        else:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error=f"Unknown circuit breaker event: {event}",
            )

        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data={},
        )

    # ------------------------------------------------------------------
    # Circuit breaker helpers
    # ------------------------------------------------------------------

    def _schedule_recovery(self, instance_id: int, timeout: float) -> None:
        """Schedule an auto-recovery timer for a tripped instance."""
        key = instance_id
        if key in self._recovery_timers:
            self._recovery_timers[key].cancel()
        task = asyncio.create_task(self._auto_recover(instance_id, timeout))
        self._recovery_timers[key] = task

    async def _auto_recover(self, instance_id: int, timeout: float) -> None:
        """Recovery timer callback. Probes the instance, then re-closes its circuit."""
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            return

        if not await self._probe_instance(instance_id):
            # Still unreachable (or dropped by _probe_instance): extend the recovery timeout (exponential, capped at 300s) and retry.
            retry_timeout = self._cb_manager.process_probe_failure(instance_id)
            if retry_timeout is None:
                return
            self._schedule_recovery(instance_id, retry_timeout)
            return

        try:
            recovered = self._cb_manager.auto_recover(instance_id)
            if recovered:
                if self._set_blocked(instance_id, False):
                    await self._publish_circuit_breaker(instance_id, "closed")
        finally:
            # Only remove our own entry: a concurrent _schedule_recovery may have
            # already replaced _recovery_timers[instance_id] with a new task before
            # this finally block runs (race window inside _publish_circuit_breaker).
            if self._recovery_timers.get(instance_id) is asyncio.current_task():
                self._recovery_timers.pop(instance_id, None)

    async def _probe_instance(self, instance_id: int) -> bool:
        """Require every endpoint to answer HTTP 200 on /health before closing a circuit; an instance outside the available pool is not probed — its recovery is dropped (circuit cleared, workers notified "closed") instead."""
        instance = self._instance_manager.get_available_instances(None).get(instance_id)
        if instance is None:
            logger.warning(
                "CircuitBreaker probe: instance_id=%d not in available pool, dropping recovery",
                instance_id,
            )
            # Not schedulable: nothing to protect; _auto_recover stops on process_probe_failure() == None.
            self._cb_manager.clear_instance(instance_id)
            if self._set_blocked(instance_id, False):
                await self._publish_circuit_breaker(instance_id, "closed")
            return False
        endpoints = instance.get_all_endpoints()
        if not endpoints:
            logger.warning(
                "CircuitBreaker probe: instance_id=%d has no endpoint, keeping circuit open",
                instance_id,
            )
            return False
        results = await asyncio.gather(*(self._probe_endpoint(instance_id, endpoint) for endpoint in endpoints))
        return all(results)

    async def _probe_endpoint(self, instance_id: int, endpoint: Endpoint) -> bool:
        """Probe one endpoint: ``GET /health`` must answer HTTP 200 (per-phase timeout, safe to run concurrently)."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(endpoint.ip, endpoint.business_port),
                timeout=_RECOVERY_PROBE_TIMEOUT_SECS,
            )
            try:
                request = (
                    f"GET {_PROBE_HEALTH_PATH} HTTP/1.1\r\n"
                    f"Host: {endpoint.ip}:{endpoint.business_port}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                )
                writer.write(request.encode("ascii"))
                await writer.drain()
                status_line = await asyncio.wait_for(reader.readline(), timeout=_RECOVERY_PROBE_TIMEOUT_SECS)
                if not status_line:
                    logger.warning(
                        "CircuitBreaker probe empty response: instance_id=%d endpoint=%s:%s",
                        instance_id,
                        endpoint.ip,
                        endpoint.business_port,
                    )
                    return False
                status_code = status_line.split(b" ", 2)[1].decode("ascii", "replace")
                if status_code != "200":
                    logger.warning(
                        "CircuitBreaker probe rejected: instance_id=%d endpoint=%s:%s status=%s",
                        instance_id,
                        endpoint.ip,
                        endpoint.business_port,
                        status_code,
                    )
                    return False
                logger.info(
                    "CircuitBreaker probe ok: instance_id=%d endpoint=%s:%s",
                    instance_id,
                    endpoint.ip,
                    endpoint.business_port,
                )
                return True
            finally:
                writer.close()
        except Exception as e:  # OSError (no route/refused) or asyncio.TimeoutError
            logger.warning(
                "CircuitBreaker probe failed: instance_id=%d endpoint=%s:%s error=%s",
                instance_id,
                endpoint.ip,
                endpoint.business_port,
                e,
            )
            return False

    def _cancel_recovery(self, instance_id: int) -> None:
        """Cancel a pending recovery timer for an instance."""
        key = instance_id
        task = self._recovery_timers.pop(key, None)
        if task and not task.done():
            task.cancel()

    async def _publish_circuit_breaker(self, instance_id: int, state: str) -> None:
        """Publish circuit breaker state change to PUB subscribers."""
        if not self._pub_socket:
            return
        payload = {
            "instance_id": instance_id,
            "state": state,
        }
        try:
            await self._pub_socket.send_multipart([CIRCUIT_BREAKER_TOPIC, msgspec.msgpack.encode(payload)])
        except Exception as e:
            logger.warning(
                "Failed to publish circuit breaker change: instance_id=%d error=%s",
                instance_id,
                e,
            )


class _SchedulerFrontendTransport:
    """
    ZMQ ROUTER socket: bind, recv(client_id + payload_frames), lock-protected send, disconnect.
    """

    def __init__(self, context: zmq.asyncio.Context) -> None:
        self._context = context
        self._socket: zmq.asyncio.Socket | None = None
        self._send_lock = asyncio.Lock()

    async def bind(self, address: str) -> None:
        """Create ROUTER socket and bind."""
        self._socket = self._context.socket(zmq.ROUTER)
        # Make send() fail loudly (EHOSTUNREACH) when routing to a peer that is no longer in the
        # ROUTER table (e.g. worker DEALER disconnected mid-request), instead of silently dropping
        # the response -- silent drops hide lost control-plane replies from operators.
        self._socket.setsockopt(zmq.ROUTER_MANDATORY, 1)
        self._socket.bind(address)

    async def recv(self) -> tuple[bytes | None, list]:
        """Receive one request; return (client_id, payload_frames). Return (None, []) if format invalid."""
        if not self._socket:
            return (None, [])
        parts = await self._socket.recv_multipart()
        if len(parts) < 3:
            logger.warning("Invalid frontend message format: %d parts", len(parts))
            return (None, [])
        return (parts[0], parts[2:])

    async def send(self, client_id: bytes, response_frames: list) -> None:
        """Send response (lock-protected, concurrent-safe)."""
        if not self._socket:
            return
        send_frames = pack_send_frames([client_id, b""], response_frames)
        async with self._send_lock:
            try:
                await self._socket.send_multipart(send_frames)
            except zmq.ZMQError as e:
                # ROUTER_MANDATORY: a peer that left the ROUTER table (worker DEALER
                # disconnected mid-request) raises instead of silently dropping the
                # response. Log and move on -- the client retries at its own layer.
                logger.warning(
                    "Dropped response to client %s: zmq errno=%s (%s)",
                    client_id,
                    e.errno,
                    e,
                )

    async def disconnect(self) -> None:
        """Close socket; do not term context (Server owns context)."""
        if self._socket:
            try:
                self._socket.close()
            except Exception as e:
                logger.warning("Error closing frontend socket: %s", e)
            self._socket = None


class AsyncSchedulerServer:
    """
    Control plane (ZMQ ROUTER + PUB + schema-4 SHM). Owned by Mgmt; Infer Workers
    attach to the same IPC addresses and SHM name advertised via GET_AVAILABLE_INSTANCES.
    """

    def __init__(
        self,
        config: CoordinatorConfig,
        frontend_address: str = "ipc:///tmp/scheduler_frontend",
        *,
        instance_manager: InstanceManager | None = None,
    ):
        """
        Args:
            config: Coordinator config
            frontend_address: ROUTER bind address (Worker/Obs DEALER)
            instance_manager: Injected Mgmt InstanceManager (TYPE_MGMT). If omitted,
                a new InstanceManager is created (tests / standalone).
        """
        self.config = config
        self.frontend_address = frontend_address

        self.instance_manager = instance_manager if instance_manager is not None else InstanceManager(config)
        self.scheduler = Scheduler(instance_provider=self.instance_manager, config=config)
        # 与 _SchedulerRequestDispatcher 保持一致（快照调度指标时计算 fresh_load 用）。
        self._endpoint_instance_score_weight = max(
            0.0,
            getattr(config.scheduler_config, "endpoint_instance_score_weight", 0.05),
        )

        self.context: zmq.asyncio.Context | None = None
        self._transport: _SchedulerFrontendTransport | None = None

        self._active_tasks: set[asyncio.Task] = set()
        self._stop_event = asyncio.Event()

        from motor.coordinator.scheduler.runtime.zmq_protocol import (
            ZMQMessageSerializer,
        )

        self._serializer = ZMQMessageSerializer()
        self._encode_lock = asyncio.Lock()
        self._decode_lock = asyncio.Lock()

        self._dispatch_timeout = 5.0

        self._dispatcher: _SchedulerRequestDispatcher | None = None
        self._workload_writer: WorkloadSharedMemoryOwner | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._pub_socket: zmq.asyncio.Socket | None = None
        self._cb_manager: CircuitBreakerManager | None = None
        self._loop_task: asyncio.Task | None = None
        self._kv_usage_reader: KvUsageShmReader | None = None
        self._kv_usage_writer: KvUsageShmWriter | None = None
        self._kv_usage_shm = None
        self._sched_metrics_collector: SchedulerMetricsCollector | None = None
        self._sched_metrics_writer: SchedulerMetricsShmWriter | None = None
        self._sched_metrics_shm = None

    async def apply_refresh(self, event_type: EventType, instances: list[Instance]) -> bool:
        """In-process instance refresh (HTTP /instances/refresh). Returns whether membership changed."""
        if self._dispatcher is None:
            logger.warning("apply_refresh called before control plane started")
            return False
        return await self._dispatcher.apply_refresh(event_type, instances)

    @property
    def circuit_breaker_manager(self) -> CircuitBreakerManager | None:
        """Mgmt-owned circuit breaker pool; None before control plane start."""
        return self._cb_manager

    async def stop(self):
        """Stop the control plane (ROUTER, PUB, SHM, heartbeat, probe timers)."""
        logger.info("Stopping control plane...")

        self._stop_event.set()

        if self._loop_task and not self._loop_task.done() and self._loop_task is not asyncio.current_task():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

        if self._kv_usage_reader is not None:
            set_kv_usage_reader(None)
            try:
                self._kv_usage_reader.detach()
            except Exception as e:
                logger.warning("Failed to detach kv usage reader: %s", e)
            self._kv_usage_reader = None
        if self._kv_usage_writer is not None:
            self._kv_usage_writer.release()
            self._kv_usage_writer = None
        if self._kv_usage_shm:
            try:
                self._kv_usage_shm.close()
                self._kv_usage_shm.unlink()
            except Exception as e:
                logger.warning("Error closing kv usage shared memory: %s", e)
            self._kv_usage_shm = None

        if self._sched_metrics_collector is not None:
            try:
                self._sched_metrics_collector.stop()
            except Exception as e:
                logger.warning("Failed to stop sched metrics collector: %s", e)
            self._sched_metrics_collector = None
        if self._sched_metrics_writer is not None:
            self._sched_metrics_writer.release()
            self._sched_metrics_writer = None
        if self._sched_metrics_shm:
            try:
                self._sched_metrics_shm.close()
                self._sched_metrics_shm.unlink()
            except Exception as e:
                logger.warning("Error closing sched metrics shared memory: %s", e)
            self._sched_metrics_shm = None

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._active_tasks:
            logger.info(
                "Waiting for %s active request tasks to complete...",
                len(self._active_tasks),
            )
            for task in self._active_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
            self._active_tasks.clear()

        if self._workload_writer:
            try:
                self._workload_writer.release()
            except Exception as e:
                logger.warning("Error releasing workload writer: %s", e)
            self._workload_writer = None
        if self._dispatcher is not None:
            for task in list(self._dispatcher._recovery_timers.values()):
                if not task.done():
                    task.cancel()
            self._dispatcher._recovery_timers.clear()
        if self._pub_socket:
            try:
                self._pub_socket.close()
            except Exception as e:
                logger.warning("Error closing instance PUB socket: %s", e)
            self._pub_socket = None
        if self._cb_manager:
            count = self._cb_manager.clear_all()
            if count:
                logger.info("Circuit breaker pool cleared on shutdown: count=%d", count)
        if self._transport:
            await self._transport.disconnect()
        if self.context:
            try:
                self.context.term()
            except Exception as e:
                logger.warning("Error terminating context: %s", e)
            self.context = None

        logger.info("Control plane stopped")

    async def start_control_plane(self) -> None:
        """Bind ROUTER+PUB, create schema-4 SHM, start heartbeat and dispatcher. Does not recv-loop."""
        self._stop_event = asyncio.Event()

        self.context = zmq.asyncio.Context()
        self._transport = _SchedulerFrontendTransport(self.context)
        await self._transport.bind(self.frontend_address)

        from motor.config.coordinator import DEFAULT_SCHEDULER_PROCESS_CONFIG

        instance_pub_address = DEFAULT_SCHEDULER_PROCESS_CONFIG.instance_pub_address
        if instance_pub_address:
            self._pub_socket = self.context.socket(zmq.PUB)
            self._pub_socket.bind(instance_pub_address)
            logger.info("Instance change PUB bound: %s", instance_pub_address)

        shm_name = f"mindie_workload_{os.getpid()}"
        self._workload_writer = WorkloadSharedMemoryOwner(
            self.instance_manager,
            max_entries=DEFAULT_WORKLOAD_SHM_MAX_ENTRIES,
            shm_name=shm_name,
        )
        self._workload_writer.write_snapshot()
        logger.info("Workload shared memory enabled: %s", shm_name)

        # KV usage shared memory: MetricsCollector (Obs) writes per-endpoint usage,
        # SchedulingServer reads and serves to Infer Workers via GET_AVAILABLE_INSTANCES.
        kv_usage_shm_name = f"mindie_kvusage_{os.getpid()}"
        self._kv_usage_shm = _create_kv_usage_shared_memory(
            shared_memory,
            kv_usage_shm_name,
            kv_usage_shm_total_size(DEFAULT_KV_USAGE_SHM_MAX_ENTRIES),
        )
        self._kv_usage_writer = KvUsageShmWriter(
            self._kv_usage_shm,
            max_entries=DEFAULT_KV_USAGE_SHM_MAX_ENTRIES,
        )
        logger.info("KV usage shared memory enabled: %s", kv_usage_shm_name)
        self._kv_usage_reader = KvUsageShmReader(kv_usage_shm_name)
        try:
            self._kv_usage_reader.attach()
        except FileNotFoundError:
            logger.warning(
                "KV usage shm %s not attachable; kv usage exclusion disabled until Obs publishes",
                kv_usage_shm_name,
            )
            self._kv_usage_reader = None
        else:
            # 注册模块级 reader：get_endpoint_kv_cache_usage 按需从共享内存拉新快照
            # （MetricsCollector 每轮采集后推送），调度与指标路径才有 usage 数据可用。
            set_kv_usage_reader(self._kv_usage_reader)

        # 调度指标（cnt/fresh_load/a_tokens）由本进程采集并发布到共享内存，
        # 供 Obs 进程的 MetricsCollector 读取并输出为 motor: 指标。
        sched_metrics_shm_name = f"mindie_schedmetrics_{os.getpid()}"
        self._sched_metrics_shm = _create_sched_metrics_shared_memory(
            shared_memory,
            sched_metrics_shm_name,
            sched_metrics_shm_total_size(DEFAULT_SCHED_METRICS_SHM_MAX_ENTRIES),
        )
        self._sched_metrics_writer = SchedulerMetricsShmWriter(
            self._sched_metrics_shm,
            max_entries=DEFAULT_SCHED_METRICS_SHM_MAX_ENTRIES,
        )
        logger.info("Scheduling metrics shared memory enabled: %s", sched_metrics_shm_name)

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        self._cb_manager = CircuitBreakerManager(self.config.circuit_config)

        self._dispatcher = _SchedulerRequestDispatcher(
            self.instance_manager,
            self.scheduler,
            self.config,
            workload_writer=self._workload_writer,
            on_instance_refresh_done=self._publish_instance_changed,
            circuit_breaker_manager=self._cb_manager,
            pub_socket=self._pub_socket,
            kv_usage_writer=self._kv_usage_writer,
            sched_metrics_writer=self._sched_metrics_writer,
        )
        # 后台定期采集调度指标（cnt/fresh_load/a_tokens）写入共享内存。
        self._sched_metrics_collector = SchedulerMetricsCollector(
            snapshot_fn=self._snapshot_sched_metrics,
            on_publish=self._sched_metrics_writer.write_snapshot,
        )
        self._sched_metrics_collector.start()

        logger.info("Control plane started, frontend: %s", self.frontend_address)

    async def run_request_loop(self) -> None:
        """Serve DEALER requests until stop(). Used by Mgmt lifespan as a background task."""
        await self._run_async_loop()

    async def start(self):
        """Standalone entry: start control plane then block on the recv loop."""
        await self.start_control_plane()
        try:
            await self._run_async_loop()
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        finally:
            await self.stop()

    def _snapshot_sched_metrics(self) -> dict[tuple[int, int], tuple[int, float, float, int, int]]:
        """Snapshot per-endpoint scheduling metrics for the Obs metrics publisher.

        For every available instance's endpoint, capture:
          - role byte (layout encoding)
          - fresh_load: authoritative load-balance score (calculate_endpoint_score)
          - a_tokens: endpoint's current active tokens
          - cnt: per-endpoint in-flight request count (maintained by Infer Workers in SHM)
          - total_cnt: cumulative allocation count (workers increment on allocate, never
            decremented; sourced from the workload SHM ``total_requests`` field)
        Runs on the background SchedulerMetricsCollector thread (off the hot path).

        ``active_tokens`` is read from the workload SHM (Infer Workers own the per-slot
        counter via CAS); Mgmt's ``endpoint.workload`` ledger is never updated from SHM,
        so reading it directly would always yield 0. ``fresh_load`` is computed from the
        same live tokens to stay consistent with the worker-side scheduling score.
        ``cnt`` is the per-endpoint in-flight request count, also maintained by workers
        in the SHM (increment on allocate, decrement on release).
        """
        snapshot: dict[tuple[int, int], tuple[int, float, float, int, int]] = {}
        score_weight = self._endpoint_instance_score_weight

        # 从 workload SHM 读取各 endpoint 的实时 (active_tokens, request_count, total_requests)。
        # Infer Worker CAS 更新 token / 请求数，Mgmt 侧 endpoint.workload 不会被更新，
        # 必须从 SHM 拿真实值，否则 a_tokens/fresh_load/cnt/total_cnt 永远为 0。
        shm_workload: dict[tuple[int, int], tuple[float, int, int]] = {}
        if self._workload_writer is not None:
            shm_workload = self._workload_writer.read_endpoint_workload()
        shm_tokens = {pair: tokens for pair, (tokens, _count, _total) in shm_workload.items()}

        for role in (PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_U):
            instances = self.instance_manager.get_available_instances(role).values()
            for instance in instances:
                try:
                    instance_role = PDRole(instance.role)
                except ValueError:
                    instance_role = PDRole.ROLE_U
                endpoints = instance.get_all_endpoints()
                # instance_score = 该实例所有 endpoint 的 SHM active_tokens 之和，
                # 与 LoadBalancePolicy.calculate_endpoint_score 中 gathered_workload 语义一致。
                instance_token_sum = sum(
                    shm_tokens.get((instance.id, ep.id), ep.workload.active_tokens if ep.workload else 0.0)
                    for ep in endpoints
                )
                for endpoint in endpoints:
                    pair = (instance.id, endpoint.id)
                    # 优先用 SHM 实时值，回退到 endpoint.workload（通常为 0，覆盖刚注册尚未写入 SHM 的边界）。
                    active_tokens = shm_tokens.get(pair, endpoint.workload.active_tokens if endpoint.workload else 0.0)
                    # cnt = 该 endpoint 上正在处理的请求数（Infer Worker 维护，释放后递减）。
                    # total_cnt = 该 endpoint 累计分配的请求数（Infer Worker 维护，只增不减）。
                    default_cnt = (0.0, 0, 0)
                    _, cnt, total_cnt = shm_workload.get(pair, default_cnt)
                    try:
                        # 统一走 LoadBalancePolicy.calculate_endpoint_score，显式传入 SHM 实时值
                        # 覆盖 stale 的 workload 缓存，避免复刻公式在计分口径变化时静默漂移。
                        fresh_load = LoadBalancePolicy.calculate_endpoint_score(
                            instance,
                            endpoint,
                            role=instance_role,
                            instance_score_weight=score_weight,
                            endpoint_score=active_tokens,
                            instance_score=instance_token_sum,
                        )
                        if instance_role == PDRole.ROLE_D:
                            # 与真实调度路径 select_endpoint_candidates_from_list 保持一致：
                            # D 实例评分叠加 KV usage 惩罚项，指标里的 fresh_load 才是调度真正使用的分。
                            # Mgmt 侧 gathered_workload 不会被更新，显式传入 SHM 实时实例 token 和。
                            fresh_load = LoadBalancePolicy._apply_kv_usage_penalty(
                                fresh_load,
                                instance,
                                endpoint,
                                role=instance_role,
                                kv_usage_provider=get_endpoint_kv_cache_usage,
                                instance_score=instance_token_sum,
                            )
                    except Exception as e:
                        logger.warning(
                            "Failed to score sched-metrics candidate instance_id=%s endpoint_id=%s: %s",
                            instance.id,
                            endpoint.id,
                            e,
                        )
                        fresh_load = 0.0
                    # total_cnt 来自 workload SHM 的累计分配计数（Worker cas_add 维护）。
                    snapshot[pair] = (
                        _pack_role(instance_role),
                        fresh_load,
                        active_tokens,
                        cnt,
                        total_cnt,
                    )
        return snapshot

    async def _publish_instance_changed(self, event_type=None, instances=None) -> None:
        """Publish instance list changed + version to SUB clients (no-op if PUB not enabled).

        For ADD/DEL a third msgpack frame carries the changed instances so workers patch their cache
        incrementally instead of each doing a full GET; other events (SET/PAUSE/RESUME) omit it and
        workers fall back to a full pull. The frame is additive -- older workers ignore it.
        """
        if not self._pub_socket:
            return
        version = self._workload_writer.instance_version if self._workload_writer else 0
        frames: list[bytes] = [INSTANCE_CHANGE_TOPIC, str(version).encode()]
        delta = self._build_instance_delta(event_type, instances)
        if delta is not None:
            frames.append(msgspec.msgpack.encode(delta))
        try:
            await self._pub_socket.send_multipart(frames)
        except Exception as e:
            logger.warning("Failed to publish instance change: %s", e)

    @staticmethod
    def _build_instance_delta(event_type, instances):
        """Build the incremental PUB delta for ADD/DEL; None for events workers don't patch (SET/…)."""
        if event_type not in (EventType.ADD, EventType.DEL) or not instances:
            return None
        return {
            "event": "add" if event_type == EventType.ADD else "del",
            "instances": [_instance_to_dict(inst) for inst in instances],
        }

    async def _heartbeat_loop(self) -> None:
        """Write heartbeat to shm every 1s so Infer can detect a dead Mgmt control plane (stale = no change)."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(1.0)
                if self._stop_event.is_set() or not self._workload_writer:
                    break
                self._workload_writer.write_heartbeat()
                if self._dispatcher is not None:
                    await self._dispatcher._retry_pending_blocked_and_publish()
                    self._dispatcher._retry_dirty_snapshot()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Workload heartbeat error: %s", e)

    async def _run_async_loop(self):
        """Async main loop: handle all requests concurrently; main loop never blocks."""
        logger.info("Control-plane request loop started")

        while not self._stop_event.is_set():
            try:
                client_id, payload_frames = await self._transport.recv()
                if client_id is None:
                    continue
                task = asyncio.create_task(self._handle_request_async(client_id, payload_frames, self._serializer))
                # Track tasks to avoid leaks
                self._active_tasks.add(task)
                task.add_done_callback(self._active_tasks.discard)

            except asyncio.CancelledError:
                logger.info("Main loop cancelled")
                break
            except Exception as e:
                logger.error("Error in main loop: %s", e, exc_info=True)
                # Brief sleep then continue
                await asyncio.sleep(0.01)

    async def _handle_request_async(self, client_id: bytes, payload_frames: list, ser):
        """Handle a single request asynchronously (does not block main loop)."""
        serializer = ser or self._serializer
        request = None
        handle_start = time.time()

        try:
            payload = unpack_recv_payload([b"", b""] + payload_frames, payload_start=2)
            async with self._decode_lock:
                request = serializer.deserialize_request(payload)

            log_req_id = (request.data or {}).get(REQUEST_ID_KEY) or request.request_id
            logger.debug(
                "Control-plane request received request_type=%s req_id=%s",
                request.request_type,
                log_req_id,
            )

            response = await asyncio.wait_for(
                self._dispatcher.dispatch(request),
                timeout=self._dispatch_timeout,
            )

            async with self._encode_lock:
                response_frames = serializer.serialize_response(response)
            await self._transport.send(client_id, response_frames)

            elapsed_ms = (time.time() - handle_start) * 1000
            logger.debug(
                "Control-plane request done request_type=%s req_id=%s elapsed_ms=%.1f",
                request.request_type,
                log_req_id,
                elapsed_ms,
            )

        except asyncio.CancelledError:
            logger.debug("Request handling cancelled")
        except asyncio.TimeoutError:
            elapsed_ms = (time.time() - handle_start) * 1000
            req_data = getattr(request, "data", None) or {}
            _log_req_id = req_data.get(REQUEST_ID_KEY) or getattr(request, "request_id", DEFAULT_REQUEST_ID)
            logger.warning(
                "Dispatch request timeout request_type=%s req_id=%s elapsed_ms=%.1f",
                getattr(request, "request_type", DEFAULT_REQUEST_ID),
                _log_req_id,
                elapsed_ms,
            )
            try:
                error_response = SchedulerResponse(
                    response_type=SchedulerResponseType.ERROR,
                    request_id=request.request_id if request else DEFAULT_REQUEST_ID,
                    error="dispatch timeout",
                )
                async with self._encode_lock:
                    error_frames = serializer.serialize_response(error_response)
                await self._transport.send(client_id, error_frames)
            except Exception as e2:
                logger.error("Error sending timeout response: %s", e2, exc_info=True)
        except Exception as e:
            elapsed_ms = (time.time() - handle_start) * 1000
            req_data = getattr(request, "data", None) or {}
            _log_req_id = req_data.get(REQUEST_ID_KEY) or getattr(request, "request_id", DEFAULT_REQUEST_ID)
            logger.error(
                "Error handling request request_type=%s req_id=%s elapsed_ms=%.1f error=%s",
                getattr(request, "request_type", DEFAULT_REQUEST_ID),
                _log_req_id,
                elapsed_ms,
                e,
                exc_info=True,
            )
            try:
                error_response = SchedulerResponse(
                    response_type=SchedulerResponseType.ERROR,
                    request_id=request.request_id if request else DEFAULT_REQUEST_ID,
                    error=str(e),
                )
                async with self._encode_lock:
                    error_frames = serializer.serialize_response(error_response)
                await self._transport.send(client_id, error_frames)
            except Exception as e2:
                logger.error("Error sending error response: %s", e2, exc_info=True)
