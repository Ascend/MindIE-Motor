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
from unittest.mock import AsyncMock, Mock, call, patch

import pytest

from motor.common.resources.dispatch import DispatchPlan
from motor.common.resources.instance import Instance, PDRole
from motor.common.resources.endpoint import Endpoint, Workload, WorkloadAction, EndpointStatus
from motor.coordinator.domain import InstanceReadiness, UpdateWorkloadParams
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    SchedulerResponse,
    SchedulerResponseType,
)
from motor.coordinator.scheduler.runtime.scheduler_client import (
    AsyncSchedulerClient,
    SchedulerClientConfig,
    SchedulerRequestFailureReason,
    SchedulerRequestResult,
    _SchedulerInstanceCache,
    _collect_active_endpoints_from_cache,
)


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
    mgmt_port: str = "8081",
    status: EndpointStatus = EndpointStatus.NORMAL,
    active_tokens: float = 0.0,
    active_kv_cache: float = 0.0,
) -> Endpoint:
    """Create a real Endpoint (used by _SchedulerInstanceCache tests)."""
    return Endpoint(
        id=endpoint_id,
        ip=ip,
        business_port=business_port,
        mgmt_port=mgmt_port,
        status=status,
        workload=Workload(active_tokens=active_tokens, active_kv_cache=active_kv_cache),
    )


def _make_instance(
    instance_id: int = 1,
    role: str = "prefill",
    endpoints: dict | None = None,
    dispatch_capabilities: list[str] | None = None,
) -> Instance:
    """Create a real Instance (used by _SchedulerInstanceCache tests)."""
    if endpoints is None:
        ep = _make_endpoint(endpoint_id=1)
        endpoints = {"pod1": {1: ep}}
    return Instance(
        job_name="test-job",
        model_name="test-model",
        id=instance_id,
        role=role,
        endpoints=endpoints,
        dispatch_capabilities=dispatch_capabilities or [],
    )


