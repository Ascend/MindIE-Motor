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
import random
from typing import Callable, Iterable

from motor.common.resources.instance import Instance, PDRole
from motor.common.resources.endpoint import Endpoint
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
    """

    def __init__(self, instance_provider: InstanceProvider):
        super().__init__(instance_provider=instance_provider)
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
        endpoint_score = endpoint.workload.calculate_workload_score()
        if instance_score_weight <= 0:
            return endpoint_score
        endpoint_count = max(1, len(instance.get_all_endpoints()))
        instance_score = instance.gathered_workload.calculate_workload_score()
        return endpoint_score + instance_score_weight * (instance_score / endpoint_count)

    @staticmethod
    def select_endpoint_candidates_from_list(
        instances: list[Instance] | Iterable[Instance],
        role: PDRole | None = None,
        top_k: int = 1,
        instance_score_weight: float = DEFAULT_ENDPOINT_INSTANCE_SCORE_WEIGHT,
        *,
        is_blocked: Callable[[int], bool] | None = None,
        excluded_pairs: set[tuple[int, int]] | None = None,
        exclude_highest_kv_usage: bool = False,
        kv_usage_provider: Callable[[Instance, Endpoint], float | None] | None = None,
    ) -> list[EndpointCandidate]:
        """
        Select top-K endpoints globally across all instances.

        ``is_blocked`` optional filter (instance_id) -> bool. Blocked instances are
        skipped during scoring (usually circuit-breaker OPEN instances from local PUB cache).

        ``excluded_pairs`` optional (instance_id, endpoint_id) pairs to skip during scoring.

        ``exclude_highest_kv_usage`` (with ``kv_usage_provider``, caller gated per role, e.g.
        D instances) enables KV cache pressure-aware scoring: instead of hard-excluding the
        highest-usage candidate, every candidate's score gains a penalty
        ``kv_usage * instance_score / endpoint_count``, so a heavily used instance is ranked
        later naturally. When the flag is off or no usage data is available for a candidate,
        the plain score is kept, preserving load-balance behavior as a fallback.

        Ties (equal score) are broken uniformly at random instead of always preferring the first
        node, so equal-load requests spread across nodes instead of piling onto node 0.
        """
        if top_k <= 0:
            return []
        if not isinstance(instances, (list, tuple)):
            instances = list(instances)
        if not instances:
            return []

        scored: list[tuple[float, float, EndpointCandidate]] = []
        for instance in instances:
            for endpoint in instance.get_all_endpoints():
                if is_blocked is not None and is_blocked(instance.id):
                    continue
                if excluded_pairs is not None and (instance.id, endpoint.id) in excluded_pairs:
                    continue
                try:
                    score = LoadBalancePolicy.calculate_endpoint_score(
                        instance,
                        endpoint,
                        role=role,
                        instance_score_weight=instance_score_weight,
                    )
                    if exclude_highest_kv_usage and kv_usage_provider is not None:
                        score = LoadBalancePolicy._apply_kv_usage_penalty(
                            score,
                            instance,
                            endpoint,
                            role=role,
                            kv_usage_provider=kv_usage_provider,
                        )
                except Exception as e:
                    logger.warning(
                        "Failed to calculate endpoint score for instance %s endpoint %s: %s",
                        instance.id,
                        endpoint.id,
                        e,
                    )
                    continue
                scored.append((score, random.random(), EndpointCandidate(instance, endpoint, score)))  # nosec B311 -- 并列随机打散
        best = heapq.nsmallest(top_k, scored, key=lambda item: (item[0], item[1]))
        return [candidate for _, _, candidate in best]

    @staticmethod
    def _apply_kv_usage_penalty(
        score: float,
        instance: Instance,
        endpoint: Endpoint,
        *,
        role: PDRole | str | None = None,
        kv_usage_provider: Callable[[Instance, Endpoint], float | None],
    ) -> float:
        """Add a KV cache pressure penalty to an endpoint's score.

        Penalty = kv_usage * instance_score / endpoint_count: an endpoint whose instance KV
        cache is heavily used is penalized proportionally to the instance's own workload
        pressure, averaged per endpoint so larger DP instances are not over-penalized. When
        no usage data is available the plain score is returned unchanged.
        """
        try:
            usage = kv_usage_provider(instance, endpoint)
        except Exception as e:
            logger.warning(
                "Failed to get KV cache usage for instance %s endpoint %s: %s",
                instance.id,
                endpoint.id,
                e,
            )
            return score
        if usage is None:
            return score
        instance_score = instance.gathered_workload.calculate_workload_score()
        endpoint_count = max(1, len(instance.get_all_endpoints()))
        return score + usage * (instance_score / endpoint_count)

    @staticmethod
    def select_endpoint_from_list(
        instances: list[Instance] | Iterable[Instance],
        role: PDRole | None = None,
        instance_score_weight: float = DEFAULT_ENDPOINT_INSTANCE_SCORE_WEIGHT,
    ) -> tuple[Instance, Endpoint] | None:
        """Select one endpoint globally across all instances."""
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            instances,
            role=role,
            top_k=1,
            instance_score_weight=instance_score_weight,
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
                    workload_score = instance.gathered_workload.calculate_workload_score()
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
                workload_score = instance.gathered_workload.calculate_workload_score()
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
                workload_score = endpoint.workload.calculate_workload_score()
                if workload_score < min_workload:
                    min_workload = workload_score
                    selected_endpoint = endpoint
            except Exception as e:
                logger.warning("Failed to calculate workload score for endpoint %s: %s", endpoint.id, e)
                continue
        return selected_endpoint

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
