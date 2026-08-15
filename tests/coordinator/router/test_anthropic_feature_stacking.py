# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Coordinator tests for Anthropic feature-stacking fixes.

Covers: rescheduling degradation guards, PD correctness (concurrent + handoff),
count_tokens lightweight routing, precision-sampling exclusion, EPD multimodal
detection, and the Anthropic error envelope for coordinator-synthesized errors.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from motor.common.logger import get_logger
from motor.common.resources.dispatch import (
    DispatchPlan,
    MOTOR_DISPATCH_KEY,
    MOTOR_PREFILL_RESULT_KEY,
)
from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.common.resources.instance import Instance, InsStatus, ParallelConfig, PDRole
from motor.config.coordinator import CoordinatorConfig, ExceptionConfig, SchedulerType
from motor.coordinator.domain import InstanceReadiness, ScheduledResource
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.router import dispatch
from motor.coordinator.router.anthropic_envelope import anthropic_error_type_for_status
from motor.coordinator.router.rescheduler.rescheduler import Rescheduler
from motor.coordinator.router.strategies.pd_hybrid import PDHybridRouter
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter
from motor.coordinator.router.stream_response import (
    CommitAwareStreamingResponse,
    StreamCommitController,
)
from motor.coordinator.router.upstream_error import UpstreamHTTPError

logger = get_logger(__name__)

ANTHROPIC_ENTRY_API = "v1/messages"
COUNT_TOKENS_API = "v1/messages/count_tokens"

ANTHROPIC_FRAMES = [
    b'event: message_start\ndata: {"type": "message_start", "message": {"id": "msg_01", "type": "message", '
    b'"role": "assistant", "content": [], "model": "test-model", "stop_reason": null, "usage": '
    b'{"input_tokens": 10, "output_tokens": 1}}}\n\n',
    b'event: content_block_start\ndata: {"type": "content_block_start", "index": 0, '
    b'"content_block": {"type": "text", "text": ""}}\n\n',
    b'event: ping\ndata: {"type": "ping"}\n\n',
    b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, '
    b'"delta": {"type": "text_delta", "text": "Hello"}}\n\n',
    b'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, '
    b'"usage": {"output_tokens": 3}}\n\n',
    b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
]

ANTHROPIC_MESSAGE_BODY = {
    "id": "msg_01",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "Hello"}],
    "model": "test-model",
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 3},
}


def _anthropic_req_data(stream: bool = True) -> dict:
    """Anthropic request body incl. system prompt, content blocks, and thinking."""
    return {
        "model": "test-model",
        "system": "You are helpful",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello"},
                ],
            }
        ],
        "thinking": {"type": "enabled", "budget_tokens": 64},
        "max_tokens": 10,
        "stream": stream,
    }


def _req_info(
    req_data: dict,
    *,
    api: str = ANTHROPIC_ENTRY_API,
    entry_api: str | None = None,
) -> RequestInfo:
    return RequestInfo(
        req_id="test-req",
        req_data=req_data,
        req_len=1,
        api=api,
        entry_api=entry_api if entry_api is not None else api,
        client_expects_chat_shape=("messages" in req_data),
    )


def _openai_req_info(stream: bool = True) -> RequestInfo:
    return _req_info(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": stream, "max_tokens": 10},
        api="v1/chat/completions",
    )


def _config() -> CoordinatorConfig:
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
    config.exception_config = ExceptionConfig(max_retry=1, retry_delay=0)
    return config


def _instance(instance_id: int, role: PDRole, *, dispatch_capabilities: list[str] | None = None) -> Instance:
    endpoint = Endpoint(
        id=instance_id,
        ip="127.0.0.1",
        business_port=str(8100 + instance_id),
        mgmt_port=str(9100 + instance_id),
        status=EndpointStatus.NORMAL,
    )
    return Instance(
        job_name=f"job-{instance_id}",
        model_name="model",
        dispatch_capabilities=dispatch_capabilities or [],
        id=instance_id,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1),
        endpoints={endpoint.ip: {endpoint.id: endpoint}},
    )


