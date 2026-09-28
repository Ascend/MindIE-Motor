# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Scheduling policies: LoadBalance, RoundRobin, DynamicBucket, and factory."""

__all__ = [
    "BaseSchedulingPolicy",
    "LoadBalancePolicy",
    "RoundRobinPolicy",
    "DynamicBucketPolicy",
    "SchedulingPolicyFactory",
]

from motor.coordinator.scheduler.policy.base import BaseSchedulingPolicy
from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy
from motor.coordinator.scheduler.policy.round_robin import RoundRobinPolicy
from motor.coordinator.scheduler.policy.dynamic_bucket import DynamicBucketPolicy
from motor.coordinator.scheduler.policy.factory import SchedulingPolicyFactory
