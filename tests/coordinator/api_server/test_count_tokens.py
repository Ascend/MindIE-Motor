# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Count Tokens contract through the real Coordinator ASGI route."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider

from motor.common.resources.endpoint import Endpoint, EndpointStatus
from motor.common.resources.instance import Instance, InsStatus, PDRole
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.api_server import count_tokens
from motor.coordinator.api_server.inference_server import InferenceServer
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.domain.scheduling import InstanceReadiness, SchedulingFacade
from motor.coordinator.tracer.tracing import TracerManager

PATH = "/v1/messages/count_tokens"
BODY = {"model": "m", "system": "system", "messages": [{"role": "user", "content": "hello"}]}
COUNT = {"input_tokens": 7, "context_management": {"original_input_tokens": 7}}


def instance(i, role=PDRole.ROLE_P, *, model="m", engine="vllm", status=EndpointStatus.NORMAL, headless=False):
    ep = Endpoint(id=0, ip=f"127.0.0.{i}", business_port="8000", status=status, headless=headless)
    return Instance(
        id=i,
        job_name=f"job-{i}",
        role=role,
        engine_type=engine,
        status=InsStatus.ACTIVE,
        model_name=model,
        endpoints={ep.ip: {ep.id: ep}},
    )


@pytest.fixture
async def proxy(monkeypatch):
    config = CoordinatorConfig()
    config.scheduler_config.enable_pd_separation_fallback_to_hybrid = False
    manager = RequestManager(config)
    server = InferenceServer(config, request_manager=manager)
    server.app.state.request_manager = manager
    pool = [instance(1), instance(2, PDRole.ROLE_D)]
    blocked = set()
    scheduler = AsyncMock(spec=SchedulingFacade)
    scheduler.has_required_instances.return_value = InstanceReadiness.REQUIRED_MET
    scheduler.get_available_instance_roles.side_effect = lambda: {i.role for i in pool}
    scheduler.get_local_instances.side_effect = lambda role=None: {i.id: i for i in pool if role in (None, i.role)}
    scheduler.get_unblocked_instances.side_effect = lambda role: [
        i.id for i in pool if i.role == role and i.id not in blocked
    ]
    monkeypatch.setattr(server._scheduler_connection, "get_client", lambda: scheduler)
    received = []

    def respond(req):
        received.append(req)
        return httpx.Response(200, json=COUNT)

    transport = httpx.MockTransport(respond)
    async with httpx.AsyncClient(base_url="http://engine", transport=transport) as engine:
        get_client = AsyncMock(return_value=engine)
        # Patch the name the proxy resolves instead of the pool class: instance-level residue
        # left on the HTTPClientPool singleton by other suites would shadow a class-level patch.
        monkeypatch.setattr(count_tokens, "HTTPClientPool", lambda: SimpleNamespace(get_client=get_client))
        async with httpx.AsyncClient(base_url="http://motor", transport=httpx.ASGITransport(app=server.app)) as client:
            yield SimpleNamespace(
                client=client,
                server=server,
                scheduler=scheduler,
                instances=pool,
                blocked=blocked,
                get_client=get_client,
                received=received,
                transport=transport,
            )
    scheduler.has_required_instances.assert_not_awaited()
    scheduler.select_and_allocate.assert_not_awaited()
    scheduler.update_workload.assert_not_awaited()


