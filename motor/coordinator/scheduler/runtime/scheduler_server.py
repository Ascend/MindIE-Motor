# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Async Scheduler standalone process server.
Uses zmq.asyncio for fully async ZMQ I/O and avoids main-loop serialization bottlenecks.
"""

import asyncio
import os
import time
from typing import Awaitable, Callable

import zmq.asyncio

from motor.common.resources.endpoint import Endpoint, WorkloadAction, Workload
from motor.common.resources.http_msg_spec import EventType
from motor.common.resources.instance import PDRole, Instance
from motor.common.logger import get_logger
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain import (
    UpdateWorkloadParams,
)
from motor.coordinator.models.constants import DEFAULT_REQUEST_ID, REQUEST_ID_KEY
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.scheduler.scheduler import Scheduler
from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy
from motor.coordinator.scheduler.runtime.workload_shm import WorkloadSharedMemoryWriter
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    DEFAULT_WORKLOAD_SHM_MAX_ENTRIES,
)
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    SchedulerRequest,
    SchedulerResponse,
    SchedulerRequestType,
    SchedulerResponseType,
    CANDIDATE_POLICY_LOAD_BALANCE,
    CANDIDATE_POLICY_KV_CACHE_AFFINITY,
    KNOWN_CANDIDATE_POLICIES,
    INSTANCE_CHANGE_TOPIC,
    pack_send_frames,
    unpack_recv_payload,
)

logger = get_logger(__name__)


def _create_workload_shared_memory(shared_memory_mod, shm_name: str, shm_size: int):
    """Create POSIX workload SharedMemory; recover from orphan segment (unclean exit / PID reuse).

    ``mindie_workload_<pid>`` can remain after SIGKILL/OOM; a new process with the same PID then
    hits FileExistsError on create=True. Unlink the stale name and recreate.
    """
    try:
        return shared_memory_mod.SharedMemory(name=shm_name, create=True, size=shm_size)
    except FileExistsError:
        logger.warning(
            "Workload SHM %s already exists (likely orphan from a prior run or PID reuse); unlinking and recreating",
            shm_name,
        )
        try:
            stale = shared_memory_mod.SharedMemory(name=shm_name, create=False)
        except FileNotFoundError:
            return shared_memory_mod.SharedMemory(name=shm_name, create=True, size=shm_size)
        try:
            stale.close()
            stale.unlink()
        except Exception as e:
            logger.error("Failed to unlink stale workload SHM %s: %s", shm_name, e)
            raise
        return shared_memory_mod.SharedMemory(name=shm_name, create=True, size=shm_size)


# Hot-path scheduling log sampling: ~1% of requests to reduce I/O and CPU at high QPS
_SCHEDULING_LOG_SAMPLE_RATE = 100

# Display string for unknown/hybrid role in logs
_ROLE_DISPLAY_HYBRID = "hybrid"

# Response data keys for allocate_only / select_and_allocate (avoid duplicate string literals)
_KEY_INSTANCE = "instance"
_KEY_ENDPOINT = "endpoint"
_KEY_SELECTED_SCORE = "selected_score"
_KEY_WORKLOAD_SEQUENCE = "workload_sequence"
_KEY_INSTANCE_VERSION = "instance_version"
_KEY_FAST_PATH = "fast_path"
_KEY_CANDIDATE_POLICY = "candidate_policy"
_KEY_CANDIDATES = "candidates"
# kv_cache_affinity unified global selection: worker sends per-candidate affinity prefill cost
# plus the two scalars so the scheduler recomputes prefill_load_scale*prefill_cost + load_weight*load.
_KEY_PREFILL_COST = "prefill_cost"
_KEY_LOAD_WEIGHT = "load_weight"
_KEY_PREFILL_LOAD_SCALE = "prefill_load_scale"


def _should_log_scheduling_sample(sample_key: str) -> bool:
    """Return True for ~1/_SCHEDULING_LOG_SAMPLE_RATE of requests (hot-path info sampling)."""
    return bool(sample_key) and hash(sample_key) % _SCHEDULING_LOG_SAMPLE_RATE == 0


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


def _serialize_instance_minimal(instance: Instance | None) -> dict:
    """Serialize minimal fields for select/allocate result (forward and release); reduce ZMQ payload."""
    if instance is None:
        return {}
    return {
        "id": instance.id,
        "role": instance.role,
        "job_name": instance.job_name,
        "model_name": instance.model_name,
        "engine_type": instance.engine_type,
        "dispatch_capabilities": list(instance.dispatch_capabilities or []),
    }


def _serialize_endpoint_minimal(endpoint: Endpoint | None) -> dict:
    """Serialize minimal fields for select/allocate result (forward and release)."""
    if endpoint is None:
        return {}
    out = {
        "id": endpoint.id,
        "ip": endpoint.ip,
        "business_port": endpoint.business_port,
        "mgmt_port": getattr(endpoint, "mgmt_port", "") or "",
    }
    if hasattr(endpoint, "status") and endpoint.status is not None:
        out["status"] = endpoint.status.value if hasattr(endpoint.status, "value") else str(endpoint.status)
    return out


# ==================== Request dispatch ====================


class _SchedulerRequestDispatcher:
    """
    Route by request_type to handlers; holds instance_manager, scheduler, config and callbacks.
    """

    def __init__(
        self,
        instance_manager: InstanceManager,
        scheduler: Scheduler,
        config: CoordinatorConfig,
        workload_writer: WorkloadSharedMemoryWriter | None = None,
        on_instance_refresh_done: Callable[[], None | Awaitable[None]] | None = None,
    ):
        self._instance_manager = instance_manager
        self._scheduler = scheduler
        self._config = config
        self._workload_writer = workload_writer
        self._on_instance_refresh_done = on_instance_refresh_done
        self._workload_commit_lock = asyncio.Lock()
        self._endpoint_instance_score_weight = max(
            0.0,
            getattr(config.scheduler_config, "endpoint_instance_score_weight", 0.05),
        )
        scheduler_type = getattr(config.scheduler_config, "scheduler_type", "")
        self._is_load_balance_scheduler = getattr(scheduler_type, "value", scheduler_type) == "load_balance"

    async def dispatch(self, request: SchedulerRequest) -> SchedulerResponse:
        """Dispatch request to the appropriate handler (async handlers supported)."""
        # Scheduler process uses its local InstanceManager for read-only; only Workers use GET_AVAILABLE_INSTANCES here.
        handlers = {
            SchedulerRequestType.UPDATE_WORKLOAD.value: self._handle_update_workload,
            SchedulerRequestType.GET_AVAILABLE_INSTANCES.value: self._handle_get_available_instances,
            SchedulerRequestType.REFRESH_INSTANCES.value: self._handle_refresh_instances,
            SchedulerRequestType.ALLOCATE_ONLY.value: self._handle_allocate_only,
            SchedulerRequestType.CONFIRM_SAMPLE.value: self._handle_confirm_sample,
            SchedulerRequestType.RECORD_PRECISION_RESULT.value: self._handle_record_precision_result,
            SchedulerRequestType.FINISH_PRECISION_ACTION.value: self._handle_finish_precision_action,
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

    async def _handle_update_workload(self, request: SchedulerRequest) -> SchedulerResponse:
        instance_id = request.data.get("instance_id")
        endpoint_id = request.data.get("endpoint_id")
        role_str = request.data.get("role")
        req_id = request.data.get("req_id")
        workload_action_str = request.data.get("workload_action")
        workload_change_data = request.data.get("workload_change")

        if instance_id is None or endpoint_id is None:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error="Missing instance_id or endpoint_id in request data",
            )
        if not workload_change_data:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error="Missing workload_change in request data",
            )
        try:
            workload_change = Workload.model_validate(workload_change_data)
        except Exception as e:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error=f"Invalid workload_change format: {e}",
            )
        workload_action = WorkloadAction(workload_action_str)
        role = PDRole(role_str) if role_str else PDRole.ROLE_U
        params = UpdateWorkloadParams(
            instance_id=int(instance_id),
            endpoint_id=int(endpoint_id),
            role=role,
            req_id=req_id or "",
            workload_action=workload_action,
            workload_change=workload_change,
        )
        async with self._workload_commit_lock:
            success = await self._scheduler.update_workload(params)
            if success and self._workload_writer:
                await self._workload_writer.write_single_entry(int(instance_id), int(endpoint_id))
        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data={"success": success},
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
        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data=data,
        )

    async def _handle_refresh_instances(self, request: SchedulerRequest) -> SchedulerResponse:
        event_type_str = request.data.get("event_type")
        instances_data = request.data.get("instances", [])
        event_type = EventType(event_type_str) if event_type_str else None
        instances = [_instance_from_dict(d) for d in instances_data]
        instances = [inst for inst in instances if inst is not None]
        if not event_type:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error=f"Invalid event type: {event_type_str}",
            )
        async with self._workload_commit_lock:
            changed = await self._instance_manager.refresh_instances(event_type, instances)
            if changed and self._workload_writer:
                self._workload_writer.write_snapshot()
        if changed:
            if self._on_instance_refresh_done:
                try:
                    result = self._on_instance_refresh_done()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.warning("Failed to publish instance change: %s", e)
        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data={
                "message": f"Refreshed {len(instances)} instances",
                "changed": changed,
            },
        )

    async def _handle_confirm_sample(self, request: SchedulerRequest) -> SchedulerResponse:
        """Cross-worker precision sampling exit gate (per PD group, interval in request data)."""
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
        confirmed = await self._scheduler.confirm_sample_exit(
            p_instance_id=p_id,
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
        if d_instance_id is None or has_issue is None or threshold is None:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error="Missing d_instance_id, has_issue, or threshold in request data",
            )
        try:
            d_id = int(d_instance_id)
            threshold_i = int(threshold)
            has_issue_b = bool(has_issue)
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
        )
        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data={"finished": ok},
        )

    async def _handle_allocate_only(self, request: SchedulerRequest) -> SchedulerResponse:
        """
        Worker proposes one endpoint; Scheduler authoritatively commits one workload allocation.
        """
        instance_id = request.data.get("instance_id")
        endpoint_id = request.data.get("endpoint_id")
        req_id = request.data.get("req_id", "")
        workload_data = request.data.get("workload")
        role_str = request.data.get("role")
        worker_workload_sequence = self._parse_optional_int(request.data.get(_KEY_WORKLOAD_SEQUENCE))
        worker_instance_version = self._parse_optional_int(request.data.get(_KEY_INSTANCE_VERSION))
        candidate_policy = request.data.get(_KEY_CANDIDATE_POLICY)
        worker_load_weight = self._parse_optional_float(request.data.get(_KEY_LOAD_WEIGHT))
        worker_prefill_load_scale = self._parse_optional_float(request.data.get(_KEY_PREFILL_LOAD_SCALE))

        if instance_id is None or endpoint_id is None:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error="Missing instance_id or endpoint_id in request data",
            )
        if not workload_data:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error="Missing workload in request data",
            )
        try:
            workload = Workload.model_validate(workload_data)
        except Exception as e:
            return SchedulerResponse(
                response_type=SchedulerResponseType.ERROR,
                request_id=request.request_id,
                error=f"Invalid workload format: {e}",
            )
        role = PDRole(role_str) if role_str in ("encode", "prefill", "decode", "union", "both") else PDRole.ROLE_U
        selected_candidate = self._extract_allocate_candidate(request.data)
        if selected_candidate is None:
            logger.warning(
                "ALLOCATE_ONLY has no valid endpoint req_id=%s instance_id=%s endpoint_id=%s",
                req_id,
                instance_id,
                endpoint_id,
            )
            return SchedulerResponse(
                response_type=SchedulerResponseType.SUCCESS,
                request_id=request.request_id,
                data={_KEY_INSTANCE: None, _KEY_ENDPOINT: None},
            )
        # Worker-proposed alternates (affinity-ranked, best-first); the authoritative path may
        # re-pick among them by fresh load. Falls back to the single top-1 for legacy callers.
        selected_candidates = self._extract_allocate_candidates(request.data) or [selected_candidate]
        # kv_cache_affinity unified mode: every endpoint with its affinity-discounted prefill cost,
        # for a global re-rank by the scheduler's fresh load. Empty for other policies/modes.
        affinity_candidates = self._extract_affinity_candidates(request.data)
        async with self._workload_commit_lock:
            fast_path = self._can_use_worker_top1_fast_path(
                worker_workload_sequence,
                worker_instance_version,
            )
            selected = (
                self._select_valid_candidate(selected_candidate, role)
                if fast_path
                else self._select_authoritative_allocate_candidate(
                    selected_candidate,
                    selected_candidates,
                    role,
                    candidate_policy,
                    affinity_candidates,
                    worker_prefill_load_scale,
                    worker_load_weight,
                )
            )
            if fast_path and selected is None:
                selected = self._select_authoritative_allocate_candidate(
                    selected_candidate,
                    selected_candidates,
                    role,
                    candidate_policy,
                    affinity_candidates,
                    worker_prefill_load_scale,
                    worker_load_weight,
                )
                fast_path = False
            if selected is None:
                logger.warning(
                    "ALLOCATE_ONLY endpoint unavailable req_id=%s candidate=%s",
                    req_id,
                    selected_candidate,
                )
                return SchedulerResponse(
                    response_type=SchedulerResponseType.SUCCESS,
                    request_id=request.request_id,
                    data={_KEY_INSTANCE: None, _KEY_ENDPOINT: None},
                )
            instance, endpoint, selected_score = selected
            params = UpdateWorkloadParams(
                instance_id=instance.id,
                endpoint_id=endpoint.id,
                role=role,
                req_id=req_id,
                workload_action=WorkloadAction.ALLOCATION,
                workload_change=workload,
            )
            success = await self._scheduler.update_workload(params)
            if success and self._workload_writer:
                await self._workload_writer.write_single_entry(instance.id, endpoint.id)

        if not success:
            return SchedulerResponse(
                response_type=SchedulerResponseType.SUCCESS,
                request_id=request.request_id,
                data={_KEY_INSTANCE: None, _KEY_ENDPOINT: None},
            )
        instance_data = _serialize_instance_minimal(instance) if instance else None
        endpoint_data = _serialize_endpoint_minimal(endpoint) if endpoint else None
        if _should_log_scheduling_sample(req_id or request.request_id):
            logger.info(
                "ALLOCATE_ONLY req_id=%s ins=%s ep=%s score=%.4f fast_path=%s",
                req_id,
                instance.id,
                endpoint.id,
                selected_score,
                fast_path,
            )
        return SchedulerResponse(
            response_type=SchedulerResponseType.SUCCESS,
            request_id=request.request_id,
            data={
                _KEY_INSTANCE: instance_data,
                _KEY_ENDPOINT: endpoint_data,
                _KEY_SELECTED_SCORE: selected_score,
                _KEY_FAST_PATH: fast_path,
            },
        )

    @staticmethod
    def _parse_optional_int(value) -> int | None:
        """Parse optional integer request field."""
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_optional_float(value) -> float | None:
        """Parse optional float request field."""
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_affinity_candidates(data: dict) -> list[tuple[int, int, float]]:
        """
        Parse the worker's full kv_cache_affinity unified candidate set: every scored endpoint with
        its affinity-discounted ``prefill_cost``. Empty when the field is absent (other policies, or
        load_gated, which omit prefill_cost). Entries missing a numeric prefill_cost are skipped.
        """
        raw = data.get(_KEY_CANDIDATES)
        result: list[tuple[int, int, float]] = []
        if not isinstance(raw, list):
            return result
        for item in raw:
            if not isinstance(item, dict):
                continue
            instance_id = item.get("instance_id")
            endpoint_id = item.get("endpoint_id")
            prefill_cost = item.get(_KEY_PREFILL_COST)
            if instance_id is None or endpoint_id is None or prefill_cost is None:
                continue
            try:
                result.append((int(instance_id), int(endpoint_id), float(prefill_cost)))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _extract_allocate_candidate(data: dict) -> tuple[int, int] | None:
        """Parse selected endpoint id from top-level request fields."""
        instance_id = data.get("instance_id")
        endpoint_id = data.get("endpoint_id")
        if instance_id is not None and endpoint_id is not None:
            try:
                return (int(instance_id), int(endpoint_id))
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _extract_allocate_candidates(data: dict) -> list[tuple[int, int]]:
        """Parse the worker's ranked alternate endpoints (best-first); empty when absent."""
        raw = data.get(_KEY_CANDIDATES)
        result: list[tuple[int, int]] = []
        if not isinstance(raw, list):
            return result
        for item in raw:
            if not isinstance(item, dict):
                continue
            instance_id = item.get("instance_id")
            endpoint_id = item.get("endpoint_id")
            if instance_id is None or endpoint_id is None:
                continue
            try:
                result.append((int(instance_id), int(endpoint_id)))
            except (TypeError, ValueError):
                continue
        return result

    def _select_authoritative_candidate(
        self,
        candidate: tuple[int, int],
        role: PDRole,
    ) -> tuple[Instance, Endpoint, float] | None:
        """Select the best candidate using SchedulerServer's current workload ledger."""
        return self._select_valid_candidate(candidate, role)

    def _select_authoritative_allocate_candidate(
        self,
        candidate: tuple[int, int],
        candidates: list[tuple[int, int]],
        role: PDRole,
        candidate_policy: str | None,
        affinity_candidates: list[tuple[int, int, float]] | None = None,
        prefill_load_scale: float | None = None,
        load_weight: float | None = None,
    ) -> tuple[Instance, Endpoint, float] | None:
        """
        Select allocation target using SchedulerServer's authoritative workload view.

        Load-balance scans all endpoints cheaply at the current cluster size. KV-cache affinity in
        unified mode re-ranks EVERY worker-reported endpoint by ``prefill_load_scale*prefill_cost +
        load_weight*fresh_load`` (a global selection that fuses affinity and the scheduler's fresh
        load -- the worker already did the affinity math, the scheduler supplies fresh load). Older
        affinity callers without per-endpoint prefill_cost fall back to "least-loaded among the
        worker's ranked alternates". Other policies keep the worker-proposed endpoint.
        """
        if self._should_scan_global_load_balance(candidate_policy):
            selected = self._select_global_load_balance_candidate(role)
            if selected is not None:
                return selected
        if candidate_policy == CANDIDATE_POLICY_KV_CACHE_AFFINITY:
            if affinity_candidates:
                selected = self._select_affinity_global(affinity_candidates, role, prefill_load_scale, load_weight)
                if selected is not None:
                    return selected
            elif len(candidates) > 1:
                selected = self._select_lowest_load_among_candidates(candidates, role)
                if selected is not None:
                    return selected
        return self._select_authoritative_candidate(candidate, role)

    def _select_affinity_global(
        self,
        affinity_candidates: list[tuple[int, int, float]],
        role: PDRole,
        prefill_load_scale: float | None,
        load_weight: float | None,
    ) -> tuple[Instance, Endpoint, float] | None:
        """
        Global kv_cache_affinity unified selection over EVERY worker-reported endpoint.

        For each candidate, recompute the unified cost with the scheduler's authoritative (fresh)
        load: ``combined = prefill_load_scale * prefill_cost + load_weight * fresh_load``. Pick the
        minimum; ties prefer the lower prefill_cost (better affinity). This makes a stale-view burst
        spread by fresh load while keeping affinity, without the scheduler needing the prompt or a
        conductor round-trip. The returned score is ``combined`` (the authoritative unified score).
        """
        pscale = prefill_load_scale if prefill_load_scale is not None else 1.0
        lweight = load_weight if load_weight is not None else 1.0
        best: tuple[Instance, Endpoint, float, float] | None = None  # (..., combined, prefill_cost)
        for instance_id, endpoint_id, prefill_cost in affinity_candidates:
            found = self._find_available_instance_endpoint(instance_id, endpoint_id)
            if found is None:
                continue
            instance, endpoint = found
            try:
                instance_role = PDRole(instance.role)
            except ValueError:
                instance_role = PDRole.ROLE_U
            if instance_role != role:
                continue
            try:
                load = LoadBalancePolicy.calculate_endpoint_score(
                    instance,
                    endpoint,
                    role=role,
                    instance_score_weight=self._endpoint_instance_score_weight,
                )
            except Exception as e:
                logger.warning(
                    "Failed to score affinity candidate instance_id=%s endpoint_id=%s: %s",
                    instance_id,
                    endpoint_id,
                    e,
                )
                continue
            combined = pscale * prefill_cost + lweight * load
            if best is None:
                best = (instance, endpoint, combined, prefill_cost)
            elif combined < best[2] or (combined == best[2] and prefill_cost < best[3]):
                best = (instance, endpoint, combined, prefill_cost)
        if best is None:
            return None
        return (best[0], best[1], best[2])

    def _select_lowest_load_among_candidates(
        self,
        candidates: list[tuple[int, int]],
        role: PDRole,
    ) -> tuple[Instance, Endpoint, float] | None:
        """
        Among the worker's affinity-ranked candidates, pick the lowest current endpoint score from
        the authoritative ledger. The candidate set is already the affinity top-k, so this spreads
        a burst by fresh load without breaking affinity. Ties keep the earliest (best-affinity) one.
        """
        best: tuple[Instance, Endpoint, float] | None = None
        for cand in candidates:
            found = self._find_available_instance_endpoint(*cand)
            if found is None:
                continue
            instance, endpoint = found
            try:
                instance_role = PDRole(instance.role)
            except ValueError:
                instance_role = PDRole.ROLE_U
            if instance_role != role:
                continue
            try:
                score = LoadBalancePolicy.calculate_endpoint_score(
                    instance,
                    endpoint,
                    role=role,
                    instance_score_weight=self._endpoint_instance_score_weight,
                )
            except Exception as e:
                logger.warning(
                    "Failed to score affinity candidate instance_id=%s endpoint_id=%s: %s",
                    cand[0],
                    cand[1],
                    e,
                )
                continue
            if best is None:
                best = (instance, endpoint, score)
            elif score < best[2]:
                best = (instance, endpoint, score)
        return best

    def _should_scan_global_load_balance(self, candidate_policy: str | None) -> bool:
        """Return True when candidates were selected by load-balance semantics."""
        if candidate_policy == CANDIDATE_POLICY_LOAD_BALANCE:
            return True
        if candidate_policy in KNOWN_CANDIDATE_POLICIES:
            return False
        if candidate_policy is not None:
            logger.warning(
                "Unknown allocate candidate_policy=%s; falling back to scheduler_type",
                candidate_policy,
            )
        return self._is_load_balance_scheduler

    def _select_global_load_balance_candidate(
        self,
        role: PDRole,
    ) -> tuple[Instance, Endpoint, float] | None:
        """Select the globally lowest-score endpoint for role from SchedulerServer's local pool."""
        instances = self._instance_manager.get_available_instances(role).values()
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            instances,
            role=role,
            top_k=1,
            instance_score_weight=self._endpoint_instance_score_weight,
        )
        if not candidates:
            return None
        candidate = candidates[0]
        return (candidate.instance, candidate.endpoint, candidate.score)

    def _can_use_worker_top1_fast_path(
        self,
        worker_workload_sequence: int | None,
        worker_instance_version: int | None,
    ) -> bool:
        """Return True when worker selected from the exact SchedulerServer workload view."""
        if not self._workload_writer:
            return False
        return (
            worker_workload_sequence is not None
            and worker_instance_version is not None
            and worker_workload_sequence == self._workload_writer.sequence
            and worker_instance_version == self._workload_writer.instance_version
        )

    def _select_valid_candidate(
        self,
        candidate: tuple[int, int],
        role: PDRole,
    ) -> tuple[Instance, Endpoint, float] | None:
        """
        Validate one worker-selected candidate and calculate its current score for observability.

        This is the fast path: when workload_sequence and instance_version match, SchedulerServer
        only validates the worker-selected endpoint.
        """
        instance_id, endpoint_id = candidate
        found = self._find_available_instance_endpoint(instance_id, endpoint_id)
        if found is None:
            return None
        instance, endpoint = found
        try:
            instance_role = PDRole(instance.role)
        except ValueError:
            instance_role = PDRole.ROLE_U
        if instance_role != role:
            return None
        try:
            score = LoadBalancePolicy.calculate_endpoint_score(
                instance,
                endpoint,
                role=role,
                instance_score_weight=self._endpoint_instance_score_weight,
            )
        except Exception as e:
            logger.warning(
                "Failed to score fast-path allocate candidate instance_id=%s endpoint_id=%s: %s",
                instance_id,
                endpoint_id,
                e,
            )
            return None
        return (instance, endpoint, score)

    def _find_available_instance_endpoint(
        self,
        instance_id: int,
        endpoint_id: int,
    ) -> tuple[Instance, Endpoint] | None:
        """Find an available instance/endpoint pair in the SchedulerServer local pool."""
        for role in (PDRole.ROLE_E, PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_U):
            instance = self._instance_manager.get_available_instances(role).get(instance_id)
            if not instance:
                continue
            for pod_eps in (instance.endpoints or {}).values():
                for endpoint in (pod_eps or {}).values():
                    if endpoint.id == endpoint_id:
                        return (instance, endpoint)
        return None


