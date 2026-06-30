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
import httpx
import json
import logging
import pytest

from motor.config.coordinator import (
    DeployMode,
    CoordinatorConfig,
    ExceptionConfig,
    SchedulerType,
)
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.domain import InstanceReadiness, ScheduledResource
from motor.coordinator.models.request import ReqState, RequestInfo
from motor.coordinator.router.strategies.base import BaseRouter
from motor.coordinator.router.strategies.cdp_separate import SeparateCDPRouter
from motor.coordinator.tracer.tracing import TracerManager
from motor.common.resources.endpoint import WorkloadAction
from motor.common.resources.instance import (
    Endpoint,
    PDRole,
    Instance,
    InsStatus,
    ParallelConfig,
)
from motor.coordinator.scheduler.scheduler import Scheduler
from motor.coordinator.domain.request_manager import RequestManager
from tests.coordinator.router.mock_openai_request import (
    MockStreamResponse,
    create_mock_request_info,
)
import motor.coordinator.router.dispatch as router

TracerManager()


def _assert_decode_retry_logs_deduped_for_label(
    caplog: pytest.LogCaptureFixture, decode_label: str, max_retry: int
) -> None:
    """Same str(e) on each retry: one ERROR (traceback), remaining attempts WARNING dedup."""
    errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR
        and decode_label in r.getMessage()
        and "same error as previous attempt" not in r.getMessage()
    ]
    dedup_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "same error as previous attempt" in r.getMessage()
        and decode_label in r.getMessage()
    ]
    assert len(errors) == 1
    assert len(dedup_warnings) == max_retry - 1


app = FastAPI()
_config = CoordinatorConfig()
# CDP separate mode requires worker metaserver; set so app-based tests have a valid config
_config.worker_metaserver_port = (
    getattr(_config, "worker_metaserver_port", None) or 12000
)
_scheduler = Scheduler(instance_provider=InstanceManager(_config), config=_config)
_request_manager = RequestManager(_config)


@app.post("/v1/chat/completions")
async def handle_completions(request: Request):
    return await router.handle_request(
        request, _config, scheduler=_scheduler, request_manager=_request_manager
    )


@app.post("/v1/metaserver")
async def handle_metaserver(request: Request):
    return await router.handle_metaserver_request(
        request, _config, scheduler=_scheduler, request_manager=_request_manager
    )


class MockAsyncClient:
    def __init__(
        self,
        post_exc: Exception = None,
        stream_exc: Exception = None,
        post_fail_times: int = 1,
        stream_fail_times: int = 1,
    ):
        self.post_exc = post_exc
        self.post_fail_times = post_fail_times
        self.post_count = 0
        self.post_fail_count = 0

        self.stream_exc = stream_exc
        self.stream_fail_times = stream_fail_times
        self.stream_count = 0
        self.stream_fail_count = 0

        self.req_data_from_metaserver = {}
        self.req_data_d_request = {}  # D request (with metaserver URL), not overwritten by inner post()
        self.req_headers_from_router = {}

        self.base_url = "test-base-url"
        self.timeout = 1
        self.is_closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def aclose(self):
        pass

    async def post(self, url, json=None, headers=None, **kwargs):
        self.post_count += 1
        if self.post_exc and self.post_fail_count < self.post_fail_times:
            self.post_fail_count += 1
            mock_response_fail = MagicMock()
            mock_response_fail.raise_for_status = MagicMock(side_effect=self.post_exc)
            return mock_response_fail

        self.req_data_from_metaserver = json
        request = httpx.Request("POST", url, headers=headers or {}, json=json)

        return httpx.Response(
            status_code=status.HTTP_200_OK,
            json={
                "choices": [
                    {
                        "delta": {"content": "decoded chunk"},
                        "index": 0,
                        "finish_reason": None,
                    }
                ],
                "id": "chatcmpl-123",
            },
            request=request,
        )

    def stream(self, method, url, json=None, headers=None, **kwargs):
        self.stream_count += 1
        if json:
            self.req_data_from_metaserver = json
            self.req_data_d_request = (
                json  # keep D request; post() may overwrite req_data_from_metaserver
            )
        # logger.info(f"----------req_data_from_coordinator:{json}")
        if self.stream_exc and self.stream_fail_count < self.stream_fail_times:
            self.stream_fail_count += 1
            return MockStreamResponse(json or {}, recomputed=False, exc=self.stream_exc)

        from urllib.parse import urlparse

        client = TestClient(app)
        self.req_headers_from_router = headers

        url = json["kv_transfer_params"]["metaserver"]
        parsed_url = urlparse(url)

        # Forward request to metaserver
        response = None
        try:
            response = client.post(
                parsed_url.path,
                json={
                    "request_id": headers.get("X-Request-Id"),
                    "do_remote_decode": False,
                    "do_remote_prefill": True,
                    "remote_engine_id": "test-engine",
                    "remote_host": parsed_url.hostname,
                    "remote_port": str(parsed_url.port),
                },
            )
            response.raise_for_status()
        except Exception as e:
            err_text = (
                getattr(response, "text", str(e)) if response is not None else str(e)
            )
            err_status = (
                getattr(response, "status_code", 500) if response is not None else 500
            )
            return MockStreamResponse(
                json or {},
                recomputed=False,
                exc=httpx.HTTPStatusError(
                    message=err_text,
                    request=MagicMock(),
                    response=httpx.Response(status_code=err_status, text=err_text),
                ),
            )

        # Return an async context manager
        return MockStreamResponse(json or {}, recomputed=False, exc=None)


