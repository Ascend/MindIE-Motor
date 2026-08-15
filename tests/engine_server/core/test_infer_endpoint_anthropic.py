# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
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
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from motor.common.resources.dispatch import (
    MOTOR_DISPATCH_KEY,
    MOTOR_PREFILL_RESULT_KEY,
    PrefillResult,
)
from motor.engine_server.core.infer_endpoint import InferEndpoint


class _EngineConfig:
    def __init__(self, configs):
        self.configs = configs

    def get(self, key, default=None):
        return self.configs.get(key, default)


class _Config:
    def __init__(self, role="decode", engine_type="vllm", engine_config=None):
        self._endpoint_config = SimpleNamespace(
            host="127.0.0.1",
            port=0,
            engine_type=engine_type,
            role=role,
            deploy_config=SimpleNamespace(
                engine_config=_EngineConfig(engine_config or {}),
                infer_tls_config=None,
                dispatch_profile=None,
            ),
        )

    def get_endpoint_config(self):
        return self._endpoint_config

    def get_args(self):
        return None


def _handoff_config():
    return {
        "kv_transfer_config": {
            "kv_connector": "MooncakeConnectorV1",
            "kv_connector_module_path": "vllm_ascend.distributed.mooncake_connector",
        }
    }


class _AnthropicMessagesRequest(BaseModel):
    """Stand-in for vLLM's AnthropicMessagesRequest (pydantic extra=ignore)."""

    model: str
    messages: list[Any]
    max_tokens: int
    stream: bool | None = False
    system: Any | None = None
    stop_sequences: list[str] | None = None
    top_k: int | None = None
    metadata: dict[str, Any] | None = None
    tools: list[Any] | None = None
    request_id: str | None = None
    kv_transfer_params: dict[str, Any] | None = None


class _OpenAIToolFunction(BaseModel):
    name: str


class _OpenAITool(BaseModel):
    """OpenAI-shaped tool: rejects Anthropic-shaped tool dicts."""

    type: str
    function: _OpenAIToolFunction


class _OpenAIChatRequest(BaseModel):
    """Stand-in for vLLM's ChatCompletionRequest (pydantic extra=ignore)."""

    model: str
    messages: list[Any]
    max_tokens: int | None = None
    stream: bool | None = False
    tools: list[_OpenAITool] | None = None
    request_id: str | None = None
    kv_transfer_params: dict[str, Any] | None = None


class _AnthropicCountTokensRequest(BaseModel):
    model: str
    messages: list[Any]


class _Endpoint(InferEndpoint):
    def get_lifespan(self):
        @asynccontextmanager
        async def _lifespan(app):
            yield

        return _lifespan

    def init_request_handlers(self) -> None:
        self.chat_completion_request = _OpenAIChatRequest
        self.completion_request = _OpenAIChatRequest
        self.anthropic_messages_request = _AnthropicMessagesRequest
        self.anthropic_count_tokens_request = _AnthropicCountTokensRequest


class _NoAnthropicEndpoint(InferEndpoint):
    """Endpoint whose engine provides no Anthropic protocol (e.g. old fork)."""

    def get_lifespan(self):
        @asynccontextmanager
        async def _lifespan(app):
            yield

        return _lifespan

    def init_request_handlers(self) -> None:
        self.chat_completion_request = _OpenAIChatRequest
        self.completion_request = _OpenAIChatRequest


class _AnthropicServing:
    def __init__(self, response=None, count_response=None):
        self.response = response
        self.count_response = count_response
        self.requests = []
        self.count_requests = []

    async def handle_request(self, request, raw_request):
        self.requests.append(request)
        return self.response

    async def count_tokens(self, request, raw_request):
        self.count_requests.append(request)
        return self.count_response


class _RaisingAnthropicServing(_AnthropicServing):
    def __init__(self, message):
        super().__init__()
        self.message = message

    async def handle_request(self, request, raw_request):
        raise RuntimeError(self.message)


def _message_body(**overrides):
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "hello"}],
        "model": "m",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 3, "output_tokens": 2},
    }
    body.update(overrides)
    return body


def _anthropic_request(**overrides):
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }
    body.update(overrides)
    return body


