# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for AsyncSchedulerClient and _SchedulerInstanceCache."""

import asyncio
import os
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from motor.common.resources.http_msg_spec import EventType
from motor.common.resources.instance import Instance, InsStatus, PDRole, ParallelConfig
from motor.common.resources.endpoint import Endpoint, Workload, EndpointStatus, WorkloadAction
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain import InstanceReadiness
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.domain.scheduling import UpdateWorkloadParams
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    SchedulerResponse,
    SchedulerResponseType,
)
from motor.coordinator.scheduler.runtime.scheduler_client import (
    AsyncSchedulerClient,
    SchedulerClientConfig,
    _SchedulerInstanceCache,
    _collect_active_endpoints_from_cache,
)
from motor.coordinator.scheduler.runtime.workload_shm.native import (
    STATUS_BLOCKED,
    STATUS_OK,
    NativeWorkloadShmUnavailable,
    load_native_library,
)
from motor.coordinator.scheduler.runtime.workload_shm.reader import WorkloadSharedMemoryReader
from motor.coordinator.scheduler.runtime.workload_shm.writer import WorkloadSharedMemoryOwner


# ========================================================================
# Helper factories  (real pydantic objects, aligned with conftest.py)
# ========================================================================


def _endpoint_workload(ep: Endpoint) -> Workload:
    """Return endpoint workload without triggering pydantic FieldInfo pylint false positives."""
    return getattr(ep, "workload")


def _make_endpoint(
    endpoint_id: int = 1,
    ip: str = "127.0.0.1",
    business_port: str = "8080",
    status: EndpointStatus = EndpointStatus.NORMAL,
    active_tokens: float = 0.0,
) -> Endpoint:
    """Create a real Endpoint (used by _SchedulerInstanceCache tests)."""
    return Endpoint(
        id=endpoint_id,
        ip=ip,
        business_port=business_port,
        status=status,
        workload=Workload(active_tokens=active_tokens),
    )


def _make_instance(
    instance_id: int = 1,
    role: str = "prefill",
    endpoints: dict | None = None,
    engine_type: str | None = None,
    dispatch_capabilities: list[str] | None = None,
) -> Instance:
    """Create a real Instance (used by _SchedulerInstanceCache tests)."""
    if endpoints is None:
        ep = _make_endpoint(endpoint_id=1)
        endpoints = {"pod1": {1: ep}}
    return Instance(
        job_name="test-job",
        model_name="test-model",
        engine_type=engine_type,
        dispatch_capabilities=dispatch_capabilities or [],
        id=instance_id,
        role=role,
        endpoints=endpoints,
    )


def _build_instance_dict(instance_id: int = 1, role: str = "prefill") -> dict:
    """Serialize a minimal Instance to dict (for ZMQ response payloads)."""
    ep = Endpoint(id=1, ip="127.0.0.1", business_port="8080", status="normal")
    inst = Instance(
        job_name="test-job",
        model_name="test-model",
        id=instance_id,
        role=role,
        endpoints={"pod1": {1: ep}},
    )
    return inst.model_dump(mode="json")


def _build_mock_scheduler_response(
    response_type: str = SchedulerResponseType.SUCCESS,
    data: dict | None = None,
    error: str | None = None,
) -> Mock:
    """Build a Mock SchedulerResponse with given fields."""
    resp = Mock(spec=SchedulerResponse)
    resp.response_type = response_type
    resp.data = data or {}
    resp.error = error
    return resp


# ========================================================================
# Module-level function test
# ========================================================================


class TestCollectActiveEndpoints:
    """Tests for _collect_active_endpoints_from_cache."""

    def test_collect_active_endpoints_returns_normal_endpoints(self):
        """_collect_active_endpoints_from_cache extracts normal endpoints."""
        cache = _SchedulerInstanceCache()

        ep1 = _make_endpoint(endpoint_id=1, ip="10.0.0.1", business_port="8001")
        ep2 = _make_endpoint(endpoint_id=2, ip="10.0.0.2", business_port="8002")
        inst = _make_instance(
            instance_id=1,
            role="prefill",
            endpoints={"pod1": {1: ep1, 2: ep2}},
        )

        async def _init():
            await cache.replace_all(PDRole.ROLE_P, [inst])

        asyncio.run(_init())

        result = _collect_active_endpoints_from_cache(cache)
        assert ("10.0.0.1", "8001") in result
        assert ("10.0.0.2", "8002") in result

    def test_collect_active_endpoints_skips_non_normal(self):
        """_collect_active_endpoints_from_cache skips non-normal status endpoints."""
        cache = _SchedulerInstanceCache()

        ep_normal = _make_endpoint(endpoint_id=1, ip="10.0.0.1", business_port="8001")
        ep_initial = _make_endpoint(
            endpoint_id=2,
            ip="10.0.0.2",
            business_port="8002",
            status=EndpointStatus.INITIAL,
        )
        inst = _make_instance(
            instance_id=1,
            role="prefill",
            endpoints={"pod1": {1: ep_normal, 2: ep_initial}},
        )

        async def _init():
            await cache.replace_all(PDRole.ROLE_P, [inst])

        asyncio.run(_init())

        result = _collect_active_endpoints_from_cache(cache)
        assert ("10.0.0.1", "8001") in result
        assert ("10.0.0.2", "8002") not in result

    def test_collect_active_endpoints_skips_empty_instances(self):
        """_collect_active_endpoints_from_cache handles empty or None endpoints."""
        cache = _SchedulerInstanceCache()
        # Use _make_instance helper which creates valid Instance objects
        inst_empty = _make_instance(instance_id=99, role="prefill")

        async def _init():
            await cache.replace_all(PDRole.ROLE_P, [inst_empty])

        asyncio.run(_init())

        # Override endpoints to empty for testing
        inst_empty.endpoints = {}
        result = _collect_active_endpoints_from_cache(cache)
        assert result == []