class _PDScheduler:
    """Fake scheduler handing out one P and one D instance."""

    def __init__(self, capabilities: list[str] | None = None):
        caps = capabilities or [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]
        self.p = _instance(1, PDRole.ROLE_P, dispatch_capabilities=caps)
        self.d = _instance(2, PDRole.ROLE_D, dispatch_capabilities=caps)
        self.update_workload = AsyncMock(return_value=True)
        self.allocate_calls: list[PDRole] = []

    async def select_and_allocate(self, role, req_info, **_kwargs):
        self.allocate_calls.append(PDRole(role))
        instance = self.p if role == PDRole.ROLE_P else self.d
        endpoint = next(iter(next(iter(instance.endpoints.values())).values()))
        return instance, endpoint, Workload(active_kv_cache=1, active_tokens=1)

    async def report_cb_event(self, instance_id: int, event: str) -> None:
        """No-op stub for circuit-breaker reporting."""

    async def get_unblocked_instances(self, role) -> list:
        return [self.p.id, self.d.id]

    async def get_available_instance_roles(self) -> set:
        return {PDRole.ROLE_P, PDRole.ROLE_D}

    async def has_compatible_pd_pair(self) -> bool:
        return True


class _HybridScheduler:
    """Fake scheduler handing out a single union instance."""

    def __init__(self, readiness: InstanceReadiness = InstanceReadiness.REQUIRED_MET):
        self.u = _instance(3, PDRole.ROLE_U)
        self.update_workload = AsyncMock(return_value=True)
        self.allocate_calls: list[PDRole] = []
        self._readiness = readiness

    async def select_and_allocate(self, role, req_info, **_kwargs):
        self.allocate_calls.append(PDRole(role))
        endpoint = next(iter(next(iter(self.u.endpoints.values())).values()))
        return self.u, endpoint, Workload(active_kv_cache=1, active_tokens=1)

    async def report_cb_event(self, instance_id: int, event: str) -> None:
        """No-op stub for circuit-breaker reporting."""

    async def get_unblocked_instances(self, role) -> list:
        return [self.u.id] if role == PDRole.ROLE_U else []

    async def has_required_instances(self) -> InstanceReadiness:
        return self._readiness


class _Client:
    def __init__(self, name: str, json_body: dict | None = None):
        self.name = name
        self.json_body = json_body
        self.requests: list[dict] = []
        self.base_url = f"http://{name}"
        self.timeout = 1

    async def post(self, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        if self.json_body is not None:
            return httpx.Response(status_code=200, json=self.json_body, request=request)
        return httpx.Response(
            status_code=200,
            json={"status": "cached", "id": json["request_id"]},
            request=request,
        )


class _StreamResponse:
    def __init__(self, chunks: list[bytes], exc_after_chunks: Exception | None = None):
        self.chunks = chunks
        self.exc_after_chunks = exc_after_chunks
        self.status_code = 200
        self.is_success = True
        self.text = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def aread(self):
        return b""

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk
        if self.exc_after_chunks is not None:
            raise self.exc_after_chunks


class _StreamClient(_Client):
    def __init__(self, name: str, chunks: list[bytes], exc_after_chunks: Exception | None = None):
        super().__init__(name)
        self.chunks = chunks
        self.exc_after_chunks = exc_after_chunks

    def stream(self, method, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        return _StreamResponse(self.chunks, exc_after_chunks=self.exc_after_chunks)


class _AnthropicPrefillResultClient(_Client):
    """Prefill client short-circuiting to a valid PrefillResult (handoff/CPCD)."""

    async def post(self, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        dispatch_meta = json[MOTOR_DISPATCH_KEY]
        return httpx.Response(
            status_code=200,
            json={
                "object": "motor.prefill_result",
                "schema_version": "1.0",
                "root_request_id": dispatch_meta["root_request_id"],
                "engine_request_id": dispatch_meta["engine_request_id"],
                "pair_id": dispatch_meta["pair_id"],
                "attempt_seq": dispatch_meta["attempt_seq"],
                "status": "completed",
                "handoff_mode": "handoff",
                "payload": {"opaque": "kv"},
            },
            request=request,
        )


def _client_dispatcher(router, p_client, d_client, monkeypatch):
    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)


def _patch_dispatch_stop(monkeypatch):
    async def _stop(self, resource, attempt, reason, timeout=1.0):
        return None

    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )


def _asgi_scope() -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/messages",
        "raw_path": b"/v1/messages",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
    }