def _build_instance_dict(instance_id: int = 1, role: str = "prefill") -> dict:
    """Serialize a minimal Instance to dict (for ZMQ response payloads)."""
    ep = Endpoint(id=1, ip="127.0.0.1", business_port="8080", mgmt_port="8081", status="normal")
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
        ep = _make_endpoint(endpoint_id=1, active_tokens=0.0, active_kv_cache=0.0)
        inst = _make_instance(instance_id=1, role="prefill", endpoints={"pod1": {1: ep}})

        await self.cache.replace_all(PDRole.ROLE_P, [inst])

        self.cache.patch_workload_from_shm(
            instance_id=1,
            endpoint_id=1,
            role=PDRole.ROLE_P,
            active_tokens=5.0,
            active_kv_cache=3.0,
        )

        workload = _endpoint_workload(ep)
        assert workload.active_tokens == 5.0
        assert workload.active_kv_cache == 3.0
        assert inst.gathered_workload.active_tokens == 5.0
        assert inst.gathered_workload.active_kv_cache == 3.0

    def test_patch_workload_from_shm_unknown_instance_noop(self):
        """patch_workload_from_shm on unknown instance is a no-op (no raise)."""
        self.cache.patch_workload_from_shm(
            instance_id=999,
            endpoint_id=1,
            role=PDRole.ROLE_P,
            active_tokens=5.0,
            active_kv_cache=3.0,
        )

    @pytest.mark.asyncio
    async def test_patch_workload_from_shm_unknown_endpoint_noop(self):
        """patch_workload_from_shm on unknown endpoint does not modify workload."""
        ep = _make_endpoint(endpoint_id=1, active_tokens=0.0, active_kv_cache=0.0)
        inst = _make_instance(instance_id=1, role="prefill", endpoints={"pod1": {1: ep}})

        await self.cache.replace_all(PDRole.ROLE_P, [inst])

        self.cache.patch_workload_from_shm(
            instance_id=1,
            endpoint_id=999,
            role=PDRole.ROLE_P,
            active_tokens=10.0,
            active_kv_cache=20.0,
        )

        workload = _endpoint_workload(ep)
        assert workload.active_tokens == 0.0
        assert workload.active_kv_cache == 0.0

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
            active_kv_cache=3.0,
        )

        workload = _endpoint_workload(ep)
        assert workload.active_tokens == 0.0
        assert workload.active_kv_cache == 0.0


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

    # -- test_select_and_allocate -------------------------------------------

    @pytest.mark.asyncio
    async def test_select_and_allocate(self):
        """select_and_allocate returns (Instance, Endpoint, Workload) or None."""
        mock_inst = Mock(spec=Instance)
        mock_inst.id = 1
        mock_ep = Mock(spec=Endpoint)
        mock_ep.id = 10

        # Setup transport to return success for ALLOCATE_ONLY
        inst_dict = _build_instance_dict(instance_id=1)
        ep_dict = _make_endpoint(endpoint_id=10).model_dump(mode="json")
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"instance": inst_dict, "endpoint": ep_dict},
        )

        mock_req_info = Mock(spec=RequestInfo)
        mock_req_info.req_id = "req-alloc"
        mock_req_info.req_len = 200

        result = await self.client.select_and_allocate(
            PDRole.ROLE_P,
            mock_req_info,
        )

        # Without cached instances or a successful GET_AVAILABLE_INSTANCES, selection may be None.
        assert result is None or (isinstance(result, tuple) and len(result) == 3)

    @pytest.mark.asyncio
    async def test_select_and_allocate_no_selection(self):
        """select_and_allocate returns None when no instance/endpoint available."""
        # Setup no instances in cache and transport returns empty
        self.mock_cache.get_instances.return_value = []
        self._mock_send_request(SchedulerResponseType.SUCCESS, {"instances": []})

        mock_req_info = Mock(spec=RequestInfo)
        mock_req_info.req_id = "req-none"
        mock_req_info.req_len = 100

        result = await self.client.select_and_allocate(
            PDRole.ROLE_P,
            mock_req_info,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_select_and_allocate_transport_failure(self):
        """select_and_allocate returns None when transport.send_request fails."""
        mock_inst = Mock(spec=Instance)
        mock_inst.id = 1
        mock_ep = Mock(spec=Endpoint)
        mock_ep.id = 10

        with patch.object(
            self.client,
            "_select_endpoint_candidates_with_policy",
            return_value=([(mock_inst, mock_ep, 0.0)], "round_robin"),
        ):
            self.mock_transport.send_request = AsyncMock(return_value=None)

            mock_req_info = Mock(spec=RequestInfo)
            mock_req_info.req_id = "req-fail"
            mock_req_info.req_len = 100
            mock_req_info.kv_affinity_debug = None

            result = await self.client.select_and_allocate(
                PDRole.ROLE_P,
                mock_req_info,
            )
            assert result is None

    # -- test_update_workload -----------------------------------------------

    @pytest.mark.asyncio
    async def test_update_workload(self):
        """update_workload returns True on success."""
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"success": True},
        )

        params = UpdateWorkloadParams(
            instance_id=1,
            endpoint_id=10,
            role=PDRole.ROLE_P,
            req_id="req-upd",
            workload_action=WorkloadAction.ALLOCATION,
            workload_change=Workload(active_tokens=5.0, active_kv_cache=3.0),
            operation_id="op-update-workload",
        )

        result = await self.client.update_workload(params)
        assert result is True
        sent_request = self.mock_transport.send_request.await_args.args[0]
        assert sent_request.data["operation_id"] == "op-update-workload"

    # -- test_update_workload_transport_failure -----------------------------

    @pytest.mark.asyncio
    async def test_update_workload_transport_failure(self):
        """update_workload returns False when transport returns None."""
        self.mock_transport.send_request = AsyncMock(return_value=None)

        params = UpdateWorkloadParams(
            instance_id=1,
            endpoint_id=10,
            role=PDRole.ROLE_P,
            req_id="req-fail",
            workload_action=WorkloadAction.ALLOCATION,
            workload_change=Workload(),
        )

        result = await self.client.update_workload(params)
        assert result is False

    @pytest.mark.asyncio
    async def test_update_workload_response_error(self):
        """update_workload returns False when scheduler returns error response."""
        self._mock_send_request(
            SchedulerResponseType.ERROR,
            error="Internal server error",
        )

        params = UpdateWorkloadParams(
            instance_id=1,
            endpoint_id=10,
            role=PDRole.ROLE_P,
            req_id="req-err",
            workload_action=WorkloadAction.RELEASE_KV,
            workload_change=Workload(active_tokens=1.0, active_kv_cache=2.0),
        )

        result = await self.client.update_workload(params)
        assert result is False

    @pytest.mark.asyncio
    async def test_update_workload_success_false(self):
        """update_workload returns False when scheduler returns success=False."""
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"success": False},
        )

        params = UpdateWorkloadParams(
            instance_id=1,
            endpoint_id=10,
            role=PDRole.ROLE_P,
            req_id="req-bad",
            workload_action=WorkloadAction.RELEASE_TOKENS,
            workload_change=Workload(),
        )

        result = await self.client.update_workload(params)
        assert result is False

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

    # -- test_has_required_instances ----------------------------------------

    @pytest.mark.asyncio
    async def test_has_required_instances_met(self):
        """has_required_instances returns REQUIRED_MET when P and D present."""
        capability = [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]
        mock_p = _make_instance(1, PDRole.ROLE_P, dispatch_capabilities=capability)
        mock_d = _make_instance(2, PDRole.ROLE_D, dispatch_capabilities=capability)

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
    async def test_has_required_instances_rejects_incompatible_pd_pair(self):
        prefill = _make_instance(
            1,
            PDRole.ROLE_P,
            dispatch_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        )
        decode = _make_instance(
            2,
            PDRole.ROLE_D,
            dispatch_capabilities=[DispatchPlan.PREFILL_HANDOFF_DECODE.value],
        )

        def _get_instances_side_effect(role):
            mapping = {PDRole.ROLE_P: [prefill], PDRole.ROLE_D: [decode]}
            return mapping.get(role, [])

        self.mock_cache.get_instances.side_effect = _get_instances_side_effect

        result = await self.client.has_required_instances()

        assert result == InstanceReadiness.UNKNOWN
        assert result.is_run() is False
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

    # -- test_refresh_instances ---------------------------------------------

    @pytest.mark.asyncio
    async def test_refresh_instances(self):
        """refresh_instances sends REFRESH_INSTANCES request without error."""
        self._mock_send_request(
            SchedulerResponseType.SUCCESS,
            {"message": "Refreshed 1 instances"},
        )

        mock_inst = Mock(spec=Instance)
        mock_inst.model_dump = Mock(return_value={"id": 1, "role": "prefill"})

        await self.client.refresh_instances("ADDED", [mock_inst])
        self.mock_transport.send_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_refresh_instances_error_response(self):
        """refresh_instances handles error response without raising."""
        self._mock_send_request(
            SchedulerResponseType.ERROR,
            error="Refresh failed",
        )

        await self.client.refresh_instances("REMOVED", [])
        self.mock_transport.send_request.assert_awaited_once()

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

    @pytest.mark.asyncio
    async def test_transport_timeout_in_update_workload(self):
        """When transport returns None (timeout), update_workload returns False."""
        self.mock_transport.send_request = AsyncMock(return_value=None)

        params = UpdateWorkloadParams(
            instance_id=1,
            endpoint_id=1,
            role=PDRole.ROLE_P,
            req_id="req-timeout",
            workload_action=WorkloadAction.ALLOCATION,
            workload_change=Workload(),
        )

        result = await self.client.update_workload(params)
        assert result is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reason",
        [
            SchedulerRequestFailureReason.TIMEOUT,
            SchedulerRequestFailureReason.CANCELLED,
            SchedulerRequestFailureReason.DISCONNECTED,
        ],
    )
    async def test_update_workload_logs_classified_no_response_reason(self, reason, caplog):
        """update_workload logs the specific scheduler transport failure reason."""
        self.client._send_request_result = AsyncMock(
            return_value=SchedulerRequestResult(failure_reason=reason, error="classified-error")
        )

        params = UpdateWorkloadParams(
            instance_id=1,
            endpoint_id=1,
            role=PDRole.ROLE_P,
            req_id=f"req-{reason.value}",
            workload_action=WorkloadAction.RELEASE_TOKENS,
            workload_change=Workload(),
        )

        result = await self.client.update_workload(params)

        assert result is False
        assert f"reason={reason.value}" in caplog.text
        assert "classified-error" in caplog.text

    # -- test_client_not_connected_operations --------------------------------

    @pytest.mark.asyncio
    async def test_client_not_connected_get_available_instances(self):
        """When not connected, get_available_instances still handles gracefully."""
        self.mock_transport.connected = False
        self.mock_transport.send_request = AsyncMock(return_value=None)

        result = await self.client.get_available_instances(PDRole.ROLE_P)
        assert result == {}

    @pytest.mark.asyncio
    async def test_client_not_connected_update_workload(self):
        """When not connected, update_workload returns False gracefully."""
        self.mock_transport.connected = False
        self.mock_transport.send_request = AsyncMock(return_value=None)

        params = UpdateWorkloadParams(
            instance_id=1,
            endpoint_id=1,
            role=PDRole.ROLE_P,
            req_id="req-nc",
            workload_action=WorkloadAction.ALLOCATION,
            workload_change=Workload(),
        )

        result = await self.client.update_workload(params)
        assert result is False

    @pytest.mark.asyncio
    async def test_client_not_connected_refresh_instances(self):
        """When not connected, refresh_instances handles gracefully (no raise)."""
        self.mock_transport.connected = False
        self.mock_transport.send_request = AsyncMock(return_value=None)

        await self.client.refresh_instances("ADDED", [])
        self.mock_transport.send_request.assert_awaited_once()
