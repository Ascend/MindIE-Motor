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
from unittest.mock import MagicMock
from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient

from motor.common.resources.dispatch import MOTOR_DISPATCH_KEY
from motor.engine_server.core.dispatch_adapter.base import DispatchResponseContext
from motor.engine_server.core.infer_endpoint import InferEndpoint


class _Config:
    def __init__(self, role="decode", engine_type="vllm"):
        self._endpoint_config = SimpleNamespace(
            host="127.0.0.1",
            port=0,
            engine_type=engine_type,
            role=role,
            deploy_config=SimpleNamespace(infer_tls_config=None),
        )

    def get_endpoint_config(self):
        return self._endpoint_config

    def get_args(self):
        return None


class _RequestModel:
    @classmethod
    def model_validate(cls, body):
        return body


class _Endpoint(InferEndpoint):
    def get_lifespan(self):
        @asynccontextmanager
        async def _lifespan(app):
            yield

        return _lifespan

    def init_request_handlers(self) -> None:
        self.chat_completion_request = _RequestModel
        self.completion_request = _RequestModel


def test_infer_endpoint_registers_vllm_handlers_only_for_vllm():
    vllm_endpoint = _Endpoint(_Config(engine_type="vllm"))
    sglang_endpoint = _Endpoint(_Config(engine_type="sglang"))

    assert HTTPException in vllm_endpoint.app.exception_handlers
    assert HTTPException not in sglang_endpoint.app.exception_handlers


class _Serving:
    def __init__(self, response):
        self.response = response

    async def handle_request(self, request, raw_request):
        return self.response


class _RaisingServing:
    def __init__(self, message):
        self.message = message

    async def handle_request(self, request, raw_request):
        raise RuntimeError(self.message)


class _RaisingHTTPServing:
    async def handle_request(self, request, raw_request):
        raise HTTPException(
            status_code=429,
            detail="engine rate limited",
            headers={"Retry-After": "3"},
        )


class _RaisingContextLengthServing:
    async def handle_request(self, request, raw_request):
        raise ValueError("This model's maximum context length is 2048 tokens. However, you requested 2049 tokens.")


class _PeerStopResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self._body


class _PeerStopHTTPClient:
    def __init__(self, calls, *, fail=False):
        self._calls = calls
        self._fail = fail

    async def post(self, path, json, timeout):
        self._calls.append({"path": path, "json": json, "timeout": timeout})
        if self._fail:
            raise RuntimeError("stop failed")
        return _PeerStopResponse(
            {
                "root_request_id": json["root_request_id"],
                "attempt_seq": json["attempt_seq"],
                "accepted": True,
                "state": "stopped",
                "message": "",
            }
        )


class _AbortEngineClient:
    def __init__(self):
        self.aborted = []

    async def abort(self, request_id):
        self.aborted.append(request_id)


def _dispatch_body(role="decode"):
    return {
        "model": "m",
        "prompt": "hello",
        MOTOR_DISPATCH_KEY: {
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
        },
    }


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


def _install_peer_stop_client(monkeypatch, *, fail=False):
    calls = []

    async def _get_client(self, ip, port, tls_config=None, **client_kwargs):
        calls.append({"ip": ip, "port": port, "tls_config": tls_config})
        return _PeerStopHTTPClient(calls, fail=fail)

    monkeypatch.setattr(
        "motor.engine_server.core.dispatch_adapter.base.HTTPClientPool.get_client",
        _get_client,
    )
    return calls


async def _dispatch_context(endpoint: _Endpoint, body: dict) -> DispatchResponseContext:
    original_body = body.copy()
    adapted_body, dispatch = await endpoint.dispatch_adapter.adapt_request_body(body.copy())
    return DispatchResponseContext(
        api="v1/completions",
        raw_path="/v1/completions",
        request_body=adapted_body,
        dispatch=dispatch,
        stream=bool(original_body.get("stream", False)),
        client_return_token_ids=bool(original_body.get("return_token_ids", False)),
        client_expects_chat_shape=("messages" in original_body),
    )


async def _call_dispatch_serving(endpoint: _Endpoint, body: dict, serving):
    context = await _dispatch_context(endpoint, body)
    raw_request = MagicMock()
    return await endpoint._call_openai_serving(
        lambda: serving.handle_request(body, raw_request),
        context,
    )


