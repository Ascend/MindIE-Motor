# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import uuid

from motor.common.resources.dispatch import (
    has_compatible_dispatch_pair,
)
from motor.common.resources.instance import Instance, PDRole
from motor.common.resources.endpoint import WorkloadAction, Workload
from motor.coordinator.domain import (
    InstanceReadiness,
    UpdateWorkloadParams,
    readiness_from_instances,
)
from motor.common.logger import get_logger
from motor.coordinator.scheduler.policy.base import BaseSchedulingPolicy
from motor.coordinator.scheduler.policy.factory import SchedulingPolicyFactory
from motor.coordinator.domain.scheduling_pin import (
    resolve_pinned_instance,
    select_endpoint_for_instance,
)
from motor.coordinator.domain.workload_calculator import calculate_demand_workload
from motor.config.coordinator import CoordinatorConfig, SchedulerType
from motor.coordinator.domain import InstanceProvider
from motor.coordinator.models.request import RequestInfo

logger = get_logger(__name__)


class Scheduler:
    """
    Main scheduler class that acts as a facade for different scheduling algorithms.
    Implements SchedulingFacade for BaseRouter DI (in-process mode).
    Created once per Scheduler process by SchedulerServer (no singleton).
    """

    def __init__(
        self,
        instance_provider: InstanceProvider,
        config: CoordinatorConfig | SchedulerType | None = None,
    ):
        """
        Initialize the scheduler.

        Args:
            instance_provider: Required. Instance source (e.g. InstanceManager); injected by SchedulerServer or tests.
            config: Can be:
                   - CoordinatorConfig object
                   - SchedulerType enum value
                   - None (uses default config)
        """
        if config is None:
            config = CoordinatorConfig()

        if isinstance(config, SchedulerType):
            self._policy_type = config
            self._config: CoordinatorConfig | None = None
        else:
            self._policy_type = config.scheduler_config.scheduler_type
            self._config = config

        self._instance_provider = instance_provider
        self._scheduling_policy = SchedulingPolicyFactory.create(self._policy_type, self._instance_provider)
        if self._config and hasattr(self._scheduling_policy, "set_endpoint_instance_score_weight"):
            self._scheduling_policy.set_endpoint_instance_score_weight(
                self._config.scheduler_config.endpoint_instance_score_weight
            )
        # Global per-PD-group precision state (shared across inference workers).
        self._sample_exit_last_time: dict[tuple[int | None, int], float] = {}
        self._precision_streak_counts: dict[tuple[int | None, int], int] = {}
        self._precision_probing: dict[tuple[int | None, int], bool] = {}
        self._precision_action_tokens: dict[tuple[int | None, int], str] = {}
        self._sample_exit_locks: dict[tuple[int | None, int], asyncio.Lock] = {}
        logger.info("Scheduler started.")

    def get_scheduling_policy(self) -> BaseSchedulingPolicy:
        """
        Get the current scheduling policy.

        Returns:
            Current scheduling policy
        """
        return self._scheduling_policy

    async def select_instance_and_endpoint(self, role: PDRole = None):
        """
        Select an instance and endpoint based on the current scheduling algorithm.
        If policy is async, awaits and returns.

        Args:
            role: Optional PDRole to filter instances by role (prefill/decode)

        Returns:
            (Instance, Endpoint) tuple or None if no instance available
        """
        r = self._scheduling_policy.select_instance_and_endpoint(role)
        return (await r) if asyncio.iscoroutine(r) else r

    async def select_and_allocate(
        self,
        role: PDRole,
        req_info: RequestInfo,
        *,
        target_instance_id: int | None = None,
    ):
        """
        Atomic: select instance + one workload allocation (ALLOCATION).
        Allocation workload is decided here: zero for policies without update_workload (e.g. RR), demand for LB.

        Returns:
            (Instance, Endpoint, Workload) tuple or None (no instance or update_workload failed).
            The returned Workload is what was allocated; caller records it for release.
        """
        if target_instance_id is not None:
            pool = self._instance_provider.get_available_instances(role)
            instance = resolve_pinned_instance(pool, target_instance_id)
            if instance is None:
                logger.warning(
                    "Pinned instance_id=%s not in available pool for role=%s req_id=%s",
                    target_instance_id,
                    role,
                    req_info.req_id,
                )
                return None
            policy_type = self._policy_type.value if hasattr(self._policy_type, "value") else str(self._policy_type)
            endpoint = select_endpoint_for_instance(instance, scheduler_type=policy_type)
            if endpoint is None:
                logger.warning(
                    "No endpoint on pinned instance_id=%s role=%s req_id=%s",
                    target_instance_id,
                    role,
                    req_info.req_id,
                )
                return None
        else:
            r = self._scheduling_policy.select_instance_and_endpoint(role)
            result = (await r) if asyncio.iscoroutine(r) else r
            if result is None:
                return None
            instance, endpoint = result
        return await self._allocate_selected(instance, endpoint, role, req_info)

    async def _allocate_selected(
        self,
        instance: Instance,
        endpoint,
        role: PDRole,
        req_info: RequestInfo,
    ):
        """Allocate workload for an already selected instance endpoint."""
        workload = (
            Workload()
            if not hasattr(self._scheduling_policy, "update_workload")
            else calculate_demand_workload(role, req_info)
        )
        params = UpdateWorkloadParams(
            instance_id=instance.id,
            endpoint_id=endpoint.id,
            role=role,
            req_id=req_info.req_id,
            workload_action=WorkloadAction.ALLOCATION,
            workload_change=workload,
        )
        success = self.update_workload_sync(params)[0]
        if not success:
            return None
        return (instance, endpoint, workload)

    def update_workload_sync(self, params: UpdateWorkloadParams) -> tuple[bool, PDRole | None, Workload | None]:
        """
        Synchronous workload update for Scheduler-process critical sections.
        Returns (success, role, updated_endpoint_workload). A None workload means the policy does not
        track workload; the caller must re-read the authoritative absolute rather than treat
        params.workload_change (a delta) as the endpoint total.
        """
        if hasattr(self._scheduling_policy, "update_workload_sync"):
            role, workload = self._scheduling_policy.update_workload_sync(
                params.instance_id,
                params.endpoint_id,
                params.req_id,
                params.workload_action,
                params.workload_change,
            )
            return (role is not None and workload is not None, role, workload)
        # Policy has no update_workload_sync (e.g. round-robin): the ledger is untouched, so we have
        # no absolute to return. Signal that with None -- returning workload_change would write a
        # delta into SHM as if it were the endpoint total.
        return (True, params.role, None)

    async def update_workload(self, params: UpdateWorkloadParams) -> bool:
        """
        Update workload information for load-aware scheduling strategies (by id only).
        Same interface as Router/AsyncSchedulerClient; role only for signature compat (in-process policy does not use).
        """
        if hasattr(self._scheduling_policy, "update_workload"):
            return await self._scheduling_policy.update_workload(
                params.instance_id,
                params.endpoint_id,
                params.req_id,
                params.workload_action,
                params.workload_change,
            )
        return True  # Ignore for strategies that don't support workload tracking

    async def get_available_instances(self, role: PDRole | None = None) -> dict[int, Instance]:
        """
        Get available instance list (for metrics/readiness etc.).
        In-process provider is fast and lock-free; direct call avoids to_thread overhead.
        """
        return dict(self._instance_provider.get_available_instances(role))

    async def get_available_instance_roles(self) -> set[PDRole]:
        """Return roles from the in-process instance provider without scheduler IPC."""
        roles: set[PDRole] = set()
        aliases = {"both": PDRole.ROLE_U, "hybrid": PDRole.ROLE_U}
        for instance in (await self.get_available_instances(None)).values():
            role = instance.role
            if isinstance(role, PDRole):
                roles.add(role)
                continue
            normalized = str(role).strip().lower()
            try:
                roles.add(PDRole(normalized))
            except ValueError:
                if normalized in aliases:
                    roles.add(aliases[normalized])
        return roles

    async def has_compatible_pd_pair(self) -> bool:
        """Return whether the in-process instance view has a compatible P/D pair."""
        prefill = self._instance_provider.get_available_instances(PDRole.ROLE_P).values()
        decode = self._instance_provider.get_available_instances(PDRole.ROLE_D).values()
        return has_compatible_dispatch_pair(prefill, decode)

    async def get_unblocked_instances(self, role: PDRole) -> list[int]:
        """Return all instance IDs for the role (in-process scheduler has no CB)."""
        return [inst.id for inst in self._instance_provider.get_available_instances(role).values()]

    async def report_cb_event(self, instance_id: int, event: str) -> None:
        """No-op: in-process scheduler has no circuit breaker (CB managed by SchedulerServer)."""

    async def has_required_instances(self) -> InstanceReadiness:
        """Return readiness inferred from currently available instance roles."""
        instances = await self.get_available_instances(None)
        readiness = readiness_from_instances(instances.values())
        if readiness != InstanceReadiness.NONE:
            return readiness
        return await asyncio.to_thread(self._instance_provider.get_required_instances_status)

    def _sample_exit_lock(self, key: tuple[int | None, int]) -> asyncio.Lock:
        if key not in self._sample_exit_locks:
            self._sample_exit_locks[key] = asyncio.Lock()
        return self._sample_exit_locks[key]

    async def confirm_sample_exit(
        self,
        *,
        p_instance_id: int | None,
        d_instance_id: int,
        now: float,
        interval_seconds: float,
    ) -> bool:
        """Atomically check/update per-PD-group sampling exit interval (scheduler-global)."""
        key = (p_instance_id, d_instance_id)
        lock = self._sample_exit_lock(key)
        async with lock:
            last_exit = self._sample_exit_last_time.get(key, 0.0)
            if now - last_exit >= interval_seconds:
                self._sample_exit_last_time[key] = now
                logger.debug(
                    "Scheduler: confirm_sample_exit ok pd_group=(%s,%s) interval=%.1fs",
                    key[0],
                    key[1],
                    interval_seconds,
                )
                return True
        return False

    async def record_precision_result(
        self,
        *,
        p_instance_id: int | None,
        d_instance_id: int,
        has_issue: bool,
        threshold: int,
    ) -> dict[str, int | bool | str | None]:
        """Atomically update global consecutive count and probing for one PD group."""
        key = (p_instance_id, d_instance_id)
        lock = self._sample_exit_lock(key)
        async with lock:
            if self._precision_probing.get(key):
                return {
                    "skip": True,
                    "threshold_hit": False,
                    "consecutive": self._precision_streak_counts.get(key, 0),
                    "action_token": None,  # nosec B105
                }
            if has_issue:
                count = self._precision_streak_counts.get(key, 0) + 1
                self._precision_streak_counts[key] = count
                if count >= threshold:
                    token = str(uuid.uuid4())
                    self._precision_probing[key] = True
                    self._precision_action_tokens[key] = token
                    logger.debug(
                        "Scheduler: precision threshold pd_group=(%s,%s) count=%s",
                        key[0],
                        key[1],
                        count,
                    )
                    return {
                        "skip": False,
                        "threshold_hit": True,
                        "consecutive": count,
                        "action_token": token,
                    }
                return {
                    "skip": False,
                    "threshold_hit": False,
                    "consecutive": count,
                    "action_token": None,  # nosec B105
                }
            self._precision_streak_counts[key] = 0
            return {
                "skip": False,
                "threshold_hit": False,
                "consecutive": 0,
                "action_token": None,  # nosec B105
            }

    async def finish_precision_action(
        self,
        *,
        p_instance_id: int | None,
        d_instance_id: int,
        action_token: str,
    ) -> bool:
        """Clear probing and streak after probe/alarm; rejects stale action_token."""
        key = (p_instance_id, d_instance_id)
        lock = self._sample_exit_lock(key)
        async with lock:
            expected = self._precision_action_tokens.get(key)
            if not expected or expected != action_token:
                logger.warning(
                    "Scheduler: finish_precision_action token mismatch pd_group=(%s,%s)",
                    key[0],
                    key[1],
                )
                return False
            self._precision_probing[key] = False
            self._precision_streak_counts[key] = 0
            self._precision_action_tokens.pop(key, None)
            logger.debug(
                "Scheduler: finish_precision_action ok pd_group=(%s,%s)",
                key[0],
                key[1],
            )
            return True
