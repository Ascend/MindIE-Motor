# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Request, status

from motor.common.resources.dispatch import DispatchPlan
from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.common.resources.instance import InsStatus, Instance, ParallelConfig, PDRole
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain import InstanceReadiness
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.models.request import ReqState
from motor.coordinator.router.dispatch import handle_metaserver_request
from motor.coordinator.router.dispatch_session import AttemptState, AttemptStopReason, PDDispatchSession
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter
from motor.coordinator.scheduler.scheduler import Scheduler
from tests.coordinator.router.mock_openai_request import create_mock_request_info
from tests.coordinator.router.token_only_support import (
    TokenOnlyEngineClient,
    assert_completion_derender,
    assert_generate_requests,
    make_render_client,
    make_render_request_info,
)
from tests.coordinator.router.test_router_native_handoff import (
    _UnifiedPDDecodeClient,
    _UnifiedPDPrefillClient,
    _patch_unified_pd_clients,
)


def _instance(instance_id: int, role: PDRole, dispatch_plan: DispatchPlan) -> Instance:
    return Instance(
        job_name=f"test-job-{instance_id}",
        model_name=f"test-model-{instance_id}",
        engine_type="vllm",
        dispatch_capabilities=[dispatch_plan.value],
        id=instance_id,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1, tp_size=1),
        endpoints={},
    )


def _trigger_instance(instance_id: int, role: PDRole) -> Instance:
    return _instance(instance_id, role, DispatchPlan.CONCURRENT_ENGINE_SYNC)


def _handoff_instance(instance_id: int, role: PDRole) -> Instance:
    return _instance(instance_id, role, DispatchPlan.PREFILL_HANDOFF_DECODE)


async def _hanging_receive():
    await asyncio.Event().wait()


def _metaserver_raw_request(body: dict, attempt: str) -> MagicMock:
    raw_request = MagicMock(spec=Request)
    raw_request.json = AsyncMock(return_value=body)
    raw_request.query_params = {"attempt": attempt}
    raw_request.receive = AsyncMock(side_effect=_hanging_receive)
    return raw_request


class _BlockingPrefillClient(_UnifiedPDPrefillClient):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def post(self, path, json=None, headers=None, timeout=None):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return await super().post(path, json=json, headers=headers, timeout=timeout)