def _dispatch_body(role="decode", **overrides):
    body = _anthropic_request(**overrides)
    body[MOTOR_DISPATCH_KEY] = {
        "schema_version": "1.0",
        "root_request_id": "req",
        "engine_request_id": "req#a1",
        "pair_id": "pair",
        "attempt_seq": 1,
        "role": role,
        "dispatch_mode": "cdp_separate",
        "endpoints": {
            "prefill": {
                "instance_id": 1,
                "endpoint_id": 0,
                "url": "http://127.0.0.1:8000",
            },
            "decode": {
                "instance_id": 2,
                "endpoint_id": 0,
                "url": "http://127.0.0.2:8000",
            },
        },
    }
    return body


def _stream_response(frames):
    async def _chunks():
        for frame in frames:
            yield frame

    return StreamingResponse(_chunks(), media_type="text/event-stream")


def test_anthropic_messages_route_registered_plain_flow():
    endpoint = _Endpoint(_Config(role="union"))
    serving = _AnthropicServing(response=JSONResponse(_message_body()))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post("/v1/messages", json=_anthropic_request())

    assert response.status_code == 200
    payload = response.json()
    assert payload["type"] == "message"
    assert payload["stop_reason"] == "end_turn"
    assert len(serving.requests) == 1


def test_anthropic_count_tokens_route():
    endpoint = _Endpoint(_Config(role="union"))
    serving = _AnthropicServing(count_response=JSONResponse({"input_tokens": 42}))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post(
        "/v1/messages/count_tokens",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.json()["input_tokens"] == 42
    # Token counting never runs generation.
    assert serving.requests == []
    assert len(serving.count_requests) == 1


def test_anthropic_routes_not_found_regression_guard():
    endpoint = _NoAnthropicEndpoint(_Config(role="union"))
    client = TestClient(endpoint.app)

    messages_response = client.post("/v1/messages", json=_anthropic_request())
    count_response = client.post("/v1/messages/count_tokens", json={"model": "m", "messages": []})

    # Engines without Anthropic serving degrade to an Anthropic-envelope 501,
    # never a 404.
    assert messages_response.status_code == 501
    assert messages_response.json()["type"] == "error"
    assert count_response.status_code == 501
    assert count_response.json()["type"] == "error"


def test_anthropic_decode_dispatch_injects_kv_transfer_params():
    endpoint = _Endpoint(_Config(role="decode"))
    serving = _AnthropicServing(response=JSONResponse(_message_body()))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post("/v1/messages", json=_dispatch_body("decode"))

    assert response.status_code == 200
    request = serving.requests[0]
    # Motor-internal fields are consumed before protocol validation.
    assert not hasattr(request, MOTOR_DISPATCH_KEY)
    assert request.request_id == "req#a1"
    assert request.kv_transfer_params == {
        "do_remote_decode": False,
        "do_remote_prefill": True,
        "metaserver": "http://127.0.0.1:8000/v1/metaserver",
    }


def test_anthropic_handoff_decode_uses_prefill_result_kv_params():
    endpoint = _Endpoint(_Config(role="decode", engine_config=_handoff_config()))
    serving = _AnthropicServing(response=JSONResponse(_message_body()))
    endpoint.app.state.anthropic_serving_messages = serving
    body = _dispatch_body("decode")
    body[MOTOR_PREFILL_RESULT_KEY] = {
        "object": "motor.prefill_result",
        "schema_version": "1.0",
        "root_request_id": "req",
        "engine_request_id": "req#a1",
        "pair_id": "pair",
        "attempt_seq": 1,
        "status": "completed",
        "handoff_mode": "handoff",
        "payload": {
            "do_remote_prefill": True,
            "remote_block_ids": [[1, 2]],
            "remote_host": "10.0.0.5",
        },
    }

    response = TestClient(endpoint.app).post("/v1/messages", json=body)

    assert response.status_code == 200
    request = serving.requests[0]
    assert request.kv_transfer_params == {
        "do_remote_prefill": True,
        "remote_block_ids": [[1, 2]],
        "remote_host": "10.0.0.5",
    }


def test_anthropic_trigger_prefill_short_circuits_to_prepared_prefill_result():
    endpoint = _Endpoint(_Config(role="prefill"))
    serving = _AnthropicServing(response=JSONResponse(_message_body()))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post("/v1/messages", json=_dispatch_body("prefill"))

    assert response.status_code == 200
    prefill_result = PrefillResult.model_validate(response.json())
    assert prefill_result.status == "prepared"
    assert prefill_result.handoff_mode == "trigger"
    # The prefill leg never reaches the serving layer in trigger mode.
    assert serving.requests == []


def test_anthropic_handoff_prefill_returns_completed_prefill_result():
    endpoint = _Endpoint(_Config(role="prefill", engine_config=_handoff_config()))
    kv_transfer_params = {
        "do_remote_decode": True,
        "do_remote_prefill": False,
        "remote_block_ids": [[7, 8]],
        "remote_host": "10.0.0.9",
        "remote_port": 9000,
    }
    serving = _AnthropicServing(response=JSONResponse(_message_body(kv_transfer_params=kv_transfer_params)))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post("/v1/messages", json=_dispatch_body("prefill"))

    assert response.status_code == 200
    prefill_result = PrefillResult.model_validate(response.json())
    assert prefill_result.status == "completed"
    assert prefill_result.handoff_mode == "handoff"
    assert prefill_result.payload == kv_transfer_params
    # The prefill generation-param rewrite is legal for the Anthropic body
    # shape (native max_tokens/stream fields).
    request = serving.requests[0]
    assert request.max_tokens == 1
    assert request.stream is False
    assert request.kv_transfer_params == {"do_remote_decode": True, "do_remote_prefill": False}


_ANTHROPIC_FRAMES = [
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant",'
    '"content":[],"model":"m","stop_reason":null,"usage":{"input_tokens":3,"output_tokens":0}}}\n\n',
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"text","text":""}}\n\n',
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"hi"}}\n\n',
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
    'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    '"usage":{"output_tokens":2}}\n\n',
    'event: message_stop\ndata: {"type":"message_stop"}\n\n',
]


