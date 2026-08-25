# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Domain logic: contracts (protocols) and implementations (instance pool, request state, probe).

Public symbols are loaded lazily so domain submodules can be imported by coordinator models
without the package initializer loading modules that depend on those models in return.
"""

from importlib import import_module
from typing import TYPE_CHECKING, Any

__all__ = [
    "calculate_demand_workload",
    "CircuitBreakerManager",
    "CircuitBreakerState",
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

if TYPE_CHECKING:
    from motor.coordinator.domain.circuit_breaker import CircuitBreakerManager, CircuitBreakerState
    from motor.coordinator.domain.instance_manager import InstanceManager, UpdateInstanceMode
    from motor.coordinator.domain.instance_provider import InstanceProvider
    from motor.coordinator.domain.probe import (
        DaemonLivenessProvider,
        LivenessProbe,
        LivenessResult,
        ReadinessProbe,
        ReadinessProbeOutput,
        ReadinessResult,
        RoleHeartbeatResult,
        RoleShmDaemonLivenessProvider,
        is_master_from_role_shm,
    )
    from motor.coordinator.domain.request_manager import RequestManager
    from motor.coordinator.domain.scheduling import (
        InstanceReadiness,
        ScheduledResource,
        SchedulingFacade,
        UpdateWorkloadParams,
        readiness_from_instances,
    )
    from motor.coordinator.domain.workload_calculator import calculate_demand_workload

_EXPORTS = {
    "calculate_demand_workload": ("motor.coordinator.domain.workload_calculator", "calculate_demand_workload"),
    "CircuitBreakerManager": ("motor.coordinator.domain.circuit_breaker", "CircuitBreakerManager"),
    "CircuitBreakerState": ("motor.coordinator.domain.circuit_breaker", "CircuitBreakerState"),
    "InstanceManager": ("motor.coordinator.domain.instance_manager", "InstanceManager"),
    "UpdateInstanceMode": ("motor.coordinator.domain.instance_manager", "UpdateInstanceMode"),
    "InstanceProvider": ("motor.coordinator.domain.instance_provider", "InstanceProvider"),
    "DaemonLivenessProvider": ("motor.coordinator.domain.probe", "DaemonLivenessProvider"),
    "is_master_from_role_shm": ("motor.coordinator.domain.probe", "is_master_from_role_shm"),
    "LivenessProbe": ("motor.coordinator.domain.probe", "LivenessProbe"),
    "LivenessResult": ("motor.coordinator.domain.probe", "LivenessResult"),
    "ReadinessProbe": ("motor.coordinator.domain.probe", "ReadinessProbe"),
    "ReadinessProbeOutput": ("motor.coordinator.domain.probe", "ReadinessProbeOutput"),
    "ReadinessResult": ("motor.coordinator.domain.probe", "ReadinessResult"),
    "RoleHeartbeatResult": ("motor.coordinator.domain.probe", "RoleHeartbeatResult"),
    "RoleShmDaemonLivenessProvider": ("motor.coordinator.domain.probe", "RoleShmDaemonLivenessProvider"),
    "RequestManager": ("motor.coordinator.domain.request_manager", "RequestManager"),
    "InstanceReadiness": ("motor.coordinator.domain.scheduling", "InstanceReadiness"),
    "readiness_from_instances": ("motor.coordinator.domain.scheduling", "readiness_from_instances"),
    "ScheduledResource": ("motor.coordinator.domain.scheduling", "ScheduledResource"),
    "SchedulingFacade": ("motor.coordinator.domain.scheduling", "SchedulingFacade"),
    "UpdateWorkloadParams": ("motor.coordinator.domain.scheduling", "UpdateWorkloadParams"),
}


def __getattr__(name: str) -> Any:
    """Load package-level exports only when callers request them."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, symbol_name = target
    value = getattr(import_module(module_name), symbol_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
