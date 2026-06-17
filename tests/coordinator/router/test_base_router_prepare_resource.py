# -*- coding: utf-8 -*-
"""Tests for BaseRouter resource preparation edge cases."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload, WorkloadAction
from motor.common.resources.instance import Instance, InsStatus, PDRole, ParallelConfig
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.models.request import ReqState, RequestInfo
from motor.coordinator.router.strategies.base import BaseRouter


class _TestRouter(BaseRouter):
    async def handle_request(self):
        return None


def _make_resource(role: PDRole) -> ScheduledResource:
    instance = Instance(
        job_name=f"{role.value}-1",
        model_name="m",
        id=1,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1),
    )
    endpoint = Endpoint(
        id=10,
        ip="127.0.0.1",
        business_port="8080",
        mgmt_port="9090",
        status=EndpointStatus.NORMAL,
    )
    return ScheduledResource(instance=instance, endpoint=endpoint)


def _make_req_info(req_id: str = "req-1") -> RequestInfo:
    return RequestInfo(
        req_id=req_id,
        req_data={"messages": []},
        req_len=2,
        api="/v1/chat/completions",
    )


@pytest.mark.asyncio
async def test_prepare_resource_rolls_back_scheduler_allocation_when_local_record_fails():
    config = CoordinatorConfig()
    config.exception_config.max_retry = 1
    req_info = _make_req_info()
    resource = _make_resource(PDRole.ROLE_E)
    allocated_workload = Workload(active_tokens=12, active_kv_cache=3)

    scheduler = MagicMock()
    scheduler.select_and_allocate = AsyncMock(
        return_value=(resource.instance, resource.endpoint, allocated_workload)
    )
    scheduler.update_workload = AsyncMock(return_value=True)
    request_manager = MagicMock()
    request_manager.add_req_workload = AsyncMock(return_value=False)
    router = _TestRouter(req_info, config, scheduler=scheduler, request_manager=request_manager)

    with pytest.raises(HTTPException):
        await router.prepare_resource(PDRole.ROLE_E)

    scheduler.update_workload.assert_called_once()
    params = scheduler.update_workload.call_args.args[0]
    assert params.instance_id == resource.instance.id
    assert params.endpoint_id == resource.endpoint.id
    assert params.role == PDRole.ROLE_E
    assert params.workload_action == WorkloadAction.RELEASE_TOKENS
    assert params.workload_change == Workload(active_tokens=-12, active_kv_cache=-3)


@pytest.mark.asyncio
async def test_prepare_resource_uses_encode_states_for_encode_role():
    config = CoordinatorConfig()
    config.exception_config.max_retry = 1
    req_info = _make_req_info()
    resource = _make_resource(PDRole.ROLE_E)

    scheduler = MagicMock()
    scheduler.select_and_allocate = AsyncMock(
        return_value=(resource.instance, resource.endpoint, Workload(active_tokens=5))
    )
    scheduler.update_workload = AsyncMock(return_value=True)
    request_manager = MagicMock()
    request_manager.add_req_workload = AsyncMock(return_value=True)
    router = _TestRouter(req_info, config, scheduler=scheduler, request_manager=request_manager)

    selected = await router.prepare_resource(PDRole.ROLE_E)

    assert selected == resource
    assert req_info.state == ReqState.E_ALLOCATED
    assert ReqState.E_SCHEDULING in req_info.status
    assert ReqState.E_ALLOCATED in req_info.status
    assert ReqState.D_SCHEDULING not in req_info.status
    assert ReqState.D_ALLOCATED not in req_info.status