def _response_json(response):
    body = response.body
    if isinstance(body, memoryview):
        body = body.tobytes()
    return json.loads(body.decode() if isinstance(body, (bytes, bytearray)) else body)


def test_infer_endpoint_normalizes_dispatch_nonstream_response():
    endpoint = _Endpoint(_Config(role="decode"))
    body = _dispatch_body("decode")
    response = asyncio.run(
        _call_dispatch_serving(
            endpoint,
            body,
            _Serving(
                JSONResponse(
                    {
                        "prompt_token_ids": [1, 2],
                        "choices": [{"text": "ok", "token_ids": [3]}],
                    }
                )
            ),
        )
    )

    payload = response.body.decode() if hasattr(response.body, "decode") else response.body
    assert "token_ids" not in payload
    assert "prompt_token_ids" not in payload

    stop = TestClient(endpoint.app).post(
        "/v1/dispatch/stop",
        json={
            "root_request_id": "req",
            "engine_request_id": "req#a1",
            "attempt_seq": 1,
            "pair_id": "pair",
            "reason": "peer_failed",
        },
    )
    assert stop.status_code == 200
    assert stop.json()["state"] == "already_done"


def test_infer_endpoint_leaves_plain_openai_response_unchanged():
    endpoint = _Endpoint(_Config(role="decode"))
    endpoint.app.state.openai_serving_completion = _Serving(
        JSONResponse({"prompt_token_ids": [1, 2], "choices": [{"token_ids": [3]}]})
    )

    response = TestClient(endpoint.app).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hello"},
    )

    assert response.status_code == 200
    assert "token_ids" in response.text
    assert "prompt_token_ids" in response.text


def test_infer_endpoint_plain_unknown_error_returns_structured_500():
    endpoint = _Endpoint(_Config(role="decode"))
    endpoint.app.state.openai_serving_completion = _RaisingServing("engine boom")

    response = TestClient(endpoint.app, raise_server_exceptions=False).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hello"},
    )

    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["message"] == "engine boom"
    assert payload["error"]["code"] == 500
    assert payload["error"]["type"] == "InternalServerError"


def test_infer_endpoint_plain_http_exception_preserves_status_headers():
    endpoint = _Endpoint(_Config(role="decode"))
    endpoint.app.state.openai_serving_completion = _RaisingHTTPServing()

    response = TestClient(endpoint.app).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hello"},
    )

    assert response.status_code == 429
    payload = response.json()
    assert payload["error"]["message"] == "engine rate limited"
    assert payload["error"]["code"] == 429
    assert payload["error"]["type"] == "Too Many Requests"
    assert response.headers["retry-after"] == "3"


def test_infer_endpoint_plain_context_length_error_returns_400():
    endpoint = _Endpoint(_Config(role="decode"))
    endpoint.app.state.openai_serving_completion = _RaisingContextLengthServing()

    response = TestClient(endpoint.app).post(
        "/v1/completions",
        json={"model": "m", "prompt": "hello"},
    )

    assert response.status_code == 400
    payload = response.json()
    assert "maximum context length" in payload["error"]["message"]
    assert payload["error"]["code"] == 400


def test_infer_endpoint_normalizes_dispatch_stream_response():
    async def _chunks():
        yield b'data: {"choices":[{"text":"A","token_ids":[1]}]}\n\n'
        yield b"data: [DONE]\n\n"

    endpoint = _Endpoint(_Config(role="decode"))
    body = _dispatch_body("decode") | {"stream": True}
    response = asyncio.run(
        _call_dispatch_serving(
            endpoint,
            body,
            _Serving(StreamingResponse(_chunks(), media_type="text/event-stream")),
        )
    )

    chunks = []

    async def _collect():
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))

    asyncio.run(_collect())
    payload = b"".join(chunks).decode("utf-8")
    assert "token_ids" not in payload
    assert "data: [DONE]" in payload


def test_infer_endpoint_dispatch_engine_error_stops_peer(monkeypatch):
    calls = _install_peer_stop_client(monkeypatch)
    endpoint = _Endpoint(_Config(role="decode"))
    body = _dispatch_body("decode")
    response = asyncio.run(
        _call_dispatch_serving(
            endpoint,
            body,
            _RaisingServing("engine boom"),
        )
    )

    assert response.status_code == 500
    payload = _response_json(response)
    assert payload["error"]["message"] == "engine boom"
    assert payload["error"]["code"] == 500
    assert payload["error"]["type"] == "InternalServerError"
    assert calls[0]["ip"] == "127.0.0.1"
    assert calls[0]["port"] == "8000"
    assert calls[1]["path"] == "/v1/dispatch/stop"
    assert calls[1]["json"]["reason"] == "peer_failed"
    assert calls[1]["json"]["engine_request_id"] == "req#a1"