async def _never_disconnect():
    await asyncio.Event().wait()
    return {"type": "http.disconnect"}


async def _invoke_asgi_response(response) -> list[dict]:
    messages = []

    async def send(message):
        messages.append(message)

    await response(_asgi_scope(), _never_disconnect, send)
    return messages


def _response_bodies(messages: list[dict]) -> list[bytes]:
    return [m["body"] for m in messages if m["type"] == "http.response.body" and m["body"]]


# ---------------------------------------------------------------------------
# Section 5: rescheduling degradation guards
# ---------------------------------------------------------------------------


class TestAnthropicReschedulerDegradation:
    def test_can_resume_explicit_false_for_anthropic(self):
        """Explicit protocol gate: False even with populated token caches and enable=True."""
        req = _req_info(_anthropic_req_data())
        req.prompt_token_ids = [1, 2]
        req.cached_token_ids = [3]
        resch = Rescheduler(True, req, logger=logger)
        assert resch.can_resume_after_visible_output(req.req_data) is False

    def test_can_resume_unchanged_for_openai(self):
        req = _openai_req_info()
        req.prompt_token_ids = [1, 2]
        req.cached_token_ids = [3]
        resch = Rescheduler(True, req, logger=logger)
        assert resch.can_resume_after_visible_output(req.req_data) is True

    def test_prepare_retry_request_returns_original_body_for_anthropic(self):
        """Pre-commit retry re-sends the original Anthropic body; never v1/completions."""
        req = _req_info(_anthropic_req_data())
        req.prompt_token_ids = [1, 2]
        req.cached_token_ids = [3]
        resch = Rescheduler(True, req, logger=logger)
        body, api = resch.prepare_retry_request(dict(req.req_data))
        assert api == ANTHROPIC_ENTRY_API
        assert "prompt" not in body
        assert body["messages"] == req.req_data["messages"]
        assert body["system"] == "You are helpful"
        assert body["max_tokens"] == 10

    def test_build_retry_plan_none_for_anthropic(self):
        """No token-replay plan is ever built for Anthropic, even with cached ids."""
        req = _req_info(_anthropic_req_data())
        req.prompt_token_ids = [1, 2]
        req.cached_token_ids = [3]
        resch = Rescheduler(True, req, logger=logger)
        assert resch.build_retry_plan(req.req_data) is None

    def test_retry_never_emits_openai_shaped_chunks_for_anthropic(self):
        """Mid-retry, even an OpenAI completion-shaped chunk is never adapted for chat."""
        req = _req_info(_anthropic_req_data())
        resch = Rescheduler(True, req, logger=logger)
        resch.is_rescheduling = True
        completion_chunk = b'data: {"choices":[{"text":"tok","index":0,"token_ids":[5]}]}\n\n'
        assert resch.process_stream_chunk(completion_chunk) == completion_chunk
        for frame in ANTHROPIC_FRAMES:
            assert resch.process_stream_chunk(frame) == frame


# ---------------------------------------------------------------------------
# Section 9: Anthropic error envelope
# ---------------------------------------------------------------------------