class TestRouterNativeTrigger:
    def _make_config(self) -> CoordinatorConfig:
        config = CoordinatorConfig()
        config.worker_metaserver_port = 12000
        return config

    def _make_router(self, req_info, monkeypatch, p_client, d_client, *, config=None, scheduler=None):
        config = config or self._make_config()
        scheduler = scheduler or Scheduler(instance_provider=InstanceManager(config), config=config)
        router_obj = UnifiedPDRouter(
            req_info,
            config,
            scheduler=scheduler,
            request_manager=RequestManager(config),
        )
        _patch_unified_pd_clients(monkeypatch, router_obj, p_client, d_client)
        return router_obj

    def _patch_instances(self, monkeypatch, instance_p, instance_d, endpoint_p, endpoint_d):
        def mock_get_available_instances(self, role=None):
            if role is None:
                return {instance_p.id: instance_p, instance_d.id: instance_d}
            if role == PDRole.ROLE_P:
                return {instance_p.id: instance_p}
            if role == PDRole.ROLE_D:
                return {instance_d.id: instance_d}
            return {}

        async def mock_select_and_allocate(self, role, req_info, *, target_instance_id=None, required_engine_type=None):
            del req_info, target_instance_id, required_engine_type
            if role == PDRole.ROLE_P:
                return instance_p, endpoint_p, Workload(active_tokens=1)
            if role == PDRole.ROLE_D:
                return instance_d, endpoint_d, Workload(active_tokens=1)
            return None

        async def mock_update_workload(self, params):
            del params
            return True

        monkeypatch.setattr(InstanceManager, "get_available_instances", mock_get_available_instances)
        monkeypatch.setattr(
            InstanceManager,
            "get_required_instances_status",
            lambda self: InstanceReadiness.REQUIRED_MET,
        )
        monkeypatch.setattr(Scheduler, "select_and_allocate", mock_select_and_allocate)
        monkeypatch.setattr(Scheduler, "update_workload", mock_update_workload)

    @pytest.fixture
    def trigger_pair(self, monkeypatch):
        host = "127.0.0.1"
        instance_p = _trigger_instance(0, PDRole.ROLE_P)
        endpoint_p = Endpoint(id=0, ip=host, business_port="8000", status=EndpointStatus.NORMAL)
        instance_p.endpoints = {host: {0: endpoint_p}}
        instance_d = _trigger_instance(1, PDRole.ROLE_D)
        endpoint_d = Endpoint(id=1, ip=host, business_port="8001", status=EndpointStatus.NORMAL)
        instance_d.endpoints = {host: {1: endpoint_d}}
        self._patch_instances(monkeypatch, instance_p, instance_d, endpoint_p, endpoint_d)
        return instance_p, instance_d, endpoint_p, endpoint_d

    @pytest.mark.asyncio
    async def test_trigger_sends_decode_first_with_metaserver(self, monkeypatch, trigger_pair):
        del trigger_pair
        p_client = _UnifiedPDPrefillClient()
        d_client = _UnifiedPDDecodeClient()
        req_info = await create_mock_request_info()
        router = self._make_router(req_info, monkeypatch, p_client, d_client)

        response = await router.handle_request()
        chunks = [chunk async for chunk in response.body_iterator]

        assert chunks
        assert p_client.requests == []
        assert len(d_client.requests) == 1
        decode_body = d_client.requests[0]
        assert decode_body["request_id"] == req_info.req_id
        assert decode_body["kv_transfer_params"]["do_remote_prefill"] is True
        assert decode_body["kv_transfer_params"]["do_remote_decode"] is False
        assert decode_body["kv_transfer_params"]["metaserver"].startswith("http://")
        assert "attempt=1" in decode_body["kv_transfer_params"]["metaserver"]
        assert req_info.state == ReqState.DECODE_END
        assert ReqState.D_ALLOCATED in req_info.status
        assert ReqState.P_ALLOCATED not in req_info.status

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("api", "req_data", "response_data", "request_field"),
        [
            (
                "v1/chat/completions",
                {
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "first"}],
                    "stream": False,
                    "max_tokens": 8,
                },
                {"choices": [{"message": {"role": "assistant", "content": "token response"}}]},
                "chat_request",
            ),
            (
                "/v1/completions",
                {"model": "test-model", "prompt": "first", "stream": False, "max_tokens": 8},
                {"choices": [{"index": 0, "text": "token response"}]},
                "completion_request",
            ),
        ],
    )
    async def test_nonstream_trigger_single_request_uses_token_only_decode_and_derender(
        self, monkeypatch, trigger_pair, api, req_data, response_data, request_field
    ):
        del trigger_pair
        p_client = _UnifiedPDPrefillClient()
        d_client = TokenOnlyEngineClient(PDRole.ROLE_D)
        req_info = make_render_request_info("test-id", req_data, api, [10, 20])
        render_client = AsyncMock()
        render_client.derender.return_value = response_data
        router = self._make_router(req_info, monkeypatch, p_client, d_client)
        router.set_render_client(render_client)

        response = await router.handle_request()

        assert response.status_code == 200
        assert d_client.paths == ["/inference/v1/generate"]
        assert (d_client.requests[0]["request_id"], p_client.requests) == (req_info.req_id, [])
        derender_request = render_client.derender.await_args.args[1]
        assert derender_request[request_field] == req_data

    @pytest.mark.asyncio
    async def test_nonstream_trigger_completion_batch_uses_unique_generate_ids(self, monkeypatch, trigger_pair):
        del trigger_pair
        p_client = _UnifiedPDPrefillClient()
        d_client = TokenOnlyEngineClient(PDRole.ROLE_D)
        req_data = {"model": "test-model", "prompt": ["first", "second"], "stream": False, "max_tokens": 8}
        req_info = make_render_request_info("test-id", req_data, "/v1/completions", [[10], [20, 21]])
        render_client = make_render_client(completion_count=2)
        router = self._make_router(req_info, monkeypatch, p_client, d_client)
        router.set_render_client(render_client)
        response = await router.handle_request()

        assert response.status_code == 200
        assert_generate_requests(
            d_client.requests,
            request_ids=[f"{req_info.req_id}#p{i}" for i in range(2)],
            prompt_token_ids=[[10], [20, 21]],
        )
        assert_completion_derender(render_client, prompt_lengths=[1, 2], response_count=2, original_request=req_data)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("api", "request_field", "request_value"),
        [
            ("v1/chat/completions", "messages", [{"role": "user", "content": "Hello"}]),
            ("v1/completions", "prompt", "Hello"),
        ],
    )
    async def test_stream_metaserver_uses_token_only_prefill(
        self, monkeypatch, trigger_pair, api, request_field, request_value
    ):
        del trigger_pair
        p_client = TokenOnlyEngineClient(PDRole.ROLE_P)
        config = self._make_config()
        request_data = {"model": "test-model", request_field: request_value, "stream": True, "max_tokens": 8}
        req_info = make_render_request_info("test-id", request_data, api, [10, 20, 30])
        request_manager = RequestManager(config)
        await request_manager.add_req_info(req_info)
        router = UnifiedPDRouter(
            req_info,
            config,
            scheduler=Scheduler(instance_provider=InstanceManager(config), config=config),
            request_manager=request_manager,
        )
        router.set_render_client(AsyncMock())
        _patch_unified_pd_clients(monkeypatch, router, p_client, _UnifiedPDDecodeClient())
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        router._bind_trigger_attempt(attempt)
        kv_params = {
            "request_id": req_info.req_id,
            "do_remote_decode": True,
            "remote_block_ids": [9, 8],
            "remote_host": "10.0.0.8",
            "remote_port": 15555,
        }

        await router.handle_metaserver_request(kv_params)

        assert p_client.paths == ["/inference/v1/generate"]

    @pytest.mark.asyncio
    async def test_select_coordination_mode_uses_cluster_trigger_when_allocate_caps_missing(
        self, monkeypatch, trigger_pair
    ):
        """Guards ALLOCATE_ONLY dropping caps: cluster detection must still drive TRIGGER."""
        del trigger_pair
        req_info = await create_mock_request_info()
        router = self._make_router(req_info, monkeypatch, _UnifiedPDPrefillClient(), _UnifiedPDDecodeClient())
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        assert attempt.decode_resource is not None
        attempt.decode_resource.instance.dispatch_capabilities = []

        from motor.coordinator.router.adapters.pd_protocol import CoordinationMode

        assert router._select_coordination_mode(attempt) == CoordinationMode.TRIGGER

    @pytest.mark.asyncio
    async def test_select_coordination_mode_trigger_caps_without_decode_returns_503(self, monkeypatch):
        """Handoff allocates P first; TRIGGER caps without D must fail-closed 503, not retry to 500."""
        host = "127.0.0.1"
        instance_p = _handoff_instance(0, PDRole.ROLE_P)
        endpoint_p = Endpoint(id=0, ip=host, business_port="8000", status=EndpointStatus.NORMAL)
        instance_p.endpoints = {host: {0: endpoint_p}}
        instance_d = _handoff_instance(1, PDRole.ROLE_D)
        endpoint_d = Endpoint(id=1, ip=host, business_port="8001", status=EndpointStatus.NORMAL)
        instance_d.endpoints = {host: {1: endpoint_d}}
        self._patch_instances(monkeypatch, instance_p, instance_d, endpoint_p, endpoint_d)

        req_info = await create_mock_request_info()
        router = self._make_router(req_info, monkeypatch, _UnifiedPDPrefillClient(), _UnifiedPDDecodeClient())
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        assert router._pd_uses_trigger is False
        assert attempt.decode_resource is None
        attempt.prefill_resource.instance.dispatch_capabilities = [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]

        with pytest.raises(HTTPException) as exc_info:
            router._select_coordination_mode(attempt)
        assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "decode instance" in str(exc_info.value.detail).lower()

    @pytest.mark.asyncio
    async def test_pd_cluster_uses_trigger_reads_local_instances_not_rpc(self, monkeypatch, trigger_pair):
        """Trigger detection must use the Worker-local instance view, not GET_AVAILABLE_INSTANCES."""
        del trigger_pair
        req_info = await create_mock_request_info()
        router = self._make_router(req_info, monkeypatch, _UnifiedPDPrefillClient(), _UnifiedPDDecodeClient())
        scheduler = router._scheduler
        local_spy = AsyncMock(side_effect=scheduler.get_local_instances)
        avail_spy = AsyncMock(side_effect=scheduler.get_available_instances)
        monkeypatch.setattr(scheduler, "get_local_instances", local_spy)
        monkeypatch.setattr(scheduler, "get_available_instances", avail_spy)

        assert await router._pd_cluster_uses_trigger() is True
        assert local_spy.await_count >= 1
        avail_spy.assert_not_awaited()

    @pytest.mark.parametrize(
        "pod_ip,configured_host,expected_url",
        [
            ("10.0.0.8", "0.0.0.0", "http://10.0.0.8:12000/v1/metaserver?attempt=3"),
            ("2001:db8::8", "::", "http://[2001:db8::8]:12000/v1/metaserver?attempt=3"),
            (None, "coordinator.example.com", "http://coordinator.example.com:12000/v1/metaserver?attempt=3"),
        ],
    )
    def test_trigger_metaserver_url_uses_reachable_host(self, monkeypatch, pod_ip, configured_host, expected_url):
        if pod_ip is None:
            monkeypatch.delenv("POD_IP", raising=False)
        else:
            monkeypatch.setenv("POD_IP", pod_ip)
        config = self._make_config()
        config.api_config.coordinator_api_host = configured_host
        router = UnifiedPDRouter(MagicMock(), config, scheduler=MagicMock(), request_manager=MagicMock())
        attempt = MagicMock(attempt_seq=3)

        assert router._trigger_metaserver_url(attempt) == expected_url

    @pytest.mark.parametrize("configured_host", ["0.0.0.0", "::"])
    def test_trigger_metaserver_url_rejects_unspecified_host_without_pod_ip(self, monkeypatch, configured_host, caplog):
        monkeypatch.delenv("POD_IP", raising=False)
        config = self._make_config()
        config.api_config.coordinator_api_host = configured_host
        router = UnifiedPDRouter(MagicMock(), config, scheduler=MagicMock(), request_manager=MagicMock())

        with caplog.at_level("ERROR"):
            with pytest.raises(HTTPException) as exc_info:
                router._trigger_metaserver_url(MagicMock(attempt_seq=1))

        assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert "POD_IP" in str(exc_info.value.detail)
        assert any(
            "callback host is unreachable" in record.getMessage() and configured_host in record.getMessage()
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_metaserver_forwards_prefill_with_remote_blocks(self, monkeypatch, trigger_pair):
        del trigger_pair
        p_client = _UnifiedPDPrefillClient()
        d_client = _UnifiedPDDecodeClient()
        config = self._make_config()
        req_info = await create_mock_request_info()
        request_manager = RequestManager(config)
        await request_manager.add_req_info(req_info)
        router = UnifiedPDRouter(
            req_info,
            config,
            scheduler=Scheduler(instance_provider=InstanceManager(config), config=config),
            request_manager=request_manager,
        )
        _patch_unified_pd_clients(monkeypatch, router, p_client, d_client)
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        router._bind_trigger_attempt(attempt)

        kv_params = {
            "request_id": f"chatcmpl-{req_info.req_id}",
            "do_remote_decode": True,
            "remote_block_ids": [9, 8],
            "remote_host": "10.0.0.8",
            "remote_port": 15555,
        }
        body = await router.handle_metaserver_request(kv_params)
        assert body["kv_transfer_params"]["do_remote_prefill"] is True
        assert len(p_client.requests) == 1
        prefill_body = p_client.requests[0]
        assert prefill_body["stream"] is False
        assert prefill_body["max_tokens"] == 1
        assert prefill_body["kv_transfer_params"]["remote_block_ids"] == [9, 8]
        assert prefill_body["kv_transfer_params"]["do_remote_decode"] is True
        assert ReqState.P_ALLOCATED in req_info.status

    @pytest.mark.asyncio
    async def test_metaserver_prefill_is_cancelled_with_attempt(self, monkeypatch, trigger_pair):
        del trigger_pair
        p_client = _BlockingPrefillClient()
        config = self._make_config()
        req_info = await create_mock_request_info()
        request_manager = RequestManager(config)
        await request_manager.add_req_info(req_info)
        router = UnifiedPDRouter(
            req_info,
            config,
            scheduler=Scheduler(instance_provider=InstanceManager(config), config=config),
            request_manager=request_manager,
        )
        _patch_unified_pd_clients(monkeypatch, router, p_client, _UnifiedPDDecodeClient())
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        router._bind_trigger_attempt(attempt)

        callback_task = asyncio.create_task(
            router.handle_metaserver_request(
                {
                    "request_id": req_info.req_id,
                    "do_remote_decode": True,
                    "remote_block_ids": [9, 8],
                    "remote_host": "10.0.0.8",
                    "remote_port": 15555,
                }
            )
        )
        await p_client.started.wait()
        try:
            await router._stop_attempt(attempt, AttemptStopReason.CLIENT_DISCONNECT)
            assert callback_task.done()
            assert p_client.cancelled.is_set()
            assert attempt.state == AttemptState.STOPPED
        finally:
            callback_task.cancel()
            await asyncio.gather(callback_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_metaserver_disconnect_stops_active_attempt(self, monkeypatch, trigger_pair):
        del trigger_pair
        p_client = _BlockingPrefillClient()
        config = self._make_config()
        req_info = await create_mock_request_info()
        request_manager = RequestManager(config)
        await request_manager.add_req_info(req_info)
        router = UnifiedPDRouter(
            req_info,
            config,
            scheduler=Scheduler(instance_provider=InstanceManager(config), config=config),
            request_manager=request_manager,
        )
        _patch_unified_pd_clients(monkeypatch, router, p_client, _UnifiedPDDecodeClient())
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        router._bind_trigger_attempt(attempt)

        callback_task = asyncio.create_task(
            router.handle_metaserver_request(
                {
                    "request_id": req_info.req_id,
                    "do_remote_decode": True,
                    "remote_block_ids": [9, 8],
                    "remote_host": "10.0.0.8",
                    "remote_port": 15555,
                }
            )
        )
        await p_client.started.wait()
        callback_task.cancel("decode metaserver connection closed")
        await asyncio.gather(callback_task, return_exceptions=True)

        assert attempt.state == AttemptState.STOPPED
        assert await request_manager.get_req_attempt_workload(req_info.req_id, 1, PDRole.ROLE_P) is None
        assert await request_manager.get_req_attempt_workload(req_info.req_id, 1, PDRole.ROLE_D) is None

    @pytest.mark.asyncio
    async def test_metaserver_retry_waits_for_first_prefill_and_is_idempotent(self, monkeypatch, trigger_pair):
        del trigger_pair
        p_client = _BlockingPrefillClient()
        config = self._make_config()
        req_info = await create_mock_request_info()
        request_manager = RequestManager(config)
        await request_manager.add_req_info(req_info)
        router = UnifiedPDRouter(
            req_info,
            config,
            scheduler=Scheduler(instance_provider=InstanceManager(config), config=config),
            request_manager=request_manager,
        )
        _patch_unified_pd_clients(monkeypatch, router, p_client, _UnifiedPDDecodeClient())
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        router._bind_trigger_attempt(attempt)
        kv_params = {
            "request_id": req_info.req_id,
            "do_remote_decode": True,
            "remote_block_ids": [9, 8],
            "remote_host": "10.0.0.8",
            "remote_port": 15555,
        }

        first_callback = asyncio.create_task(router.handle_metaserver_request(kv_params))
        await p_client.started.wait()
        retry_callback = asyncio.create_task(router.handle_metaserver_request(kv_params))
        await asyncio.sleep(0)
        assert not retry_callback.done()

        p_client.release.set()
        first_result, retry_result = await asyncio.gather(first_callback, retry_callback)

        assert first_result["kv_transfer_params"]["do_remote_prefill"] is True
        assert retry_result == {}
        assert len(p_client.requests) == 1

    @pytest.mark.asyncio
    async def test_attempt_allocation_rolls_back_when_local_workload_already_exists(self, monkeypatch, trigger_pair):
        del trigger_pair
        config = self._make_config()
        req_info = await create_mock_request_info()
        request_manager = RequestManager(config)
        scheduler = Scheduler(instance_provider=InstanceManager(config), config=config)
        scheduler.update_workload = AsyncMock(return_value=True)
        router = UnifiedPDRouter(
            req_info,
            config,
            scheduler=scheduler,
            request_manager=request_manager,
        )
        await request_manager.add_req_attempt_workload(
            req_info.req_id,
            1,
            PDRole.ROLE_P,
            Workload(active_tokens=1),
        )

        with pytest.raises(RuntimeError, match="already allocated"):
            await router._prepare_attempt_resource(PDRole.ROLE_P, 1)

        params = scheduler.update_workload.await_args.args[0]
        assert params.workload_change.active_tokens == -1

    @pytest.mark.asyncio
    async def test_metaserver_unknown_request_id_returns_404(self, monkeypatch, trigger_pair):
        del trigger_pair, monkeypatch
        config = self._make_config()
        request_manager = RequestManager(config)
        raw_request = _metaserver_raw_request({"request_id": "missing"}, "1")

        with pytest.raises(HTTPException) as exc_info:
            await handle_metaserver_request(
                raw_request,
                config,
                scheduler=Scheduler(instance_provider=InstanceManager(config), config=config),
                request_manager=request_manager,
            )
        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_metaserver_stale_attempt_returns_409(self, monkeypatch, trigger_pair):
        del trigger_pair
        p_client = _UnifiedPDPrefillClient()
        d_client = _UnifiedPDDecodeClient()
        config = self._make_config()
        req_info = await create_mock_request_info()
        request_manager = RequestManager(config)
        await request_manager.add_req_info(req_info)
        router = UnifiedPDRouter(
            req_info,
            config,
            scheduler=Scheduler(instance_provider=InstanceManager(config), config=config),
            request_manager=request_manager,
        )
        _patch_unified_pd_clients(monkeypatch, router, p_client, d_client)
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        router._bind_trigger_attempt(attempt)

        raw_request = _metaserver_raw_request({"request_id": req_info.req_id}, "99")
        with pytest.raises(HTTPException) as exc_info:
            await handle_metaserver_request(
                raw_request,
                config,
                scheduler=router._scheduler,
                request_manager=request_manager,
            )
        assert exc_info.value.status_code == status.HTTP_409_CONFLICT

    @pytest.mark.asyncio
    async def test_trigger_without_metaserver_port_fails(self, monkeypatch, trigger_pair):
        del trigger_pair
        config = CoordinatorConfig()
        req_info = await create_mock_request_info()
        router = self._make_router(
            req_info,
            monkeypatch,
            _UnifiedPDPrefillClient(),
            _UnifiedPDDecodeClient(),
            config=config,
        )
        with pytest.raises(HTTPException) as exc_info:
            await router.handle_request()
        assert exc_info.value.status_code == 503
        assert "worker_metaserver_base_port" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    async def test_mixed_handoff_and_trigger_returns_503(self, monkeypatch):
        host = "127.0.0.1"
        instance_p = _handoff_instance(0, PDRole.ROLE_P)
        endpoint_p = Endpoint(id=0, ip=host, business_port="8000", status=EndpointStatus.NORMAL)
        instance_p.endpoints = {host: {0: endpoint_p}}
        instance_d = _trigger_instance(1, PDRole.ROLE_D)
        endpoint_d = Endpoint(id=1, ip=host, business_port="8001", status=EndpointStatus.NORMAL)
        instance_d.endpoints = {host: {1: endpoint_d}}
        self._patch_instances(monkeypatch, instance_p, instance_d, endpoint_p, endpoint_d)

        req_info = await create_mock_request_info()
        router = self._make_router(req_info, monkeypatch, _UnifiedPDPrefillClient(), _UnifiedPDDecodeClient())
        with pytest.raises(HTTPException) as exc_info:
            await router.handle_request()
        assert exc_info.value.status_code == 503
        assert "Mixed vLLM" in str(exc_info.value.detail)