def test_anthropic_streaming_preserves_event_framing_byte_identically():
    endpoint = _Endpoint(_Config(role="union"))
    serving = _AnthropicServing(response=_stream_response(_ANTHROPIC_FRAMES))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post("/v1/messages", json=_anthropic_request(stream=True))

    assert response.status_code == 200
    assert response.text == "".join(_ANTHROPIC_FRAMES)


def test_anthropic_streaming_with_dispatch_preserves_event_frames():
    endpoint = _Endpoint(_Config(role="decode"))
    serving = _AnthropicServing(response=_stream_response(_ANTHROPIC_FRAMES))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post("/v1/messages", json=_dispatch_body("decode", stream=True))

    assert response.status_code == 200
    assert response.text == "".join(_ANTHROPIC_FRAMES)


def test_anthropic_nonstream_missing_stop_reason_rewritten_to_end_turn():
    endpoint = _Endpoint(_Config(role="union"))
    body = _message_body()
    del body["stop_reason"]
    serving = _AnthropicServing(response=JSONResponse(body))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post("/v1/messages", json=_anthropic_request())

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "end_turn"


def test_anthropic_nonstream_recomputed_stop_reason_rewritten_with_dispatch():
    endpoint = _Endpoint(_Config(role="decode"))
    serving = _AnthropicServing(response=JSONResponse(_message_body(stop_reason="recomputed")))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post("/v1/messages", json=_dispatch_body("decode"))

    assert response.status_code == 200
    payload = response.json()
    assert payload["stop_reason"] == "end_turn"
    assert "recomputed" not in response.text


def test_anthropic_nonstream_null_stop_reason_rewritten_with_dispatch():
    endpoint = _Endpoint(_Config(role="decode"))
    serving = _AnthropicServing(response=JSONResponse(_message_body(stop_reason=None)))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post("/v1/messages", json=_dispatch_body("decode"))

    assert response.status_code == 200
    assert response.json()["stop_reason"] == "end_turn"


def test_anthropic_stream_null_stop_reason_in_message_delta_rewritten():
    frames = _ANTHROPIC_FRAMES.copy()
    frames[4] = (
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":null},'
        '"usage":{"output_tokens":2}}\n\n'
    )
    endpoint = _Endpoint(_Config(role="union"))
    serving = _AnthropicServing(response=_stream_response(frames))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post("/v1/messages", json=_anthropic_request(stream=True))

    assert response.status_code == 200
    # Only the message_delta data line is rewritten; every other frame is
    # byte-identical (message_start keeps its legitimate null stop_reason).
    assert frames[0] in response.text
    assert '"delta":{"stop_reason":"end_turn"}' in response.text
    assert response.text.index("event: message_delta") < response.text.index("event: message_stop")