def test_infer_endpoint_dispatch_error_response_stops_peer(monkeypatch):
    calls = _install_peer_stop_client(monkeypatch)
    endpoint = _Endpoint(_Config(role="decode"))
    body = _dispatch_body("decode")
    response = asyncio.run(
        _call_dispatch_serving(
            endpoint,
            body,
            _Serving(
                JSONResponse(
                    {"error": {"message": "engine rejected"}},
                    status_code=503,
                )
            ),
        )
    )

    assert response.status_code == 503
    assert _response_json(response)["error"]["message"] == "engine rejected"
    assert calls[0]["ip"] == "127.0.0.1"
    assert calls[1]["path"] == "/v1/dispatch/stop"


def test_infer_endpoint_dispatch_http_exception_preserves_status_headers(monkeypatch):
    calls = _install_peer_stop_client(monkeypatch)
    endpoint = _Endpoint(_Config(role="decode"))
    endpoint.app.state.openai_serving_completion = _RaisingHTTPServing()

    response = TestClient(endpoint.app).post(
        "/v1/completions",
        json=_dispatch_body("decode"),
    )

    assert response.status_code == 429
    payload = response.json()
    assert payload["error"]["message"] == "engine rate limited"
    assert payload["error"]["code"] == 429
    assert payload["error"]["type"] == "Too Many Requests"
    assert response.headers["retry-after"] == "3"
    assert calls[1]["path"] == "/v1/dispatch/stop"


def test_infer_endpoint_dispatch_context_length_error_returns_400(monkeypatch):
    calls = _install_peer_stop_client(monkeypatch)
    endpoint = _Endpoint(_Config(role="decode"))
    endpoint.app.state.openai_serving_completion = _RaisingContextLengthServing()

    response = TestClient(endpoint.app).post(
        "/v1/completions",
        json=_dispatch_body("decode"),
    )

    assert response.status_code == 400
    payload = response.json()
    assert "maximum context length" in payload["error"]["message"]
    assert payload["error"]["code"] == 400
    assert calls[1]["path"] == "/v1/dispatch/stop"


def test_infer_endpoint_peer_stop_failure_preserves_engine_error(monkeypatch):
    calls = _install_peer_stop_client(monkeypatch, fail=True)
    endpoint = _Endpoint(_Config(role="decode"))
    body = _dispatch_body("decode")
    response = asyncio.run(
        _call_dispatch_serving(
            endpoint,
            body,
            _RaisingServing("engine boom"),
        )
    )

    assert response.status_code == 500
    payload = _response_json(response)
    assert payload["error"]["message"] == "engine boom"
    assert payload["error"]["code"] == 500
    assert payload["error"]["type"] == "InternalServerError"
    assert calls[1]["path"] == "/v1/dispatch/stop"


def test_infer_endpoint_prefill_prepared_stop_reports_stopped():
    endpoint = _Endpoint(_Config(role="prefill"))
    client = TestClient(endpoint.app)

    prepared = client.post(
        "/v1/completions",
        json=_dispatch_body("prefill"),
    )
    stop = client.post(
        "/v1/dispatch/stop",
        json={
            "root_request_id": "req",
            "engine_request_id": "req#a1",
            "attempt_seq": 1,
            "pair_id": "pair",
            "reason": "peer_failed",
        },
    )

    assert prepared.status_code == 200
    assert prepared.json()["status"] == "prepared"
    assert stop.status_code == 200
    assert stop.json()["state"] == "stopped"


def test_infer_endpoint_dispatch_stop_aborts_engine_client():
    endpoint = _Endpoint(_Config(role="decode"))
    engine_client = _AbortEngineClient()
    endpoint.app.state.engine_client = engine_client
    asyncio.run(endpoint.dispatch_adapter.adapt_request_body(_dispatch_body("decode")))

    response = TestClient(endpoint.app).post(
        "/v1/dispatch/stop",
        json={
            "root_request_id": "req",
            "engine_request_id": "req#a1",
            "attempt_seq": 1,
            "pair_id": "pair",
            "reason": "peer_failed",
        },
    )

    assert response.status_code == 200
    assert response.json()["state"] == "stopped"
    assert engine_client.aborted == ["req#a1"]


