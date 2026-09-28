# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Scheduling policy factory: create policy instances by SchedulerType (OCP: new
policies only register, no Scheduler changes).
"""

from __future__ import annotations

from typing import Callable

from motor.config.coordinator import SchedulerType
from motor.coordinator.domain import InstanceProvider
from motor.coordinator.scheduler.policy.base import BaseSchedulingPolicy

# Type: takes InstanceProvider, returns policy instance
PolicyFactory = Callable[[InstanceProvider], BaseSchedulingPolicy]


def _create_round_robin(instance_provider: InstanceProvider) -> BaseSchedulingPolicy:
    from motor.coordinator.scheduler.policy.round_robin import RoundRobinPolicy

    return RoundRobinPolicy(instance_provider=instance_provider)


def _create_load_balance(instance_provider: InstanceProvider) -> BaseSchedulingPolicy:
    from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy

    return LoadBalancePolicy(instance_provider=instance_provider)


def _create_kv_cache_affinity(instance_provider: InstanceProvider) -> BaseSchedulingPolicy:
    from motor.coordinator.scheduler.policy.kv_cache_affinity import KvCacheAffinityPolicy

    return KvCacheAffinityPolicy(instance_provider=instance_provider)


def _create_dynamic_bucket(instance_provider: InstanceProvider) -> BaseSchedulingPolicy:
    from motor.coordinator.scheduler.policy.dynamic_bucket import DynamicBucketPolicy

    return DynamicBucketPolicy(instance_provider=instance_provider)


_REGISTRY: dict[SchedulerType, PolicyFactory] = {}


def register(scheduler_type: SchedulerType, factory: PolicyFactory) -> None:
    """Register SchedulerType -> policy factory."""
    _REGISTRY[scheduler_type] = factory


def create(
    scheduler_type: SchedulerType,
    instance_provider: InstanceProvider,
) -> BaseSchedulingPolicy:
    """
    Create policy instance from SchedulerType and instance provider.
    Unregistered type raises ValueError.
    """
    factory = _REGISTRY.get(scheduler_type)
    if factory is None:
        raise ValueError(f"Unsupported scheduling policy: {scheduler_type}")
    return factory(instance_provider)


class SchedulingPolicyFactory:
    """
    Policy factory facade: create policy by SchedulerType (OCP).
    """

    create = staticmethod(create)
    register = staticmethod(register)


# Register built-in policies
def _register_builtin() -> None:
    register(SchedulerType.ROUND_ROBIN, _create_round_robin)
    register(SchedulerType.LOAD_BALANCE, _create_load_balance)
    register(SchedulerType.KV_CACHE_AFFINITY, _create_kv_cache_affinity)
    register(SchedulerType.DYNAMIC_BUCKET, _create_dynamic_bucket)


_register_builtin()