# ========================================================================
# Tests for _SchedulerInstanceCache
# ========================================================================


class TestSchedulerInstanceCache:
    """Tests for _SchedulerInstanceCache (real Instance/Endpoint objects)."""

    # pylint: disable=attribute-defined-outside-init
    @pytest.fixture(autouse=True)
    def setup(self):
        self.cache = _SchedulerInstanceCache()

    # -- test_replace_all_and_get_instances ---------------------------------

    @pytest.mark.asyncio
    async def test_replace_all_and_get_instances(self):
        """replace_all stores instances per role; get_instances returns correct lists."""
        inst_p1 = _make_instance(instance_id=1, role="prefill")
        inst_p2 = _make_instance(instance_id=2, role="prefill")
        inst_d1 = _make_instance(instance_id=3, role="decode")

        await self.cache.replace_all(PDRole.ROLE_P, [inst_p1, inst_p2])
        await self.cache.replace_all(PDRole.ROLE_D, [inst_d1])

        p_list = self.cache.get_instances(PDRole.ROLE_P)
        assert len(p_list) == 2
        assert p_list[0].id == 1
        assert p_list[1].id == 2

        d_list = self.cache.get_instances(PDRole.ROLE_D)
        assert len(d_list) == 1
        assert d_list[0].id == 3

        u_list = self.cache.get_instances(PDRole.ROLE_U)
        assert u_list == []

    # -- test_patch_workload_from_shm ---------------------------------------

    @pytest.mark.asyncio
    async def test_patch_workload_from_shm(self):
        """patch_workload_from_shm updates the endpoint workload and gathers it."""
        ep = _make_endpoint(endpoint_id=1, active_tokens=0.0)
        inst = _make_instance(instance_id=1, role="prefill", endpoints={"pod1": {1: ep}})

        await self.cache.replace_all(PDRole.ROLE_P, [inst])

        self.cache.patch_workload_from_shm(
            instance_id=1,
            endpoint_id=1,
            role=PDRole.ROLE_P,
            active_tokens=5.0,
        )

        workload = _endpoint_workload(ep)
        assert workload.active_tokens == 5.0
        assert inst.gathered_workload.active_tokens == 5.0

    def test_patch_workload_from_shm_unknown_instance_noop(self):
        """patch_workload_from_shm on unknown instance is a no-op (no raise)."""
        self.cache.patch_workload_from_shm(
            instance_id=999,
            endpoint_id=1,
            role=PDRole.ROLE_P,
            active_tokens=5.0,
        )

    @pytest.mark.asyncio
    async def test_patch_workload_from_shm_unknown_endpoint_noop(self):
        """patch_workload_from_shm on unknown endpoint does not modify workload."""
        ep = _make_endpoint(endpoint_id=1, active_tokens=0.0)
        inst = _make_instance(instance_id=1, role="prefill", endpoints={"pod1": {1: ep}})

        await self.cache.replace_all(PDRole.ROLE_P, [inst])

        self.cache.patch_workload_from_shm(
            instance_id=1,
            endpoint_id=999,
            role=PDRole.ROLE_P,
            active_tokens=10.0,
        )

        workload = _endpoint_workload(ep)
        assert workload.active_tokens == 0.0

    @pytest.mark.asyncio
    async def test_patch_workload_from_shm_wrong_role_noop(self):
        """patch_workload_from_shm for the wrong role does not affect instance."""
        ep = _make_endpoint(endpoint_id=1)
        inst = _make_instance(instance_id=1, role="prefill", endpoints={"pod1": {1: ep}})

        await self.cache.replace_all(PDRole.ROLE_P, [inst])

        self.cache.patch_workload_from_shm(
            instance_id=1,
            endpoint_id=1,
            role=PDRole.ROLE_D,
            active_tokens=5.0,
        )

        workload = _endpoint_workload(ep)
        assert workload.active_tokens == 0.0


# ========================================================================
# Tests for AsyncSchedulerClient
# ========================================================================


