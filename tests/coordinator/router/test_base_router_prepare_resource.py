# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for BaseRouter resource preparation edge cases."""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from motor.common.logger.logger import _resolve_logger_name
from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload, WorkloadAction
from motor.common.resources.instance import Instance, InsStatus, PDRole, ParallelConfig
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.models.request import ReqState, RequestInfo
from motor.coordinator.router.strategies.base import BaseRouter

_ROUTER_LOGGER = _resolve_logger_name("motor.coordinator.router.strategies.base")


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
        status=EndpointStatus.NORMAL,
    )
    return ScheduledResource(instance=instance, endpoint=endpoint)


def _make_router(config: CoordinatorConfig | None = None) -> _TestRouter:
    return _TestRouter(
        _make_req_info(),
        config or CoordinatorConfig(),
        scheduler=MagicMock(),
        request_manager=MagicMock(),
    )


def _make_req_info(req_id: str = "req-1") -> RequestInfo:
    return RequestInfo(
        req_id=req_id,
        req_data={"messages": []},
        req_len=2,
        api="/v1/chat/completions",
    )


def test_infer_base_url_for_resource_brackets_ipv6_literal():
    router = _make_router()
    resource = _make_resource(PDRole.ROLE_D)
    resource.endpoint.ip = "2001:db8::1"

    assert router._infer_base_url_for_resource(resource) == "http://[2001:db8::1]:8080"


def test_infer_base_url_for_resource_keeps_ipv4_format():
    router = _make_router()
    resource = _make_resource(PDRole.ROLE_D)

    assert router._infer_base_url_for_resource(resource) == "http://127.0.0.1:8080"


@pytest.mark.asyncio
async def test_prepare_resource_rolls_back_scheduler_allocation_when_local_record_fails():
    config = CoordinatorConfig()
    config.exception_config.max_retry = 1
    req_info = _make_req_info()
    resource = _make_resource(PDRole.ROLE_E)
    allocated_workload = Workload(active_tokens=12)

    scheduler = MagicMock()
    scheduler.select_and_allocate = AsyncMock(return_value=(resource.instance, resource.endpoint, allocated_workload))
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
    assert params.workload_change == Workload(active_tokens=-12)


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


@pytest.mark.asyncio
async def test_prepare_resource_rolls_back_when_bookkeeping_cancelled():
    config = CoordinatorConfig()
    config.exception_config.max_retry = 1
    req_info = _make_req_info()
    resource = _make_resource(PDRole.ROLE_E)
    allocated_workload = Workload(active_tokens=12)

    scheduler = MagicMock()
    scheduler.select_and_allocate = AsyncMock(return_value=(resource.instance, resource.endpoint, allocated_workload))
    scheduler.update_workload = AsyncMock(return_value=True)
    request_manager = MagicMock()
    request_manager.add_req_workload = AsyncMock(side_effect=asyncio.CancelledError())
    router = _TestRouter(req_info, config, scheduler=scheduler, request_manager=request_manager)

    with pytest.raises(asyncio.CancelledError):
        await router.prepare_resource(PDRole.ROLE_E)

    scheduler.update_workload.assert_called_once()
    params = scheduler.update_workload.call_args.args[0]
    assert params.workload_change == Workload(active_tokens=-12)


@pytest.mark.asyncio
async def test_reclaim_residual_workloads_releases_and_logs(caplog):
    caplog.set_level(logging.ERROR, logger=_ROUTER_LOGGER)
    router = _make_router()
    router._request_manager.pop_residual_workloads = AsyncMock(
        return_value=[(("req-1", PDRole.ROLE_P), Workload(active_tokens=7), (1, 10))]
    )
    router._scheduler.update_workload = AsyncMock(return_value=True)

    await router._reclaim_residual_workloads()

    router._scheduler.update_workload.assert_called_once()
    params = router._scheduler.update_workload.call_args.args[0]
    assert params.instance_id == 1
    assert params.endpoint_id == 10
    assert params.role == PDRole.ROLE_P
    assert params.workload_action == WorkloadAction.RELEASE_TOKENS
    assert params.workload_change == Workload(active_tokens=-7)
    assert any(
        rec.levelno >= logging.ERROR and "Reclaiming orphan workload" in rec.getMessage() for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_reclaim_residual_workloads_skips_when_owner_missing(caplog):
    caplog.set_level(logging.ERROR, logger=_ROUTER_LOGGER)
    router = _make_router()
    router._request_manager.pop_residual_workloads = AsyncMock(
        return_value=[(("req-1", PDRole.ROLE_P), Workload(active_tokens=7), None)]
    )
    router._scheduler.update_workload = AsyncMock(return_value=True)

    await router._reclaim_residual_workloads()

    router._scheduler.update_workload.assert_not_called()
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


@pytest.mark.asyncio
async def test_reclaim_residual_workloads_skips_on_drain_failure(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=_ROUTER_LOGGER)
    router = _make_router()
    router._request_manager.pop_residual_workloads = AsyncMock(return_value=[])
    monkeypatch.setattr(router, "_drain_pending_releases", AsyncMock(side_effect=RuntimeError("boom")))

    await router._reclaim_residual_workloads()

    router._request_manager.pop_residual_workloads.assert_not_called()
    assert any(rec.levelno >= logging.ERROR for rec in caplog.records)


@pytest.mark.asyncio
async def test_forward_request_logs_dispatch_to_p_before_http(caplog):
    caplog.set_level(logging.INFO, logger=_ROUTER_LOGGER)
    router = _make_router()
    router._forward_resource = _make_resource(PDRole.ROLE_P)
    seen_before_post = []

    async def _post(*_args, **_kwargs):
        seen_before_post.extend(rec.getMessage() for rec in caplog.records if "stage=dispatch_to_p" in rec.getMessage())
        response = MagicMock()
        response.is_success = True
        response.status_code = 200
        response.content = b"{}"
        response.aclose = AsyncMock()
        return response

    client = MagicMock()
    client.base_url = "http://127.0.0.1:8080"
    client.timeout = 1
    client.post = _post

    await router.forward_request("v1/completions", {"prompt": "hi"}, client, 1)

    assert len(seen_before_post) == 1
    assert "elapsed_ms=" in seen_before_post[0]
    assert "role=prefill" in seen_before_post[0]
