# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse

from motor.common.resources.instance import PDRole
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.fault_tolerance.probe.router_probe import InternalRouterProbe


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_runtime_guard_probe_uses_one_router_request_without_retries(monkeypatch, failed):
    config = CoordinatorConfig()
    config.exception_config.max_retry = 5
    config.exception_config.transport_max_retry = 4
    config.scheduler_config.enable_pd_separation_fallback_to_hybrid = True
    router_configs = []
    requests = []

    class Router:
        def __init__(self, req, router_config, **kwargs):
            router_configs.append(router_config)
            requests.append(req)

        async def handle_request(self):
            if failed:
                raise RuntimeError("failed")
            return JSONResponse({"choices": [{"message": {"content": "爱因斯坦"}}]})

    monkeypatch.setattr("motor.coordinator.router.dispatch.select_router_class", AsyncMock(return_value=Router))
    manager = type("Manager", (), {"generate_request_id": AsyncMock(return_value="probe")})()
    probe = InternalRouterProbe(config, scheduler=object(), request_manager=manager)
    outcome = await probe.run(
        p_instance_id=None, d_instance_id=7, d_endpoint_id=2, model="model", max_attempts=1, timeout_seconds=1
    )

    assert len(requests) == 1
    assert requests[0].scheduling_constraint.endpoint_for_role(PDRole.ROLE_D) == 2
    assert router_configs[0].exception_config.max_retry == 1
    assert router_configs[0].exception_config.transport_retry_limit == 1
    assert not router_configs[0].scheduler_config.enable_pd_separation_fallback_to_hybrid
    assert config.exception_config.max_retry == 5
    assert config.exception_config.transport_retry_limit == 4
    assert config.scheduler_config.enable_pd_separation_fallback_to_hybrid
    assert outcome.failures == int(failed)


@pytest.mark.asyncio
async def test_sampling_probe_keeps_configured_attempts(monkeypatch):
    config = CoordinatorConfig()
    requests = []

    class Router:
        def __init__(self, req, router_config, **kwargs):
            requests.append(router_config)

        async def handle_request(self):
            return JSONResponse({"choices": [{"message": {"content": "爱因斯坦"}}]})

    monkeypatch.setattr("motor.coordinator.router.dispatch.select_router_class", AsyncMock(return_value=Router))
    manager = type("Manager", (), {"generate_request_id": AsyncMock(return_value="probe")})()
    probe = InternalRouterProbe(config, scheduler=object(), request_manager=manager)
    outcome = await probe.run(p_instance_id=1, d_instance_id=7, model="model", max_attempts=3, timeout_seconds=1)
    assert requests == [config, config, config]
    assert outcome.failures == 0