class TestAnthropicErrorEnvelope:
    def test_error_type_mapping(self):
        assert anthropic_error_type_for_status(400) == "invalid_request_error"
        assert anthropic_error_type_for_status(404) == "not_found_error"
        assert anthropic_error_type_for_status(429) == "rate_limit_error"
        assert anthropic_error_type_for_status(503) == "overloaded_error"
        assert anthropic_error_type_for_status(500) == "api_error"
        assert anthropic_error_type_for_status(502) == "api_error"

    @pytest.mark.asyncio
    async def test_precommit_http_error_anthropic_envelope(self):
        messages = []
        controller = StreamCommitController.requiring({"engine"})
        controller.begin_attempt(1)

        async def source():
            raise HTTPException(status_code=503, detail="No instance available")
            yield b""  # pylint: disable=unreachable

        async def send(message):
            messages.append(message)

        response = CommitAwareStreamingResponse(source(), controller, anthropic_entry=True)
        await response(_asgi_scope(), _never_disconnect, send)

        assert messages[0]["status"] == 503
        body = json.loads(messages[1]["body"])
        assert body["type"] == "error"
        assert body["error"]["type"] == "overloaded_error"
        assert "No instance available" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_precommit_transport_error_anthropic_envelope(self):
        messages = []
        controller = StreamCommitController.requiring({"engine"})
        controller.begin_attempt(1)

        async def source():
            raise httpx.ConnectError("engine down")
            yield b""  # pylint: disable=unreachable

        async def send(message):
            messages.append(message)

        response = CommitAwareStreamingResponse(source(), controller, anthropic_entry=True)
        await response(_asgi_scope(), _never_disconnect, send)

        assert messages[0]["status"] == 502
        body = json.loads(messages[1]["body"])
        assert body["type"] == "error"
        assert body["error"]["type"] == "api_error"

    @pytest.mark.asyncio
    async def test_precommit_openai_shape_unchanged(self):
        """OpenAI entry APIs keep the pre-existing pre-commit error shapes."""
        messages = []
        controller = StreamCommitController.requiring({"engine"})
        controller.begin_attempt(1)

        async def source():
            raise HTTPException(status_code=503, detail="No instance available")
            yield b""  # pylint: disable=unreachable

        async def send(message):
            messages.append(message)

        response = CommitAwareStreamingResponse(source(), controller)
        await response(_asgi_scope(), _never_disconnect, send)

        assert messages[0]["status"] == 503
        assert json.loads(messages[1]["body"]) == {"detail": "No instance available"}

    @pytest.mark.asyncio
    async def test_precommit_upstream_body_passthrough_unchanged_for_anthropic(self):
        """Engine-supplied Anthropic error bodies pass through verbatim pre-commit."""
        error_body = b'{"type":"error","error":{"type":"invalid_request_error","message":"bad tool"}}'
        messages = []
        controller = StreamCommitController.requiring({"engine"})
        controller.begin_attempt(1)

        async def source():
            raise UpstreamHTTPError(
                status_code=400,
                body=error_body,
                headers={"content-type": "application/json"},
                phase="stream",
            )
            yield b""  # pylint: disable=unreachable

        async def send(message):
            messages.append(message)

        response = CommitAwareStreamingResponse(source(), controller, anthropic_entry=True)
        await response(_asgi_scope(), _never_disconnect, send)

        assert messages[0]["status"] == 400
        assert messages[1]["body"] == error_body

    @pytest.mark.asyncio
    async def test_postcommit_failure_emits_single_anthropic_error_event(self):
        messages = []
        controller = StreamCommitController.requiring({"engine"})
        controller.begin_attempt(1)

        async def source():
            controller.mark_ready("engine", 1)
            yield ANTHROPIC_FRAMES[0]
            raise httpx.ReadError("connection lost")

        async def send(message):
            messages.append(message)

        response = CommitAwareStreamingResponse(source(), controller, anthropic_entry=True)
        await response(_asgi_scope(), _never_disconnect, send)

        assert messages[0]["status"] == 200
        bodies = _response_bodies(messages)
        assert bodies[0] == ANTHROPIC_FRAMES[0]
        error_frames = [b for b in bodies if b.startswith(b"event: error\n")]
        assert len(error_frames) == 1
        payload = json.loads(error_frames[0].decode().split("data: ", 1)[1])
        assert payload["type"] == "error"
        assert payload["error"]["type"] == "api_error"
        assert not any(b"[DONE]" in b for b in bodies)
        assert not any(b.startswith(b'data: {"error"') for b in bodies)

    @pytest.mark.asyncio
    async def test_postcommit_upstream_anthropic_body_forwarded_in_error_event(self):
        engine_body = b'{"type":"error","error":{"type":"overloaded_error","message":"engine busy"}}'
        messages = []
        controller = StreamCommitController.requiring({"engine"})
        controller.begin_attempt(1)

        async def source():
            controller.mark_ready("engine", 1)
            yield ANTHROPIC_FRAMES[0]
            raise UpstreamHTTPError(status_code=503, body=engine_body, headers={}, phase="stream")

        async def send(message):
            messages.append(message)

        response = CommitAwareStreamingResponse(source(), controller, anthropic_entry=True)
        await response(_asgi_scope(), _never_disconnect, send)

        bodies = _response_bodies(messages)
        error_frames = [b for b in bodies if b.startswith(b"event: error\n")]
        assert len(error_frames) == 1
        assert json.loads(error_frames[0].decode().split("data: ", 1)[1]) == json.loads(engine_body)

    def test_pd_hybrid_streaming_error_chunk_anthropic(self):
        router = PDHybridRouter.__new__(PDHybridRouter)
        router.req_info = _req_info(_anthropic_req_data())
        chunk = router._generate_streaming_error_chunk(httpx.ReadError("boom"))
        assert chunk.startswith(b"event: error\n")
        payload = json.loads(chunk.decode().split("data: ", 1)[1])
        assert payload["type"] == "error"
        assert payload["error"]["type"] == "api_error"

    def test_pd_hybrid_streaming_error_chunk_openai_unchanged(self):
        router = PDHybridRouter.__new__(PDHybridRouter)
        router.req_info = _openai_req_info()
        chunk = router._generate_streaming_error_chunk(httpx.ReadError("boom"))
        assert chunk.startswith(b"data: {")
        payload = json.loads(chunk.decode().removeprefix("data: ").strip())
        assert payload["error"]["code"] == 502

    def test_dispatch_transport_error_anthropic_envelope(self, monkeypatch):
        """dispatch.handle_request renders transport failures as Anthropic envelope."""

        class _FailingRouter:
            def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
                pass

            async def handle_request(self):
                raise httpx.ConnectError("engine down")

        monkeypatch.setattr(dispatch, "PDHybridRouter", _FailingRouter)

        config = _config()
        app = FastAPI()
        request_manager = RequestManager(config)
        scheduler = _PDScheduler()

        @app.post("/v1/messages")
        async def messages(request: Request):
            return await dispatch.handle_request(request, config, scheduler=scheduler, request_manager=request_manager)

        @app.post("/v1/chat/completions")
        async def chat(request: Request):
            return await dispatch.handle_request(request, config, scheduler=scheduler, request_manager=request_manager)

        client = TestClient(app)
        response = client.post("/v1/messages", json=_anthropic_req_data(stream=False))
        assert response.status_code == 502
        body = response.json()
        assert body["type"] == "error"
        assert body["error"]["type"] == "api_error"

        openai_response = client.post(
            "/v1/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert openai_response.status_code == 502
        openai_body = openai_response.json()
        assert openai_body["error"]["code"] == 502
        assert openai_body.get("type") != "error"


# ---------------------------------------------------------------------------
# Section 7: count_tokens lightweight routing
# ---------------------------------------------------------------------------


class TestCountTokensRouting:
    def test_count_tokens_bypasses_unified_pd_in_pd_deployment(self, monkeypatch):
        """In a compatible P/D topology count_tokens must not use the UnifiedPD router."""
        used: list[str] = []

        class _FakeUnified:
            def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
                used.append("unified")

            async def handle_request(self):
                return JSONResponse({"router": "unified"})

        class _FakeHybrid:
            def __init__(self, req_info, config, scheduler=None, request_manager=None, sampling_manager=None):
                used.append("hybrid")

            async def handle_request(self):
                return JSONResponse({"input_tokens": 3})

        monkeypatch.setattr(dispatch, "UnifiedPDRouter", _FakeUnified)
        monkeypatch.setattr(dispatch, "PDHybridRouter", _FakeHybrid)

        config = _config()
        request_manager = RequestManager(config)
        scheduler = _PDScheduler()
        app = FastAPI()

        @app.post("/v1/messages/count_tokens")
        async def count_tokens(request: Request):
            return await dispatch.handle_request(request, config, scheduler=scheduler, request_manager=request_manager)

        @app.post("/v1/messages")
        async def messages(request: Request):
            return await dispatch.handle_request(request, config, scheduler=scheduler, request_manager=request_manager)

        client = TestClient(app)
        response = client.post(
            "/v1/messages/count_tokens",
            json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert response.status_code == 200
        assert response.json() == {"input_tokens": 3}
        assert used == ["hybrid"]

        used.clear()
        control = client.post("/v1/messages", json=_anthropic_req_data(stream=False))
        assert control.status_code == 200
        assert used == ["unified"]

    @pytest.mark.asyncio
    async def test_count_tokens_single_instance_verbatim_forward(self, monkeypatch):
        """Exactly one instance is scheduled and the body is forwarded verbatim."""
        req_data = {
            "model": "test-model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "data": "zzz"}},
                        {"type": "text", "text": "describe"},
                    ],
                }
            ],
        }
        req_info = _req_info(req_data, api=COUNT_TOKENS_API)
        # EPD readiness + image block: proves the encode leg is skipped for count_tokens.
        scheduler = _HybridScheduler(readiness=InstanceReadiness.REQUIRED_MET_EPD)
        router = PDHybridRouter(
            req_info,
            _config(),
            scheduler=scheduler,
            request_manager=RequestManager(_config()),
        )

        forwarded: list[tuple[str, dict]] = []

        async def mock_forward_request(self, api, body, client, timeout):
            forwarded.append((api, dict(body)))
            request = httpx.Request("POST", f"/{api}", json=body)
            return httpx.Response(status_code=200, json={"input_tokens": 5}, request=request)

        monkeypatch.setattr(PDHybridRouter, "forward_request", mock_forward_request)

        response = await router.handle_request()

        assert json.loads(response.body) == {"input_tokens": 5}
        assert scheduler.allocate_calls == [PDRole.ROLE_U]
        # Exactly one engine call: no encode/prefill side effect.
        assert len(forwarded) == 1
        api, body = forwarded[0]
        assert api == COUNT_TOKENS_API
        assert body == req_data
        for forbidden in ("max_tokens", "min_tokens", "return_token_ids", "logprobs", MOTOR_DISPATCH_KEY):
            assert forbidden not in body


