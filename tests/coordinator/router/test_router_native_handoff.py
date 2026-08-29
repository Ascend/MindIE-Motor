# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from pytest import MonkeyPatch
from fastapi import FastAPI, status, Request
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
import asyncio
from contextlib import asynccontextmanager
import httpx
import json
import pytest

from motor.common.resources.endpoint import (
    Endpoint,
    EndpointStatus,
    Workload,
    WorkloadAction,
)
from motor.common.resources.instance import PDRole, Instance, InsStatus, ParallelConfig
from motor.config.coordinator import CoordinatorConfig, ExceptionConfig, SchedulerType
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.domain import InstanceReadiness, ScheduledResource
from motor.coordinator.models.request import ReqState, RequestInfo
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter
from motor.coordinator.tracer.tracing import TracerManager
from motor.coordinator.scheduler.scheduler import Scheduler
from motor.coordinator.domain.request_manager import RequestManager
from tests.coordinator.router.mock_openai_request import create_mock_request_info
import motor.coordinator.router.dispatch as router

TracerManager()


def _native_prefill_body(request_body, *, usage=None):
    body = {
        "kv_transfer_params": {
            "do_remote_prefill": True,
            "remote_request_id": request_body["request_id"],
            "remote_host": "127.0.0.1",
            "remote_port": 9000,
        }
    }
    if usage is not None:
        body["usage"] = usage
    return body


class _UnifiedPDPrefillClient:
    def __init__(self, *, exc: Exception | None = None, post_fail_times: int = 0):
        self.exc = exc
        self.post_fail_times = post_fail_times
        self.post_fail_count = 0
        self.requests = []
        self.base_url = "http://prefill"
        self.timeout = 1

    async def post(self, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        if self.exc is not None and (self.post_fail_times == 0 or self.post_fail_count < self.post_fail_times):
            self.post_fail_count += 1
            raise self.exc
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        return httpx.Response(
            status_code=200,
            json=_native_prefill_body(json),
            request=request,
        )


class _UnifiedPDStreamResponse:
    def __init__(self, chunks, exc: Exception | None = None):
        self.chunks = list(chunks)
        self.exc = exc
        if isinstance(exc, httpx.HTTPStatusError):
            self.status_code = exc.response.status_code
            self.is_success = False
            self.text = exc.response.text
            self.headers = exc.response.headers
            self._error_body = exc.response.content or str(exc).encode()
        else:
            self.status_code = 200
            self.is_success = True
            self.text = ""
            self.headers = {}
            self._error_body = b""

    async def __aenter__(self):
        if isinstance(self.exc, httpx.RequestError):
            raise self.exc
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def aread(self):
        return self._error_body

    def raise_for_status(self):
        if self.exc is not None and not self.chunks:
            raise self.exc

    async def aiter_bytes(self):
        if not self.is_success:
            yield self._error_body
            return
        for chunk in self.chunks:
            yield chunk
        if self.exc is not None and not isinstance(self.exc, (httpx.HTTPStatusError, httpx.RequestError)):
            raise self.exc


class _UnifiedPDDecodeClient:
    def __init__(
        self,
        *,
        stream_chunks=None,
        stream_exc: Exception | None = None,
        stream_fail_times: int = 0,
        post_exc: Exception | None = None,
        post_fail_times: int = 0,
    ):
        self.stream_chunks = stream_chunks or [
            b'data: {"choices":[{"delta":{"content":"decoded chunk"},"index":0,"finish_reason":null}]}\n\n',
        ]
        self.stream_exc = stream_exc
        self.stream_fail_times = stream_fail_times
        self.post_exc = post_exc
        self.post_fail_times = post_fail_times
        self.requests = []
        self.stream_count = 0
        self.stream_fail_count = 0
        self.post_count = 0
        self.post_fail_count = 0
        self.base_url = "http://decode"
        self.timeout = 1

    def stream(self, method, url, json=None, headers=None, timeout=None):
        self.stream_count += 1
        if json:
            self.requests.append(json)
        if self.stream_exc is not None and (
            self.stream_fail_times == 0 or self.stream_fail_count < self.stream_fail_times
        ):
            self.stream_fail_count += 1
            return _UnifiedPDStreamResponse([], exc=self.stream_exc)
        return _UnifiedPDStreamResponse(self.stream_chunks)

    async def post(self, path, json=None, headers=None, timeout=None):
        self.post_count += 1
        if json:
            self.requests.append(json)
        if self.post_exc is not None and (self.post_fail_times == 0 or self.post_fail_count < self.post_fail_times):
            self.post_fail_count += 1
            raise self.post_exc
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        return httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": "test response"}}]},
            request=request,
        )