def test_anthropic_stream_recomputed_stop_reason_in_message_delta_rewritten_with_dispatch():
    frames = _ANTHROPIC_FRAMES.copy()
    frames[4] = (
        'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"recomputed"},'
        '"usage":{"output_tokens":2}}\n\n'
    )
    endpoint = _Endpoint(_Config(role="decode"))
    serving = _AnthropicServing(response=_stream_response(frames))
    endpoint.app.state.anthropic_serving_messages = serving

    response = TestClient(endpoint.app).post("/v1/messages", json=_dispatch_body("decode", stream=True))

    assert response.status_code == 200
    assert "recomputed" not in response.text
    assert '"delta":{"stop_reason":"end_turn"}' in response.text


def test_anthropic_validation_error_uses_anthropic_envelope():
    endpoint = _Endpoint(_Config(role="union"))
    endpoint.app.state.anthropic_serving_messages = _AnthropicServing()

    response = TestClient(endpoint.app).post("/v1/messages", json={"model": "m"})

    assert response.status_code == 400
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "invalid_request_error"
    assert "message" in payload["error"]


def test_anthropic_count_tokens_validation_error_uses_anthropic_envelope():
    endpoint = _Endpoint(_Config(role="union"))
    endpoint.app.state.anthropic_serving_messages = _AnthropicServing()

    response = TestClient(endpoint.app).post("/v1/messages/count_tokens", json={})

    assert response.status_code == 400
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "invalid_request_error"


def test_anthropic_engine_error_uses_anthropic_envelope():
    endpoint = _Endpoint(_Config(role="union"))
    endpoint.app.state.anthropic_serving_messages = _RaisingAnthropicServing("engine boom")

    response = TestClient(endpoint.app, raise_server_exceptions=False).post("/v1/messages", json=_anthropic_request())

    assert response.status_code == 500
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "api_error"
    assert payload["error"]["message"] == "engine boom"


def test_anthropic_invalid_dispatch_metadata_uses_anthropic_envelope():
    endpoint = _Endpoint(_Config(role="decode"))
    endpoint.app.state.anthropic_serving_messages = _AnthropicServing()
    body = _anthropic_request()
    body[MOTOR_DISPATCH_KEY] = {"role": "decode"}

    response = TestClient(endpoint.app).post("/v1/messages", json=body)

    assert response.status_code == 400
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "invalid_request_error"


def test_anthropic_dispatch_engine_error_stops_peer_and_uses_anthropic_envelope(monkeypatch):
    calls = []

    async def _get_client(self, ip, port, tls_config=None, **client_kwargs):
        calls.append({"ip": ip, "port": port})

        class _Client:
            async def post(self, path, json, timeout):
                calls.append({"path": path, "json": json})
                return SimpleNamespace(
                    raise_for_status=lambda: None,
                    json=lambda: {
                        "root_request_id": "req",
                        "attempt_seq": 1,
                        "accepted": True,
                        "state": "stopped",
                        "message": "",
                    },
                )

        return _Client()

    monkeypatch.setattr(
        "motor.engine_server.core.dispatch_adapter.base.HTTPClientPool.get_client",
        _get_client,
    )
    endpoint = _Endpoint(_Config(role="decode"))
    endpoint.app.state.anthropic_serving_messages = _RaisingAnthropicServing("engine boom")

    response = TestClient(endpoint.app, raise_server_exceptions=False).post(
        "/v1/messages", json=_dispatch_body("decode")
    )

    assert response.status_code == 500
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["message"] == "engine boom"
    assert calls[1]["path"] == "/v1/dispatch/stop"


def test_anthropic_handoff_prefill_engine_error_keeps_anthropic_envelope():
    endpoint = _Endpoint(_Config(role="prefill", engine_config=_handoff_config()))
    endpoint.app.state.anthropic_serving_messages = _RaisingAnthropicServing("engine boom")

    response = TestClient(endpoint.app, raise_server_exceptions=False).post(
        "/v1/messages", json=_dispatch_body("prefill")
    )

    assert response.status_code == 500
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["message"] == "engine boom"


