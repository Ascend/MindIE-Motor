# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""HTTP-contract tests for Coordinator-to-native-engine P/D traffic.

These tests use real ``httpx`` request serialization and ASGI applications as
fake native engines. Only endpoint-to-transport binding is replaced; the
Coordinator request, response, SSE, error, and protocol-adapter paths run
unchanged.
"""

from contextlib import asynccontextmanager
import json
from unittest.mock import AsyncMock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import httpx
import pytest

from motor.common.http import HTTPClientPool
from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.common.resources.instance import Instance, InsStatus, ParallelConfig, PDRole
from motor.config.coordinator import CoordinatorConfig, ExceptionConfig
from motor.config.tls_config import TLSConfig
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.models.request import RequestInfo, ReqState
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter
from motor.coordinator.router.upstream_error import UpstreamHTTPError


def _instance(
    instance_id: int,
    role: PDRole,
    engine_type: str,
    *,
    bootstrap_port: int | None = None,
) -> Instance:
    endpoint = Endpoint(
        id=instance_id,
        ip="127.0.0.1",
        business_port=str(8300 + instance_id),
        mgmt_port=str(9300 + instance_id),
        bootstrap_port=bootstrap_port,
        status=EndpointStatus.NORMAL,
    )
    return Instance(
        job_name=f"native-{engine_type}-{role.value}-{instance_id}",
        model_name="test-model",
        engine_type=engine_type,
        id=instance_id,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1, tp_size=1),
        endpoints={endpoint.ip: {endpoint.id: endpoint}},
    )


class _ContractScheduler:
    def __init__(self, engine_type: str):
        bootstrap_port = 21001 if engine_type == "sglang" else None
        self.p = _instance(1, PDRole.ROLE_P, engine_type, bootstrap_port=bootstrap_port)
        self.d = _instance(2, PDRole.ROLE_D, engine_type)
        self.cb_events: list[tuple[int, str]] = []
        self.workload_updates = []

    async def select_and_allocate(self, role, _req_info, **_kwargs):
        instance = self.p if role == PDRole.ROLE_P else self.d
        endpoint = next(iter(next(iter(instance.endpoints.values())).values()))
        return instance, endpoint, Workload(active_tokens=1)

    async def update_workload(self, params):
        self.workload_updates.append(params)
        return True

    async def report_cb_event(self, instance_id: int, event: str) -> None:
        self.cb_events.append((instance_id, event))

    async def get_unblocked_instances(self, role) -> list[int]:
        if role == PDRole.ROLE_P:
            return [self.p.id]
        if role == PDRole.ROLE_D:
            return [self.d.id]
        return []


def _config() -> CoordinatorConfig:
    config = CoordinatorConfig()
    config.exception_config = ExceptionConfig(max_retry=1, retry_delay=0)
    return config


def _router(req_info: RequestInfo, scheduler: _ContractScheduler) -> UnifiedPDRouter:
    config = _config()
    return UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )


@asynccontextmanager
async def _bind_native_apps(monkeypatch, router: UnifiedPDRouter, p_app: FastAPI, d_app: FastAPI):
    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=p_app),
            base_url="http://native-prefill",
        ) as p_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=d_app),
            base_url="http://native-decode",
        ) as d_client,
    ):

        @asynccontextmanager
        async def _client_for(resource: ScheduledResource):
            yield p_client if resource.instance.role == PDRole.ROLE_P else d_client

        monkeypatch.setattr(router, "_client_for", _client_for)
        try:
            yield
        finally:
            await HTTPClientPool().close_all()


async def _collect_stream_body(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


def _sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload, separators=(",", ":")).encode() + b"\n\n"


@pytest.mark.asyncio
async def test_vllm_handoff_preserves_chat_tools_logprobs_and_usage_over_http(monkeypatch):
    p_requests = []
    d_requests = []
    p_headers = []
    d_headers = []
    p_app = FastAPI()
    d_app = FastAPI()

    @p_app.post("/v1/chat/completions")
    async def prefill(request: Request):
        body = await request.json()
        p_requests.append(body)
        p_headers.append(dict(request.headers))
        return {
            "kv_transfer_params": {
                "do_remote_prefill": True,
                "remote_request_id": body["request_id"],
                "remote_host": "10.0.0.8",
                "remote_port": 25000,
                "connector_private": {"ticket": "opaque"},
            },
            "usage": {
                "prompt_tokens": 9,
                "prompt_tokens_details": {"cached_tokens": 4},
            },
        }

    @d_app.post("/v1/chat/completions")
    async def decode(request: Request):
        body = await request.json()
        d_requests.append(body)
        d_headers.append(dict(request.headers))
        return {
            "id": "chatcmpl-native",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_weather",
                                "type": "function",
                                "function": {
                                    "name": "weather",
                                    "arguments": '{"city":"Shenzhen"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                    "logprobs": {
                        "content": [
                            {
                                "token": "weather",
                                "logprob": -0.1,
                                "top_logprobs": [],
                            }
                        ]
                    },
                    "token_ids": [42],
                }
            ],
            "usage": {
                "prompt_tokens": 9,
                "completion_tokens": 1,
                "total_tokens": 10,
            },
            "kv_transfer_params": {"must_not_leak": True},
        }

    tools = [
        {
            "type": "function",
            "function": {
                "name": "weather",
                "description": "Get weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        }
    ]
    req_info = RequestInfo(
        req_id="contract-vllm-chat",
        req_data={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Weather?"}],
            "tools": tools,
            "tool_choice": "auto",
            "logprobs": True,
            "top_logprobs": 2,
            "max_completion_tokens": 6,
            "stream": False,
        },
        api="v1/chat/completions",
        entry_api="v1/chat/completions",
        req_len=9,
    )
    scheduler = _ContractScheduler("vllm")
    router = _router(req_info, scheduler)

    async with _bind_native_apps(monkeypatch, router, p_app, d_app):
        response = await router.handle_request()

    body = json.loads(response.body)
    assert p_requests[0]["stream"] is False
    assert p_requests[0]["max_tokens"] == 1
    assert p_requests[0]["max_completion_tokens"] == 1
    assert p_requests[0]["tools"] == tools
    assert d_requests[0]["max_completion_tokens"] == 6
    assert d_requests[0]["tools"] == tools
    assert d_requests[0]["kv_transfer_params"]["connector_private"] == {"ticket": "opaque"}
    assert p_headers[0]["x-request-id"] == d_headers[0]["x-request-id"] == "contract-vllm-chat#a1"
    assert "authorization" not in p_headers[0]
    assert "authorization" not in d_headers[0]
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "weather"
    assert body["choices"][0]["logprobs"]["content"][0]["logprob"] == -0.1
    assert body["usage"]["prompt_tokens_details"] == {"cached_tokens": 4}
    assert "token_ids" not in body["choices"][0]
    assert "kv_transfer_params" not in body
    assert req_info.state == ReqState.DECODE_END
    assert scheduler.cb_events == [(1, "success"), (2, "success")]


@pytest.mark.asyncio
async def test_sglang_bootstrap_preserves_stream_sse_tools_logprobs_and_usage_over_http(monkeypatch):
    p_requests = []
    d_requests = []
    p_app = FastAPI()
    d_app = FastAPI()

    @p_app.post("/v1/chat/completions")
    async def prefill(request: Request):
        p_requests.append(await request.json())

        async def frames():
            yield _sse(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 7,
                        "prompt_tokens_details": {"cached_tokens": 3},
                    },
                }
            )
            yield b"data: [DONE]\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    @d_app.post("/v1/chat/completions")
    async def decode(request: Request):
        d_requests.append(await request.json())

        async def frames():
            yield _sse(
                {
                    "id": "chatcmpl-sglang",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_time",
                                        "type": "function",
                                        "function": {
                                            "name": "local_time",
                                            "arguments": '{"city":"Shanghai"}',
                                        },
                                    }
                                ]
                            },
                            "logprobs": {
                                "content": [
                                    {
                                        "token": "time",
                                        "logprob": -0.2,
                                        "top_logprobs": [],
                                    }
                                ]
                            },
                            "token_ids": [51],
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "bootstrap_host": "127.0.0.1",
                    "bootstrap_port": 21001,
                    "bootstrap_room": d_requests[0]["bootstrap_room"],
                }
            )
            yield _sse(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 1,
                        "total_tokens": 8,
                    },
                    "bootstrap_room": d_requests[0]["bootstrap_room"],
                }
            )
            yield b"data: [DONE]\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    tools = [
        {
            "type": "function",
            "function": {
                "name": "local_time",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        }
    ]
    req_info = RequestInfo(
        req_id="contract-sglang-stream",
        req_data={
            "model": "test-model",
            "messages": [{"role": "user", "content": "Time?"}],
            "tools": tools,
            "logprobs": True,
            "top_logprobs": 2,
            "max_tokens": 5,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
        api="v1/chat/completions",
        entry_api="v1/chat/completions",
        req_len=7,
    )
    scheduler = _ContractScheduler("sglang")
    router = _router(req_info, scheduler)

    async with _bind_native_apps(monkeypatch, router, p_app, d_app):
        response = await router.handle_request()
        body = await _collect_stream_body(response)

    assert p_requests[0]["rid"] == d_requests[0]["rid"] == "contract-sglang-stream#a1"
    assert p_requests[0]["bootstrap_room"] == d_requests[0]["bootstrap_room"]
    assert p_requests[0]["bootstrap_host"] == d_requests[0]["bootstrap_host"] == "127.0.0.1"
    assert p_requests[0]["bootstrap_port"] == d_requests[0]["bootstrap_port"] == 21001
    assert p_requests[0]["tools"] == d_requests[0]["tools"] == tools
    assert b'"tool_calls"' in body
    assert b'"logprobs"' in body
    assert b'"prompt_tokens_details":{"cached_tokens":3}' in body
    assert b"token_ids" not in body
    assert b"bootstrap_host" not in body
    assert b"bootstrap_port" not in body
    assert b"bootstrap_room" not in body
    assert body.count(b"data: [DONE]") == 1
    assert req_info.state == ReqState.DECODE_END
    assert scheduler.cb_events == [(1, "success"), (2, "success")]


@pytest.mark.parametrize(
    ("status_code", "expected_cb_event"),
    [
        (400, None),
        (503, (1, "failure")),
    ],
)
@pytest.mark.asyncio
async def test_vllm_prefill_http_error_preserves_status_and_circuit_breaker_semantics(
    monkeypatch,
    status_code,
    expected_cb_event,
):
    p_app = FastAPI()
    d_app = FastAPI()
    d_requests = []

    @p_app.post("/v1/completions")
    async def prefill(_request: Request):
        return JSONResponse(
            status_code=status_code,
            content={"error": {"message": f"native prefill {status_code}"}},
            headers={"retry-after": "2"},
        )

    @d_app.post("/v1/completions")
    async def decode(request: Request):
        d_requests.append(await request.json())
        return {"choices": [{"text": "must not run"}]}

    req_info = RequestInfo(
        req_id=f"contract-vllm-error-{status_code}",
        req_data={
            "model": "test-model",
            "prompt": "hello",
            "max_tokens": 4,
            "stream": False,
        },
        api="v1/completions",
        entry_api="v1/completions",
        req_len=3,
    )
    scheduler = _ContractScheduler("vllm")
    router = _router(req_info, scheduler)

    async with _bind_native_apps(monkeypatch, router, p_app, d_app):
        with pytest.raises(UpstreamHTTPError) as exc_info:
            await router.handle_request()

    assert exc_info.value.status_code == status_code
    assert exc_info.value.headers["retry-after"] == "2"
    assert d_requests == []
    assert scheduler.cb_events == ([] if expected_cb_event is None else [expected_cb_event])


@pytest.mark.asyncio
async def test_native_pd_client_uses_coordinator_inference_tls_config(monkeypatch):
    req_info = RequestInfo(
        req_id="contract-native-tls",
        req_data={"model": "test-model", "prompt": "hello", "max_tokens": 1},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=1,
    )
    scheduler = _ContractScheduler("vllm")
    config = _config()
    config.infer_tls_config = TLSConfig(
        enable_tls=True,
        ca_file="/certs/ca.pem",
        cert_file="/certs/client.pem",
        key_file="/certs/client.key",
    )
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    endpoint = next(iter(next(iter(scheduler.p.endpoints.values())).values()))
    resource = ScheduledResource(instance=scheduler.p, endpoint=endpoint)
    fake_client = object()
    pool = HTTPClientPool()
    get_client = AsyncMock(return_value=fake_client)
    monkeypatch.setattr(pool, "get_client", get_client)

    async with router._client_for(resource) as client:
        assert client is fake_client

    get_client.assert_awaited_once_with(
        ip=endpoint.ip,
        port=endpoint.business_port,
        tls_config=config.infer_tls_config,
    )