def _patch_unified_pd_clients(monkeypatch, router_obj, p_client, d_client):
    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router_obj, "_client_for", _client_for)


def _patch_unified_pd_router_clients(monkeypatch, p_client, d_client):
    """Patch UnifiedPDRouter._client_for at class level (for app-level integration tests)."""

    def _client_for(self, resource: ScheduledResource):
        @asynccontextmanager
        async def _cm():
            if resource.instance.role == PDRole.ROLE_P:
                yield p_client
            else:
                yield d_client

        return _cm()

    monkeypatch.setattr(UnifiedPDRouter, "_client_for", _client_for)


async def _collect_stream_chunks(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode("utf-8", errors="replace"))
        else:
            chunks.append(chunk)
    return "".join(chunks)


def _parse_stream_error_payload(chunk_str: str) -> dict:
    data_lines = [
        line.removeprefix("data: ").strip()
        for line in chunk_str.splitlines()
        if line.startswith("data: ") and line.strip() != "data: [DONE]"
    ]
    assert data_lines, f"expected streaming error chunk, got: {chunk_str!r}"
    return json.loads(data_lines[-1])


def _assert_stream_error_chunk(
    chunk_str: str,
    *,
    error_message: str,
    error_type: str | None = None,
) -> None:
    """Assert the SSE error payload propagates the expected message (and optional type)."""
    payload = _parse_stream_error_payload(chunk_str)
    # Coordinator-synthesized stream errors use the {"error": {...}} envelope (matching the
    # pre-commit / non-stream shape); unwrap it. Engine-verbatim bodies may already be flat.
    if isinstance(payload.get("error"), dict):
        payload = payload["error"]
    assert error_message in payload["message"], (
        f"expected {error_message!r} in error message, got {payload['message']!r}"
    )
    if error_type is not None:
        assert payload["type"] == error_type


app = FastAPI()
_config = CoordinatorConfig()
_scheduler = Scheduler(instance_provider=InstanceManager(_config), config=_config)
_request_manager = RequestManager(_config)


@app.post("/v1/chat/completions")
async def handle_completions(request: Request):
    return await router.handle_request(request, _config, scheduler=_scheduler, request_manager=_request_manager)