# ---------------------------------------------------------------------------
# Section 8: precision sampling skip + EPD detection
# ---------------------------------------------------------------------------


class TestAnthropicSamplingSkip:
    def test_init_sampling_state_anthropic_not_sampleable(self):
        config = _config()
        config.precision_detection_config.precision_check_enabled = True
        router = UnifiedPDRouter(
            _req_info(_anthropic_req_data()),
            config,
            scheduler=_PDScheduler(),
            request_manager=RequestManager(config),
            sampling_manager=AsyncMock(),
        )
        state = router._init_sampling_state()
        assert state["sampleable"] is False
        assert state["enabled"] is False

    def test_init_sampling_state_openai_unchanged(self):
        config = _config()
        config.precision_detection_config.precision_check_enabled = True
        router = UnifiedPDRouter(
            _openai_req_info(),
            config,
            scheduler=_PDScheduler(),
            request_manager=RequestManager(config),
            sampling_manager=AsyncMock(),
        )
        state = router._init_sampling_state()
        assert state["sampleable"] is True
        assert state["enabled"] is True


class TestAnthropicEPDDetection:
    async def _can_encode(self, req_data: dict, readiness: InstanceReadiness, api: str) -> bool:
        router = PDHybridRouter.__new__(PDHybridRouter)
        router.req_info = _req_info(req_data, api=api)
        router._scheduler = SimpleNamespace(has_required_instances=AsyncMock(return_value=readiness))
        return await router._check_can_encode()

    @pytest.mark.asyncio
    async def test_anthropic_image_block_triggers_encode_in_epd(self):
        req_data = _anthropic_req_data()
        req_data["messages"][0]["content"].append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "zzz"}}
        )
        assert await self._can_encode(req_data, InstanceReadiness.REQUIRED_MET_EPD, ANTHROPIC_ENTRY_API) is True

    @pytest.mark.asyncio
    async def test_anthropic_image_block_without_epd_readiness(self):
        req_data = _anthropic_req_data()
        req_data["messages"][0]["content"].append(
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "zzz"}}
        )
        assert await self._can_encode(req_data, InstanceReadiness.REQUIRED_MET, ANTHROPIC_ENTRY_API) is False

    @pytest.mark.asyncio
    async def test_anthropic_text_only_not_multimodal(self):
        assert (
            await self._can_encode(_anthropic_req_data(), InstanceReadiness.REQUIRED_MET_EPD, ANTHROPIC_ENTRY_API)
            is False
        )

    @pytest.mark.asyncio
    async def test_openai_image_url_still_detected(self):
        req_data = {
            "model": "m",
            "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x/y.png"}}]}],
        }
        assert await self._can_encode(req_data, InstanceReadiness.REQUIRED_MET_EPD, "v1/chat/completions") is True

    @pytest.mark.asyncio
    async def test_count_tokens_never_encodes(self):
        req_data = {
            "model": "m",
            "messages": [{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "data": "zzz"}}]}],
        }
        assert await self._can_encode(req_data, InstanceReadiness.REQUIRED_MET_EPD, COUNT_TOKENS_API) is False


