#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from pytest import MonkeyPatch
from fastapi import FastAPI, status, Request
from fastapi.testclient import TestClient
import pytest

import motor.common.utils.error as cancel_error

from motor.config.coordinator import (
    CoordinatorConfig,
    SchedulerType,
    ExceptionConfig,
    TracerConfig,
)
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.router.strategies.pd_hybrid import PDHybridRouter
from motor.common.resources.instance import Endpoint, PDRole, Instance, InsStatus, ParallelConfig
from motor.common.resources.endpoint import Workload
from motor.coordinator.domain import InstanceReadiness
from motor.coordinator.scheduler.scheduler import Scheduler
from motor.coordinator.tracer.tracing import TracerManager
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.models.request import RequestInfo, ReqState
from motor.coordinator.router.upstream_error import UpstreamHTTPError
import motor.coordinator.router.dispatch as router
from motor.common.logger import get_logger

TracerManager()

logger = get_logger(__name__)

app = FastAPI()
_config = CoordinatorConfig()
_scheduler = Scheduler(instance_provider=InstanceManager(_config), config=_config)
_request_manager = RequestManager(_config)


@app.post("/v1/chat/completions")
async def handle_completions(request: Request):
    return await router.handle_request(request, _config, scheduler=_scheduler, request_manager=_request_manager)


@pytest.fixture(name="forward_stream_patch")
def patch_forward_stream_request(monkeypatch):
    """Mock forward_stream_request 并自动设置和清理"""

    async def mock_impl(self, api, req_data: dict, client, timeout, *, on_response_ready=None):
        if on_response_ready is not None:
            on_response_ready()
        responses = [
            b'{"choices": [{"text": "chunk 1"}]}',
            b'{"choices": [{"text": "chunk 2"}]}',
            b'{"choices": [{"text": "chunk 3"}]}',
        ]
        for chunk in responses:
            yield chunk
        trace_obj = getattr(self.req_info, "trace_obj", None)
        if trace_obj is not None:
            trace_obj.set_count_token(1)

    # Patch the forward_stream_request function to return an async generator directly
    monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_impl)
    yield mock_impl


