# -*- coding: utf-8 -*-
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

from motor.common.resources.instance import Instance, PDRole
from motor.common.resources.endpoint import WorkloadAction, Workload
from motor.coordinator.domain import (
    InstanceReadiness,
    ScheduledPair,
    ScheduledResource,
    UpdateWorkloadParams,
    build_release_workload_params,
    readiness_from_instances,
)
from motor.common.resources.http_msg_spec import EventType
from motor.common.logger import get_logger
from motor.coordinator.scheduler.policy.base import BaseSchedulingPolicy
from motor.coordinator.scheduler.policy.factory import SchedulingPolicyFactory
from motor.coordinator.domain.workload_calculator import calculate_demand_workload
from motor.config.coordinator import CoordinatorConfig, DeployMode, SchedulerType
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

    async def select_and_allocate(self, role: PDRole, req_info: RequestInfo):
        """
        Atomic: select instance + one workload allocation (ALLOCATION).
        Allocation workload is decided here: zero for policies without update_workload (e.g. RR), demand for LB.

        Returns:
            (Instance, Endpoint, Workload) tuple or None (no instance or update_workload failed).
            The returned Workload is what was allocated; caller records it for release.
        """
        r = self._scheduling_policy.select_instance_and_endpoint(role)
        result = (await r) if asyncio.iscoroutine(r) else r
        if result is None:
            return None
        instance, endpoint = result
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
        success = await self.update_workload(params)
        if not success:
            return None
        return (instance, endpoint, workload)

    async def select_pair_and_allocate(self, req_info: RequestInfo) -> ScheduledPair | None:
        p_result = await self.select_and_allocate(PDRole.ROLE_P, req_info)
        if p_result is None:
            return None
        p_instance, p_endpoint, p_workload = p_result
        d_result = await self.select_and_allocate(PDRole.ROLE_D, req_info)
        if d_result is None:
            for params in build_release_workload_params(
                p_instance.id,
                p_endpoint.id,
                PDRole.ROLE_P,
                req_info.req_id,
                p_workload,
            ):
                await self.update_workload(params)
            return None
        d_instance, d_endpoint, d_workload = d_result
        return ScheduledPair(
            prefill=ScheduledResource(instance=p_instance, endpoint=p_endpoint),
            decode=ScheduledResource(instance=d_instance, endpoint=d_endpoint),
            prefill_workload=p_workload,
            decode_workload=d_workload,
        )

    async def update_workload(self, params: UpdateWorkloadParams) -> bool:
        """
        Update workload information for load-aware scheduling strategies (by id only).
        Same interface as Router/AsyncSchedulerClient; role only for signature compat (in-process policy does not use).
        """
        if hasattr(self._scheduling_policy, 'update_workload'):
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

    async def has_required_instances(self) -> InstanceReadiness:
        """
        Check by deploy mode; returns InstanceReadiness (REQUIRED_MET, ONLY_PREFILL, ONLY_DECODE, NONE, UNKNOWN).
        deploy_mode from init config; default DeployMode.PD_SEPARATE when no config.
        """
        deploy_mode = self._config.scheduler_config.deploy_mode if self._config else DeployMode.PD_SEPARATE
        instances = await self.get_available_instances(None)
        readiness = readiness_from_instances(instances.values())
        if readiness != InstanceReadiness.NONE:
            return readiness
        return await asyncio.to_thread(self._instance_provider.get_required_instances_status, deploy_mode)

    async def get_all_instances(self) -> tuple[dict[int, Instance], dict[int, Instance]]:
        """Return (available, unavailable) instance dicts from in-process InstanceManager."""
        return await self._instance_provider.get_all_instances()

    async def refresh_instances(self, event_type: EventType, instances: list[Instance]) -> bool:
        """Refresh instance list (delegate to in-process InstanceManager). Returns True if pools changed."""
        return await self._instance_provider.refresh_instances(event_type, instances)