class MockAsyncClientFirstStreamRecompute(MockAsyncClient):
    """First decode stream simulates recompute after partial output; second completes."""

    def stream(self, method, url, json=None, headers=None, **kwargs):
        self.stream_count += 1
        if json:
            self.req_data_from_metaserver = json
            self.req_data_d_request = json
        if self.stream_exc and self.stream_fail_count < self.stream_fail_times:
            self.stream_fail_count += 1
            return MockStreamResponse(json or {}, recomputed=False, exc=self.stream_exc)

        from urllib.parse import urlparse

        client = TestClient(app)
        self.req_headers_from_router = headers or {}

        url_ms = json["kv_transfer_params"]["metaserver"]
        parsed_url = urlparse(url_ms)

        response = None
        try:
            response = client.post(
                parsed_url.path,
                json={
                    "request_id": headers.get("X-Request-Id"),
                    "do_remote_decode": False,
                    "do_remote_prefill": True,
                    "remote_engine_id": "test-engine",
                    "remote_host": parsed_url.hostname,
                    "remote_port": str(parsed_url.port),
                },
            )
            response.raise_for_status()
        except Exception as e:
            err_text = (
                getattr(response, "text", str(e)) if response is not None else str(e)
            )
            err_status = (
                getattr(response, "status_code", 500) if response is not None else 500
            )
            return MockStreamResponse(
                json or {},
                recomputed=False,
                exc=httpx.HTTPStatusError(
                    message=err_text,
                    request=MagicMock(),
                    response=httpx.Response(status_code=err_status, text=err_text),
                ),
            )

        recomputed = self.stream_count == 1
        return MockStreamResponse(json or {}, recomputed=recomputed, exc=None)