class TestRouterPDHybrid:
    @pytest.fixture
    def client(self):
        return TestClient(app)

    @classmethod
    def create_mock_instance(cls, instance_id, role):
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
    def setup_pd_hybrid(self, monkeypatch: MonkeyPatch):
        # Create proper instance for PD hybrid flow
        mock_instance = self.create_mock_instance(0, PDRole.ROLE_U)
        mock_endpoint = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="8000")
        mock_instance.endpoints = {"127.0.0.1": {0: mock_endpoint}}

        # Mock functions (Scheduler uses get_required_instances_status for readiness)
        def mock_get_required_instances_status(self):
            return InstanceReadiness.REQUIRED_MET

        def mock_has_required_instances(self):
            return True

        async def mock_get_available_instance_roles(self):
            return {PDRole.ROLE_U}

        async def mock_select_and_allocate(self, role, req_info, **_kwargs):
            if role == PDRole.ROLE_U:
                return mock_instance, mock_endpoint, Workload()
            return None

        async def mock_update_workload(self, params):
            return True

        monkeypatch.setattr(InstanceManager, "get_required_instances_status", mock_get_required_instances_status)
        monkeypatch.setattr(InstanceManager, "has_required_instances", mock_has_required_instances)
        monkeypatch.setattr(Scheduler, "get_available_instance_roles", mock_get_available_instance_roles)
        monkeypatch.setattr(Scheduler, "select_and_allocate", mock_select_and_allocate)
        monkeypatch.setattr(Scheduler, "update_workload", mock_update_workload)

        mock_scheduler_config = MagicMock()
        mock_scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
        mock_exception_config = ExceptionConfig(max_retry=5, retry_delay=0.0001)
        mock_api_config = MagicMock()
        mock_api_config.coordinator_api_host = "127.0.0.1"
        mock_api_config.coordinator_api_mgmt_port = 1025

        mock_config = MagicMock()
        mock_config.scheduler_config = mock_scheduler_config
        mock_config.exception_config = mock_exception_config
        mock_config.api_config = mock_api_config

        monkeypatch.setattr(CoordinatorConfig, "__new__", lambda cls: mock_config)

    @pytest.mark.asyncio
    async def test_pd_hybrid_request_forwarding(self, monkeypatch: MonkeyPatch, setup_pd_hybrid, forward_stream_patch):
        """Test PD hybrid request forwarding functionality"""
        # Create a mock scope for the request
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
        }

        # Create a request body
        request_body = {"model": "test-model", "messages": [{"role": "user", "content": "Hello"}], "stream": True}

        # Create a mock request object
        request = Request(scope)
        request._body = json.dumps(request_body).encode()

        request_body = await request.body()
        req_len = len(request_body)
        request_json = await request.json()

        # Create a RequestInfo
        req_info = RequestInfo(
            req_id="test-id", req_data=request_json.copy(), req_len=req_len, api="v1/chat/completions"
        )

        # Test the PD hybrid forwarding function
        hybrid_router = PDHybridRouter(
            req_info,
            CoordinatorConfig(),
            scheduler=Scheduler(instance_provider=InstanceManager(CoordinatorConfig()), config=CoordinatorConfig()),
            request_manager=RequestManager(_config),
        )
        chunks = []

        response = await hybrid_router.handle_request()
        async for chunk in response.body_iterator:
            chunks.append(chunk)

        # Verify we got response chunks
        assert len(chunks) > 0
        # Verify request state was updated
        assert req_info.state == ReqState.DECODE_END

    @pytest.mark.asyncio
    async def test_pd_hybrid_request_failure(self, monkeypatch: MonkeyPatch, setup_pd_hybrid):
        """Test handling of PD hybrid request failure"""
        # Create a mock scope for the request
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
        }

        # Create a request body
        request_body = {"model": "test-model", "messages": [{"role": "user", "content": "Hello"}], "stream": True}

        # Create a mock request object
        request = Request(scope)
        request._body = json.dumps(request_body).encode()
        request_body = await request.body()
        req_len = len(request_body)
        request_json = await request.json()

        # Create a RequestInfo
        req_info = RequestInfo(
            req_id="test-id", req_data=request_json.copy(), req_len=req_len, api="v1/chat/completions"
        )

        error_message = "PD hybrid request failed"

        # Mock the stream request function to fail in PDHybridRouter
        async def failing_forward_stream_request(self, api, req_data, client, timeout, *, on_response_ready=None):
            raise RuntimeError(error_message)
            # Required so this mock remains an async generator for ``async for``.
            yield b""  # pylint: disable=unreachable

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", failing_forward_stream_request)

        # Test the PD hybrid forwarding function with failure
        hybrid_router = PDHybridRouter(
            req_info,
            CoordinatorConfig(),
            scheduler=Scheduler(instance_provider=InstanceManager(CoordinatorConfig()), config=CoordinatorConfig()),
            request_manager=RequestManager(_config),
        )
        # Create an async generator and consume it
        stream_resp = await hybrid_router.handle_request()
        chunks = []
        async for chunk in stream_resp.body_iterator:
            chunks.append(chunk)
        chunk_str = "".join(chunks)

        assert error_message in chunk_str

    @pytest.mark.asyncio
    async def test_successful_request_with_pd_hybrid(
        self, client, monkeypatch: MonkeyPatch, setup_pd_hybrid, forward_stream_patch
    ):
        """
        Expected behavior:
        1) Check request status is DecodeEnd
        2) Return normal response
        """

        response = client.post(
            "/v1/chat/completions",
            json={"model": "test-model", "messages": [{"role": "user", "content": "Hello"}], "stream": True},
        )

        # Should get a 200 success status
        assert response.status_code == status.HTTP_200_OK
        # Should be a streaming response
        assert "text/event-stream" in response.headers.get("content-type")

    def test_streaming_engine_400_is_forwarded_before_http_200(
        self,
        client,
        monkeypatch: MonkeyPatch,
        setup_pd_hybrid,
    ):
        attempts = 0
        error_body = b'{"error":{"message":"maximum context length is 4096 tokens","code":400}}'

        async def failing_forward_stream_request(
            self,
            api,
            req_data,
            client,
            timeout,
            *,
            on_response_ready=None,
        ):
            nonlocal attempts
            attempts += 1
            raise UpstreamHTTPError(
                status_code=400,
                body=error_body,
                headers={"content-type": "application/json"},
                phase="stream",
            )
            yield b""  # pylint: disable=unreachable

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", failing_forward_stream_request)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.content == error_body
        assert attempts == 1

    @pytest.mark.asyncio
    async def test_pd_hybrid_nonstream_adapts_text_completion_for_chat_entry(
        self, monkeypatch: MonkeyPatch, setup_pd_hybrid
    ):
        """Engine may return Completion JSON while client called /v1/chat/completions."""
        body = {
            "object": "text_completion",
            "id": "cmpl-adapt-test",
            "choices": [{"index": 0, "text": "hello", "finish_reason": "stop"}],
        }

        async def mock_forward(self, api, req_data, client, timeout):
            resp = MagicMock()
            resp.json = MagicMock(return_value=body)
            return resp

        monkeypatch.setattr(PDHybridRouter, "forward_request", mock_forward)

        req_info = RequestInfo(
            req_id="rid-p1-hybrid",
            req_data={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
            req_len=99,
            api="v1/chat/completions",
            entry_api="v1/chat/completions",
        )
        hybrid_router = PDHybridRouter(
            req_info,
            CoordinatorConfig(),
            scheduler=Scheduler(
                instance_provider=InstanceManager(CoordinatorConfig()),
                config=CoordinatorConfig(),
            ),
            request_manager=RequestManager(_config),
        )
        response = await hybrid_router.handle_request()
        assert response.status_code == status.HTTP_200_OK
        payload = json.loads(response.body.decode())
        assert payload["object"] == "chat.completion"
        assert payload["choices"][0]["message"]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_pd_hybrid_fallback_to_prefill_when_hybrid_pool_empty(self, monkeypatch: MonkeyPatch, caplog):
        """PD degradation: pre-check empty U pool, schedule ROLE_P directly without U attempt."""
        mock_instance = self.create_mock_instance(0, PDRole.ROLE_P)
        mock_endpoint = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="8000")
        mock_instance.endpoints = {"127.0.0.1": {0: mock_endpoint}}
        called_roles = []

        async def mock_get_available_instance_roles(self):
            return {PDRole.ROLE_P}

        async def mock_select_and_allocate(self, role, req_info, **_kwargs):
            called_roles.append(role)
            if role == PDRole.ROLE_P:
                return mock_instance, mock_endpoint, Workload()
            return None

        async def mock_update_workload(self, params):
            return True

        async def mock_forward(self, api, req_data, client, timeout):
            resp = MagicMock()
            resp.json = MagicMock(
                return_value={
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                }
            )
            return resp

        monkeypatch.setattr(Scheduler, "get_available_instance_roles", mock_get_available_instance_roles)
        monkeypatch.setattr(Scheduler, "select_and_allocate", mock_select_and_allocate)
        monkeypatch.setattr(Scheduler, "update_workload", mock_update_workload)
        monkeypatch.setattr(PDHybridRouter, "forward_request", mock_forward)

        req_info = RequestInfo(
            req_id="rid-fallback-p",
            req_data={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
            req_len=99,
            api="v1/chat/completions",
            entry_api="v1/chat/completions",
        )

        hybrid_router = PDHybridRouter(
            req_info,
            CoordinatorConfig(),
            scheduler=Scheduler(
                instance_provider=InstanceManager(CoordinatorConfig()),
                config=CoordinatorConfig(),
            ),
            request_manager=RequestManager(_config),
        )

        with caplog.at_level("WARNING"):
            response = await hybrid_router.handle_request()
        payload = json.loads(response.body.decode())

        assert response.status_code == status.HTTP_200_OK
        assert payload["choices"][0]["message"]["content"] == "ok"
        assert called_roles == [PDRole.ROLE_P]
        assert "Hybrid scheduling with role" not in caplog.text

    @pytest.mark.asyncio
    async def test_pd_hybrid_schedules_union_when_hybrid_pool_available(self, monkeypatch: MonkeyPatch):
        """True hybrid: pre-check finds U pool, schedule ROLE_U only."""
        mock_instance = self.create_mock_instance(0, PDRole.ROLE_U)
        mock_endpoint = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="8000")
        mock_instance.endpoints = {"127.0.0.1": {0: mock_endpoint}}
        called_roles = []

        async def mock_get_available_instance_roles(self):
            return {PDRole.ROLE_U}

        async def mock_select_and_allocate(self, role, req_info, **_kwargs):
            called_roles.append(role)
            if role == PDRole.ROLE_U:
                return mock_instance, mock_endpoint, Workload()
            return None

        async def mock_update_workload(self, params):
            return True

        async def mock_forward(self, api, req_data, client, timeout):
            resp = MagicMock()
            resp.json = MagicMock(
                return_value={
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "union-ok"}}],
                }
            )
            return resp

        monkeypatch.setattr(Scheduler, "get_available_instance_roles", mock_get_available_instance_roles)
        monkeypatch.setattr(Scheduler, "select_and_allocate", mock_select_and_allocate)
        monkeypatch.setattr(Scheduler, "update_workload", mock_update_workload)
        monkeypatch.setattr(PDHybridRouter, "forward_request", mock_forward)

        req_info = RequestInfo(
            req_id="rid-union-only",
            req_data={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
            req_len=99,
            api="v1/chat/completions",
            entry_api="v1/chat/completions",
        )

        hybrid_router = PDHybridRouter(
            req_info,
            CoordinatorConfig(),
            scheduler=Scheduler(
                instance_provider=InstanceManager(CoordinatorConfig()),
                config=CoordinatorConfig(),
            ),
            request_manager=RequestManager(_config),
        )

        response = await hybrid_router.handle_request()
        payload = json.loads(response.body.decode())

        assert response.status_code == status.HTTP_200_OK
        assert payload["choices"][0]["message"]["content"] == "union-ok"
        assert called_roles == [PDRole.ROLE_U]


def _make_tracer_coordinator_config(monkeypatch: MonkeyPatch) -> MagicMock:
    mock_scheduler_config = MagicMock()
    mock_scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
    mock_exception_config = ExceptionConfig(max_retry=5, retry_delay=0.0001, transport_max_retry=1)
    mock_api_config = MagicMock()
    mock_api_config.coordinator_api_host = "127.0.0.1"
    mock_api_config.coordinator_api_mgmt_port = 1025
    mock_config = MagicMock()
    mock_config.scheduler_config = mock_scheduler_config
    mock_config.exception_config = mock_exception_config
    mock_config.api_config = mock_api_config
    mock_config.tracer_config = TracerConfig()
    mock_config.infer_tls_config = None
    monkeypatch.setattr(CoordinatorConfig, "__new__", lambda cls: mock_config)
    return mock_config


def _make_hybrid_router(req_info: RequestInfo, monkeypatch: MonkeyPatch) -> PDHybridRouter:
    config = _make_tracer_coordinator_config(monkeypatch)
    return PDHybridRouter(
        req_info,
        config,
        scheduler=Scheduler(instance_provider=InstanceManager(config), config=config),
        request_manager=RequestManager(config),
    )


@contextmanager
def _record_span_names():
    span_names: list[str] = []

    @contextmanager
    def _recording_start_as_current_span(name, context=None):
        span_names.append(name)
        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        yield mock_span

    with patch.object(
        TracerManager().tracer,
        "start_as_current_span",
        side_effect=_recording_start_as_current_span,
    ):
        yield span_names


class TestPDHybridTracer:
    @pytest.fixture
    def setup_role_u_hybrid(self, monkeypatch: MonkeyPatch):
        mock_instance = TestRouterPDHybrid.create_mock_instance(0, PDRole.ROLE_U)
        mock_endpoint = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="8000")
        mock_instance.endpoints = {"127.0.0.1": {0: mock_endpoint}}

        async def mock_get_available_instance_roles(self):
            return {PDRole.ROLE_U}

        async def mock_select_and_allocate(self, role, req_info, **_kwargs):
            if role == PDRole.ROLE_U:
                return mock_instance, mock_endpoint, Workload()
            return None

        async def mock_update_workload(self, params):
            return True

        monkeypatch.setattr(Scheduler, "get_available_instance_roles", mock_get_available_instance_roles)
        monkeypatch.setattr(Scheduler, "select_and_allocate", mock_select_and_allocate)
        monkeypatch.setattr(Scheduler, "update_workload", mock_update_workload)

    @pytest.mark.asyncio
    async def test_stream_creates_inference_span(
        self, monkeypatch: MonkeyPatch, setup_role_u_hybrid, forward_stream_patch
    ):
        req_info = RequestInfo(
            req_id="tracer-stream-1",
            req_data={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
            req_len=99,
            api="v1/chat/completions",
        )
        hybrid_router = _make_hybrid_router(req_info, monkeypatch)

        with _record_span_names() as span_names:
            response = await hybrid_router.handle_request()
            async for _ in response.body_iterator:
                pass

        assert "PDHybrid_Stream" in span_names
        assert "PDHybrid_Inference" in span_names
        assert span_names.index("PDHybrid_Stream") < span_names.index("PDHybrid_Inference")
        assert req_info.trace_obj.meta_span is not None
        req_info.trace_obj.meta_span.set_attribute.assert_any_call("requestId", "tracer-stream-1")

    @pytest.mark.asyncio
    async def test_stream_emits_scheduling_trace_events(
        self, monkeypatch: MonkeyPatch, setup_role_u_hybrid, forward_stream_patch
    ):
        req_info = RequestInfo(
            req_id="tracer-stream-2",
            req_data={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
            req_len=99,
            api="v1/chat/completions",
        )
        hybrid_router = _make_hybrid_router(req_info, monkeypatch)
        trace_obj = req_info.trace_obj
        event_names: list[str] = []
        original_add_event = trace_obj.add_trace_event

        def _capture_event(name, attributes=None, timestamp=None, is_meta=False):
            event_names.append(name)
            return original_add_event(name, attributes, timestamp, is_meta)

        monkeypatch.setattr(trace_obj, "add_trace_event", _capture_event)

        response = await hybrid_router.handle_request()
        async for _ in response.body_iterator:
            pass

        assert "Begin Scheduled Resource" in event_names
        assert "Scheduled Resource ok" in event_names

    @pytest.mark.asyncio
    async def test_stream_sets_ttft_on_success(self, monkeypatch: MonkeyPatch, setup_role_u_hybrid):
        async def mock_impl(self, api, req_data: dict, client, timeout, *, on_response_ready=None):
            if on_response_ready is not None:
                on_response_ready()
            trace_obj = self.req_info.trace_obj
            trace_obj.set_time_first_token()
            yield b'{"choices": [{"text": "chunk 1"}]}'
            trace_obj.set_count_token(1)

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_impl)

        req_info = RequestInfo(
            req_id="tracer-stream-3",
            req_data={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "stream": True,
            },
            req_len=99,
            api="v1/chat/completions",
        )
        hybrid_router = _make_hybrid_router(req_info, monkeypatch)
        attribute_calls: list[tuple[str, str]] = []
        original_set_attr = req_info.trace_obj.set_trace_attribute

        def _capture_set_attr(key, value, is_meta=False):
            if not is_meta:
                attribute_calls.append((key, str(value)))
            return original_set_attr(key, value, is_meta)

        monkeypatch.setattr(req_info.trace_obj, "set_trace_attribute", _capture_set_attr)

        response = await hybrid_router.handle_request()
        async for _ in response.body_iterator:
            pass

        attribute_keys = [key for key, _ in attribute_calls]
        assert "TTFT(ms)" in attribute_keys
        assert "TOKEN_COUNT" in attribute_keys

    @pytest.mark.asyncio
    async def test_nonstream_creates_inference_span(self, monkeypatch: MonkeyPatch, setup_role_u_hybrid):
        async def mock_forward(self, api, req_data, client, timeout):
            resp = MagicMock()
            resp.json = MagicMock(
                return_value={
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                }
            )
            return resp

        monkeypatch.setattr(PDHybridRouter, "forward_request", mock_forward)

        req_info = RequestInfo(
            req_id="tracer-post-1",
            req_data={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
            req_len=99,
            api="v1/chat/completions",
        )
        hybrid_router = _make_hybrid_router(req_info, monkeypatch)

        with _record_span_names() as span_names:
            await hybrid_router.handle_request()

        assert "PDHybrid" in span_names
        assert "PDHybrid_Inference" in span_names
        assert span_names.index("PDHybrid") < span_names.index("PDHybrid_Inference")

    @pytest.mark.asyncio
    async def test_role_fallback_scheduling_events(self, monkeypatch: MonkeyPatch):
        mock_instance = TestRouterPDHybrid.create_mock_instance(0, PDRole.ROLE_P)
        mock_endpoint = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="8000")
        mock_instance.endpoints = {"127.0.0.1": {0: mock_endpoint}}

        async def mock_get_available_instance_roles(self):
            return {PDRole.ROLE_P}

        async def mock_select_and_allocate(self, role, req_info, **_kwargs):
            if role == PDRole.ROLE_P:
                return mock_instance, mock_endpoint, Workload()
            return None

        async def mock_update_workload(self, params):
            return True

        async def mock_forward(self, api, req_data, client, timeout):
            resp = MagicMock()
            resp.json = MagicMock(
                return_value={
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                }
            )
            return resp

        monkeypatch.setattr(Scheduler, "get_available_instance_roles", mock_get_available_instance_roles)
        monkeypatch.setattr(Scheduler, "select_and_allocate", mock_select_and_allocate)
        monkeypatch.setattr(Scheduler, "update_workload", mock_update_workload)
        monkeypatch.setattr(PDHybridRouter, "forward_request", mock_forward)

        req_info = RequestInfo(
            req_id="tracer-fallback-1",
            req_data={
                "model": "test-model",
                "messages": [{"role": "user", "content": "Hi"}],
                "stream": False,
            },
            req_len=99,
            api="v1/chat/completions",
        )
        hybrid_router = _make_hybrid_router(req_info, monkeypatch)
        trace_obj = req_info.trace_obj
        event_names: list[str] = []
        original_add_event = trace_obj.add_trace_event

        def _capture_event(name, attributes=None, timestamp=None, is_meta=False):
            event_names.append(name)
            return original_add_event(name, attributes, timestamp, is_meta)

        monkeypatch.setattr(trace_obj, "add_trace_event", _capture_event)

        await hybrid_router.handle_request()

        assert event_names.count("Begin Scheduled Resource") == 1
        assert event_names.count("Scheduled Resource ok") == 1


def _make_cancel_test_config(
    monkeypatch: MonkeyPatch,
    *,
    transport_max_retry: int = 2,
    reschedule_enabled: bool = True,
) -> MagicMock:
    mock_scheduler_config = MagicMock()
    mock_scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
    mock_exception_config = ExceptionConfig(
        max_retry=5,
        retry_delay=0.0001,
        transport_max_retry=transport_max_retry,
        reschedule_enabled=reschedule_enabled,
    )
    mock_api_config = MagicMock()
    mock_api_config.coordinator_api_host = "127.0.0.1"
    mock_api_config.coordinator_api_mgmt_port = 1025
    mock_config = MagicMock()
    mock_config.scheduler_config = mock_scheduler_config
    mock_config.exception_config = mock_exception_config
    mock_config.api_config = mock_api_config
    mock_config.tracer_config = TracerConfig()
    mock_config.infer_tls_config = None
    monkeypatch.setattr(CoordinatorConfig, "__new__", lambda cls: mock_config)
    return mock_config


class TestPDHybridCancelReschedule:
    """PD hybrid alignment with unified PD: cancel-reason aware retry and abort."""

    @pytest.fixture(name="hybrid_pool")
    def _hybrid_pool(self, monkeypatch: MonkeyPatch):
        mock_instance = TestRouterPDHybrid.create_mock_instance(0, PDRole.ROLE_U)
        mock_endpoint = Endpoint(id=0, ip="127.0.0.1", business_port="8000", mgmt_port="8000")
        mock_instance.endpoints = {"127.0.0.1": {0: mock_endpoint}}

        async def mock_get_available_instance_roles(self):
            return {PDRole.ROLE_U}

        async def mock_select_and_allocate(self, role, req_info, **_kwargs):
            if role == PDRole.ROLE_U:
                return mock_instance, mock_endpoint, Workload()
            return None

        async def mock_update_workload(self, params):
            return True

        monkeypatch.setattr(Scheduler, "get_available_instance_roles", mock_get_available_instance_roles)
        monkeypatch.setattr(Scheduler, "select_and_allocate", mock_select_and_allocate)
        monkeypatch.setattr(Scheduler, "update_workload", mock_update_workload)

    @staticmethod
    def _build_router(config, req_data: dict, api: str = "v1/completions") -> PDHybridRouter:
        req_info = RequestInfo(
            req_id="cancel-resched-id",
            req_data=req_data.copy(),
            req_len=99,
            api=api,
            entry_api=api,
        )
        return PDHybridRouter(
            req_info,
            config,
            scheduler=Scheduler(instance_provider=InstanceManager(config), config=config),
            request_manager=RequestManager(config),
        )

    @staticmethod
    async def _consume_stream(response) -> str:
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    @staticmethod
    def _asgi_scope() -> dict:
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/completions",
            "raw_path": b"/v1/completions",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        }

    @pytest.mark.asyncio
    async def test_stream_node_fault_reschedules_with_token_replay(self, monkeypatch: MonkeyPatch, hybrid_pool):
        """Node-fault cancel mid-stream: reschedule and continue from cached token ids."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=2, reschedule_enabled=True)
        calls: list[tuple[str, dict]] = []

        async def mock_forward(self, api, req_data, client, timeout, *, on_response_ready=None):
            calls.append((api, req_data.copy()))
            if len(calls) == 1:
                if on_response_ready is not None:
                    on_response_ready()
                self.first_chunk_sent = True
                yield (
                    b'data: {"choices": [{"index": 0, "text": "A", "prompt_token_ids": [1, 2], "token_ids": [10]}]}\n\n'
                )
                raise asyncio.CancelledError(f"{cancel_error.NODE_FAULT}: http://127.0.0.1:8000")
            if on_response_ready is not None:
                on_response_ready()
            yield b'data: {"choices": [{"index": 0, "text": "B", "token_ids": [11], "finish_reason": "stop"}]}\n\n'

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_forward)
        router_obj = self._build_router(
            config,
            {"model": "test-model", "prompt": "Hello", "stream": True, "max_tokens": 50},
        )

        response = await router_obj.handle_request()
        body = await self._consume_stream(response)

        assert len(calls) == 2
        # First leg must request token ids so the rescheduler can cache progress.
        assert calls[0][1].get("return_token_ids") is True
        # Retry leg continues from prompt + cached output token ids with reduced budget.
        assert calls[1][1]["prompt"] == [1, 2, 10]
        assert calls[1][1]["max_tokens"] == 49
        assert calls[1][0] == "v1/completions"
        assert "A" in body and "B" in body
        assert "error" not in body
        # Internal token id fields must not leak to the client.
        assert "token_ids" not in body

    @pytest.mark.asyncio
    async def test_stream_client_disconnect_does_not_retry(self, monkeypatch: MonkeyPatch, hybrid_pool):
        """Client disconnect cancel must propagate without rescheduling."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=3, reschedule_enabled=True)
        calls: list[str] = []

        async def mock_forward(self, api, req_data, client, timeout, *, on_response_ready=None):
            calls.append(api)
            self.first_chunk_sent = True
            yield b'data: {"choices": [{"index": 0, "text": "A", "token_ids": [10]}]}\n\n'
            raise asyncio.CancelledError(cancel_error.CLIENT_DISCONNECT)

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_forward)
        router_obj = self._build_router(
            config,
            {"model": "test-model", "prompt": "Hello", "stream": True, "max_tokens": 50},
        )

        response = await router_obj.handle_request()
        with pytest.raises(asyncio.CancelledError):
            await self._consume_stream(response)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_stream_scope_abort_does_not_retry(self, monkeypatch: MonkeyPatch, hybrid_pool):
        """Uvicorn/anyio scope cancellation must propagate without rescheduling."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=3, reschedule_enabled=True)
        calls: list[str] = []

        async def mock_forward(self, api, req_data, client, timeout, *, on_response_ready=None):
            calls.append(api)
            raise asyncio.CancelledError("Cancelled via cancel scope ffff980d3d10")
            yield b""  # pylint: disable=unreachable

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_forward)
        router_obj = self._build_router(
            config,
            {"model": "test-model", "prompt": "Hello", "stream": True, "max_tokens": 50},
        )

        response = await router_obj.handle_request()
        with pytest.raises(asyncio.CancelledError):
            await self._consume_stream(response)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_stream_node_fault_without_reschedule_after_first_chunk_errors(
        self, monkeypatch: MonkeyPatch, hybrid_pool
    ):
        """Token replay disabled + chunk already sent: keep error-chunk termination."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=3, reschedule_enabled=False)
        calls: list[str] = []

        async def mock_forward(self, api, req_data, client, timeout, *, on_response_ready=None):
            calls.append(api)
            if on_response_ready is not None:
                on_response_ready()
            self.first_chunk_sent = True
            yield b'data: {"choices": [{"index": 0, "text": "A"}]}\n\n'
            raise asyncio.CancelledError(f"{cancel_error.NODE_FAULT}: http://127.0.0.1:8000")

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_forward)
        router_obj = self._build_router(
            config,
            {"model": "test-model", "prompt": "Hello", "stream": True, "max_tokens": 50},
        )

        response = await router_obj.handle_request()
        body = await self._consume_stream(response)

        assert len(calls) == 1
        assert "error" in body or "Cancelled" in body
        assert router_obj.req_info.state == ReqState.EXCEPTION

    @pytest.mark.asyncio
    async def test_nonstream_node_fault_cancel_retries(self, monkeypatch: MonkeyPatch, hybrid_pool):
        """Non-streaming node-fault cancel: resend the request to another attempt."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=2, reschedule_enabled=True)
        calls: list[str] = []

        async def mock_forward(self, api, req_data, client, timeout):
            calls.append(api)
            if len(calls) == 1:
                raise asyncio.CancelledError(f"{cancel_error.NODE_FAULT}: http://127.0.0.1:8000")
            resp = MagicMock()
            resp.json = MagicMock(
                return_value={
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                }
            )
            return resp

        monkeypatch.setattr(PDHybridRouter, "forward_request", mock_forward)
        router_obj = self._build_router(
            config,
            {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}], "stream": False},
            api="v1/chat/completions",
        )

        response = await router_obj.handle_request()
        payload = json.loads(response.body.decode())

        assert len(calls) == 2
        assert response.status_code == status.HTTP_200_OK
        assert payload["choices"][0]["message"]["content"] == "ok"

    @pytest.mark.asyncio
    async def test_nonstream_client_disconnect_does_not_retry(self, monkeypatch: MonkeyPatch, hybrid_pool):
        """Non-streaming client disconnect cancel: no retry, propagate cancellation."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=3, reschedule_enabled=True)
        calls: list[str] = []

        async def mock_forward(self, api, req_data, client, timeout):
            calls.append(api)
            raise asyncio.CancelledError(cancel_error.CLIENT_DISCONNECT)

        monkeypatch.setattr(PDHybridRouter, "forward_request", mock_forward)
        router_obj = self._build_router(
            config,
            {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}], "stream": False},
            api="v1/chat/completions",
        )

        with pytest.raises(asyncio.CancelledError):
            await router_obj.handle_request()
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_stream_client_disconnect_logs_instance_at_warning(
        self, monkeypatch: MonkeyPatch, hybrid_pool, caplog
    ):
        """Client disconnect must be visible at WARNING with the serving instance, like unified PD."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=3, reschedule_enabled=True)

        async def mock_forward(self, api, req_data, client, timeout, *, on_response_ready=None):
            self.first_chunk_sent = True
            yield b'data: {"choices": [{"index": 0, "text": "A"}]}\n\n'
            raise asyncio.CancelledError(cancel_error.CLIENT_DISCONNECT)

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_forward)
        router_obj = self._build_router(
            config,
            {"model": "test-model", "prompt": "Hello", "stream": True, "max_tokens": 50},
        )

        response = await router_obj.handle_request()
        with caplog.at_level("WARNING"):
            with pytest.raises(asyncio.CancelledError):
                await self._consume_stream(response)

        cancelled_records = [r for r in caplog.records if "Cancelled stream" in r.getMessage()]
        assert cancelled_records, "client disconnect must log at WARNING or above"
        message = cancelled_records[0].getMessage()
        assert cancel_error.CLIENT_DISCONNECT in message
        assert "127.0.0.1" in message and "test-job-0" in message

    @pytest.mark.asyncio
    async def test_nonstream_client_disconnect_logs_instance_at_warning(
        self, monkeypatch: MonkeyPatch, hybrid_pool, caplog
    ):
        """Non-stream client disconnect must log the serving instance at WARNING."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=3, reschedule_enabled=True)

        async def mock_forward(self, api, req_data, client, timeout):
            raise asyncio.CancelledError(cancel_error.CLIENT_DISCONNECT)

        monkeypatch.setattr(PDHybridRouter, "forward_request", mock_forward)
        router_obj = self._build_router(
            config,
            {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}], "stream": False},
            api="v1/chat/completions",
        )

        with caplog.at_level("WARNING"):
            with pytest.raises(asyncio.CancelledError):
                await router_obj.handle_request()

        cancelled_records = [r for r in caplog.records if "Cancelled nonstream" in r.getMessage()]
        assert cancelled_records, "client disconnect must log at WARNING or above"
        message = cancelled_records[0].getMessage()
        assert cancel_error.CLIENT_DISCONNECT in message
        assert "127.0.0.1" in message and "test-job-0" in message

    @pytest.mark.asyncio
    async def test_stream_cancel_all_cancels_inflight_and_reschedules(self, monkeypatch: MonkeyPatch, hybrid_pool):
        """HTTP pool cancel_all (node fault) cancels in-flight hybrid stream and triggers reschedule."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=2, reschedule_enabled=False)
        calls: list[str] = []
        started = asyncio.Event()
        clients: list = []

        async def mock_forward(self, api, req_data, client, timeout, *, on_response_ready=None):
            calls.append(api)
            clients.append(client)
            if len(calls) == 1:
                started.set()
                await asyncio.Event().wait()  # block until cancelled by cancel_all
            yield b'data: {"choices": [{"index": 0, "text": "ok"}]}\n\n'

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_forward)
        router_obj = self._build_router(
            config,
            {"model": "test-model", "prompt": "Hello", "stream": True, "max_tokens": 50},
        )

        response = await router_obj.handle_request()
        consume_task = asyncio.create_task(self._consume_stream(response))
        await asyncio.wait_for(started.wait(), timeout=5)

        # Canceller must be registered on the endpoint client while the attempt is in flight.
        client_ctx = clients[0]
        assert router_obj.req_info.req_id in client_ctx._cancellers
        await client_ctx.cancel_all()

        body = await asyncio.wait_for(consume_task, timeout=5)
        assert len(calls) == 2
        assert "ok" in body
        # Canceller must be unregistered once the request finishes.
        assert router_obj.req_info.req_id not in client_ctx._cancellers

    @pytest.mark.asyncio
    async def test_stream_node_fault_after_headers_before_body_reschedules(self, monkeypatch: MonkeyPatch, hybrid_pool):
        """HTTP 200 alone must not prevent retry before any body reaches the client."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=2, reschedule_enabled=False)
        calls: list[str] = []
        clients: list = []
        first_attempt_started = asyncio.Event()
        messages: list[dict] = []

        async def mock_forward(self, api, req_data, client, timeout, *, on_response_ready=None):
            calls.append(api)
            clients.append(client)
            if on_response_ready is not None:
                on_response_ready()
            if len(calls) == 1:
                first_attempt_started.set()
                await asyncio.Event().wait()
            yield b'data: {"choices": [{"index": 0, "text": "ok"}]}\n\n'

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            messages.append(message)

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_forward)
        router_obj = self._build_router(
            config,
            {"model": "test-model", "prompt": "Hello", "stream": True, "max_tokens": 50},
        )

        response = await router_obj.handle_request()
        response_task = asyncio.create_task(response(self._asgi_scope(), receive, send))
        await asyncio.wait_for(first_attempt_started.wait(), timeout=5)
        while not any(message["type"] == "http.response.start" for message in messages):
            await asyncio.sleep(0)

        await clients[0].cancel_all()
        await asyncio.wait_for(response_task, timeout=5)

        body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        assert calls == ["v1/completions", "v1/completions"]
        assert [message["status"] for message in messages if message["type"] == "http.response.start"] == [200]
        assert b"ok" in body

    @pytest.mark.asyncio
    async def test_asgi_client_disconnect_does_not_reschedule(self, monkeypatch: MonkeyPatch, hybrid_pool):
        """The ASGI disconnect cancellation reason must reach the router unchanged."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=2, reschedule_enabled=True)
        calls: list[str] = []
        first_attempt_started = asyncio.Event()

        async def mock_forward(self, api, req_data, client, timeout, *, on_response_ready=None):
            calls.append(api)
            if on_response_ready is not None:
                on_response_ready()
            first_attempt_started.set()
            await asyncio.Event().wait()
            yield b""  # pragma: no cover

        async def receive():
            await first_attempt_started.wait()
            return {"type": "http.disconnect"}

        async def send(_message):
            return None

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_forward)
        router_obj = self._build_router(
            config,
            {"model": "test-model", "prompt": "Hello", "stream": True, "max_tokens": 50},
        )

        response = await router_obj.handle_request()
        await asyncio.wait_for(response(self._asgi_scope(), receive, send), timeout=5)

        assert calls == ["v1/completions"]

    @pytest.mark.asyncio
    async def test_stream_retry_validation_error_is_not_retried(self, monkeypatch: MonkeyPatch, hybrid_pool):
        """A deterministic token-replay validation error must terminate immediately."""
        config = _make_cancel_test_config(monkeypatch, transport_max_retry=3, reschedule_enabled=True)
        calls: list[str] = []

        async def mock_forward(self, api, req_data, client, timeout, *, on_response_ready=None):
            calls.append(api)
            if on_response_ready is not None:
                on_response_ready()
            yield (b'data: {"choices": [{"index": 0, "text": "A", "prompt_token_ids": [1, 2], "token_ids": [10]}]}\n\n')
            raise asyncio.CancelledError(f"{cancel_error.NODE_FAULT}: http://127.0.0.1:8000")

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_forward)
        router_obj = self._build_router(
            config,
            {
                "model": "test-model",
                "prompt": "Hello",
                "stream": True,
                "max_tokens": 50,
                "n": 2,
            },
        )

        response = await router_obj.handle_request()
        body = await self._consume_stream(response)

        assert calls == ["v1/completions"]
        assert "parallel sampling" in body
