# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

from typing import Callable

from threading import Lock
from motor.common.resources.instance import Instance, PDRole
from motor.common.resources.endpoint import Endpoint
from motor.coordinator.domain import InstanceProvider
from motor.coordinator.scheduler.policy.base import BaseSchedulingPolicy
from motor.common.logger import get_logger

logger = get_logger(__name__)


class RoundRobinPolicy(BaseSchedulingPolicy):
    """
    Round Robin Scheduler Policy implementation.
    Selects instances and endpoints in a round-robin fashion.
    Uses per-role instance counters so P and D are round-robin'd independently.
    """

    def __init__(self, instance_provider: InstanceProvider):
        super().__init__(instance_provider=instance_provider)
        self._instance_rr_counters: dict[PDRole | None, int] = {}
        self._endpoint_rr_counters: dict[int | str, int] = {}
        self._instance_lock = Lock()
        self._endpoint_lock = Lock()
        logger.info("RoundRobinPolicy started.")

    @staticmethod
    def select_instance_from_list(instances: list[Instance], counter: int) -> tuple[Instance | None, int]:
        """
        Round-robin select one instance from a list and return the next counter value.

        Args:
            instances: List of instances to choose from.
            counter: Current round-robin counter (will be incremented).

        Returns:
            (selected_instance, next_counter). selected_instance is None if list is empty.
        """
        if not instances:
            return (None, counter)
        idx = counter % len(instances)
        return (instances[idx], counter + 1)

    @staticmethod
    def select_endpoint_from_instance(
        instance: Instance,
        counters: dict[int | str, int],
        *,
        is_blocked: Callable[[int], bool] | None = None,
    ) -> Endpoint | None:
        """
        Round-robin select one endpoint from the instance; updates counters[instance.id] in place.
        Reusable by SchedulerClient etc.; caller holds counters so selection state is not shared.

        Args:
            instance: Instance to select an endpoint from.
            counters: Per-instance round-robin counters (mutated in place).
            is_blocked: Optional filter (instance_id) -> bool. Blocked instances are skipped.

        Returns:
            Selected Endpoint or None if no endpoints available.
        """
        if not instance:
            return None
        all_endpoints = instance.get_all_endpoints()
        if not all_endpoints:
            return None
        if is_blocked is not None:
            all_endpoints = [ep for ep in all_endpoints if not is_blocked(instance.id)]
            if not all_endpoints:
                return None
        if instance.id not in counters:
            counters[instance.id] = 0
        idx = counters[instance.id] % len(all_endpoints)
        ep = all_endpoints[idx]
        counters[instance.id] = (counters[instance.id] + 1) % len(all_endpoints)
        return ep

    def _select_instance(self, role: PDRole = None) -> Instance | None:
        """
        Select an instance using round-robin algorithm.
        Uses per-role counter so P and D are round-robin'd independently.
        """
        active_instances = list(self._instance_provider.get_available_instances(role).values())
        if not active_instances:
            logger.warning("No active instances available for scheduling")
            return None

        if role not in self._instance_rr_counters:
            self._instance_rr_counters[role] = 0
        with self._instance_lock:
            counter = self._instance_rr_counters[role]
            selected_instance, next_counter = RoundRobinPolicy.select_instance_from_list(active_instances, counter)
            self._instance_rr_counters[role] = next_counter % len(active_instances)
        return selected_instance

    def _select_endpoint(self, instance: Instance) -> Endpoint | None:
        """
        Select an endpoint from the given instance using round-robin algorithm.

        Args:
            instance: The instance to select an endpoint from

        Returns:
            Selected Endpoint or None if no endpoint available
        """
        if not instance:
            logger.warning("No instance provided for endpoint selection")
            return None

        all_endpoints = instance.get_all_endpoints()
        if not all_endpoints:
            logger.warning(f"No endpoints available in instance {instance.id}")
            return None

        with self._endpoint_lock:
            return RoundRobinPolicy.select_endpoint_from_instance(instance, self._endpoint_rr_counters)

    def select_instance_and_endpoint_from_list(
        self,
        instances: list[Instance],
        role: PDRole | None = None,
        req_info=None,
    ):
        """Round-robin within a capability-compatible subset."""
        del req_info
        if not instances:
            return None
        with self._instance_lock:
            counter = self._instance_rr_counters.get(role, 0)
            instance, next_counter = RoundRobinPolicy.select_instance_from_list(instances, counter)
            self._instance_rr_counters[role] = next_counter % len(instances)
        if instance is None:
            return None
        with self._endpoint_lock:
            endpoint = RoundRobinPolicy.select_endpoint_from_instance(instance, self._endpoint_rr_counters)
        return (instance, endpoint) if endpoint is not None else None