# ---------------------------------------------------------------------------
# Section 4: PD correctness flows (mocked engine HTTP)
# ---------------------------------------------------------------------------


class TestAnthropicUnifiedPDFlow:
    @pytest.mark.asyncio
    async def test_concurrent_streaming_flow(self, monkeypatch):
        """Concurrent PD: prefill leg rewritten legally, decode leg streams Anthropic frames intact."""
        req_info = _req_info(_anthropic_req_data(stream=True))
        scheduler = _PDScheduler()
        router = UnifiedPDRouter(
            req_info,
            _config(),
            scheduler=scheduler,
            request_manager=RequestManager(_config()),
        )
        p_client = _Client("prefill")
        d_client = _StreamClient("decode", ANTHROPIC_FRAMES)
        _client_dispatcher(router, p_client, d_client, monkeypatch)

        response = await router.handle_request()
        messages = await asyncio.wait_for(_invoke_asgi_response(response), timeout=5)

        assert messages[0]["status"] == 200
        bodies = _response_bodies(messages)
        assert bodies == ANTHROPIC_FRAMES

        assert len(p_client.requests) == 1
        p_req = p_client.requests[0]
        # Prefill-leg rewrite is protocol-legal for Anthropic (native field names).
        assert p_req["stream"] is False
        assert p_req["max_tokens"] == 1
        assert "return_token_ids" not in p_req
        assert p_req[MOTOR_DISPATCH_KEY]["role"] == "prefill"
        # Anthropic-native fields pass through untouched.
        assert p_req["system"] == "You are helpful"
        assert p_req["thinking"] == {"type": "enabled", "budget_tokens": 64}
        assert isinstance(p_req["messages"][0]["content"], list)

        assert len(d_client.requests) == 1
        d_req = d_client.requests[0]
        assert d_req["stream"] is True
        assert d_req["max_tokens"] == 10
        assert "return_token_ids" not in d_req
        assert d_req[MOTOR_DISPATCH_KEY]["role"] == "decode"
        assert d_req["system"] == "You are helpful"

    @pytest.mark.asyncio
    async def test_concurrent_flow_excludes_anthropic_from_sampling_injection(self, monkeypatch):
        """precision_check_enabled: no sampling fields are injected into Anthropic decode bodies."""
        config = _config()
        config.precision_detection_config.precision_check_enabled = True
        req_info = _req_info(_anthropic_req_data(stream=True))
        scheduler = _PDScheduler()
        router = UnifiedPDRouter(
            req_info,
            config,
            scheduler=scheduler,
            request_manager=RequestManager(config),
            sampling_manager=AsyncMock(),
        )
        p_client = _Client("prefill")
        d_client = _StreamClient("decode", ANTHROPIC_FRAMES)
        _client_dispatcher(router, p_client, d_client, monkeypatch)

        response = await router.handle_request()
        messages = await asyncio.wait_for(_invoke_asgi_response(response), timeout=5)

        assert messages[0]["status"] == 200
        assert _response_bodies(messages) == ANTHROPIC_FRAMES
        d_req = d_client.requests[0]
        for field in ("logprobs", "top_logprobs", "return_token_ids", "return_tokens_as_token_ids"):
            assert field not in d_req

    @pytest.mark.asyncio
    async def test_handoff_flow_valid_prefill_result(self, monkeypatch):
        """Handoff (CPCD): engine PrefillResult passes coordinator validation; decode carries it."""
        handoff = [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
        req_info = _req_info(_anthropic_req_data(stream=False))
        scheduler = _PDScheduler(capabilities=handoff)
        router = UnifiedPDRouter(
            req_info,
            _config(),
            scheduler=scheduler,
            request_manager=RequestManager(_config()),
        )
        p_client = _AnthropicPrefillResultClient("prefill")
        d_client = _Client("decode", json_body=ANTHROPIC_MESSAGE_BODY)
        _client_dispatcher(router, p_client, d_client, monkeypatch)

        response = await router.handle_request()

        assert json.loads(response.body) == ANTHROPIC_MESSAGE_BODY
        assert len(p_client.requests) == 1
        assert len(d_client.requests) == 1
        prefill_result = d_client.requests[0][MOTOR_PREFILL_RESULT_KEY]
        assert prefill_result["status"] == "completed"
        assert prefill_result["payload"] == {"opaque": "kv"}
        assert "return_token_ids" not in d_client.requests[0]
        # Anthropic usage is not extended with OpenAI-only fields.
        assert "prompt_tokens_details" not in json.loads(response.body)["usage"]

    @pytest.mark.asyncio
    async def test_post_commit_failure_fails_stream_without_resume(self, monkeypatch):
        """Post-commit mid-stream failure: one Anthropic error event, no resume attempt."""
        req_info = _req_info(_anthropic_req_data(stream=True))
        scheduler = _PDScheduler()
        router = UnifiedPDRouter(
            req_info,
            _config(),
            scheduler=scheduler,
            request_manager=RequestManager(_config()),
        )
        p_client = _Client("prefill")
        d_client = _StreamClient("decode", ANTHROPIC_FRAMES[:2], exc_after_chunks=httpx.ReadError("connection lost"))
        _client_dispatcher(router, p_client, d_client, monkeypatch)
        _patch_dispatch_stop(monkeypatch)

        response = await router.handle_request()
        messages = await asyncio.wait_for(_invoke_asgi_response(response), timeout=5)

        assert messages[0]["status"] == 200
        bodies = _response_bodies(messages)
        assert bodies[:2] == ANTHROPIC_FRAMES[:2]
        error_frames = [b for b in bodies if b.startswith(b"event: error\n")]
        assert len(error_frames) == 1
        payload = json.loads(error_frames[0].decode().split("data: ", 1)[1])
        assert payload["type"] == "error"
        assert not any(b"[DONE]" in b for b in bodies)
        assert not any(b.startswith(b'data: {"error"') for b in bodies)
        # No resume/retry: exactly one prefill and one decode request.
        assert len(p_client.requests) == 1
        assert len(d_client.requests) == 1
        assert scheduler.allocate_calls == [PDRole.ROLE_P, PDRole.ROLE_D]


class TestAnthropicPDHybridFallback:
    @pytest.mark.asyncio
    async def test_hybrid_stream_serves_anthropic_frames_without_token_id_injection(self, monkeypatch):
        """Hybrid fallback (single instance): full Anthropic stream, protocol shape intact."""
        req_info = _req_info(_anthropic_req_data(stream=True))
        scheduler = _HybridScheduler()
        router = PDHybridRouter(
            req_info,
            _config(),
            scheduler=scheduler,
            request_manager=RequestManager(_config()),
        )

        forwarded: list[dict] = []

        async def mock_forward_stream(self, api, req_data, client, timeout, *, on_response_ready=None):
            forwarded.append(dict(req_data))
            if on_response_ready is not None:
                on_response_ready()
            for frame in ANTHROPIC_FRAMES:
                yield frame

        monkeypatch.setattr(PDHybridRouter, "forward_stream_request", mock_forward_stream)

        response = await router.handle_request()
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())

        assert chunks == ANTHROPIC_FRAMES
        assert len(forwarded) == 1
        body = forwarded[0]
        assert "return_token_ids" not in body
        assert body["stream"] is True
        assert body["system"] == "You are helpful"
        assert MOTOR_DISPATCH_KEY not in body

    @pytest.mark.asyncio
    async def test_hybrid_nonstream_serves_anthropic_body_intact(self, monkeypatch):
        req_info = _req_info(_anthropic_req_data(stream=False))
        scheduler = _HybridScheduler()
        router = PDHybridRouter(
            req_info,
            _config(),
            scheduler=scheduler,
            request_manager=RequestManager(_config()),
        )

        async def mock_forward_request(self, api, req_data, client, timeout):
            request = httpx.Request("POST", f"/{api}", json=req_data)
            return httpx.Response(status_code=200, json=ANTHROPIC_MESSAGE_BODY, request=request)

        monkeypatch.setattr(PDHybridRouter, "forward_request", mock_forward_request)

        response = await router.handle_request()

        assert json.loads(response.body) == ANTHROPIC_MESSAGE_BODY