class TestAsyncSchedulerClient:
    """Tests for AsyncSchedulerClient with mocked transport and cache."""

    # pylint: disable=attribute-defined-outside-init
    @pytest.fixture(autouse=True)
    def setup(self):
        # Patch _SchedulerTransport to avoid any ZMQ dependency
        patcher_transport = patch(
            "motor.coordinator.scheduler.runtime.scheduler_client._SchedulerTransport",
        )
        self.mock_transport = AsyncMock()
        self.mock_transport.connected = False
        self.mock_transport_cls = patcher_transport.start()
        self.mock_transport_cls.return_value = self.mock_transport

        # Patch _SchedulerInstanceCache for controlled instance/endpoint data
        patcher_cache = patch(
            "motor.coordinator.scheduler.runtime.scheduler_client._SchedulerInstanceCache",
        )
        self.mock_cache = Mock()
        self.mock_cache.replace_all = AsyncMock()
        self.mock_cache.get_instances.return_value = []
        self.mock_cache_cls = patcher_cache.start()
        self.mock_cache_cls.return_value = self.mock_cache

        self.config = SchedulerClientConfig(
            scheduler_address="ipc:///tmp/test_sock",
            timeout=5.0,
        )
        self.client = AsyncSchedulerClient(self.config)
        self.client._push_subscriber = None  # disable push subscriber

        # Default: transport.send_request returns a success with empty instances
        self._setup_default_send_request()

        yield

        patcher_transport.stop()
        patcher_cache.stop()

    # -- helpers ------------------------------------------------------------

    def _setup_default_send_request(self):
        """Configure transport.send_request to return empty-success by default."""
        resp = _build_mock_scheduler_response(
            SchedulerResponseType.SUCCESS,
            {"instances": []},
        )
        self.mock_transport.send_request = AsyncMock(return_value=resp)

    def _mock_send_request(self, response_type, data=None, error=None):
        """Replace transport.send_request with a specific mock return."""
        resp = _build_mock_scheduler_response(response_type, data, error)
        self.mock_transport.send_request = AsyncMock(return_value=resp)

    # -- test_connect_success -----------------------------------------------

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """connect returns True and connected is True on transport success."""

        async def _connect_and_set():
            self.mock_transport.connected = True
            return True

        self.mock_transport.connect = _connect_and_set
        # connect() calls _init_cache which calls get_available_instances -> send_request
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"instances": []},
        )
        result = await self.client.connect()
        assert result is True
        assert self.client.connected is True

    # -- test_connect_failure -----------------------------------------------

    @pytest.mark.asyncio
    async def test_connect_failure(self):
        """connect returns False and connected stays False on transport failure."""
        self.mock_transport.connect = AsyncMock(return_value=False)
        result = await self.client.connect()
        assert result is False
        assert self.client.connected is False
        self.mock_transport.connect.assert_awaited_once()

    # -- test_disconnect ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_disconnect(self):
        """disconnect sets connected to False."""

        async def _connect_and_set():
            self.mock_transport.connected = True
            return True

        self.mock_transport.connect = _connect_and_set
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"instances": []},
        )
        await self.client.connect()
        assert self.client.connected is True

        async def _set_transport_disconnected():
            self.mock_transport.connected = False

        self.mock_transport.disconnect = AsyncMock(side_effect=_set_transport_disconnected)
        await self.client.disconnect()
        assert self.client.connected is False

    # -- test_select_endpoint_candidates_fallback --------------------------------

    @pytest.mark.asyncio
    async def test_select_endpoint_candidates_returns_empty_on_transport_failure(self):
        """When cache miss and transport fails, candidate selection returns empty."""
        self.mock_cache.get_instances.return_value = []
        self.mock_transport.send_request = AsyncMock(return_value=None)  # transport fails

        mock_req_info = Mock(spec=RequestInfo)
        mock_req_info.req_id = "req-fallback"
        mock_req_info.req_len = 50

        result = await self.client._select_endpoint_candidates(
            mock_req_info,
            PDRole.ROLE_P,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_select_endpoint_candidates_filters_required_engine_type(self):
        """Decode candidate selection must not mix native engine protocols."""
        vllm = _make_instance(
            instance_id=1,
            role="decode",
            endpoints={"pod1": {1: _make_endpoint(endpoint_id=1)}},
            engine_type="vllm",
        )
        sglang = _make_instance(
            instance_id=2,
            role="decode",
            endpoints={"pod2": {2: _make_endpoint(endpoint_id=2)}},
            engine_type="sglang",
        )
        self.mock_cache.get_instances.return_value = [vllm, sglang]
        req_info = Mock(spec=RequestInfo)
        req_info.req_id = "req-engine-filter"
        req_info.req_len = 10

        candidates, _ = await self.client._select_endpoint_candidates_with_policy(
            req_info,
            PDRole.ROLE_D,
            required_engine_type=" SGLang ",
        )

        assert [(instance.id, endpoint.id) for instance, endpoint, _ in candidates] == [(2, 2)]

    @pytest.mark.asyncio
    async def test_select_endpoint_candidates_filters_required_dispatch_capability(self):
        """Decode co-location must preserve LB while excluding unsupported instances."""
        self.client._scheduler_type = "load_balance"
        unsupported = _make_instance(
            instance_id=1,
            role="decode",
            endpoints={"pod1": {1: _make_endpoint(endpoint_id=1, active_tokens=0)}},
            engine_type="vllm",
        )
        eligible_busy = _make_instance(
            instance_id=2,
            role="decode",
            endpoints={"pod2": {2: _make_endpoint(endpoint_id=2, active_tokens=5)}},
            engine_type="vllm",
            dispatch_capabilities=["decode_colocation"],
        )
        eligible_idle = _make_instance(
            instance_id=3,
            role="decode",
            endpoints={"pod3": {3: _make_endpoint(endpoint_id=3, active_tokens=1)}},
            engine_type="vllm",
            dispatch_capabilities=["decode_colocation"],
        )
        self.mock_cache.get_instances.return_value = [unsupported, eligible_busy, eligible_idle]
        req_info = Mock(spec=RequestInfo)
        req_info.req_id = "req-capability-filter"
        req_info.req_len = 10

        candidates, _ = await self.client._select_endpoint_candidates_with_policy(
            req_info,
            PDRole.ROLE_D,
            required_engine_type="vllm",
            required_dispatch_capability="decode_colocation",
        )

        assert [(instance.id, endpoint.id) for instance, endpoint, _ in candidates] == [(3, 3)]

    # -- test_get_available_instances ---------------------------------------

    @pytest.mark.asyncio
    async def test_get_available_instances(self):
        """get_available_instances returns dict of deserialized instances and updates cache."""
        inst_dict = _build_instance_dict(instance_id=1, role="prefill")
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"instances": [inst_dict]},
        )

        result = await self.client.get_available_instances(PDRole.ROLE_P)

        assert 1 in result
        assert isinstance(result[1], Instance)
        assert result[1].id == 1
        assert result[1].role == "prefill"
        self.mock_cache.replace_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_available_instances_empty(self):
        """get_available_instances returns {} and clears the requested role."""
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"instances": []},
        )

        result = await self.client.get_available_instances(PDRole.ROLE_P)
        assert result == {}
        self.mock_cache.replace_all.assert_awaited_once_with(PDRole.ROLE_P, [])

    @pytest.mark.asyncio
    async def test_get_available_instances_empty_all_roles_clears_topology_cache(self):
        """An empty full refresh removes every stale topology role."""
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"instances": []},
        )

        result = await self.client.get_available_instances(None)

        assert result == {}
        assert self.mock_cache.replace_all.await_args_list == [
            call(PDRole.ROLE_E, []),
            call(PDRole.ROLE_P, []),
            call(PDRole.ROLE_D, []),
            call(PDRole.ROLE_U, []),
        ]

    @pytest.mark.asyncio
    async def test_get_available_instances_transport_failure(self):
        """get_available_instances returns {} when transport fails."""
        self.mock_transport.send_request = AsyncMock(return_value=None)

        result = await self.client.get_available_instances(PDRole.ROLE_P)
        assert result == {}

    @pytest.mark.asyncio
    async def test_get_available_instances_error_response(self):
        """get_available_instances returns {} when scheduler returns error."""
        self._mock_send_request(
            SchedulerResponseType.ERROR,
            error="No available instances",
        )

        result = await self.client.get_available_instances(PDRole.ROLE_P)
        assert result == {}

    @pytest.mark.asyncio
    async def test_get_available_instance_roles_uses_cache_without_transport(self):
        """Topology role reads stay local and do not issue GET_AVAILABLE_INSTANCES."""
        mock_p = Mock(spec=Instance)
        mock_d = Mock(spec=Instance)

        def _get_instances_side_effect(role):
            mapping = {PDRole.ROLE_P: [mock_p], PDRole.ROLE_D: [mock_d]}
            return mapping.get(role, [])

        self.mock_cache.get_instances.side_effect = _get_instances_side_effect

        result = await self.client.get_available_instance_roles()

        assert result == {PDRole.ROLE_P, PDRole.ROLE_D}
        self.mock_transport.send_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_local_instances_uses_cache_without_transport(self):
        """A warm cache is the local instance view; do not issue GET_AVAILABLE_INSTANCES."""
        mock_p = _make_instance(1, "prefill")

        def _get_instances_side_effect(role):
            return [mock_p] if role == PDRole.ROLE_P else []

        self.mock_cache.get_instances.side_effect = _get_instances_side_effect

        result = await self.client.get_local_instances(PDRole.ROLE_P)

        assert result[1] is mock_p
        self.mock_transport.send_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_local_instances_warms_up_when_cache_empty(self):
        """An empty local view may warm-up once via GET_AVAILABLE_INSTANCES."""
        inst_dict = _build_instance_dict(instance_id=7, role="prefill")
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"instances": [inst_dict]},
        )
        cached: dict[PDRole, list] = {
            PDRole.ROLE_E: [],
            PDRole.ROLE_P: [],
            PDRole.ROLE_D: [],
            PDRole.ROLE_U: [],
        }

        async def _replace_all(role, instances):
            cached[role] = list(instances)

        self.mock_cache.replace_all = AsyncMock(side_effect=_replace_all)
        self.mock_cache.get_instances.side_effect = lambda role: cached.get(role, [])

        result = await self.client.get_local_instances(PDRole.ROLE_P)

        self.mock_transport.send_request.assert_awaited_once()
        assert 7 in result
        assert result[7].id == 7

    # -- test_has_required_instances ----------------------------------------

    @pytest.mark.asyncio
    async def test_has_required_instances_met(self):
        """has_required_instances returns REQUIRED_MET when P and D present."""
        mock_p = _make_instance(1, PDRole.ROLE_P)
        mock_d = _make_instance(2, PDRole.ROLE_D)

        def _get_instances_side_effect(role):
            mapping = {PDRole.ROLE_P: [mock_p], PDRole.ROLE_D: [mock_d]}
            return mapping.get(role, [])

        self.mock_cache.get_instances.side_effect = _get_instances_side_effect

        result = await self.client.has_required_instances()
        assert result == InstanceReadiness.REQUIRED_MET
        assert result.is_ready() is True

    @pytest.mark.asyncio
    async def test_has_required_instances_only_prefill(self):
        """has_required_instances returns ONLY_PREFILL when only P present."""
        mock_p = _make_instance(1, PDRole.ROLE_P)

        def _get_instances_side_effect(role):
            mapping = {PDRole.ROLE_P: [mock_p], PDRole.ROLE_D: []}
            return mapping.get(role, [])

        self.mock_cache.get_instances.side_effect = _get_instances_side_effect

        result = await self.client.has_required_instances()
        assert result == InstanceReadiness.ONLY_PREFILL
        assert result.is_ready() is False
        self.mock_transport.send_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_has_required_instances_union_wins_over_partial_roles(self):
        decode = _make_instance(1, PDRole.ROLE_D)
        union = _make_instance(2, PDRole.ROLE_U)

        def _get_instances_side_effect(role):
            mapping = {PDRole.ROLE_D: [decode], PDRole.ROLE_U: [union]}
            return mapping.get(role, [])

        self.mock_cache.get_instances.side_effect = _get_instances_side_effect

        result = await self.client.has_required_instances()

        assert result == InstanceReadiness.REQUIRED_MET
        assert result.is_run() is True

    @pytest.mark.asyncio
    async def test_has_required_instances_none(self):
        """has_required_instances returns NONE when no instances."""
        self.mock_cache.get_instances.return_value = []

        result = await self.client.has_required_instances()
        assert result == InstanceReadiness.NONE
        assert result.is_ready() is False

    # -- test_get_all_instances ---------------------------------------------

    @pytest.mark.asyncio
    async def test_get_all_instances(self):
        """get_all_instances returns empty tuple (interface compat in async mode)."""
        decouple, encode = await self.client.get_all_instances()
        assert decouple == {}
        assert encode == {}

    # -- test_on_instance_change_notify ------------------------------------

    @pytest.mark.asyncio
    async def test_on_instance_change_notify(self):
        """_on_instance_change_notify calls get_available_instances and updates version."""
        self.client._last_instance_version = None

        inst_dict = _build_instance_dict(instance_id=1, role="prefill")
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"instances": [inst_dict]},
        )

        await self.client._on_instance_change_notify(version=5)

        assert self.client._last_instance_version == 5
        self.mock_transport.send_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_instance_change_notify_dedup(self):
        """_on_instance_change_notify skips refresh when version unchanged."""
        self.client._last_instance_version = 3
        self.mock_transport.send_request.reset_mock()
        self.mock_transport.send_request = AsyncMock(return_value=None)

        await self.client._on_instance_change_notify(version=3)

        self.mock_transport.send_request.assert_not_called()

    @pytest.mark.skip(reason="Endpoint status resolution in model_validate needs investigation")
    @pytest.mark.asyncio
    async def test_on_instance_change_notify_calls_refresh_callback(self):
        """_on_instance_change_notify invokes on_instance_refreshed when set."""
        self.client._last_instance_version = None
        on_refreshed = AsyncMock()
        self.client._on_instance_refreshed = on_refreshed

        inst_dict = _build_instance_dict(instance_id=1, role="prefill")
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"instances": [inst_dict]},
        )

        await self.client._on_instance_change_notify(version=1)

        on_refreshed.assert_awaited_once()

    # -- test_select_endpoint_candidates_by_load_balance --------------------

    def test_select_endpoint_candidates_by_load_balance(self):
        """_select_endpoint_candidates_by_load_balance returns lowest-workload endpoint."""
        client = self.client
        client._client_index = 0
        client._client_count = 1

        ep1 = _make_endpoint(endpoint_id=1, active_tokens=10.0)
        ep2 = _make_endpoint(endpoint_id=2, active_tokens=5.0)
        inst1 = _make_instance(instance_id=1, role="prefill", endpoints={"pod1": {1: ep1}})
        inst2 = _make_instance(instance_id=2, role="prefill", endpoints={"pod2": {2: ep2}})

        result = client._select_endpoint_candidates_by_load_balance(
            [inst1, inst2],
            PDRole.ROLE_P,
            top_k=1,
        )
        assert len(result) == 1
        selected_instance, selected_endpoint, _score = result[0]
        assert selected_instance.id == 2
        assert selected_endpoint.id == 2

    # -- test_transport_timeout ---------------------------------------------

    @pytest.mark.asyncio
    async def test_transport_timeout_in_get_available_instances(self):
        """When transport returns None (timeout), get_available_instances returns {}."""
        self.mock_transport.send_request = AsyncMock(return_value=None)

        result = await self.client.get_available_instances(PDRole.ROLE_P)
        assert result == {}

    # -- test_client_not_connected_operations --------------------------------

    @pytest.mark.asyncio
    async def test_client_not_connected_get_available_instances(self):
        """When not connected, get_available_instances still handles gracefully."""
        self.mock_transport.connected = False
        self.mock_transport.send_request = AsyncMock(return_value=None)

        result = await self.client.get_available_instances(PDRole.ROLE_P)
        assert result == {}

    @pytest.mark.asyncio
    async def test_update_workload_rejects_allocation_without_cas(self):
        """Release-only gate must fire before cas_sub_floor0, even with a stub native handle."""
        native = Mock()
        reader = Mock()
        reader.native = native
        reader.entry_meta.return_value = {"generation": 0, "active_tokens": 5.0}
        self.client._workload_reader = reader
        ok = await self.client.update_workload(
            UpdateWorkloadParams(
                instance_id=1,
                endpoint_id=10,
                role=PDRole.ROLE_P,
                req_id="req-alloc",
                workload_action=WorkloadAction.ALLOCATION,
                workload_change=Workload(active_tokens=4.0),
            )
        )
        assert ok is False
        native.cas_sub_floor0.assert_not_called()