def test_infer_endpoint_dispatch_stop_during_stream_does_not_raise():
    """
    When dispatch is stopped during streaming, the _normalized_body generator
    should emit SSE error + [DONE] and exit, instead of raising HTTPException(499).

    The old behavior raised HTTPException(499) after the response headers had
    already been sent, causing Starlette to throw:
        RuntimeError: Caught handled exception, but response already started.

    After the fix, the generator emits an SSE error event and [DONE] via
    map_stream_error, then returns — no exception propagates to the ASGI layer.
    """

    async def _chunks():
        yield b'data: {"choices":[{"text":"A","token_ids":[1]}]}\n\n'
        yield b'data: {"choices":[{"text":"B","token_ids":[2]}]}\n\n'
        yield b"data: [DONE]\n\n"

    stop_body = {
        "root_request_id": "req",
        "engine_request_id": "req#a1",
        "attempt_seq": 1,
        "pair_id": "pair",
        "reason": "peer_failed",
    }

    endpoint = _Endpoint(_Config(role="decode"))
    body = _dispatch_body("decode") | {"stream": True}
    response = asyncio.run(
        _call_dispatch_serving(
            endpoint,
            body,
            _Serving(StreamingResponse(_chunks(), media_type="text/event-stream")),
        )
    )

    # Scenario A: dispatch stopped before any chunk is yielded
    # The generator should emit SSE error + [DONE] and stop.
    asyncio.run(endpoint.dispatch_adapter.handle_stop(stop_body))

    chunks = []

    async def _collect():
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))

    asyncio.run(_collect())
    # SSE error + [DONE] are emitted, no exception raised
    assert len(chunks) == 2
    payload = b"".join(chunks).decode("utf-8")
    assert "ClientClosedRequest" in payload
    assert "Dispatch stopped by peer." in payload
    assert payload.rstrip().endswith("data: [DONE]")


def test_infer_endpoint_dispatch_stop_mid_stream_does_not_raise():
    """
    When dispatch is stopped mid-stream (after some chunks have been yielded),
    the _normalized_body generator should emit SSE error + [DONE] on the next
    chunk check and exit, without raising an exception that propagates to
    the ASGI layer.
    """

    async def _test():
        async def _chunks():
            yield b'data: {"choices":[{"text":"A","token_ids":[1]}]}\n\n'
            yield b'data: {"choices":[{"text":"B","token_ids":[2]}]}\n\n'
            yield b"data: [DONE]\n\n"

        stop_body = {
            "root_request_id": "req",
            "engine_request_id": "req#a1",
            "attempt_seq": 1,
            "pair_id": "pair",
            "reason": "peer_failed",
        }

        endpoint = _Endpoint(_Config(role="decode"))
        body = _dispatch_body("decode") | {"stream": True}
        response = await _call_dispatch_serving(
            endpoint,
            body,
            _Serving(StreamingResponse(_chunks(), media_type="text/event-stream")),
        )

        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
            if len(chunks) == 1:
                # Stop dispatch after first chunk — mid-stream
                await endpoint.dispatch_adapter.handle_stop(stop_body)

        # First chunk + SSE error + [DONE] = 3 chunks
        assert len(chunks) == 3
        payload = b"".join(chunks).decode("utf-8")
        assert "ClientClosedRequest" in payload
        assert "Dispatch stopped by peer." in payload
        assert payload.rstrip().endswith("data: [DONE]")

    asyncio.run(_test())


def test_infer_endpoint_metaserver_engine_error_stops_decode_peer(monkeypatch):
    calls = _install_peer_stop_client(monkeypatch)
    endpoint = _Endpoint(_Config(role="prefill"))
    endpoint.app.state.openai_serving_completion = _RaisingServing("metaserver boom")
    client = TestClient(endpoint.app)

    prepared = client.post(
        "/v1/completions",
        json=_dispatch_body("prefill"),
    )
    response = client.post(
        "/v1/metaserver",
        json=_metaserver_trigger("req#a1"),
    )

    assert prepared.status_code == 200
    assert response.status_code == 500
    assert response.json()["error"]["message"] == "metaserver boom"
    assert calls[0]["ip"] == "127.0.0.2"
    assert calls[0]["port"] == "8000"
    assert calls[1]["path"] == "/v1/dispatch/stop"
    assert calls[1]["json"]["reason"] == "peer_failed"


