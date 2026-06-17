# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

from dataclasses import dataclass
import heapq
from typing import Iterable

from motor.common.resources.instance import Instance, PDRole
from motor.common.resources.endpoint import Endpoint, Workload, WorkloadAction
from motor.coordinator.domain import InstanceProvider
from motor.coordinator.scheduler.policy.base import BaseSchedulingPolicy
from motor.common.logger import get_logger

logger = get_logger(__name__)

DEFAULT_ENDPOINT_INSTANCE_SCORE_WEIGHT = 0.05


@dataclass(frozen=True)
class EndpointCandidate:
    """Endpoint candidate plus its load-balance score."""

    instance: Instance
    endpoint: Endpoint
    score: float


class LoadBalancePolicy(BaseSchedulingPolicy):
    """
    Load Balance Scheduler Policy implementation.
    Selects instances and endpoints based on their current workload.
    Implements select_and_endpoint and update_workload required by SchedulingFacade (forwarded via Scheduler).
    """

    def __init__(self, instance_provider: InstanceProvider):
        super().__init__(instance_provider=instance_provider)
        self._instance_provider = instance_provider
        self._endpoint_instance_score_weight = DEFAULT_ENDPOINT_INSTANCE_SCORE_WEIGHT
        # Removed req_workload_dict - workload state is now managed by API Server's RequestManager
        logger.info("LoadBalancePolicy started.")

    def set_endpoint_instance_score_weight(self, weight: float) -> None:
        """Set instance pressure weight used by endpoint-first composite scoring."""
        self._endpoint_instance_score_weight = max(0.0, float(weight))

    @staticmethod
    def calculate_endpoint_score(
        instance: Instance,
        endpoint: Endpoint,
        role: PDRole | str | None = None,
        instance_score_weight: float = DEFAULT_ENDPOINT_INSTANCE_SCORE_WEIGHT,
    ) -> float:
        """
        Score an endpoint globally while preserving some instance-level pressure awareness.

        Endpoint workload is the primary signal. Instance workload is averaged by endpoint count so
        larger DP instances are not penalized just because they have more endpoints.
        """
        score_role = role if role is not None else instance.role
        endpoint_score = endpoint.workload.calculate_workload_score(role=score_role)
        if instance_score_weight <= 0:
            return endpoint_score
        endpoint_count = max(1, len(instance.get_all_endpoints()))
        instance_score = instance.gathered_workload.calculate_workload_score(role=score_role)
        return endpoint_score + instance_score_weight * (instance_score / endpoint_count)

    @staticmethod
    def select_endpoint_candidates_from_list(
        instances: list[Instance] | Iterable[Instance],
        role: PDRole | None = None,
        top_k: int = 1,
        instance_score_weight: float = DEFAULT_ENDPOINT_INSTANCE_SCORE_WEIGHT,
        start_index: int = 0,
    ) -> list[EndpointCandidate]:
        """
        Select top-K endpoints globally across all instances.

        ``start_index`` rotates traversal order and only affects ties, spreading equal-load choices
        across worker processes without changing load-based ordering.
        """
        if top_k <= 0:
            return []
        if not isinstance(instances, (list, tuple)):
            instances = list(instances)
        if not instances:
            return []

        n = len(instances)
        rotated_instances = [instances[(start_index + i) % n] for i in range(n)]
        scored: list[tuple[float, int, EndpointCandidate]] = []
        tie_order = 0
        for instance in rotated_instances:
            for endpoint in instance.get_all_endpoints():
                try:
                    score = LoadBalancePolicy.calculate_endpoint_score(
                        instance,
                        endpoint,
                        role=role,
                        instance_score_weight=instance_score_weight,
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to calculate endpoint score for instance %s endpoint %s: %s",
                        instance.id,
                        endpoint.id,
                        e,
                    )
                    continue
                scored.append((score, tie_order, EndpointCandidate(instance, endpoint, score)))
                tie_order += 1
        best = heapq.nsmallest(top_k, scored, key=lambda item: (item[0], item[1]))
        return [candidate for _, _, candidate in best]

    @staticmethod
    def select_endpoint_from_list(
        instances: list[Instance] | Iterable[Instance],
        role: PDRole | None = None,
        start_index: int = 0,
        instance_score_weight: float = DEFAULT_ENDPOINT_INSTANCE_SCORE_WEIGHT,
    ) -> tuple[Instance, Endpoint] | None:
        """Select one endpoint globally across all instances."""
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            instances,
            role=role,
            top_k=1,
            instance_score_weight=instance_score_weight,
            start_index=start_index,
        )
        if not candidates:
            return None
        candidate = candidates[0]
        return (candidate.instance, candidate.endpoint)

    @staticmethod
    def select_instance_from_list(
        instances: list[Instance] | Iterable[Instance],
        role: PDRole = None,
        start_index: int = 0,
    ) -> Instance | None:
        """
        Select one instance with minimum workload from list/iterable (shared by Policy and Client).
        Single pass, always picks globally lowest; start_index only affects order (tie-break).
        When start_index==0 can pass values() view to avoid list alloc; when !=0 materialize to list.

        Args:
            instances: Instance list or iterable (e.g. InstanceManager.get_available_instances(role).values())
            role: Optional, for workload score
            start_index: Traversal start offset (start_index + i) % n, for multi API Server tie-break, default 0

        Returns:
            Selected instance, or None (empty or all failed)
        """
        min_workload = float('inf')
        selected_instance = None

        if start_index != 0:
            if not isinstance(instances, (list, tuple)):
                instances = list(instances)
            if not instances:
                return None
            n = len(instances)
            for i in range(n):
                idx = (start_index + i) % n
                instance = instances[idx]
                try:
                    workload_score = instance.gathered_workload.calculate_workload_score(role=instance.role)
                    if workload_score < min_workload:
                        min_workload = workload_score
                        selected_instance = instance
                except Exception as e:
                    logger.warning("Failed to calculate workload score for instance %s: %s", instance.id, e)
                    continue
            return selected_instance

        # start_index == 0: single pass, no materialize, save list allocation
        for instance in instances:
            try:
                workload_score = instance.gathered_workload.calculate_workload_score(role=instance.role)
                if workload_score < min_workload:
                    min_workload = workload_score
                    selected_instance = instance
            except Exception as e:
                logger.warning("Failed to calculate workload score for instance %s: %s", instance.id, e)
                continue
        return selected_instance

    @staticmethod
    def select_endpoint_from_instance(instance: Instance) -> Endpoint | None:
        """
        Select one endpoint with minimum workload from instance (shared by Policy and Client).

        Args:
            instance: Instance to select endpoint from

        Returns:
            Selected Endpoint, or None if none available
        """
        if not instance:
            logger.warning("No instance provided for endpoint selection")
            return None

        all_endpoints = instance.get_all_endpoints()
        if not all_endpoints:
            logger.warning(f"No endpoints available in instance {instance.id}")
            return None

        min_workload = float('inf')
        selected_endpoint = None
        for endpoint in all_endpoints:
            try:
                workload_score = endpoint.workload.calculate_workload_score(role=instance.role)
                if workload_score < min_workload:
                    min_workload = workload_score
                    selected_endpoint = endpoint
            except Exception as e:
                logger.warning("Failed to calculate workload score for endpoint %s: %s", endpoint.id, e)
                continue
        return selected_endpoint

    async def update_workload(
        self,
        instance_id: int,
        endpoint_id: int,
        req_id: str,
        workload_action: WorkloadAction,
        workload_change: Workload,
    ) -> bool:
        """
        Update workload information for load-aware scheduling (by id only).

        Args:
            instance_id: Instance ID
            endpoint_id: Endpoint ID
            req_id: Request identifier (optional, only for logging)
            workload_action: Workload action type
            workload_change: Workload change value (calculated and passed by API Server)

        Returns:
            True if workload was updated successfully, False otherwise
        """
        if hasattr(self._instance_provider, "update_instance_workload"):
            await self._instance_provider.update_instance_workload(instance_id, endpoint_id, workload_change)
        else:
            raise RuntimeError("InstanceProvider must support update_instance_workload for LoadBalancePolicy")

        if req_id:
            logger.debug(
                f"Request {req_id} updated workload: instance_id={instance_id}, "
                f"endpoint_id={endpoint_id}, action={workload_action.value}, "
                f"change={workload_change}"
            )
        else:
            logger.debug(
                f"Updated workload: instance_id={instance_id}, "
                f"endpoint_id={endpoint_id}, action={workload_action.value}, "
                f"change={workload_change}"
            )

        return True

    def select_instance_and_endpoint(self, role: PDRole = None):
        """
        Load-balance by endpoint first, with a configurable instance pressure penalty.
        """
        active_instances = self._instance_provider.get_available_instances(role)
        if not active_instances:
            logger.warning("No active instances available for scheduling")
            return None
        return LoadBalancePolicy.select_endpoint_from_list(
            active_instances.values(),
            role,
            instance_score_weight=self._endpoint_instance_score_weight,
        )

    def select_instance_and_endpoint_from_list(
        self,
        instances: list[Instance],
        role: PDRole | None = None,
        req_info=None,
    ):
        """Load-balance within a capability-compatible subset."""
        del req_info
        return LoadBalancePolicy.select_endpoint_from_list(
            instances,
            role,
            instance_score_weight=self._endpoint_instance_score_weight,
        )

    def _select_instance(self, role: PDRole = None) -> Instance | None:
        """
        Select an instance with the least workload.
        """
        active_instances = self._instance_provider.get_available_instances(role)
        if not active_instances:
            logger.warning("No active instances available for scheduling")
            return None
        return LoadBalancePolicy.select_instance_from_list(active_instances.values(), role)

    def _select_endpoint(self, instance: Instance) -> Endpoint | None:
        """
        Select an endpoint with the least workload from the given instance.
        """
        return LoadBalancePolicy.select_endpoint_from_instance(instance)