def test_anthropic_stream_dispatch_stop_emits_anthropic_error_frame():
    frames = _ANTHROPIC_FRAMES.copy()
    endpoint = _Endpoint(_Config(role="decode"))
    serving = _AnthropicServing(response=_stream_response(frames))
    endpoint.app.state.anthropic_serving_messages = serving
    client = TestClient(endpoint.app)

    stop_body = {
        "root_request_id": "req",
        "engine_request_id": "req#a1",
        "attempt_seq": 1,
        "pair_id": "pair",
        "reason": "peer_failed",
    }

    async def _stopped_chunk_stream():
        # Stop the dispatch after the first frame is served.
        yield frames[0]
        await endpoint.dispatch_adapter.handle_stop(stop_body)
        for frame in frames[1:]:
            yield frame

    serving.response = StreamingResponse(_stopped_chunk_stream(), media_type="text/event-stream")
    response = client.post("/v1/messages", json=_dispatch_body("decode", stream=True))

    assert response.status_code == 200
    assert "event: error" in response.text
    error_line = next(
        line for line in response.text.splitlines() if line.startswith("data: ") and '"type":"error"' in line
    )
    payload = json.loads(error_line[len("data: ") :])
    assert payload["type"] == "error"
    assert "Dispatch stopped by peer." in payload["error"]["message"]
    # Anthropic streams have no [DONE] sentinel.
    assert "[DONE]" not in response.text


def _metaserver_trigger(request_id="req#a1"):
    return {
        "request_id": request_id,
        "do_remote_prefill": False,
        "do_remote_decode": True,
        "remote_block_ids": [[1, 2, 3]],
        "remote_block_size": [16],
        "remote_engine_id": "decode-engine",
        "remote_host": "127.0.0.2",
        "remote_port": 9000,
        "remote_cached_tokens": 32,
    }


def _openai_dispatch_body(role="prefill"):
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }
    body[MOTOR_DISPATCH_KEY] = _dispatch_body(role)[MOTOR_DISPATCH_KEY]
    return body


def _prepare_prefill_cache(client, body):
    """Run the prefill leg so the trigger-profile body is cached for replay."""
    prepared = client.post("/v1/messages", json=body)
    assert prepared.status_code == 200
    assert prepared.json()["status"] == "prepared"


def test_metaserver_replay_of_anthropic_body_uses_anthropic_serving():
    endpoint = _Endpoint(_Config(role="prefill"))
    anthropic_serving = _AnthropicServing(response=JSONResponse(_message_body()))
    chat_serving = _AnthropicServing(response=JSONResponse({}))
    endpoint.app.state.anthropic_serving_messages = anthropic_serving
    endpoint.app.state.openai_serving_chat = chat_serving
    client = TestClient(endpoint.app)

    _prepare_prefill_cache(client, _dispatch_body("prefill"))
    response = client.post("/v1/metaserver", json=_metaserver_trigger())

    assert response.status_code == 200
    assert len(anthropic_serving.requests) == 1
    assert isinstance(anthropic_serving.requests[0], _AnthropicMessagesRequest)
    # The OpenAI chat serving must not handle Anthropic-ingress replays.
    assert chat_serving.requests == []


def test_metaserver_replay_of_anthropic_body_with_tools_system_and_sampling_fields():
    endpoint = _Endpoint(_Config(role="prefill"))
    anthropic_serving = _AnthropicServing(response=JSONResponse(_message_body()))
    endpoint.app.state.anthropic_serving_messages = anthropic_serving
    endpoint.app.state.openai_serving_chat = _AnthropicServing(response=JSONResponse({}))
    client = TestClient(endpoint.app)
    body = _dispatch_body(
        "prefill",
        tools=[{"name": "get_weather", "description": "d", "input_schema": {"type": "object"}}],
        system="You are helpful.",
        stop_sequences=["\n\nHuman:"],
        top_k=5,
    )

    _prepare_prefill_cache(client, body)
    response = client.post("/v1/metaserver", json=_metaserver_trigger())

    # Anthropic-shaped tools fail OpenAI chat validation; the replay must
    # validate against the Anthropic protocol instead (no 400).
    assert response.status_code == 200
    request = anthropic_serving.requests[0]
    assert request.tools == [{"name": "get_weather", "description": "d", "input_schema": {"type": "object"}}]
    assert request.system == "You are helpful."
    assert request.stop_sequences == ["\n\nHuman:"]
    assert request.top_k == 5
    # The metaserver replay keeps the one-token prefill rewrite.
    assert request.max_tokens == 1
    assert request.stream is False


def test_metaserver_replay_of_openai_chat_body_unchanged():
    endpoint = _Endpoint(_Config(role="prefill"))
    anthropic_serving = _AnthropicServing(response=JSONResponse({}))
    chat_serving = _AnthropicServing(response=JSONResponse({"choices": []}))
    endpoint.app.state.anthropic_serving_messages = anthropic_serving
    endpoint.app.state.openai_serving_chat = chat_serving
    client = TestClient(endpoint.app)

    prepared = client.post("/v1/chat/completions", json=_openai_dispatch_body("prefill"))
    assert prepared.status_code == 200
    assert prepared.json()["status"] == "prepared"
    response = client.post("/v1/metaserver", json=_metaserver_trigger())

    assert response.status_code == 200
    assert len(chat_serving.requests) == 1
    assert isinstance(chat_serving.requests[0], _OpenAIChatRequest)
    assert anthropic_serving.requests == []


