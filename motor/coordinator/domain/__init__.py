# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Domain logic: contracts (protocols) and implementations (instance pool, request state, probe).
"""

__all__ = [
    "calculate_demand_workload",
    "InstanceManager",
    "InstanceProvider",
    "InstanceReadiness",
    "readiness_from_instances",
    "RequestManager",
    "ScheduledResource",
    "SchedulingFacade",
    "UpdateInstanceMode",
    "UpdateWorkloadParams",
    # probe
    "DaemonLivenessProvider",
    "is_master_from_role_shm",
    "LivenessProbe",
    "LivenessResult",
    "ReadinessProbe",
    "ReadinessProbeOutput",
    "ReadinessResult",
    "RoleHeartbeatResult",
    "RoleShmDaemonLivenessProvider",
]

from motor.coordinator.domain.workload_calculator import calculate_demand_workload
from motor.coordinator.domain.instance_manager import (
    InstanceManager,
    UpdateInstanceMode,
)
from motor.coordinator.domain.instance_provider import InstanceProvider
from motor.coordinator.domain.probe import (
    DaemonLivenessProvider,
    is_master_from_role_shm,
    LivenessProbe,
    LivenessResult,
    ReadinessProbe,
    ReadinessProbeOutput,
    ReadinessResult,
    RoleHeartbeatResult,
    RoleShmDaemonLivenessProvider,
)
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.domain.scheduling import (
    InstanceReadiness,
    readiness_from_instances,
    ScheduledResource,
    SchedulingFacade,
    UpdateWorkloadParams,
)
