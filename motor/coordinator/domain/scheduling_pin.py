# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Resolve pinned instance + endpoint from an available-instance pool."""

from __future__ import annotations

from typing import Callable, Mapping

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import Instance
from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy
from motor.coordinator.scheduler.policy.round_robin import RoundRobinPolicy


def resolve_pinned_instance(
    instances: Mapping[int, Instance],
    target_instance_id: int,
) -> Instance | None:
    return instances.get(target_instance_id)


def select_endpoint_for_instance(
    instance: Instance,
    *,
    scheduler_type: str = "round_robin",
    endpoint_rr_counters: dict[int, int] | None = None,
    is_blocked: Callable[[int], bool] | None = None,
) -> Endpoint | None:
    """Pick endpoint on a pinned instance (same rules as AsyncSchedulerClient).

    Args:
        is_blocked: Optional filter (instance_id) -> bool.
            Circuit-broken instances are skipped.
    """
    if not instance:
        return None
    st = scheduler_type or "round_robin"
    if st in ("load_balance", "kv_cache_affinity"):
        ep = LoadBalancePolicy.select_endpoint_from_instance(instance)
        if ep and is_blocked is not None and is_blocked(instance.id):
            ep = None
        if ep:
            return ep
        all_eps = instance.get_all_endpoints()
        if all_eps:
            if is_blocked is not None:
                all_eps = [e for e in all_eps if not is_blocked(instance.id)]
            return all_eps[0] if all_eps else None
        return None
    counters = endpoint_rr_counters if endpoint_rr_counters is not None else {}
    ep = RoundRobinPolicy.select_endpoint_from_instance(
        instance,
        counters,
        is_blocked=is_blocked,
    )
    return ep