class TestRouterNativeHandoff:
    @pytest.fixture(autouse=True)
    def fast_retry(self, monkeypatch: MonkeyPatch):
        """Skip real backoff in transport retry tests."""

        async def _instant_sleep(*_args, **_kwargs):
            return None

        monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    @pytest.fixture
    def client(self):
        return TestClient(app)

    @classmethod
    def create_mock_instance(cls, instance_id, role):
        """Create a proper mock Instance object"""
        mock_instance = Instance(
            job_name=f"test-job-{instance_id}",
            model_name=f"test-model-{instance_id}",
            engine_type="vllm",
            id=instance_id,
            role=role,
            status=InsStatus.ACTIVE,
            parallel_config=ParallelConfig(dp_size=1, tp_size=1),
            endpoints={},
        )
        return mock_instance

    def _make_router(self, req_info, monkeypatch, p_client, d_client):
        router_obj = UnifiedPDRouter(
            req_info,
            CoordinatorConfig(),
            scheduler=Scheduler(
                instance_provider=InstanceManager(CoordinatorConfig()),
                config=CoordinatorConfig(),
            ),
            request_manager=_request_manager,
        )
        _patch_unified_pd_clients(monkeypatch, router_obj, p_client, d_client)
        return router_obj

    @pytest.fixture
    def setup_native_handoff(self, monkeypatch: MonkeyPatch):
        host = "127.0.0.1"
        # Create proper instances for separate P/D flow
        mock_instance_p = self.create_mock_instance(0, PDRole.ROLE_P)
        mock_endpoint_p = Endpoint(
            id=0,
            ip=host,
            business_port="8000",
            status=EndpointStatus.NORMAL,
        )
        mock_instance_p.endpoints = {host: {0: mock_endpoint_p}}

        mock_instance_d = self.create_mock_instance(1, PDRole.ROLE_D)
        mock_endpoint_d = Endpoint(
            id=1,
            ip=host,
            business_port="8001",
            status=EndpointStatus.NORMAL,
        )
        mock_instance_d.endpoints = {host: {1: mock_endpoint_d}}

        # Mock functions (Scheduler uses get_required_instances_status for readiness)
        def mock_get_required_instances_status(self):
            return InstanceReadiness.REQUIRED_MET

        def mock_has_required_instances(self):
            return True

        def mock_get_available_instances(self, role=None):
            if role is None:
                return {
                    mock_instance_p.id: mock_instance_p,
                    mock_instance_d.id: mock_instance_d,
                }
            if role == PDRole.ROLE_U:  # PD hybrid role
                return {}  # No PD hybrid instances, will use separate P/D
            if role == PDRole.ROLE_P:
                return {mock_instance_p.id: mock_instance_p}
            if role == PDRole.ROLE_D:
                return {mock_instance_d.id: mock_instance_d}
            return {}

        async def mock_select_instance_and_endpoint(self, role):
            if role == PDRole.ROLE_P:
                return mock_instance_p, mock_endpoint_p
            elif role == PDRole.ROLE_D:
                return mock_instance_d, mock_endpoint_d
            return None, None

        async def mock_select_and_allocate(
            self,
            role,
            req_info,
            *,
            target_instance_id=None,
            required_engine_type=None,
        ):
            del required_engine_type
            if role == PDRole.ROLE_P:
                return (
                    mock_instance_p,
                    mock_endpoint_p,
                    Workload(active_tokens=1),
                )
            if role == PDRole.ROLE_D:
                return (
                    mock_instance_d,
                    mock_endpoint_d,
                    Workload(active_tokens=1),
                )
            return None

        async def mock_update_workload(self, params):
            return True

        monkeypatch.setattr(
            InstanceManager,
            "get_required_instances_status",
            mock_get_required_instances_status,
        )
        monkeypatch.setattr(InstanceManager, "has_required_instances", mock_has_required_instances)
        monkeypatch.setattr(InstanceManager, "get_available_instances", mock_get_available_instances)
        monkeypatch.setattr(Scheduler, "select_instance_and_endpoint", mock_select_instance_and_endpoint)
        monkeypatch.setattr(Scheduler, "select_and_allocate", mock_select_and_allocate)
        monkeypatch.setattr(Scheduler, "update_workload", mock_update_workload)

        mock_scheduler_config = MagicMock()
        mock_scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
        # Real ExceptionConfig so transport_retry_limit and rescheduling settings work;
        # MagicMock lacks @property implementation and breaks decode transport loops (range / last-attempt check).
        mock_exception_config = ExceptionConfig(max_retry=5, retry_delay=0.0001)
        mock_api_config = MagicMock()
        mock_api_config.coordinator_api_host = "127.0.0.1"
        mock_tls_config = MagicMock()
        mock_tls_config.enable_tls = False

        mock_config = MagicMock()
        mock_config.scheduler_config = mock_scheduler_config
        mock_config.exception_config = mock_exception_config
        mock_config.api_config = mock_api_config
        mock_config.infer_tls_config = mock_tls_config
        mock_config.mgmt_tls_config = mock_tls_config
        monkeypatch.setattr(CoordinatorConfig, "__new__", lambda cls: mock_config)
        _config.exception_config = mock_exception_config

    @pytest.fixture
    def mock_raw_request(self):
        # Mock Request
        mock_req = MagicMock(spec=Request)
        mock_req.body = AsyncMock(return_value=b'{"model": "test"}')
        mock_req.json = AsyncMock(return_value={"model": "test"})
        mock_req.headers = {}
        mock_req.url.path = "/v1/chat/completions"
        mock_req.app.state.tokenization_service = None

        # Must be awaitable so listen_for_disconnect() does not raise; never completes so handler wins.
        async def _never_receive():
            await asyncio.Event().wait()

        mock_req.receive = AsyncMock(side_effect=_never_receive)
        return mock_req

    @pytest.mark.asyncio
    async def test_successful_native_handoff_request(self, client, monkeypatch: MonkeyPatch, setup_native_handoff):
        """Test case: native handoff request succeeds.
        Expected behavior:
        1) Check request status is DecodeEnd
        2) Return normal response
        """
        p_client = _UnifiedPDPrefillClient()
        d_client = _UnifiedPDDecodeClient()

        req_info = await create_mock_request_info()
        origin_req_id = req_info.req_id
        origin_req_len = req_info.req_len
        origin_req_data = req_info.req_data

        handoff_router = self._make_router(req_info, monkeypatch, p_client, d_client)
        response = await handoff_router.handle_request()
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)

        assert response.status_code == status.HTTP_200_OK
        assert "text/event-stream" in response.headers.get("content-type")

        assert len(p_client.requests) == 1
        assert len(d_client.requests) == 1
        req_data_p = p_client.requests[0]
        assert req_data_p["stream"] is False
        assert req_data_p["max_tokens"] == 1
        assert "_motor_dispatch" not in req_data_p
        assert "_motor_dispatch" not in d_client.requests[0]
        assert d_client.requests[0]["request_id"] == req_data_p["request_id"]
        assert d_client.requests[0]["kv_transfer_params"] == {
            "do_remote_prefill": True,
            "remote_request_id": req_data_p["request_id"],
            "remote_host": "127.0.0.1",
            "remote_port": 9000,
        }

        assert req_info.req_id == origin_req_id
        assert req_info.req_len == origin_req_len
        assert req_info.req_data == origin_req_data

        assert req_info.state == ReqState.DECODE_END
        assert req_info.status[ReqState.D_ALLOCATED] >= req_info.status[ReqState.ARRIVE]
        assert req_info.status[ReqState.P_ALLOCATED] >= req_info.status[ReqState.ARRIVE]
        allocated_at = min(
            req_info.status[ReqState.P_ALLOCATED],
            req_info.status[ReqState.D_ALLOCATED],
        )
        assert req_info.status[ReqState.DECODE_END] >= allocated_at

    @pytest.mark.asyncio
    async def test_native_handoff_stream_recompute_after_partial_output_continues(
        self, client, monkeypatch: MonkeyPatch, setup_native_handoff
    ):
        """Decode stream with recomputed stop_reason still completes successfully."""
        d_client = _UnifiedPDDecodeClient(
            stream_chunks=[
                b'data: {"choices":[{"delta":{"content":"partial "},"index":0,"stop_reason":"recomputed","token_ids":[1,2]}],"prompt_token_ids":[10,11]}\n\n',
                b'data: {"choices":[{"delta":{"content":"continuation"},"index":0,"finish_reason":"stop"}]}\n\n',
            ]
        )
        p_client = _UnifiedPDPrefillClient()
        req_info = await create_mock_request_info()
        handoff_router = self._make_router(req_info, monkeypatch, p_client, d_client)

        response = await handoff_router.handle_request()
        chunk_str = await _collect_stream_chunks(response)

        assert req_info.state == ReqState.DECODE_END, chunk_str
        assert d_client.stream_count == 1
        assert "partial" in chunk_str
        assert "recompute after first chunk" not in chunk_str

    @pytest.mark.asyncio
    async def test_native_engine_decode_4xx_status_code(self, client, monkeypatch: MonkeyPatch, setup_native_handoff):
        """Test case: native decode engine returns a 4XX status code.
        Expected behavior:
        1) No request retry triggered
        2) Directly return error message
        """
        error_message = "Test Bad Request"
        d_client = _UnifiedPDDecodeClient(
            stream_exc=httpx.HTTPStatusError(
                message=error_message,
                request=MagicMock(),
                response=httpx.Response(status_code=status.HTTP_400_BAD_REQUEST, text=error_message),
            )
        )
        p_client = _UnifiedPDPrefillClient()
        req_info = await create_mock_request_info()

        release_p_tokens = 0
        release_d_tokens = 0
        original_release = UnifiedPDRouter._release_attempt_resource

        async def mock_release_attempt_resource(self, resource, attempt_seq, action, attempt=None, **kwargs):
            nonlocal release_p_tokens, release_d_tokens
            if resource.instance.role == PDRole.ROLE_P:
                if action == WorkloadAction.RELEASE_TOKENS:
                    release_p_tokens += 1
            elif resource.instance.role == PDRole.ROLE_D:
                if action == WorkloadAction.RELEASE_TOKENS:
                    release_d_tokens += 1
            await original_release(self, resource, attempt_seq, action, attempt, **kwargs)

        monkeypatch.setattr(
            UnifiedPDRouter,
            "_release_attempt_resource",
            mock_release_attempt_resource,
        )

        handoff_router = self._make_router(req_info, monkeypatch, p_client, d_client)
        response = await handoff_router.handle_request()
        chunk_str = await _collect_stream_chunks(response)

        assert req_info.state == ReqState.EXCEPTION
        _assert_stream_error_chunk(chunk_str, error_message=error_message, error_type="UpstreamHTTPError")
        assert str(status.HTTP_400_BAD_REQUEST) in chunk_str
        assert d_client.stream_count == 1
        assert release_d_tokens >= 1
        assert release_p_tokens >= 1

    @pytest.mark.asyncio
    async def test_native_engine_decode_continuous_5xx_status_code(
        self,
        client,
        monkeypatch: MonkeyPatch,
        setup_native_handoff,
        caplog: pytest.LogCaptureFixture,
    ):
        """Decode keeps getting 5XX with the same message: retries exhaust, error chunk returned;
        identical-error logs: one ERROR + (max_retry-1) WARNING dedup lines.
        """
        error_message = "Test Internal Server Error"
        max_retry = CoordinatorConfig().exception_config.transport_retry_limit
        d_client = _UnifiedPDDecodeClient(
            stream_exc=httpx.HTTPStatusError(
                message=error_message,
                request=MagicMock(),
                response=httpx.Response(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    text=error_message,
                ),
            )
        )
        p_client = _UnifiedPDPrefillClient()
        req_info = await create_mock_request_info()

        exec_release = 0
        original_release = UnifiedPDRouter._release_attempt_resource

        async def mock_release_attempt_resource(self, resource, attempt_seq, action, attempt=None, **kwargs):
            nonlocal exec_release
            exec_release += 1
            await original_release(self, resource, attempt_seq, action, attempt, **kwargs)

        monkeypatch.setattr(
            UnifiedPDRouter,
            "_release_attempt_resource",
            mock_release_attempt_resource,
        )

        handoff_router = self._make_router(req_info, monkeypatch, p_client, d_client)
        response = await handoff_router.handle_request()
        chunk_str = await _collect_stream_chunks(response)

        assert req_info.state == ReqState.EXCEPTION
        _assert_stream_error_chunk(chunk_str, error_message=error_message, error_type="UpstreamHTTPError")
        assert str(status.HTTP_500_INTERNAL_SERVER_ERROR) in chunk_str
        assert d_client.stream_count == max_retry
        assert exec_release >= 1

    @pytest.mark.asyncio
    async def test_native_engine_decode_once_5xx_status_code(
        self, client, monkeypatch: MonkeyPatch, setup_native_handoff
    ):
        """Test case: native decode request first returns 5XX, then 200.
        Expected behavior:
        1) Check request status is DecodeEnd
        2) Trigger request retry
        3) Request retry succeeds
        """
        error_message = "Test Internal Server Error"
        d_client = _UnifiedPDDecodeClient(
            stream_exc=httpx.HTTPStatusError(
                message=error_message,
                request=MagicMock(),
                response=httpx.Response(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR),
            ),
            stream_fail_times=1,
        )
        p_client = _UnifiedPDPrefillClient()
        req_info = await create_mock_request_info()
        handoff_router = self._make_router(req_info, monkeypatch, p_client, d_client)

        response = await handoff_router.handle_request()
        chunk_str = await _collect_stream_chunks(response)

        assert response.status_code == status.HTTP_200_OK
        assert d_client.stream_fail_count == 1
        assert d_client.stream_count >= 2
        assert req_info.state == ReqState.DECODE_END
        assert "decoded chunk" in chunk_str

    @pytest.mark.asyncio
    async def test_native_engine_decode_network_exception(self, client, monkeypatch: MonkeyPatch, setup_native_handoff):
        """Test case: native decode engine raises a network exception.
        Expected behavior:
        1) Check request status is Exception
        2) Retries exhaust transport_retry_limit
        3) Directly return error message
        """
        error_message = "Connection error"
        max_retry = CoordinatorConfig().exception_config.transport_retry_limit
        d_client = _UnifiedPDDecodeClient(
            stream_exc=httpx.ConnectError(error_message, request=MagicMock()),
            stream_fail_times=max_retry,
        )
        p_client = _UnifiedPDPrefillClient()
        req_info = await create_mock_request_info()
        handoff_router = self._make_router(req_info, monkeypatch, p_client, d_client)

        response = await handoff_router.handle_request()
        chunk_str = await _collect_stream_chunks(response)

        _assert_stream_error_chunk(chunk_str, error_message=error_message, error_type="ConnectError")
        assert d_client.stream_count == max_retry
        assert d_client.stream_fail_count == max_retry
        assert req_info.state == ReqState.EXCEPTION

    @pytest.mark.asyncio
    async def test_native_handoff_decode_non_stream_retry_exhausts_transport_limit(
        self, client, monkeypatch: MonkeyPatch, setup_native_handoff
    ):
        """Non-stream decode failures retry whole transport attempts until limit."""
        error_message = "Same post Decode error every retry"
        max_retry = CoordinatorConfig().exception_config.transport_retry_limit
        d_client = _UnifiedPDDecodeClient(
            post_exc=httpx.HTTPStatusError(
                message=error_message,
                request=MagicMock(),
                response=httpx.Response(status_code=status.HTTP_502_BAD_GATEWAY, text=error_message),
            ),
            post_fail_times=max_retry,
        )
        p_client = _UnifiedPDPrefillClient()
        req_info = await create_mock_request_info(stream=False)

        handoff_router = self._make_router(req_info, monkeypatch, p_client, d_client)

        with pytest.raises(httpx.HTTPStatusError):
            await handoff_router.handle_request()

        assert d_client.post_count == max_retry
        assert d_client.post_fail_count == max_retry
        assert req_info.state == ReqState.EXCEPTION

    @pytest.mark.asyncio
    async def test_native_engine_prefill_network_exception(
        self, client, monkeypatch: MonkeyPatch, setup_native_handoff
    ):
        """Test case: native prefill engine raises a network exception.
        Expected behavior:
        1) Check request status is Exception
        2) Retries exhaust transport_retry_limit
        3) Directly return error message
        """
        error_message = "Connection error"
        retry_times = CoordinatorConfig().exception_config.transport_retry_limit
        p_client = _UnifiedPDPrefillClient(
            exc=httpx.ConnectError(message=error_message, request=MagicMock()),
            post_fail_times=retry_times,
        )
        d_client = _UnifiedPDDecodeClient()
        _patch_unified_pd_router_clients(monkeypatch, p_client, d_client)

        state: ReqState = None

        def mock_update_state(self, new_state: ReqState):
            nonlocal state
            state = new_state

        monkeypatch.setattr(RequestInfo, "update_state", mock_update_state)

        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        ) as response:
            chunks = []
            for chunk in response.iter_lines():
                chunks.append(chunk)
            chunk_str = "".join(chunks)

        assert error_message in chunk_str
        assert p_client.post_fail_count == retry_times
        assert state == ReqState.EXCEPTION

    @pytest.mark.asyncio
    async def test_degradation_to_single_node(
        self, monkeypatch: MonkeyPatch, setup_native_handoff, mock_raw_request, client
    ):
        """
        Test that when no ROLE_D instances are available, the router degrades to SINGLE_NODE mode
        and uses PDHybridRouter.
        """
        # Let listen_for_disconnect() exit immediately so the task does not hang (avoids WSL Terminated
        # when handler and disconnect run concurrently and disconnect awaits a never-completing receive).
        disconnect_msg = {"type": "http.disconnect"}
        mock_raw_request.receive = AsyncMock(return_value=disconnect_msg)

        # Mock InstanceManager.get_available_instances
        host = "127.0.0.1"
        mock_instance_p = self.create_mock_instance(0, PDRole.ROLE_P)
        mock_endpoint_p = Endpoint(id=0, ip=host, business_port="8000")
        mock_instance_p.endpoints = {host: {0: mock_endpoint_p}}

        def mock_get_available_instances(self, role=None):
            if role is None:
                return {mock_instance_p.id: mock_instance_p}
            if role == PDRole.ROLE_U:  # PD hybrid role
                return {}
            elif role == PDRole.ROLE_P:
                return {mock_instance_p.id: mock_instance_p}
            elif role == PDRole.ROLE_D:
                return {}
            return {}

        monkeypatch.setattr(InstanceManager, "get_available_instances", mock_get_available_instances)

        # So router chooses SINGLE_NODE (PDHybridRouter) before creating the router
        def mock_get_required_instances_status(self):
            return InstanceReadiness.ONLY_PREFILL  # not ready -> fallback to SINGLE_NODE

        monkeypatch.setattr(
            InstanceManager,
            "get_required_instances_status",
            mock_get_required_instances_status,
        )

        def mock_has_required_instances(self):
            return False

        monkeypatch.setattr(InstanceManager, "has_required_instances", mock_has_required_instances)

        def mock_select_instance_and_endpoint(self, role):
            if role == PDRole.ROLE_P:
                return mock_instance_p, mock_endpoint_p
            elif role == PDRole.ROLE_D:
                return None, None
            return None, None

        monkeypatch.setattr(Scheduler, "select_instance_and_endpoint", mock_select_instance_and_endpoint)

        # Mock PDHybridRouter response
        mock_response = "mock_message"
        with patch(
            "motor.coordinator.router.dispatch.PDHybridRouter.handle_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_handle_request:
            response = await router.handle_request(
                mock_raw_request,
                CoordinatorConfig(),
                scheduler=_scheduler,
                request_manager=_request_manager,
            )
            # Verify PDHybridRouter.handle_request was called
            mock_handle_request.assert_called_once()
            # Verify response
            assert response == mock_response

    @pytest.mark.asyncio
    async def test_no_degradation_when_d_instances_exist(self, monkeypatch, setup_native_handoff, mock_raw_request):
        """
        Test that when ROLE_D instances are available, the router uses the configured mode (PD_SEPARATE).
        """
        # Let listen_for_disconnect() exit immediately (same as test_degradation_to_single_node).
        disconnect_msg = {"type": "http.disconnect"}
        mock_raw_request.receive = AsyncMock(return_value=disconnect_msg)

        # Mock UnifiedPDRouter response
        mock_response = "mock_message"
        with patch(
            "motor.coordinator.router.dispatch.UnifiedPDRouter.handle_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_handle_request:
            response = await router.handle_request(
                mock_raw_request,
                CoordinatorConfig(),
                scheduler=_scheduler,
                request_manager=_request_manager,
            )

            # Verify UnifiedPDRouter.handle_request was called
            mock_handle_request.assert_called_once()
            # Verify response
            assert response == mock_response

    @pytest.mark.parametrize(
        ("prefill_details", "expected_details"),
        [
            ({"cached_tokens": 10}, {"cached_tokens": 10}),
            (None, {"cached_tokens": 0}),
        ],
    )
    @pytest.mark.asyncio
    async def test_prompt_tokens_details_propagation(
        self,
        client,
        monkeypatch: MonkeyPatch,
        setup_native_handoff,
        prefill_details,
        expected_details,
    ):
        """UnifiedPD returns prompt cache details collected from the prefill response."""
        req_info = await create_mock_request_info(stream=False)

        class _PrefillClient(_UnifiedPDPrefillClient):
            async def post(self, path, json=None, headers=None, timeout=None):
                self.requests.append(json)
                request = httpx.Request("POST", path, headers=headers or {}, json=json)
                return httpx.Response(
                    status_code=200,
                    json=_native_prefill_body(
                        json,
                        usage={"prompt_tokens_details": prefill_details},
                    ),
                    request=request,
                )

        class _DecodeClient(_UnifiedPDDecodeClient):
            async def post(self, path, json=None, headers=None, timeout=None):
                self.post_count += 1
                if json:
                    self.requests.append(json)
                request = httpx.Request("POST", path, headers=headers or {}, json=json)
                return httpx.Response(
                    status_code=200,
                    json={
                        "choices": [{"message": {"content": "test response"}}],
                        "usage": {
                            "prompt_tokens": 15,
                            "completion_tokens": 1,
                            "total_tokens": 16,
                        },
                    },
                    request=request,
                )

        p_client = _PrefillClient()
        d_client = _DecodeClient()
        handoff_router = self._make_router(req_info, monkeypatch, p_client, d_client)

        response = await handoff_router.handle_request()
        response_json = response.body.decode() if hasattr(response.body, "decode") else response.body
        response_data = json.loads(response_json)

        assert req_info.prompt_tokens_details == expected_details
        assert response_data["choices"][0]["message"]["content"] == "test response"
        assert response_data["usage"]["prompt_tokens_details"] == expected_details

    @pytest.mark.asyncio
    async def test_stream_prompt_tokens_details_when_recompute_disabled(
        self, client, monkeypatch: MonkeyPatch, setup_native_handoff
    ):
        """Prompt cache details are independent of the recompute feature switch."""
        prompt_tokens_details = {"cached_tokens": 10}
        req_info = await create_mock_request_info(stream=True)

        usage_chunk = b'data: {"choices":[],"usage":{"prompt_tokens":15,"completion_tokens":1,"total_tokens":16}}\n\n'

        class _DelayedPrefillClient(_UnifiedPDPrefillClient):
            async def post(self, path, json=None, headers=None, timeout=None):
                self.requests.append(json)
                await asyncio.sleep(0.01)
                request = httpx.Request("POST", path, headers=headers or {}, json=json)
                return httpx.Response(
                    status_code=200,
                    json=_native_prefill_body(
                        json,
                        usage={"prompt_tokens_details": prompt_tokens_details},
                    ),
                    request=request,
                )

        p_client = _DelayedPrefillClient()
        d_client = _UnifiedPDDecodeClient(stream_chunks=[usage_chunk])
        handoff_router = self._make_router(req_info, monkeypatch, p_client, d_client)
        handoff_router.config.exception_config.reschedule_enabled = False

        response = await handoff_router.handle_request()
        chunks = [chunk async for chunk in response.body_iterator]
        response_data = json.loads(chunks[0].removeprefix(b"data: ").strip())

        assert response_data["usage"]["prompt_tokens_details"] == prompt_tokens_details

    @pytest.mark.asyncio
    async def test_native_handoff_nonstream_strips_token_ids_at_coordinator_exit(
        self, client, monkeypatch: MonkeyPatch, setup_native_handoff
    ):
        """UnifiedPD non-stream strips token ids and normalizes stop_reason before returning to client."""
        req_info = await create_mock_request_info(stream=False)
        req_info.entry_api = req_info.api

        recomputed_body = {
            "prompt_token_ids": [1, 2],
            "choices": [
                {
                    "message": {"role": "assistant", "content": "partial "},
                    "stop_reason": "recomputed",
                    "token_ids": [3, 4],
                }
            ],
            "usage": {"completion_tokens": 2},
        }

        class _RecomputedDecodeClient(_UnifiedPDDecodeClient):
            async def post(self, path, json=None, headers=None, timeout=None):
                self.post_count += 1
                if json:
                    self.requests.append(json)
                request = httpx.Request("POST", path, headers=headers or {}, json=json)
                return httpx.Response(status_code=200, json=recomputed_body, request=request)

        p_client = _UnifiedPDPrefillClient()
        d_client = _RecomputedDecodeClient()
        handoff_router = self._make_router(req_info, monkeypatch, p_client, d_client)

        response = await handoff_router.handle_request()
        response_json = response.body.decode() if hasattr(response.body, "decode") else response.body
        response_data = json.loads(response_json)

        assert response_data["choices"][0]["message"]["content"] == "partial "
        assert response_data["choices"][0]["stop_reason"] == "stop"
        assert "token_ids" not in response_data["choices"][0]
        assert "prompt_token_ids" not in response_data
        assert req_info.state == ReqState.DECODE_END