def test_infer_endpoint_stream_peer_stop_emits_sse_error_and_done(monkeypatch):
    _install_peer_stop_client(monkeypatch)
    checks = {"n": 0}

    async def _is_stopped(dispatch):
        # One pre-stream check in `_call_openai_serving`, then one check per chunk.
        # After peer stop is observed, later checks stay true so `_handle_dispatch_failure`
        # does not attempt another peer stop.
        checks["n"] += 1
        return checks["n"] > 2

    async def _chunks():
        yield b'data: {"choices":[{"text":"A"}]}\n\n'
        yield b'data: {"choices":[{"text":"B"}]}\n\n'

    endpoint = _Endpoint(_Config(role="decode"))
    monkeypatch.setattr(endpoint.dispatch_adapter, "is_dispatch_stopped", _is_stopped)
    body = _dispatch_body("decode") | {"stream": True}
    response = asyncio.run(
        _call_dispatch_serving(
            endpoint,
            body,
            _Serving(StreamingResponse(_chunks(), media_type="text/event-stream")),
        )
    )

    chunks = []

    async def _collect():
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))

    asyncio.run(_collect())
    payload = b"".join(chunks).decode("utf-8")
    assert '"text":"A"' in payload or '"text": "A"' in payload
    assert "ClientClosedRequest" in payload
    assert "Dispatch stopped by peer." in payload
    assert payload.rstrip().endswith("data: [DONE]")


def test_infer_endpoint_stream_runtime_error_emits_sse_error_and_done(monkeypatch):
    _install_peer_stop_client(monkeypatch)

    async def _chunks():
        yield b'data: {"choices":[{"text":"A"}]}\n\n'
        raise RuntimeError("stream boom")

    endpoint = _Endpoint(_Config(role="decode"))
    body = _dispatch_body("decode") | {"stream": True}
    response = asyncio.run(
        _call_dispatch_serving(
            endpoint,
            body,
            _Serving(StreamingResponse(_chunks(), media_type="text/event-stream")),
        )
    )

    chunks = []

    async def _collect():
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))

    asyncio.run(_collect())
    payload = b"".join(chunks).decode("utf-8")
    assert "stream boom" in payload
    assert "InternalServerError" in payload
    assert payload.rstrip().endswith("data: [DONE]")


def test_infer_endpoint_stream_ignores_error_after_done(monkeypatch):
    _install_peer_stop_client(monkeypatch)

    async def _chunks():
        yield b'data: {"choices":[{"text":"A"}]}\n\n'
        yield b"data: [DONE]\n\n"
        raise RuntimeError("late stream boom")

    endpoint = _Endpoint(_Config(role="decode"))
    body = _dispatch_body("decode") | {"stream": True}
    response = asyncio.run(
        _call_dispatch_serving(
            endpoint,
            body,
            _Serving(StreamingResponse(_chunks(), media_type="text/event-stream")),
        )
    )

    chunks = []

    async def _collect():
        async for chunk in response.body_iterator:
            chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))

    asyncio.run(_collect())
    payload = b"".join(chunks).decode("utf-8")
    assert '"text":"A"' in payload or '"text": "A"' in payload
    assert "data: [DONE]" in payload
    assert "late stream boom" not in payload
    assert "InternalServerError" not in payload


def test_sglang_map_stream_error_uses_engine_error_envelope():
    endpoint = _Endpoint(_Config(role="decode", engine_type="sglang"))
    payload = json.loads(endpoint.dispatch_adapter.map_stream_error(RuntimeError("sglang boom"), MagicMock()))
    assert payload["error"]["message"] == "sglang boom"
    assert payload["error"]["type"] == "EngineError"
    assert payload["error"]["code"] == "engine_error"


def test_chunk_contains_done_requires_exact_sse_line():
    assert InferEndpoint._chunk_contains_done(b"data: [DONE]\n")
    assert not InferEndpoint._chunk_contains_done(b'data: {"text":"data: [DONE] marker"}\n')
