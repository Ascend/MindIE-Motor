# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Build the authoritative Coordinator instance-status response."""

from typing import Any

from motor.common.resources.instance import InsStatus
from motor.coordinator.domain.circuit_breaker import CircuitBreakerManager
from motor.coordinator.domain.instance_manager import InstanceManager


_CB_CLOSED_VIEW = {
    "state": "closed",
    "trip_count": 0,
    "failure_count": 0,
    "current_timeout": 0.0,
}


def _controller_status_value(instance: Any) -> str:
    status = instance.status
    return status.value if hasattr(status, "value") else str(status)


def _circuit_breaker_view(state: Any | None) -> dict[str, Any]:
    if state is None:
        return dict(_CB_CLOSED_VIEW)
    return {
        "state": state.state,
        "trip_count": state.trip_count,
        "failure_count": state.failure_count,
        "current_timeout": state.current_timeout,
    }


def _summarize_instance(
    instance: Any,
    *,
    pool: str | None = None,
    circuit_breaker: dict[str, Any] | None = None,
) -> dict[str, Any]:
    endpoints: list[dict[str, Any]] = []
    for pod_eps in (instance.endpoints or {}).values():
        for endpoint in (pod_eps or {}).values():
            endpoints.append(
                {
                    "id": endpoint.id,
                    "ip": endpoint.ip,
                    "business_port": str(endpoint.business_port),
                    "headless": bool(getattr(endpoint, "headless", False)),
                }
            )
    pool_name = pool if pool is not None else "unknown"
    cb_view = circuit_breaker or dict(_CB_CLOSED_VIEW)
    status = _controller_status_value(instance)
    role = instance.role.value if hasattr(instance.role, "value") else instance.role
    healthy = pool_name == "available" and status == InsStatus.ACTIVE.value and cb_view.get("state") == "closed"
    return {
        "id": instance.id,
        "role": role,
        "job_name": instance.job_name,
        "model_name": instance.model_name,
        "status": status,
        "pool": pool_name,
        "healthy": healthy,
        "circuit_breaker": cb_view,
        "endpoints": endpoints,
    }


async def build_instance_status_response(
    instance_manager: InstanceManager,
    circuit_breaker_manager: CircuitBreakerManager | None,
) -> dict[str, Any]:
    """Return all tracked instances with pool and circuit-breaker health."""
    tracked = await instance_manager.snapshot_instances()
    summaries = [
        _summarize_instance(
            instance,
            pool=instance_manager.get_tracked_instance_pool(instance.id),
            circuit_breaker=_circuit_breaker_view(
                circuit_breaker_manager.get(instance.id) if circuit_breaker_manager is not None else None
            ),
        )
        for instance in tracked
    ]
    summaries.sort(key=lambda item: (item.get("role") or "", item.get("id") or 0))
    return {"count": len(summaries), "instances": summaries}