def _cas_shm_name(tag: str) -> str:
    return f"mw{os.getpid()}{tag}"[:24]


def _make_cas_instance(instance_id: int, endpoint_id: int) -> Instance:
    inst = Instance(
        job_name=f"p-{instance_id}",
        model_name="test_model",
        id=instance_id,
        role=PDRole.ROLE_P,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1),
    )
    inst.add_endpoints(
        f"pod-{instance_id}",
        {
            0: Endpoint(
                id=endpoint_id,
                ip=f"10.0.0.{instance_id}",
                business_port="8080",
                status=EndpointStatus.NORMAL,
                workload=Workload(),
            )
        },
    )
    return inst


def _seed_shm_tokens(writer: WorkloadSharedMemoryOwner, instance_id: int, endpoint_id: int, tokens: float) -> None:
    """CAS-seed SHM after snapshot. ADD clears IM, so fixture tokens never reach a new pair."""
    header = writer.native.read_header()
    for slot in range(int(header.get("entry_count", 0) or 0)):
        entry = writer.native.load_entry(slot)
        if int(entry["instance_id"]) == instance_id and int(entry["endpoint_id"]) == endpoint_id:
            status, actual = writer.native.cas_add(instance_id, endpoint_id, int(entry["generation"]), 0.0, tokens)
            assert status == STATUS_OK
            assert actual == tokens
            return
    raise AssertionError(f"missing slot for ({instance_id}, {endpoint_id})")