@pytest.mark.parametrize(
    ("roles", "extra"),
    [
        ([PDRole.ROLE_P, PDRole.ROLE_D], {}),
        ([PDRole.ROLE_P], {"max_tokens": 100}),
        ([PDRole.ROLE_U], {"tools": [{"name": "t", "input_schema": {"type": "object"}}]}),
    ],
)
async def test_native_count_once_without_inference_workload(proxy, roles, extra):
    proxy.instances[:] = [instance(i, role, model="M") for i, role in enumerate(roles, 1)]
    body = json.dumps(BODY | extra, indent=2).encode()
    response = await proxy.client.post(
        PATH,
        content=body,
        headers={
            "authorization": "Bearer inbound",
            "x-api-key": "inbound",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "feature",
            "content-type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json() == COUNT
    (req,) = proxy.received
    assert req.url.path == PATH and req.method == "POST" and req.content == body
    assert req.headers["anthropic-beta"] == "feature" and req.headers["anthropic-version"] == "2023-06-01"
    assert req.headers["accept-encoding"] == "identity"
    assert "authorization" not in req.headers and "x-api-key" not in req.headers
    assert proxy.get_client.await_args.kwargs["ip"] == "127.0.0.1"
    assert proxy.get_client.await_args.kwargs["tls_config"] is proxy.server.coordinator_config.infer_tls_config


async def test_count_tokens_propagates_w3c_trace_context(proxy, monkeypatch):
    provider = TracerProvider()
    monkeypatch.setattr(TracerManager(), "tracer", provider.get_tracer(__name__))
    trace_id = "1234567890abcdef1234567890abcdef"
    parent_span_id = "1234567890abcdef"
    response = await proxy.client.post(
        PATH,
        json=BODY,
        headers={
            "traceparent": f"00-{trace_id}-{parent_span_id}-01",
            "tracestate": "vendor=value",
            "baggage": "private=metadata",
        },
    )

    assert response.status_code == 200
    (request,) = proxy.received
    _, forwarded_trace_id, child_span_id, _ = request.headers["traceparent"].split("-")
    assert forwarded_trace_id == trace_id
    assert child_span_id != parent_span_id
    assert request.headers["tracestate"] == "vendor=value"
    assert "baggage" not in request.headers


async def test_select_only_healthy_matching_prefill_endpoints(proxy):
    recovery_paused = instance(10)
    recovery_paused.recovery_paused = True
    proxy.instances[:] = [
        instance(1),
        instance(2),
        instance(3, engine="sglang"),
        instance(4, model="other"),
        instance(5, headless=True),
        instance(6, status=EndpointStatus.PAUSED),
        instance(7),
        instance(8, PDRole.ROLE_D),
        instance(9, PDRole.ROLE_U),
        recovery_paused,
    ]
    proxy.blocked.add(7)
    for _ in range(2):
        assert (await proxy.client.post(PATH, json=BODY)).status_code == 200
    assert {call.kwargs["ip"] for call in proxy.get_client.await_args_list} == {"127.0.0.1", "127.0.0.2"}
    proxy.blocked.update((1, 2))
    assert (await proxy.client.post(PATH, json=BODY)).status_code == 503
    assert proxy.get_client.await_count == 2


@pytest.mark.parametrize("bad_body", [b"{", b"[]", b"null", b'{"model":123,"messages":[{}]}'])
async def test_invalid_count_body_never_reaches_engine(proxy, bad_body):
    assert (await proxy.client.post(PATH, content=bad_body)).status_code == 400
    proxy.get_client.assert_not_awaited()


async def test_decode_only_and_disconnected_scheduler_are_unavailable(proxy, monkeypatch):
    proxy.instances[:] = [instance(2, PDRole.ROLE_D)]
    assert (await proxy.client.post(PATH, json=BODY)).status_code == 503
    monkeypatch.setattr(proxy.server._scheduler_connection, "get_client", lambda: None)
    assert (await proxy.client.post(PATH, json=BODY)).status_code == 503
    proxy.get_client.assert_not_awaited()


@pytest.mark.parametrize("method", ["get_available_instance_roles", "get_local_instances", "get_unblocked_instances"])
async def test_missing_scheduler_view_returns_503_without_forwarding(proxy, method):
    delattr(proxy.scheduler, method)
    assert (await proxy.client.post(PATH, json=BODY)).status_code == 503
    proxy.get_client.assert_not_awaited()


async def test_auth_and_obfuscation_precede_plaintext_forwarding(proxy):
    proxy.server.coordinator_config.api_key_config.enable_api_key = True
    assert (await proxy.client.post(PATH, json=BODY)).status_code == 401
    proxy.server.coordinator_config.api_key_config.enable_api_key = False
    proxy.server.app.state.token_obfuscation_service = object()
    assert (await proxy.client.post(PATH, json=BODY)).status_code == 501
    proxy.get_client.assert_not_awaited()


@pytest.mark.parametrize("upstream_status", [404, 429, 500])
async def test_native_error_response_preserved(proxy, upstream_status):
    proxy.transport.handler = lambda req: httpx.Response(
        upstream_status,
        content=b'{"error":"engine"}',
        headers={"content-type": "application/problem+json", "retry-after": "2", "set-cookie": "private=value"},
    )
    response = await proxy.client.post(PATH, json=BODY)
    assert response.status_code == upstream_status
    assert response.content == b'{"error":"engine"}'
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["retry-after"] == "2"
    assert "set-cookie" not in response.headers


async def test_large_engine_error_uses_bounded_error_response(proxy):
    proxy.server.coordinator_config.exception_config.upstream_error_body_max_bytes = 3
    proxy.transport.handler = lambda req: httpx.Response(400, content=b"too long")
    response = await proxy.client.post(PATH, json=BODY)
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "upstream_http_error"


@pytest.mark.parametrize(("extra_bytes", "expected_status"), [(0, 200), (1, 502)])
async def test_engine_success_response_size_limit(proxy, extra_bytes, expected_status):
    size = count_tokens.COUNT_TOKENS_RESPONSE_MAX_BYTES + extra_bytes
    body = json.dumps(COUNT).encode().ljust(size)
    proxy.transport.handler = lambda req: httpx.Response(200, content=body)
    response = await proxy.client.post(PATH, json=BODY)

    assert response.status_code == expected_status
    assert proxy.get_client.await_count == 1
    if extra_bytes:
        assert response.json()["detail"] == "Upstream token count response is too large"
    else:
        assert response.content == body


async def test_unexpected_success_encoding_is_rejected_before_reading(proxy):
    proxy.transport.handler = lambda req: httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"invalid gzip")
    )
    response = await proxy.client.post(PATH, json=BODY)

    assert response.status_code == 502
    assert response.json()["detail"] == "Unexpected encoding for upstream token count response"


