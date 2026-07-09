# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 license for more details.

"""Tests for HTTP pool refresh on instance change: collect_active_endpoints_from_cache and on_instance_refreshed callback."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from motor.common.resources.dispatch import DispatchPlan
from motor.common.resources.instance import Instance, PDRole
from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.coordinator.domain.scheduling import InstanceReadiness
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.scheduler.runtime.scheduler_client import (
    _collect_active_endpoints_from_cache,
    _SchedulerInstanceCache,
    AsyncSchedulerClient,
    SchedulerClientConfig,
)
from motor.coordinator.scheduler.runtime.zmq_protocol import SchedulerResponseType


def _make_instance(instance_id: int, role: PDRole, endpoints: list[tuple[str, str, EndpointStatus]]) -> Instance:
    """Create Instance with given endpoints (ip, port, status)."""
    inst = Instance(
        job_name=f"job-{instance_id}",
        model_name="test",
        id=instance_id,
        role=role.value,
    )
    ep_dict = {}
    for i, (ip, port, status) in enumerate(endpoints):
        ep_dict[i] = Endpoint(
            id=instance_id * 10 + i,
            ip=ip,
            business_port=port,
            mgmt_port=f"9{port}",
            status=status,
            workload=Workload(),
        )
    inst.add_endpoints(f"pod-{instance_id}", ep_dict)
    return inst


class TestCollectActiveEndpointsFromCache:
    """Test _collect_active_endpoints_from_cache filters by status=normal."""

    @pytest.mark.asyncio
    async def test_returns_only_normal_endpoints(self):
        """Endpoints with status=normal are included; others are excluded."""
        cache = _SchedulerInstanceCache()
        inst0 = _make_instance(
            0,
            PDRole.ROLE_E,
            [
                ("10.0.0.5", "8005", EndpointStatus.NORMAL),
            ],
        )
        inst1 = _make_instance(
            1,
            PDRole.ROLE_P,
            [
                ("10.0.0.1", "8001", EndpointStatus.NORMAL),
                ("10.0.0.2", "8002", EndpointStatus.ABNORMAL),
            ],
        )
        inst2 = _make_instance(
            2,
            PDRole.ROLE_D,
            [
                ("10.0.0.3", "8003", EndpointStatus.NORMAL),
                ("10.0.0.4", "8004", EndpointStatus.PAUSED),
            ],
        )
        await cache.replace_all(PDRole.ROLE_E, [inst0])
        await cache.replace_all(PDRole.ROLE_P, [inst1])
        await cache.replace_all(PDRole.ROLE_D, [inst2])

        result = _collect_active_endpoints_from_cache(cache)
        expected = {("10.0.0.5", "8005"), ("10.0.0.1", "8001"), ("10.0.0.3", "8003")}
        assert set(result) == expected

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_normal_endpoints(self):
        """When all endpoints are non-normal, returns empty list."""
        cache = _SchedulerInstanceCache()
        inst = _make_instance(
            1,
            PDRole.ROLE_P,
            [
                ("10.0.0.1", "8001", EndpointStatus.ABNORMAL),
                ("10.0.0.2", "8002", EndpointStatus.PAUSED),
            ],
        )
        await cache.replace_all(PDRole.ROLE_P, [inst])

        result = _collect_active_endpoints_from_cache(cache)
        assert result == []

    def test_returns_empty_for_empty_cache(self):
        """Empty cache returns empty list."""
        cache = _SchedulerInstanceCache()
        result = _collect_active_endpoints_from_cache(cache)
        assert result == []


class TestSchedulerClientEndpointTopK:
    """Test worker-side endpoint TopK selection without a persistent endpoint heap."""

    def test_load_balance_selects_topk_from_all_endpoints(self):
        config = SchedulerClientConfig(
            scheduler_address="ipc:///tmp/test_scheduler",
            scheduler_type="load_balance",
            endpoint_instance_score_weight=0.0,
        )
        client = AsyncSchedulerClient(config)
        inst1 = _make_instance(
            1,
            PDRole.ROLE_P,
            [
                ("10.0.0.1", "8001", EndpointStatus.NORMAL),
                ("10.0.0.2", "8002", EndpointStatus.NORMAL),
            ],
        )
        inst2 = _make_instance(
            2,
            PDRole.ROLE_P,
            [
                ("10.0.0.3", "8003", EndpointStatus.NORMAL),
                ("10.0.0.4", "8004", EndpointStatus.NORMAL),
            ],
        )
        workloads = {
            10: Workload(active_tokens=10),
            11: Workload(active_tokens=12),
            20: Workload(active_tokens=1),
            21: Workload(active_tokens=50),
        }
        for inst in (inst1, inst2):
            for endpoint in inst.get_all_endpoints():
                endpoint.workload = workloads[endpoint.id]

        req_info = RequestInfo(
            req_id="req-topk",
            req_data={"test": "data"},
            req_len=100,
            api="/test/api",
        )

        candidates, _ = client._select_endpoint_candidates_from_list_with_policy(
            [inst1, inst2], PDRole.ROLE_P, req_info, top_k=2
        )

        assert [(inst.id, ep.id) for inst, ep, _ in candidates] == [(2, 20), (1, 10)]


@pytest.mark.asyncio
async def test_has_required_instances_uses_cache_after_warmup():
    """Readiness warm-up should return status from the freshly refreshed cache."""
    config = SchedulerClientConfig(
        scheduler_address="ipc:///tmp/test_scheduler",
    )
    client = AsyncSchedulerClient(config)

    async def mock_get_available_instances(role=None):
        e_inst = _make_instance(
            1,
            PDRole.ROLE_E,
            [
                ("10.0.0.1", "8001", EndpointStatus.NORMAL),
            ],
        )
        p_inst = _make_instance(
            2,
            PDRole.ROLE_P,
            [
                ("10.0.0.2", "8002", EndpointStatus.NORMAL),
            ],
        )
        d_inst = _make_instance(
            3,
            PDRole.ROLE_D,
            [
                ("10.0.0.3", "8003", EndpointStatus.NORMAL),
            ],
        )
        p_inst.dispatch_capabilities = [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
        d_inst.dispatch_capabilities = [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
        await client._cache.replace_all(PDRole.ROLE_E, [e_inst])
        await client._cache.replace_all(PDRole.ROLE_P, [p_inst])
        await client._cache.replace_all(PDRole.ROLE_D, [d_inst])
        return {1: e_inst, 2: p_inst, 3: d_inst}

    with patch.object(client, "get_available_instances", side_effect=mock_get_available_instances):
        readiness = await client.has_required_instances()

    assert readiness == InstanceReadiness.REQUIRED_MET_EPD


@pytest.mark.asyncio
async def test_on_instance_refreshed_callback_invoked_on_version_change():
    """When instance_version changes and get_available_instances succeeds, callback is invoked."""
    pytest.importorskip("uvloop", reason="uvloop required for scheduler runtime (use WSL/Linux)")

    callback_called = []
    callback_endpoints = []

    async def mock_callback(active_endpoints: list[tuple[str, str]]) -> None:
        callback_called.append(True)
        callback_endpoints.append(active_endpoints)

    config = SchedulerClientConfig(
        scheduler_address="ipc:///tmp/test_scheduler",
        on_instance_refreshed=mock_callback,
    )
    client = AsyncSchedulerClient(config)
    client._workload_reader = MagicMock()
    client._workload_reader.read_and_patch_cache = MagicMock(return_value=(2, False))
    client._last_instance_version = 1

    async def mock_get_available_instances(role=None):
        inst = _make_instance(
            1,
            PDRole.ROLE_P,
            [
                ("10.0.0.1", "8001", EndpointStatus.NORMAL),
            ],
        )
        await client._cache.replace_all(PDRole.ROLE_P, [inst])
        return {1: inst}

    alloc_response = MagicMock()
    alloc_response.response_type = SchedulerResponseType.SUCCESS
    alloc_response.data = {
        "instance": {"id": 1, "job_name": "j", "model_name": "m", "role": "prefill"},
        "endpoint": {"id": 11, "ip": "10.0.0.1", "business_port": "8001", "mgmt_port": "9001", "status": "normal"},
    }

    with patch.object(client, "get_available_instances", side_effect=mock_get_available_instances):
        with patch.object(client, "_transport") as mock_transport:
            mock_transport.connected = True
            mock_transport.send_request = AsyncMock(return_value=alloc_response)
            req_info = RequestInfo(req_id="", req_data={"test": "data"}, req_len=100, api="/test/api")
            result = await client.select_and_allocate(PDRole.ROLE_P, req_info)

    assert result is not None
    ins, ep, workload = result
    assert ins is not None and ep is not None
    assert len(callback_called) == 1
    assert callback_endpoints[0] == [("10.0.0.1", "8001")]


@pytest.mark.asyncio
async def test_on_instance_refreshed_not_called_when_no_callback():
    """When on_instance_refreshed is None, no callback is invoked."""
    pytest.importorskip("uvloop", reason="uvloop required for scheduler runtime (use WSL/Linux)")

    config = SchedulerClientConfig(
        scheduler_address="ipc:///tmp/test_scheduler",
        on_instance_refreshed=None,
    )
    client = AsyncSchedulerClient(config)
    client._workload_reader = MagicMock()
    client._workload_reader.read_and_patch_cache = MagicMock(return_value=(2, False))
    client._last_instance_version = 1

    with patch.object(client, "get_available_instances", side_effect=AsyncMock(return_value={})):
        with patch.object(client, "_transport") as mock_transport:
            mock_transport.connected = True
            mock_transport.send_request = AsyncMock(return_value=None)
            req_info = RequestInfo(req_id="", req_data={"test": "data"}, req_len=100, api="/test/api")
            await client.select_and_allocate(PDRole.ROLE_P, req_info)

    assert client._on_instance_refreshed is None


@pytest.mark.asyncio
async def test_inference_server_callback_cleanup_and_warmup():
    """InferenceServer's on_instance_refreshed callback calls cleanup_unused_clients and warmup_clients."""
    pytest.importorskip("uvloop", reason="uvloop required for scheduler runtime (use WSL/Linux)")

    from motor.config.coordinator import CoordinatorConfig
    from motor.coordinator.api_server.inference_server import InferenceServer
    from motor.coordinator.domain.request_manager import RequestManager

    mock_pool = MagicMock()
    mock_pool.get_pool_keys_for_endpoints.return_value = {"10.0.0.1:8001:abc"}
    mock_pool.cleanup_unused_clients = AsyncMock(return_value=1)
    mock_pool.warmup_clients = AsyncMock(return_value={"10.0.0.1:8001:abc": True})

    with patch("motor.coordinator.api_server.inference_server.HTTPClientPool", return_value=mock_pool):
        config = CoordinatorConfig()
        request_manager = MagicMock(spec=RequestManager)
        server = InferenceServer(config=config, request_manager=request_manager)

        callback = server._make_on_instance_refreshed()
        await callback([("10.0.0.1", "8001")])

    mock_pool.get_pool_keys_for_endpoints.assert_called_once()
    mock_pool.cleanup_unused_clients.assert_awaited_once()
    mock_pool.warmup_clients.assert_awaited_once()
