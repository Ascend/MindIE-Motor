# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
from contextlib import asynccontextmanager

import httpx
import pytest

from motor.config.coordinator import RenderConfig
from motor.coordinator.render.models import TokenizerSource
from motor.coordinator.render.vllm_render_client import (
    RenderInvalidResponseError,
    RenderTimeoutError,
    RenderUnavailableError,
    RenderUnsupportedError,
    VLLMRenderClient,
)

pytestmark = pytest.mark.anyio
RENDER_ARGS = ("v1/completions", {"prompt": "hello"})


@asynccontextmanager
async def _client(handler):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://render",
    ) as http_client:
        yield VLLMRenderClient(RenderConfig(enabled=True, timeout_ms=50), http_client)


async def _static_render(response, data=None):
    async def handler(request):
        return httpx.Response(request=request, **response)

    async with _client(handler) as client:
        return await client.render("v1/completions", data or {"prompt": "hello"})


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize(
    "api,expected_path,request_data",
    [
        (
            "v1/chat/completions",
            "/v1/chat/completions/render",
            {"model": "test-model", "messages": [{"role": "user", "content": "hello"}]},
        ),
        (
            "/v1/completions",
            "/v1/completions/render",
            {"model": "test-model", "prompt": "hello"},
        ),
    ],
)
async def test_render_maps_openai_path_and_normalizes_response(api, expected_path, request_data):
    async def handler(request):
        assert request.url.path == expected_path
        assert json.loads(request.content) == request_data
        return httpx.Response(
            200,
            json={
                "token_ids": [10, 20],
                "sampling_params": {"max_tokens": 10},
                "multi_modal_data": {"image": "serialized"},
            },
            request=request,
        )

    async with _client(handler) as client:
        result = await client.render(api, request_data)
    assert (result[0].prompt_token_ids, result[0].tokenizer_source) == (
        [10, 20],
        TokenizerSource.RENDER,
    )
    assert result[0].metadata == {
        "sampling_params": {"max_tokens": 10},
        "multi_modal_data": {"image": "serialized"},
    }


async def test_completion_render_preserves_multiple_generate_requests():
    response = [
        {"token_ids": [10], "sampling_params": {"max_tokens": 4}, "request_id": "r0"},
        {"token_ids": [20, 21], "sampling_params": {"max_tokens": 4}, "request_id": "r1"},
    ]
    result = await _static_render(
        {"status_code": 200, "json": response},
        data={"model": "test-model", "prompt": ["a", "b"]},
    )
    assert [item.prompt_token_ids for item in result] == [[10], [20, 21]]
    assert [item.metadata["request_id"] for item in result] == ["r0", "r1"]


async def test_health_reports_response_and_transport_state():
    async def healthy(request):
        assert request.url.path == "/health"
        return httpx.Response(200, request=request)

    async with _client(healthy) as client:
        assert await client.health() is True
    calls = 0

    async def unavailable(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("connection refused", request=request)

    async with _client(unavailable) as client:
        assert await client.health() is False
        with pytest.raises(RenderUnavailableError, match="circuit is open"):
            await client.render(*RENDER_ARGS)
    assert calls == 1


async def test_render_converts_timeout_without_retrying():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    async with _client(handler) as client:
        with pytest.raises(RenderTimeoutError, match="Render"):
            await client.render(*RENDER_ARGS)
    assert calls == 1


async def test_render_circuit_skips_requests_until_cooldown_expires(monkeypatch):
    now = [100.0]
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            json={"token_ids": [1], "sampling_params": {"max_tokens": 1}},
            request=request,
        )

    monkeypatch.setattr(
        "motor.coordinator.render.vllm_render_client.time.monotonic",
        lambda: now[0],
    )
    async with _client(handler) as render_client:
        with pytest.raises(RenderUnavailableError, match="503"):
            await render_client.render(*RENDER_ARGS)
        with pytest.raises(RenderUnavailableError, match="circuit is open"):
            await render_client.render(*RENDER_ARGS)
        assert calls == 1

        now[0] += 6
        result = await render_client.render(*RENDER_ARGS)
    assert result[0].prompt_token_ids == [1]
    assert calls == 2


async def test_render_request_error_does_not_open_circuit():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(422, request=request)

    async with _client(handler) as render_client:
        for _ in range(2):
            with pytest.raises(RenderUnsupportedError, match="422"):
                await render_client.render(*RENDER_ARGS)
    assert calls == 2


@pytest.mark.parametrize(
    "response",
    [
        {"content": b"not-json"},
        {"json": ["not", "an", "object"]},
        {"json": {"token_ids": [], "sampling_params": {}}},
        {"json": {"token_ids": [1, -1], "sampling_params": {}}},
        {"json": {"token_ids": [1, True], "sampling_params": {}}},
        {"json": {"token_ids": [1, "2"], "sampling_params": {}}},
        {"json": {"token_ids": [1]}},
    ],
)
async def test_render_rejects_invalid_response(response):
    async def handler(request):
        return httpx.Response(200, request=request, **response)

    async with _client(handler) as render_client:
        with pytest.raises(RenderInvalidResponseError):
            await render_client.render(*RENDER_ARGS)
        with pytest.raises(RenderUnavailableError, match="circuit is open"):
            await render_client.render(*RENDER_ARGS)


async def test_render_rejects_unsupported_api_without_http_call():
    async def handler(_request):
        pytest.fail("unsupported API must not issue an HTTP request")

    async with _client(handler) as client:
        with pytest.raises(RenderUnsupportedError, match="v1/messages"):
            await client.render("v1/messages", {"messages": []})


@pytest.mark.parametrize(
    ("api", "expected_path", "request_data", "response_data"),
    [
        (
            "v1/chat/completions",
            "/v1/chat/completions/derender",
            {"generate_response": {"choices": [{"index": 0, "token_ids": [30]}]}},
            {"choices": [{"index": 0, "message": {"content": "ok"}}]},
        ),
        (
            "/v1/completions",
            "/v1/completions/derender",
            {"generate_responses": [{"choices": [{"index": 0, "token_ids": [30]}]}]},
            {"choices": [{"index": 0, "text": "ok"}]},
        ),
    ],
)
async def test_derender_maps_openai_path_and_returns_response(api, expected_path, request_data, response_data):
    request_data = {"model": "test-model", "prompt_tokens": 2, **request_data}

    async def handler(request):
        assert request.url.path == expected_path
        assert json.loads(request.content) == request_data
        return httpx.Response(200, json=response_data, request=request)

    async with _client(handler) as client:
        response = await client.derender(api, request_data)
    assert response == response_data


async def test_derender_rejects_invalid_response():
    async def handler(request):
        return httpx.Response(200, json={"id": "missing-choices"}, request=request)

    async with _client(handler) as client:
        with pytest.raises(RenderInvalidResponseError, match="choices"):
            await client.derender("v1/chat/completions", {"model": "test-model"})