def test_metaserver_replay_anthropic_engine_error_renders_error_response():
    endpoint = _Endpoint(_Config(role="prefill"))
    endpoint.app.state.anthropic_serving_messages = _RaisingAnthropicServing("replay boom")
    endpoint.app.state.openai_serving_chat = _AnthropicServing(response=JSONResponse({}))
    client = TestClient(endpoint.app, raise_server_exceptions=False)

    _prepare_prefill_cache(client, _dispatch_body("prefill"))
    response = client.post("/v1/metaserver", json=_metaserver_trigger())

    # The metaserver route is engine-internal and keeps the OpenAI-style
    # engine error envelope; what matters is the error surfaces instead of
    # hanging the decode leg.
    assert response.status_code == 500
    assert response.json()["error"]["message"] == "replay boom"


def test_metaserver_replay_anthropic_without_serving_returns_501():
    endpoint = _NoAnthropicEndpoint(_Config(role="prefill"))
    client = TestClient(endpoint.app)

    async def _cache_anthropic_prefill_body():
        engine_body, dispatch = await endpoint.dispatch_adapter.adapt_request_body(_dispatch_body("prefill"))
        prepared = await endpoint.dispatch_adapter.maybe_prepare_response(
            engine_body, dispatch, entry_api="v1/messages"
        )
        assert prepared is not None

    asyncio.run(_cache_anthropic_prefill_body())
    response = client.post("/v1/metaserver", json=_metaserver_trigger())

    assert response.status_code == 501


def test_anthropic_messages_unexpected_error_uses_anthropic_envelope(monkeypatch):
    endpoint = _Endpoint(_Config(role="union"))
    endpoint.app.state.anthropic_serving_messages = _AnthropicServing()

    async def _boom(raw_request):
        raise RuntimeError("body pipeline boom")

    monkeypatch.setattr(endpoint, "_anthropic_messages_body", _boom)
    response = TestClient(endpoint.app, raise_server_exceptions=False).post("/v1/messages", json=_anthropic_request())

    assert response.status_code == 500
    payload = response.json()
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "api_error"
    assert payload["error"]["message"] == "body pipeline boom"


def test_anthropic_stream_client_disconnect_emits_no_error_frame():
    from starlette.requests import ClientDisconnect

    endpoint = _Endpoint(_Config(role="union"))
    first = _ANTHROPIC_FRAMES[0]

    async def _disconnecting_stream():
        yield first
        raise ClientDisconnect()

    serving = _AnthropicServing(response=StreamingResponse(_disconnecting_stream(), media_type="text/event-stream"))
    endpoint.app.state.anthropic_serving_messages = serving
    client = TestClient(endpoint.app, raise_server_exceptions=False)

    response = client.post("/v1/messages", json=_anthropic_request(stream=True))

    assert response.status_code == 200
    assert "event: message_start" in response.text
    # A disconnected client must not get an error frame on the closed stream.
    assert "event: error" not in response.text


def test_message_delta_prefilter_ignores_event_text_inside_data_payload():
    from motor.engine_server.core.anthropic.normalization import normalize_anthropic_stream_chunk

    payload = json.dumps(
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "docs mention event: message_delta here"},
        }
    )
    chunk = f"event: content_block_delta\ndata: {payload}\n\n".encode()

    # The literal event text inside the data payload must not mark this frame
    # for message_delta rewriting; it passes through byte-identically.
    assert normalize_anthropic_stream_chunk(chunk) == chunk


def test_normalize_stream_chunk_tolerates_non_utf8_bytes():
    from motor.engine_server.core.anthropic.normalization import normalize_anthropic_stream_chunk

    # A mid-stream chunk split inside a multi-byte UTF-8 sequence must not
    # raise; it passes through unchanged.
    chunk = "event: content_block_delta\ndata: {\"type\": \"content_block_delta\"}\n\n".encode()
    broken = chunk[:-1] + b"\xe4\xb8"

    assert normalize_anthropic_stream_chunk(broken) == broken