# ==================== Transport (ROUTER frontend) ====================


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
            await self._socket.send_multipart(send_frames)

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
    Fully async Scheduler Server (zmq.asyncio).
    """

    def __init__(
        self,
        config: CoordinatorConfig,
        frontend_address: str = "ipc:///tmp/scheduler_frontend",
    ):
        """
        Args:
            config: Coordinator config
            frontend_address: Frontend address (receives API Server process requests, IPC)
        """
        self.config = config
        self.frontend_address = frontend_address

        # Scheduler process holds InstanceManager and Scheduler (single source of truth)
        self.instance_manager = InstanceManager(config)
        self.scheduler = Scheduler(instance_provider=self.instance_manager, config=config)

        # Async ZMQ context and sockets
        self.context: zmq.asyncio.Context | None = None
        self._transport: _SchedulerFrontendTransport | None = None

        # Background task refs
        self._active_tasks: set[asyncio.Task] = set()
        self._stop_event = asyncio.Event()

        # Serializer (instance-level, shared by all tasks for cache reuse)
        # Encode/decode locks separate so encode and decode can run concurrently
        from motor.coordinator.scheduler.runtime.zmq_protocol import (
            ZMQMessageSerializer,
        )

        self._serializer = ZMQMessageSerializer()
        self._encode_lock = asyncio.Lock()
        self._decode_lock = asyncio.Lock()

        # Dispatch timeout to avoid thread-pool exhaustion from long blocks
        self._dispatch_timeout = 5.0

        # Set in start() (G.CLS.08: declare in __init__)
        self._dispatcher: _SchedulerRequestDispatcher | None = None
        self._workload_shm = None
        self._workload_writer: WorkloadSharedMemoryWriter | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._pub_socket: zmq.asyncio.Socket | None = None

    async def stop(self):
        """Stop the async server."""
        logger.info("Stopping async scheduler server...")

        self._stop_event.set()

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        # Wait for all active request-handling tasks to finish
        if self._active_tasks:
            logger.info(
                "Waiting for %s active request tasks to complete...",
                len(self._active_tasks),
            )
            # Cancel all unfinished tasks
            for task in self._active_tasks:
                if not task.done():
                    task.cancel()
            # Wait for all tasks (including cancelled)
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
            self._active_tasks.clear()

        # Close shared memory (release writer's buffer first to avoid BufferError: exported pointers exist)
        if self._workload_writer:
            self._workload_writer.release()
            self._workload_writer = None
        if self._workload_shm:
            try:
                self._workload_shm.close()
                self._workload_shm.unlink()
            except Exception as e:
                logger.warning("Error closing workload shared memory: %s", e)
            self._workload_shm = None
        if self._pub_socket:
            try:
                self._pub_socket.close()
            except Exception as e:
                logger.warning("Error closing instance PUB socket: %s", e)
            self._pub_socket = None
        if self._transport:
            await self._transport.disconnect()
        if self.context:
            try:
                # term() is synchronous on zmq.asyncio.Context; do not await.
                self.context.term()
            except Exception as e:
                logger.warning("Error terminating context: %s", e)

        logger.info("Async scheduler server stopped")

    async def start(self):
        """Start the async Scheduler server."""
        from multiprocessing import shared_memory
        from motor.coordinator.scheduler.runtime.workload_shm import total_size

        # Create async ZMQ context and ROUTER transport
        self.context = zmq.asyncio.Context()
        self._transport = _SchedulerFrontendTransport(self.context)
        await self._transport.bind(self.frontend_address)

        from motor.config.coordinator import DEFAULT_SCHEDULER_PROCESS_CONFIG

        instance_pub_address = DEFAULT_SCHEDULER_PROCESS_CONFIG.instance_pub_address
        if instance_pub_address:
            self._pub_socket = self.context.socket(zmq.PUB)
            self._pub_socket.bind(instance_pub_address)
            logger.info("Instance change PUB bound: %s", instance_pub_address)

        max_entries = DEFAULT_WORKLOAD_SHM_MAX_ENTRIES
        shm_name = f"mindie_workload_{os.getpid()}"
        shm_size = total_size(max_entries)
        self._workload_shm = _create_workload_shared_memory(shared_memory, shm_name, shm_size)
        self._workload_writer = WorkloadSharedMemoryWriter(
            self._workload_shm,
            self.instance_manager,
            max_entries=max_entries,
        )
        self._workload_writer.write_snapshot()
        logger.info("Workload shared memory enabled: %s (%d entries)", shm_name, max_entries)

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        self._dispatcher = _SchedulerRequestDispatcher(
            self.instance_manager,
            self.scheduler,
            self.config,
            workload_writer=self._workload_writer,
            on_instance_refresh_done=self._publish_instance_changed,
        )

        logger.info("Async scheduler server started, frontend: %s", self.frontend_address)

        # Async main loop (fully non-blocking)
        try:
            await self._run_async_loop()
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
        finally:
            await self.stop()

    async def _publish_instance_changed(self) -> None:
        """Publish instance list changed + version to SUB clients (no-op if PUB not enabled)."""
        if not self._pub_socket:
            return
        version = self._workload_writer.instance_version if self._workload_writer else 0
        try:
            await self._pub_socket.send_multipart([INSTANCE_CHANGE_TOPIC, str(version).encode()])
        except Exception as e:
            logger.warning("Failed to publish instance change: %s", e)

    async def _heartbeat_loop(self) -> None:
        """Write heartbeat to shm every 1s so Infer can detect Scheduler restart (stale = no change)."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(1.0)
                if self._stop_event.is_set() or not self._workload_writer:
                    break
                self._workload_writer.write_heartbeat()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Workload heartbeat error: %s", e)

    async def _run_async_loop(self):
        """Async main loop: handle all requests concurrently; main loop never blocks."""
        logger.info("Async main loop started")

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
                "Scheduler request received request_type=%s req_id=%s",
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
                "Scheduler request done request_type=%s req_id=%s elapsed_ms=%.1f",
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


# ==================== Entry points ====================


async def run_async_scheduler_server(config: CoordinatorConfig):
    """Run Scheduler server asynchronously (asyncio entry)."""
    # Set process title
    try:
        import setproctitle

        setproctitle.setproctitle("AsyncSchedulerServer")
    except ImportError:
        pass

    logger.info("Async scheduler server process starting (PID: %s)", os.getpid())

    from motor.config.coordinator import DEFAULT_SCHEDULER_PROCESS_CONFIG

    frontend_address = DEFAULT_SCHEDULER_PROCESS_CONFIG.frontend_address

    # Create and start async server
    server = AsyncSchedulerServer(config, frontend_address)

    try:
        await server.start()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    finally:
        await server.stop()


def run_async_scheduler_server_proc(config: CoordinatorConfig) -> None:
    """Async Scheduler server process entry (for sync entry points)."""
    asyncio.run(run_async_scheduler_server(config))


# Backward compat: scheduler_manager (process/) etc. import SchedulerServer from this module
SchedulerServer = AsyncSchedulerServer