class TestRouterCDPSeparation:
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
    def create_mock_instance(self, instance_id, role):
        """Create a proper mock Instance object"""
        mock_instance = Instance(
            job_name=f"test-job-{instance_id}",
            model_name=f"test-model-{instance_id}",
            id=instance_id,
            role=role,
            status=InsStatus.ACTIVE,
            parallel_config=ParallelConfig(dp_size=1, tp_size=1),
            endpoints={},
        )
        return mock_instance

    @pytest.fixture
    def setup_cdp_separation(self, monkeypatch: MonkeyPatch):
        host = "127.0.0.1"
        # Create proper instances for separate P/D flow
        mock_instance_p = self.create_mock_instance(0, PDRole.ROLE_P)
        mock_endpoint_p = Endpoint(
            id=0, ip=host, business_port="8000", mgmt_port="8000"
        )
        mock_instance_p.endpoints = {host: {0: mock_endpoint_p}}

        mock_instance_d = self.create_mock_instance(1, PDRole.ROLE_D)
        mock_endpoint_d = Endpoint(
            id=1, ip=host, business_port="8001", mgmt_port="8001"
        )
        mock_instance_d.endpoints = {host: {1: mock_endpoint_d}}

        # Mock functions (Scheduler uses get_required_instances_status for readiness)
        def mock_get_required_instances_status(self, deploy_mode=None):
            return InstanceReadiness.REQUIRED_MET

        def mock_has_required_instances(self, deploy_mode=None):
            return True

        def mock_get_available_instances(*args, **kwargs):
            # Accept (self, role) when patched on InstanceManager; role is 2nd positional or in kwargs
            role = kwargs.get("role")
            if role is None and len(args) >= 2:
                role = args[1]
            elif role is None and len(args) == 1:
                role = args[0]  # staticmethod-style call
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

        async def mock_update_workload(self, params):
            return True

        monkeypatch.setattr(
            InstanceManager,
            "get_required_instances_status",
            mock_get_required_instances_status,
        )
        monkeypatch.setattr(
            InstanceManager, "has_required_instances", mock_has_required_instances
        )
        monkeypatch.setattr(
            InstanceManager, "get_available_instances", mock_get_available_instances
        )
        monkeypatch.setattr(
            Scheduler, "select_instance_and_endpoint", mock_select_instance_and_endpoint
        )
        monkeypatch.setattr(Scheduler, "update_workload", mock_update_workload)

        # Mock CoordinatorConfig to return CDP_SEPARATE deploy mode
        mock_scheduler_config = MagicMock()
        mock_scheduler_config.deploy_mode = DeployMode.CDP_SEPARATE
        mock_scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
        # Real ExceptionConfig so transport_retry_limit / recompute_retry_limit properties work;
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
        # CDP separate requires worker metaserver; use a fixed port for test
        mock_config.worker_metaserver_port = 12000

        monkeypatch.setattr(CoordinatorConfig, "__new__", lambda cls: mock_config)

    @pytest.fixture
    def mock_raw_request(self):
        # Mock Request
        mock_req = MagicMock(spec=Request)
        mock_req.body = AsyncMock(return_value=b'{"model": "test"}')
        mock_req.json = AsyncMock(return_value={"model": "test"})
        mock_req.headers = {}
        mock_req.url.path = "/v1/chat/completions"

        # Must be awaitable so listen_for_disconnect() does not raise; never completes so handler wins.
        async def _never_receive():
            await asyncio.Event().wait()

        mock_req.receive = AsyncMock(side_effect=_never_receive)
        return mock_req

    @pytest.mark.asyncio
    async def test_successful_request_with_separate_cdp(
        self, client, monkeypatch: MonkeyPatch, setup_cdp_separation
    ):
        """Test case: CDP separation mode request success
        Expected behavior:
        1) Check request status is DecodeEnd
        2) Return normal response
        """

        mock_async_client = MockAsyncClient()

        req_info = await create_mock_request_info()
        origin_req_id = req_info.req_id
        origin_req_len = req_info.req_len
        origin_req_data = req_info.req_data

        with patch(
            "motor.coordinator.router.strategies.base.httpx.AsyncClient",
            return_value=mock_async_client,
        ):
            # Must use _request_manager so metaserver (handle_metaserver) finds req_info
            cdp_router = SeparateCDPRouter(
                req_info,
                CoordinatorConfig(),
                scheduler=Scheduler(
                    instance_provider=InstanceManager(CoordinatorConfig()),
                    config=CoordinatorConfig(),
                ),
                request_manager=_request_manager,
            )
            response = await cdp_router.handle_request()
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

            # Should get a 200 success status
            assert response.status_code == status.HTTP_200_OK
            # Should be a streaming response
            assert "text/event-stream" in response.headers.get("content-type")

            # req_data_from_metaserver may be P request (after metaserver handler's post); use D request for URL
            req_data_p = mock_async_client.req_data_from_metaserver
            assert (
                req_data_p["stream"] is False
            )  # P request uses stream=False for KV cache fill
            assert req_data_p["max_tokens"] == 1  # P request uses max_tokens=1
            # D request: metaserver URL points to this Worker's metaserver
            kv_transfer_params = mock_async_client.req_data_d_request[
                "kv_transfer_params"
            ]
            assert kv_transfer_params["do_remote_decode"] is False
            assert kv_transfer_params["do_remote_prefill"] is True
            assert (
                kv_transfer_params["metaserver"]
                == "http://127.0.0.1:12000/v1/metaserver"
            )

            # Request info should not be modified by metaserver
            assert req_info.req_id == origin_req_id
            assert req_info.req_len == origin_req_len
            assert req_info.req_data == origin_req_data

            # Check request state and metrics
            assert req_info.state == ReqState.DECODE_END
            assert (
                req_info.status[ReqState.D_ALLOCATED]
                >= req_info.status[ReqState.ARRIVE]
            )
            assert (
                req_info.status[ReqState.P_ALLOCATED]
                >= req_info.status[ReqState.D_ALLOCATED]
            )
            assert (
                req_info.status[ReqState.PREFILL_END]
                >= req_info.status[ReqState.P_ALLOCATED]
            )
            assert (
                req_info.status[ReqState.FIRST_TOKEN_FINISH]
                >= req_info.status[ReqState.PREFILL_END]
            )
            assert (
                req_info.status[ReqState.DECODE_END]
                >= req_info.status[ReqState.FIRST_TOKEN_FINISH]
            )

    @pytest.mark.asyncio
    async def test_cdp_stream_recompute_after_partial_output_continues(
        self, client, monkeypatch: MonkeyPatch, setup_cdp_separation
    ):
        """Decode signals recomputed after partial stream; second flight completes (aligned with PD router)."""
        mock_async_client = MockAsyncClientFirstStreamRecompute()

        req_info = await create_mock_request_info()

        with patch(
            "motor.coordinator.router.strategies.base.httpx.AsyncClient",
            return_value=mock_async_client,
        ):
            cdp_router = SeparateCDPRouter(
                req_info,
                CoordinatorConfig(),
                scheduler=Scheduler(
                    instance_provider=InstanceManager(CoordinatorConfig()),
                    config=CoordinatorConfig(),
                ),
                request_manager=_request_manager,
            )
            response = await cdp_router.handle_request()
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            chunk_str = b"".join(chunks).decode("utf-8", errors="replace")

        assert req_info.state == ReqState.DECODE_END, chunk_str
        assert mock_async_client.stream_count == 2
        assert "data:" in chunk_str
        assert "recompute after first chunk" not in chunk_str

    @pytest.mark.asyncio
    async def test_cdp_requires_worker_metaserver_port(self, setup_cdp_separation):
        """CDP separate mode raises RuntimeError when worker_metaserver_port is not set."""
        mock_config = CoordinatorConfig()
        mock_config.worker_metaserver_port = None

        req_info = await create_mock_request_info()
        cdp_router = SeparateCDPRouter(
            req_info,
            CoordinatorConfig(),
            scheduler=Scheduler(
                instance_provider=InstanceManager(CoordinatorConfig()),
                config=CoordinatorConfig(),
            ),
            request_manager=_request_manager,
        )
        with pytest.raises(RuntimeError, match="worker_metaserver_base_port > 0"):
            await cdp_router.handle_request()

    @pytest.mark.asyncio
    async def test_engine_server_decode_4xx_status_code(
        self, client, monkeypatch: MonkeyPatch, setup_cdp_separation
    ):
        """Test case: Decode EngineServer returns 4XX status code
        Expected behavior:
        1) No request retry triggered
        2) Directly return error message
        """
        # Mock the HTTP forwarding function to return a 4XX error
        error_message = "Test Bad Request"
        mock_async_client = MockAsyncClient(
            stream_exc=httpx.HTTPStatusError(
                message=error_message,
                request=MagicMock(),
                response=httpx.Response(
                    status_code=status.HTTP_400_BAD_REQUEST, text=error_message
                ),
            ),
            stream_fail_times=CoordinatorConfig().exception_config.max_retry,
        )
        req_info = await create_mock_request_info()

        release_p_tokens = 0
        release_p_kv = 0
        release_d_tokens = 0

        async def mock_update_workload(
            self, resource: ScheduledResource, action: WorkloadAction
        ):
            nonlocal release_p_tokens
            nonlocal release_p_kv
            nonlocal release_d_tokens
            if resource.instance.role == PDRole.ROLE_P:
                if action == WorkloadAction.RELEASE_TOKENS:
                    release_p_tokens += 1
                elif action == WorkloadAction.RELEASE_KV:
                    release_p_kv += 1
            elif resource.instance.role == PDRole.ROLE_D:
                if action == WorkloadAction.RELEASE_TOKENS:
                    release_d_tokens += 1
            return True

        monkeypatch.setattr(BaseRouter, "_update_workload", mock_update_workload)

        with patch(
            "motor.coordinator.router.strategies.base.httpx.AsyncClient",
            return_value=mock_async_client,
        ):
            cdp_router = SeparateCDPRouter(
                req_info,
                CoordinatorConfig(),
                scheduler=Scheduler(
                    instance_provider=InstanceManager(CoordinatorConfig()),
                    config=CoordinatorConfig(),
                ),
                request_manager=_request_manager,
            )
            response = await cdp_router.handle_request()
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            chunk_str = "".join(chunks)

        assert req_info.state == ReqState.EXCEPTION
        assert error_message in chunk_str
        # Should get a 4XX error
        assert str(status.HTTP_400_BAD_REQUEST) in chunk_str
        assert (
            mock_async_client.stream_count
            == CoordinatorConfig().exception_config.max_retry
        )
        assert release_d_tokens >= 1
        assert release_p_tokens == 0

    @pytest.mark.asyncio
    async def test_engine_server_decode_continuous_5xx_status_code(
        self,
        client,
        monkeypatch: MonkeyPatch,
        setup_cdp_separation,
        caplog: pytest.LogCaptureFixture,
    ):
        """Decode keeps getting 5XX with the same message: retries exhaust, error chunk returned;
        identical-error logs: one ERROR + (max_retry-1) WARNING dedup lines."""
        error_message = "Test Internal Server Error"
        max_retry = CoordinatorConfig().exception_config.max_retry
        mock_async_client = MockAsyncClient(
            stream_exc=httpx.HTTPStatusError(
                message=error_message,
                request=MagicMock(),
                response=httpx.Response(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    text=error_message,
                ),
            ),
            stream_fail_times=max_retry,
        )
        req_info = await create_mock_request_info()

        exec_release = 0

        async def mock_update_workload(
            self, resource: ScheduledResource, action: WorkloadAction
        ):
            nonlocal exec_release
            exec_release += 1
            return True

        monkeypatch.setattr(BaseRouter, "_update_workload", mock_update_workload)

        with caplog.at_level(
            logging.WARNING, logger="motor.coordinator.router.strategies.base"
        ):
            with patch(
                "motor.coordinator.router.strategies.base.httpx.AsyncClient",
                return_value=mock_async_client,
            ):
                cdp_router = SeparateCDPRouter(
                    req_info,
                    CoordinatorConfig(),
                    scheduler=Scheduler(
                        instance_provider=InstanceManager(CoordinatorConfig()),
                        config=CoordinatorConfig(),
                    ),
                    request_manager=_request_manager,
                )
                response = await cdp_router.handle_request()
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                chunk_str = "".join(chunks)

        assert req_info.state == ReqState.EXCEPTION
        assert error_message in chunk_str
        assert str(status.HTTP_500_INTERNAL_SERVER_ERROR) in chunk_str
        assert mock_async_client.stream_count == max_retry
        assert exec_release >= 1
        _assert_decode_retry_logs_deduped_for_label(
            caplog, "streaming Decode", max_retry
        )

    @pytest.mark.asyncio
    async def test_engine_server_decode_once_5xx_status_code(
        self, client, monkeypatch: MonkeyPatch, setup_cdp_separation
    ):
        """Test case: EngineServer Decode request first returns 5XX, then 200.
        Expected behavior:
        1) Check request status is Exception
        2) Trigger request retry
        3) Request retry succeeds
        """
        # Mock the HTTP stream forwarding function to return a 5XX error once
        error_message = "Test Internal Server Error"
        mock_async_client = MockAsyncClient(
            stream_exc=httpx.HTTPStatusError(
                message=error_message,
                request=MagicMock(),
                response=httpx.Response(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
                ),
            ),
            stream_fail_times=1,
        )
        req_info = await create_mock_request_info()

        with patch(
            "motor.coordinator.router.strategies.base.httpx.AsyncClient",
            return_value=mock_async_client,
        ):
            cdp_router = SeparateCDPRouter(
                req_info,
                CoordinatorConfig(),
                scheduler=Scheduler(
                    instance_provider=InstanceManager(CoordinatorConfig()),
                    config=CoordinatorConfig(),
                ),
                request_manager=_request_manager,
            )
            response = await cdp_router.handle_request()
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

            # Should get a 200 after retry
            assert response.status_code == status.HTTP_200_OK
            # Decode: at least one fail then success; stream_count may be 2 or up to max_retry
            assert mock_async_client.stream_fail_count == 1
            assert mock_async_client.stream_count >= 2
            # Decode path may use stream only; post is used for metaserver/other branches
            assert mock_async_client.post_count >= 0
            assert req_info.state == ReqState.DECODE_END

    @pytest.mark.asyncio
    async def test_engine_server_decode_network_exception(
        self, client, monkeypatch: MonkeyPatch, setup_cdp_separation
    ):
        """Test case: EngineServer Decode network exception
        Expected behavior:
        1) Check request status is Exception
        2) No request retry triggered
        3) Directly return error message
        """
        # Mock the HTTP forwarding function to always raise a network exception
        error_message = "Connection error"
        # mock AsyncClient in router
        mock_async_client = MockAsyncClient(
            stream_exc=httpx.ConnectError(error_message, request=MagicMock()),
            stream_fail_times=CoordinatorConfig().exception_config.max_retry,
        )

        req_info = await create_mock_request_info()

        with patch(
            "motor.coordinator.router.strategies.base.httpx.AsyncClient",
            return_value=mock_async_client,
        ):
            cdp_router = SeparateCDPRouter(
                req_info,
                CoordinatorConfig(),
                scheduler=Scheduler(
                    instance_provider=InstanceManager(CoordinatorConfig()),
                    config=CoordinatorConfig(),
                ),
                request_manager=_request_manager,
            )
            response = await cdp_router.handle_request()
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            chunk_str = "".join(chunks)
        assert error_message in chunk_str
        assert (
            mock_async_client.stream_count
            == CoordinatorConfig().exception_config.max_retry
        )
        assert (
            mock_async_client.stream_fail_count
            == CoordinatorConfig().exception_config.max_retry
        )
        assert req_info.state == ReqState.EXCEPTION

    @pytest.mark.asyncio
    async def test_cdp_decode_non_stream_retry_dedupes_identical_error_logs(
        self,
        client,
        monkeypatch: MonkeyPatch,
        setup_cdp_separation,
        caplog: pytest.LogCaptureFixture,
    ):
        """Non-streaming decode: same dedup rule as streaming."""
        error_message = "Same post Decode error every retry"
        max_retry = CoordinatorConfig().exception_config.max_retry
        mock_async_client = MockAsyncClient(
            post_exc=httpx.HTTPStatusError(
                message=error_message,
                request=MagicMock(),
                response=httpx.Response(
                    status_code=status.HTTP_502_BAD_GATEWAY, text=error_message
                ),
            ),
            post_fail_times=max_retry,
        )
        req_info = await create_mock_request_info(stream=False)

        async def mock_update_workload(
            self, resource: ScheduledResource, action: WorkloadAction
        ):
            return True

        monkeypatch.setattr(BaseRouter, "_update_workload", mock_update_workload)

        with caplog.at_level(
            logging.WARNING, logger="motor.coordinator.router.strategies.base"
        ):
            with patch(
                "motor.coordinator.router.strategies.base.httpx.AsyncClient",
                return_value=mock_async_client,
            ):
                cdp_router = SeparateCDPRouter(
                    req_info,
                    CoordinatorConfig(),
                    scheduler=Scheduler(
                        instance_provider=InstanceManager(CoordinatorConfig()),
                        config=CoordinatorConfig(),
                    ),
                    request_manager=_request_manager,
                )
                with pytest.raises(httpx.HTTPStatusError):
                    await cdp_router.handle_request()

        _assert_decode_retry_logs_deduped_for_label(caplog, "post Decode", max_retry)

    @pytest.mark.asyncio
    async def test_engine_server_prefill_network_exception(
        self, client, monkeypatch: MonkeyPatch, setup_cdp_separation
    ):
        """Test case: EngineServer network exception
        Expected behavior:
        1) Check request status is Exception
        2) No request retry triggered
        3) Directly return error message
        """
        # Mock the HTTP forwarding function to always raise a network exception
        error_message = "Connection error"
        retry_times = CoordinatorConfig().exception_config.max_retry
        # mock AsyncClient in router
        mock_async_client = MockAsyncClient(
            post_exc=httpx.ConnectError(message=error_message, request=MagicMock()),
            post_fail_times=retry_times,
        )

        state: ReqState = None

        def mock_update_state(self, new_state: ReqState):
            nonlocal state
            state = new_state

        monkeypatch.setattr(RequestInfo, "update_state", mock_update_state)

        with patch(
            "motor.coordinator.router.strategies.base.httpx.AsyncClient",
            return_value=mock_async_client,
        ):
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
        assert mock_async_client.post_count == retry_times
        assert mock_async_client.post_fail_count == retry_times
        assert state == ReqState.EXCEPTION

    @pytest.mark.asyncio
    async def test_degradation_to_single_node(
        self, monkeypatch: MonkeyPatch, setup_cdp_separation, mock_raw_request, client
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
        mock_endpoint_p = Endpoint(
            id=0, ip=host, business_port="8000", mgmt_port="8000"
        )
        mock_instance_p.endpoints = {host: {0: mock_endpoint_p}}

        def mock_get_available_instances(self, role):
            if role == PDRole.ROLE_U:  # PD hybrid role
                return []  # No PD hybrid instances, will use separate P/D
            elif role == PDRole.ROLE_P:
                return [mock_instance_p]
            elif role == PDRole.ROLE_D:
                return []
            return []

        monkeypatch.setattr(
            InstanceManager, "get_available_instances", mock_get_available_instances
        )

        # So router chooses SINGLE_NODE (PDHybridRouter) before creating the router
        def mock_get_required_instances_status(self, deploy_mode=None):
            return (
                InstanceReadiness.ONLY_PREFILL
            )  # not ready -> fallback to SINGLE_NODE

        monkeypatch.setattr(
            InstanceManager,
            "get_required_instances_status",
            mock_get_required_instances_status,
        )

        def mock_has_required_instances(self, deploy_mode=None):
            return False

        monkeypatch.setattr(
            InstanceManager, "has_required_instances", mock_has_required_instances
        )

        def mock_select_instance_and_endpoint(self, role):
            if role == PDRole.ROLE_P:
                return mock_instance_p, mock_endpoint_p
            elif role == PDRole.ROLE_D:
                return None, None
            return None, None

        monkeypatch.setattr(
            Scheduler, "select_instance_and_endpoint", mock_select_instance_and_endpoint
        )

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
    async def test_no_degradation_when_d_instances_exist(
        self, monkeypatch, setup_cdp_separation, mock_raw_request
    ):
        """
        Test that when ROLE_D instances are available, the router uses the configured mode (PD_SEPARATE).
        """
        # Let listen_for_disconnect() exit immediately (same as test_degradation_to_single_node).
        disconnect_msg = {"type": "http.disconnect"}
        mock_raw_request.receive = AsyncMock(return_value=disconnect_msg)

        # Mock SeparateCDPRouter response
        mock_response = "mock_message"
        with patch(
            "motor.coordinator.router.dispatch.SeparateCDPRouter.handle_request",
            return_value=mock_response,
        ) as mock_handle_request:
            response = await router.handle_request(
                mock_raw_request,
                CoordinatorConfig(),
                scheduler=_scheduler,
                request_manager=_request_manager,
            )

            # Verify SeparateCDPRouter.handle_request was called
            mock_handle_request.assert_called_once()
            # Verify response
            assert response == mock_response

    @pytest.mark.asyncio
    async def test_prompt_tokens_details_propagation(
        self, client, monkeypatch: MonkeyPatch, setup_cdp_separation
    ):
        """Test case: prompt_tokens_details from P role is properly propagated to D role response
        Expected behavior:
        1) P role returns usage with prompt_tokens_details
        2) D role includes prompt_tokens_details in final response
        3) RequestInfo is updated with prompt_tokens_details
        """

        prompt_tokens_details = {"cached_tokens": 10}

        req_info = await create_mock_request_info()
        req_info.update_prompt_tokens_details(prompt_tokens_details)

        async def mock_forward_request(
            self, req_data: dict, client: httpx.AsyncClient, timeout
        ):
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock()
            mock_response.aclose = AsyncMock(return_value=None)
            mock_response.json.return_value = {
                "choices": [{"message": {"content": "test response"}}],
                "usage": {
                    "prompt_tokens": 15,
                    "completion_tokens": 1,
                    "total_tokens": 16,
                },
            }
            return mock_response

        monkeypatch.setattr(SeparateCDPRouter, "forward_request", mock_forward_request)

        cdp_router = SeparateCDPRouter(
            req_info,
            CoordinatorConfig(),
            scheduler=Scheduler(
                instance_provider=InstanceManager(CoordinatorConfig()),
                config=CoordinatorConfig(),
            ),
            request_manager=_request_manager,
        )

        req_info.req_data["stream"] = False
        response = await cdp_router.handle_request()

        response_json = (
            response.body.decode()
            if hasattr(response.body, "decode")
            else response.body
        )
        response_data = json.loads(response_json)

        assert "usage" in response_data
        assert "prompt_tokens_details" in response_data["usage"]
        assert response_data["usage"]["prompt_tokens_details"] == prompt_tokens_details

        assert req_info.prompt_tokens_details == prompt_tokens_details