@pytest.mark.parametrize(("error", "status"), [(httpx.ConnectError, 502), (httpx.ReadTimeout, 504)])
async def test_transport_errors_do_not_retry_inference(proxy, error, status):
    def fail(req):
        raise error("engine unavailable", request=req)

    proxy.transport.handler = fail
    assert (await proxy.client.post(PATH, json=BODY)).status_code == status
    assert proxy.get_client.await_count == 1


async def test_admission_slot_released_on_deadline_and_cancellation(proxy):
    settings = proxy.server.coordinator_config.count_tokens_config
    settings.max_inflight = 1
    started = asyncio.Event()

    async def hang(req):
        started.set()
        await asyncio.Event().wait()

    proxy.transport.handler = hang
    pending = asyncio.create_task(proxy.client.post(PATH, json=BODY))
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        assert (await proxy.client.post(PATH, json=BODY)).status_code == 429
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    settings.timeout_seconds = 0.05
    assert (await proxy.client.post(PATH, json=BODY)).status_code == 504
    proxy.transport.handler = lambda req: httpx.Response(200, json=COUNT)
    assert (await proxy.client.post(PATH, json=BODY)).json() == COUNT


async def test_deadline_during_response_close_releases_connection_slot(proxy):
    settings = proxy.server.coordinator_config.count_tokens_config
    settings.timeout_seconds = 0.2
    close_completed = asyncio.Event()

    async def trace_close(name: str, _info: dict) -> None:
        if name == "http11.response_closed.started":
            await asyncio.sleep(2 * settings.timeout_seconds)
        elif name == "http11.response_closed.complete":
            close_completed.set()

    async def trace_first_request(request: httpx.Request) -> None:
        if not close_completed.is_set():
            request.extensions["trace"] = trace_close

    body = json.dumps(COUNT).encode()
    headers = f"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: {len(body)}\r\n\r\n".encode()
    transport = httpx.AsyncHTTPTransport(limits=httpx.Limits(max_connections=1))
    # Use deterministic I/O while exercising HTTPX's actual connection pool.
    transport._pool._network_backend = httpcore.AsyncMockBackend([headers + body])
    async with httpx.AsyncClient(
        base_url="http://engine", transport=transport, event_hooks={"request": [trace_first_request]}
    ) as engine:
        proxy.get_client.return_value = engine
        assert (await proxy.client.post(PATH, json=BODY)).status_code == 504
        assert close_completed.is_set()

        settings.timeout_seconds = 1
        response = await proxy.client.post(PATH, json=BODY)
        assert response.status_code == 200
        assert response.json() == COUNT