@pytest.fixture
def native_lib():
    try:
        return load_native_library()
    except NativeWorkloadShmUnavailable as e:
        pytest.skip(f"native workload-shm library not built: {e}")
        return None


async def _client_with_shm(im: InstanceManager, name: str) -> tuple[AsyncSchedulerClient, WorkloadSharedMemoryOwner]:
    writer = WorkloadSharedMemoryOwner(im, max_entries=8, shm_name=name)
    writer.write_snapshot()
    client = AsyncSchedulerClient(
        SchedulerClientConfig(scheduler_type="load_balance", endpoint_instance_score_weight=0.0)
    )
    cache = _SchedulerInstanceCache()
    instances = list(im.get_available_instances(PDRole.ROLE_P).values())
    await cache.replace_all(PDRole.ROLE_P, instances)
    client._cache = cache
    reader = WorkloadSharedMemoryReader(name)
    reader.attach()
    client._workload_reader = reader
    return client, writer


class TestSelectAndAllocateCas:
    """Local scoring + schema-4 CAS. Does not mock send_request."""

    @pytest.mark.asyncio
    async def test_select_and_allocate_cas_commits_lowest_load(self, native_lib):
        """Read SHM, score with LoadBalance, CAS-add, return (Instance, Endpoint, Workload)."""
        del native_lib
        config = CoordinatorConfig()
        im = InstanceManager(config)
        await im.refresh_instances(
            EventType.ADD,
            [_make_cas_instance(1, 10), _make_cas_instance(2, 20)],
        )
        name = _cas_shm_name("al")
        client, writer = await _client_with_shm(im, name)
        _seed_shm_tokens(writer, 1, 10, 1.0)
        _seed_shm_tokens(writer, 2, 20, 50.0)
        try:
            req = RequestInfo(req_id="req-cas", req_data={}, req_len=8, api="completions", token_ids=[1, 2, 3, 4])
            result = await client.select_and_allocate(PDRole.ROLE_P, req)
            assert result is not None
            instance, endpoint, committed = result
            assert instance.id == 1
            assert endpoint.id == 10
            assert committed.active_tokens == pytest.approx(4.0)
            meta = client._workload_reader.entry_meta(1, 10)
            assert meta is not None
            assert meta["active_tokens"] == pytest.approx(5.0)
        finally:
            client._workload_reader.detach()
            writer.release()

    @pytest.mark.asyncio
    async def test_select_and_allocate_fast_path_refreshes_once(self, native_lib):
        """First CAS attempt must not redo the refresh candidate selection already did."""
        del native_lib
        config = CoordinatorConfig()
        im = InstanceManager(config)
        await im.refresh_instances(EventType.ADD, [_make_cas_instance(1, 10)])
        name = _cas_shm_name("frf")
        client, writer = await _client_with_shm(im, name)
        _seed_shm_tokens(writer, 1, 10, 1.0)
        orig_refresh = client._refresh_cache_from_workload_reader
        calls = {"n": 0}

        async def counting_refresh(*args, **kwargs):
            calls["n"] += 1
            return await orig_refresh(*args, **kwargs)

        client._refresh_cache_from_workload_reader = counting_refresh
        try:
            req = RequestInfo(req_id="req-frf", req_data={}, req_len=4, api="completions", token_ids=[1, 2, 3, 4])
            result = await client.select_and_allocate(PDRole.ROLE_P, req)
            assert result is not None
            assert calls["n"] == 1
        finally:
            client._workload_reader.detach()
            writer.release()

    @pytest.mark.asyncio
    async def test_select_and_allocate_changed_reloads_and_rescores(self, native_lib):
        """Stale expected (CHANGED) must re-score on the fresh vector, not blindly add on the old winner."""
        del native_lib
        config = CoordinatorConfig()
        im = InstanceManager(config)
        await im.refresh_instances(
            EventType.ADD,
            [_make_cas_instance(1, 10), _make_cas_instance(2, 20)],
        )
        name = _cas_shm_name("ch")
        client, writer = await _client_with_shm(im, name)
        _seed_shm_tokens(writer, 1, 10, 1.0)
        _seed_shm_tokens(writer, 2, 20, 8.0)
        native = client._workload_reader.native
        orig = native.cas_add
        calls = {"n": 0}

        def wrapped(iid, eid, gen, expected, delta, slot=None):
            calls["n"] += 1
            if calls["n"] == 1:
                orig(iid, eid, gen, expected, 80.0, slot=slot)
            return orig(iid, eid, gen, expected, delta, slot=slot)

        native.cas_add = wrapped
        try:
            req = RequestInfo(req_id="req-changed", req_data={}, req_len=4, api="completions", token_ids=[1, 2, 3, 4])
            result = await client.select_and_allocate(PDRole.ROLE_P, req)
            assert result is not None
            instance, endpoint, _committed = result
            assert instance.id == 2
            assert endpoint.id == 20
            assert calls["n"] >= 2
        finally:
            native.cas_add = orig
            client._workload_reader.detach()
            writer.release()

    @pytest.mark.asyncio
    async def test_select_and_allocate_blocked_excludes_pair_and_switches_candidate(self, native_lib):
        """CAS BLOCKED on the proposed pair must exclude it from the LB re-scan and pick the other
        healthy pair, instead of re-selecting it until the retry budget is exhausted.
        """
        del native_lib
        config = CoordinatorConfig()
        im = InstanceManager(config)
        await im.refresh_instances(
            EventType.ADD,
            [_make_cas_instance(1, 10), _make_cas_instance(2, 20)],
        )
        name = _cas_shm_name("blk")
        client, writer = await _client_with_shm(im, name)
        _seed_shm_tokens(writer, 1, 10, 1.0)  # globally lowest -> proposed
        _seed_shm_tokens(writer, 2, 20, 8.0)
        native = client._workload_reader.native
        orig = native.cas_add
        calls = {"n": 0}

        def wrapped(iid, eid, gen, expected, delta, slot=None):
            calls["n"] += 1
            if (iid, eid) == (1, 10):
                return (STATUS_BLOCKED, expected)
            return orig(iid, eid, gen, expected, delta, slot=slot)

        native.cas_add = wrapped
        try:
            req = RequestInfo(req_id="req-blocked", req_data={}, req_len=4, api="completions", token_ids=[1, 2, 3, 4])
            result = await client.select_and_allocate(PDRole.ROLE_P, req)
            assert result is not None
            instance, endpoint, _committed = result
            assert (instance.id, endpoint.id) == (2, 20)
            # Must switch on the second attempt, not exhaust retries re-selecting the excluded pair.
            assert calls["n"] == 2
        finally:
            native.cas_add = orig
            client._workload_reader.detach()
            writer.release()

    @pytest.mark.asyncio
    async def test_select_and_allocate_without_shm_returns_none(self):
        """Missing native attach fails closed (A7): no silent Python ledger."""
        client = AsyncSchedulerClient(SchedulerClientConfig(scheduler_type="load_balance"))
        req = RequestInfo(req_id="req-none", req_data={}, req_len=4, api="completions", token_ids=[1])
        assert await client.select_and_allocate(PDRole.ROLE_P, req) is None

    @pytest.mark.asyncio
    async def test_update_workload_cas_sub_floor0(self, native_lib):
        """Release path CAS-sub on the same slot allocate just filled."""
        del native_lib
        config = CoordinatorConfig()
        im = InstanceManager(config)
        await im.refresh_instances(EventType.ADD, [_make_cas_instance(1, 10)])
        name = _cas_shm_name("rl")
        client, writer = await _client_with_shm(im, name)
        _seed_shm_tokens(writer, 1, 10, 1.0)
        try:
            req = RequestInfo(req_id="req-rel", req_data={}, req_len=4, api="completions", token_ids=[1, 2, 3, 4])
            result = await client.select_and_allocate(PDRole.ROLE_P, req)
            assert result is not None
            instance, endpoint, committed = result
            ok = await client.update_workload(
                UpdateWorkloadParams(
                    instance_id=instance.id,
                    endpoint_id=endpoint.id,
                    role=PDRole.ROLE_P,
                    req_id="req-rel",
                    workload_action=WorkloadAction.RELEASE_TOKENS,
                    workload_change=Workload(active_tokens=-committed.active_tokens),
                )
            )
            assert ok is True
            meta = client._workload_reader.entry_meta(instance.id, endpoint.id)
            assert meta is not None
            assert meta["active_tokens"] == pytest.approx(1.0)
        finally:
            client._workload_reader.detach()
            writer.release()

    @pytest.mark.asyncio
    async def test_update_workload_cas_success_survives_cache_patch_failure(self, native_lib):
        """A cache-patch error after cas_sub_floor0 commits must not fail the release (would
        cause the caller to retry and subtract the same delta twice).
        """
        del native_lib
        config = CoordinatorConfig()
        im = InstanceManager(config)
        await im.refresh_instances(EventType.ADD, [_make_cas_instance(1, 10)])
        name = _cas_shm_name("rlp")
        client, writer = await _client_with_shm(im, name)
        _seed_shm_tokens(writer, 1, 10, 1.0)
        try:
            req = RequestInfo(req_id="req-relp", req_data={}, req_len=4, api="completions", token_ids=[1, 2, 3, 4])
            result = await client.select_and_allocate(PDRole.ROLE_P, req)
            assert result is not None
            instance, endpoint, committed = result
            native = client._workload_reader.native
            orig_cas_sub = native.cas_sub_floor0
            calls = {"n": 0}

            def counting_cas_sub(*args, **kwargs):
                calls["n"] += 1
                return orig_cas_sub(*args, **kwargs)

            native.cas_sub_floor0 = counting_cas_sub
            client._cache.patch_workload_from_shm = Mock(side_effect=RuntimeError("cache patch boom"))
            try:
                ok = await client.update_workload(
                    UpdateWorkloadParams(
                        instance_id=instance.id,
                        endpoint_id=endpoint.id,
                        role=PDRole.ROLE_P,
                        req_id="req-relp",
                        workload_action=WorkloadAction.RELEASE_TOKENS,
                        workload_change=Workload(active_tokens=-committed.active_tokens),
                    )
                )
                assert ok is True
                assert calls["n"] == 1
                meta = client._workload_reader.entry_meta(instance.id, endpoint.id)
                assert meta is not None
                assert meta["active_tokens"] == pytest.approx(1.0)
            finally:
                native.cas_sub_floor0 = orig_cas_sub
        finally:
            client._workload_reader.detach()
            writer.release()

    @pytest.mark.asyncio
    async def test_update_workload_rejects_non_release_action(self, native_lib):
        """update_workload is release-only; ALLOCATION must not subtract."""
        del native_lib
        config = CoordinatorConfig()
        im = InstanceManager(config)
        await im.refresh_instances(EventType.ADD, [_make_cas_instance(1, 10)])
        name = _cas_shm_name("aloc")
        client, writer = await _client_with_shm(im, name)
        try:
            req = RequestInfo(req_id="req-aloc", req_data={}, req_len=4, api="completions", token_ids=[1, 2, 3, 4])
            result = await client.select_and_allocate(PDRole.ROLE_P, req)
            assert result is not None
            instance, endpoint, committed = result
            before = client._workload_reader.entry_meta(instance.id, endpoint.id)["active_tokens"]
            ok = await client.update_workload(
                UpdateWorkloadParams(
                    instance_id=instance.id,
                    endpoint_id=endpoint.id,
                    role=PDRole.ROLE_P,
                    req_id="req-aloc",
                    workload_action=WorkloadAction.ALLOCATION,
                    workload_change=Workload(active_tokens=committed.active_tokens),
                )
            )
            assert ok is False
            meta = client._workload_reader.entry_meta(instance.id, endpoint.id)
            assert meta is not None
            assert meta["active_tokens"] == pytest.approx(before)
        finally:
            client._workload_reader.detach()
            writer.release()
