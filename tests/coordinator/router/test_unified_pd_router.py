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
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from starlette.requests import ClientDisconnect

import motor.common.utils.error as cancel_error
from motor.common.http import HTTPClientPool
from motor.common.resources.dispatch import (
    DispatchPlan,
    DispatchStopReason,
    MOTOR_DISPATCH_KEY,
    MOTOR_PREFILL_RESULT_KEY,
    PrefillContextBudget,
)
from motor.common.resources.endpoint import (
    Endpoint,
    EndpointStatus,
    Workload,
    WorkloadAction,
)
from motor.common.resources.instance import Instance, InsStatus, ParallelConfig, PDRole
from motor.config.coordinator import CoordinatorConfig, ExceptionConfig, SchedulerType
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.models.request import RequestInfo, ReqState
from motor.coordinator.router.dispatch_session import (
    AttemptContext,
    AttemptState,
    PDDispatchSession,
)
from motor.coordinator.router.dispatch_capability import (
    DispatchPlanNotSupported,
    select_dispatch_plan_for_pair,
)
from motor.coordinator.router.rescheduler.rescheduler import Rescheduler, RetryRequestPlan
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter
from motor.coordinator.router.upstream_error import UpstreamHTTPError


def _instance(
    instance_id: int,
    role: PDRole,
    *,
    engine_type: str | None = None,
    dispatch_capabilities: list[str] | None = None,
) -> Instance:
    endpoint = Endpoint(
        id=instance_id,
        ip="127.0.0.1",
        business_port=str(8100 + instance_id),
        mgmt_port=str(9100 + instance_id),
        status=EndpointStatus.NORMAL,
    )
    return Instance(
        job_name=f"job-{instance_id}",
        model_name=engine_type or "model",
        engine_type=engine_type,
        dispatch_capabilities=dispatch_capabilities or [],
        id=instance_id,
        role=role,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1),
        endpoints={endpoint.ip: {endpoint.id: endpoint}},
    )


class _Scheduler:
    def __init__(
        self,
        *,
        prefill_engine_type: str | None = None,
        decode_engine_type: str | None = None,
        prefill_capabilities: list[str] | None = None,
        decode_capabilities: list[str] | None = None,
    ):
        if prefill_capabilities is None:
            prefill_capabilities = [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]
        if decode_capabilities is None:
            decode_capabilities = [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]
        self.p = _instance(
            1,
            PDRole.ROLE_P,
            engine_type=prefill_engine_type,
            dispatch_capabilities=prefill_capabilities,
        )
        self.d = _instance(
            2,
            PDRole.ROLE_D,
            engine_type=decode_engine_type,
            dispatch_capabilities=decode_capabilities,
        )
        self.update_workload = AsyncMock(return_value=True)

    async def select_and_allocate(self, role, req_info, **_kwargs):
        instance = self.p if role == PDRole.ROLE_P else self.d
        endpoint = next(iter(next(iter(instance.endpoints.values())).values()))
        return instance, endpoint, Workload(active_kv_cache=1, active_tokens=1)

    async def report_cb_event(self, instance_id: int, event: str) -> None:
        """No-op stub for circuit-breaker reporting."""

    async def get_unblocked_instances(self, role) -> list:
        """Return both instances as unblocked (no circuit breaker in test)."""
        return [self.p.id, self.d.id]


class _Client:
    def __init__(self, name: str, exc: Exception | None = None):
        self.name = name
        self.exc = exc
        self.requests = []
        self.headers = []
        self.base_url = f"http://{name}"
        self.timeout = 1

    async def post(self, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        if self.exc is not None:
            raise self.exc
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        if self.name == "prefill":
            return httpx.Response(
                status_code=200,
                json={"status": "cached", "id": json["request_id"]},
                request=request,
            )
        return httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            request=request,
        )


class _HTTPErrorClient(_Client):
    def __init__(self, name: str, status_code: int, body: dict):
        super().__init__(name)
        self.status_code = status_code
        self.body = body

    async def post(self, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        return httpx.Response(
            status_code=self.status_code,
            json=self.body,
            request=request,
        )


class _DelayedHTTPErrorClient(_HTTPErrorClient):
    def __init__(self, name: str, status_code: int, body: dict, release: asyncio.Event):
        super().__init__(name, status_code, body)
        self.release = release

    async def post(self, path, json=None, headers=None, timeout=None):
        await self.release.wait()
        return await super().post(path, json=json, headers=headers, timeout=timeout)


class _PrefillResultClient(_Client):
    async def post(self, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        dispatch = json[MOTOR_DISPATCH_KEY]
        return httpx.Response(
            status_code=200,
            json={
                "object": "motor.prefill_result",
                "schema_version": "1.0",
                "root_request_id": dispatch["root_request_id"],
                "engine_request_id": dispatch["engine_request_id"],
                "pair_id": dispatch["pair_id"],
                "attempt_seq": dispatch["attempt_seq"],
                "status": "completed",
                "handoff_mode": "handoff",
                "payload": {"opaque": "kv"},
            },
            request=request,
        )


class _StreamResponse:
    def __init__(self, chunks, exc_after_chunks: Exception | None = None):
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

    def raise_for_status(self):
        return None

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk
        if self.exc_after_chunks is not None:
            raise self.exc_after_chunks


class _StreamClient(_Client):
    def __init__(self, name: str, exc_after_chunks: Exception | None = None):
        super().__init__(name)
        self.exc_after_chunks = exc_after_chunks

    def stream(self, method, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        return _StreamResponse(
            [
                b'data: {"choices":[{"delta":{"content":"A"},"index":0}]}\n\n',
            ],
            exc_after_chunks=self.exc_after_chunks,
        )


class _SequenceStreamClient(_Client):
    def __init__(self, name: str, responses: list[_StreamResponse]):
        super().__init__(name)
        self.responses = responses

    def stream(self, method, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        return self.responses[len(self.requests) - 1]


class _SignallingStreamResponse(_StreamResponse):
    def __init__(self, release: asyncio.Event):
        super().__init__(
            [
                b'data: {"choices":[{"delta":{"content":"invisible"},"index":0}],"token_ids":[101]}\n\n',
            ]
        )
        self.release = release

    async def aiter_bytes(self):
        self.release.set()
        async for chunk in super().aiter_bytes():
            yield chunk


class _SignallingStreamClient(_Client):
    def __init__(self, name: str, release: asyncio.Event):
        super().__init__(name)
        self.release = release

    def stream(self, method, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        return _SignallingStreamResponse(self.release)


class _BlockingStreamResponse(_StreamResponse):
    def __init__(self, *, chunk: bytes | None = None):
        super().__init__([])
        self.chunk = chunk
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def aiter_bytes(self):
        self.started.set()
        try:
            if self.chunk is not None:
                yield self.chunk
            await asyncio.Event().wait()
        finally:
            self.closed.set()


class _BlockingStreamClient(_Client):
    def __init__(self, name: str, response: _BlockingStreamResponse):
        super().__init__(name)
        self.response = response

    def stream(self, method, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        return self.response


def _config() -> CoordinatorConfig:
    config = CoordinatorConfig()
    config.scheduler_config.scheduler_type = SchedulerType.LOAD_BALANCE
    config.exception_config = ExceptionConfig(max_retry=1, retry_delay=0)
    return config


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


async def _never_disconnect():
    await asyncio.Event().wait()
    return {"type": "http.disconnect"}


async def _invoke_asgi_response(response) -> list[dict]:
    messages = []

    async def send(message):
        messages.append(message)

    await response(_asgi_scope(), _never_disconnect, send)
    return messages


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (
            f"{cancel_error.NODE_FAULT}: http://127.0.0.1:8102",
            DispatchStopReason.PEER_FAILED,
        ),
        (cancel_error.CLIENT_DISCONNECT, DispatchStopReason.CLIENT_DISCONNECT),
        (cancel_error.DISPATCH_ABORT, DispatchStopReason.OTHER),
        (cancel_error.SCOPE_ABORT, DispatchStopReason.OTHER),
    ],
)
def test_unified_pd_cancel_stop_reason_mapping(reason, expected):
    assert UnifiedPDRouter._cancel_stop_reason(reason) == expected


def test_dispatch_plan_prefers_explicit_capability_over_engine_fallback():
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="sglang",
        prefill_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        decode_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
    )
    p_endpoint = next(iter(next(iter(scheduler.p.endpoints.values())).values()))
    d_endpoint = next(iter(next(iter(scheduler.d.endpoints.values())).values()))

    plan = select_dispatch_plan_for_pair(
        prefill=ScheduledResource(instance=scheduler.p, endpoint=p_endpoint),
        decode=ScheduledResource(instance=scheduler.d, endpoint=d_endpoint),
    )

    assert plan == DispatchPlan.CONCURRENT_ENGINE_SYNC


def test_dispatch_plan_requires_connector_capability():
    scheduler = _Scheduler(prefill_capabilities=[], decode_capabilities=[])
    p_endpoint = next(iter(next(iter(scheduler.p.endpoints.values())).values()))
    d_endpoint = next(iter(next(iter(scheduler.d.endpoints.values())).values()))

    with pytest.raises(DispatchPlanNotSupported, match="do not advertise"):
        select_dispatch_plan_for_pair(
            prefill=ScheduledResource(instance=scheduler.p, endpoint=p_endpoint),
            decode=ScheduledResource(instance=scheduler.d, endpoint=d_endpoint),
        )


def test_dispatch_plan_requires_capability_from_both_instances():
    scheduler = _Scheduler(
        prefill_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        decode_capabilities=[],
    )
    p_endpoint = next(iter(next(iter(scheduler.p.endpoints.values())).values()))
    d_endpoint = next(iter(next(iter(scheduler.d.endpoints.values())).values()))

    with pytest.raises(DispatchPlanNotSupported, match="do not advertise"):
        select_dispatch_plan_for_pair(
            prefill=ScheduledResource(instance=scheduler.p, endpoint=p_endpoint),
            decode=ScheduledResource(instance=scheduler.d, endpoint=d_endpoint),
        )


@pytest.mark.asyncio
async def test_unified_pd_nonstream_dispatches_prefill_and_decode_with_same_attempt(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-1",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()

    assert response.body == b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'
    assert len(p_client.requests) == 1
    assert len(d_client.requests) == 1

    p_dispatch = p_client.requests[0][MOTOR_DISPATCH_KEY]
    d_dispatch = d_client.requests[0][MOTOR_DISPATCH_KEY]
    assert p_dispatch["role"] == "prefill"
    assert d_dispatch["role"] == "decode"
    assert p_dispatch["root_request_id"] == "root-1"
    assert d_dispatch["root_request_id"] == "root-1"
    assert p_dispatch["attempt_seq"] == d_dispatch["attempt_seq"] == 1
    assert p_dispatch["pair_id"] == d_dispatch["pair_id"]
    assert p_client.requests[0]["request_id"] == "root-1#a1"
    assert d_client.requests[0]["request_id"] == "root-1#a1"
    assert p_client.headers[0]["X-Request-Id"] == "root-1#a1"
    assert d_client.headers[0]["X-Request-Id"] == "root-1#a1"
    assert scheduler.update_workload.await_count == 3
    assert ReqState.PREFILL_END in req_info.status
    assert req_info.status[ReqState.P_ALLOCATED] <= req_info.status[ReqState.PREFILL_END]
    assert req_info.status[ReqState.PREFILL_END] <= req_info.status[ReqState.DECODE_END]


@pytest.mark.asyncio
async def test_unified_pd_decode_failure_stops_both_legs(monkeypatch):
    req_info = RequestInfo(
        req_id="root-stop",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    req_info.trace_obj.set_trace_prompt = MagicMock()
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _Client("decode", exc=httpx.ConnectError("decode down"))
    stop_calls = []

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        stop_calls.append((resource.instance.role, attempt.attempt_seq, reason.value))
        return None

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    with pytest.raises(httpx.ConnectError):
        await router.handle_request()

    assert len(stop_calls) == 2
    req_info.trace_obj.set_trace_prompt.assert_called_with(req_info.req_data)
    assert {call[0] for call in stop_calls} == {PDRole.ROLE_P, PDRole.ROLE_D}
    assert all(call[1] == 1 for call in stop_calls)
    assert scheduler.update_workload.await_count == 3


@pytest.mark.asyncio
async def test_unified_pd_dual_dispatch_uses_dispatch_context_not_bootstrap_fields(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-sglang",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    await router.handle_request()

    for request_body in (p_client.requests[0], d_client.requests[0]):
        assert "bootstrap_host" not in request_body
        assert "bootstrap_port" not in request_body
        assert "bootstrap_room" not in request_body
        assert request_body[MOTOR_DISPATCH_KEY]["dispatch_mode"] == "pd_pair"


@pytest.mark.asyncio
async def test_unified_pd_cpcd_waits_for_prefill_result_before_decode(monkeypatch):
    req_info = RequestInfo(
        req_id="root-cpcd",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    handoff = [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
    scheduler = _Scheduler(prefill_capabilities=handoff, decode_capabilities=handoff)
    events = []
    select_and_allocate = scheduler.select_and_allocate

    async def _select_and_allocate(role, req_info, **kwargs):
        events.append(("select", PDRole(role)))
        return await select_and_allocate(role, req_info, **kwargs)

    async def _update_workload(params):
        events.append(("release", PDRole(params.role), params.workload_action))
        return True

    scheduler.select_and_allocate = AsyncMock(side_effect=_select_and_allocate)
    scheduler.update_workload = AsyncMock(side_effect=_update_workload)
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )

    class _RecordingPrefillClient(_PrefillResultClient):
        async def post(self, path, json=None, headers=None, timeout=None):
            response = await super().post(path, json=json, headers=headers, timeout=timeout)
            events.append(("prefill_result",))
            return response

    p_client = _RecordingPrefillClient("prefill")
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    await router.handle_request()

    assert len(p_client.requests) == 1
    assert len(d_client.requests) == 1
    p_dispatch = p_client.requests[0][MOTOR_DISPATCH_KEY]
    d_dispatch = d_client.requests[0][MOTOR_DISPATCH_KEY]
    assert p_dispatch["attempt_seq"] == d_dispatch["attempt_seq"] == 1
    assert p_dispatch["pair_id"] == d_dispatch["pair_id"]
    prefill_result = d_client.requests[0][MOTOR_PREFILL_RESULT_KEY]
    assert prefill_result["status"] == "completed"
    assert prefill_result["handoff_mode"] == "handoff"
    assert prefill_result["payload"] == {"opaque": "kv"}
    assert scheduler.update_workload.await_count == 3
    assert [event for event in events if event[0] == "select"] == [
        ("select", PDRole.ROLE_P),
        ("select", PDRole.ROLE_D),
    ]
    assert events.index(("prefill_result",)) < events.index(("select", PDRole.ROLE_D))
    assert ("release", PDRole.ROLE_P, WorkloadAction.RELEASE_TOKENS) in events
    assert ReqState.PREFILL_END in req_info.status
    assert req_info.status[ReqState.P_ALLOCATED] <= req_info.status[ReqState.PREFILL_END]
    assert req_info.status[ReqState.PREFILL_END] <= req_info.status[ReqState.DECODE_END]


@pytest.mark.asyncio
async def test_unified_pd_handoff_registers_decode_canceller_after_client_open(
    monkeypatch,
):
    # Regression: the late-allocated decode canceller must be registered only after the
    # decode client is opened, because HTTPClientPool.register_canceller is a no-op until
    # the client exists in the pool.
    req_info = RequestInfo(
        req_id="root-handoff-canceller-order",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    handoff = [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
    scheduler = _Scheduler(prefill_capabilities=handoff, decode_capabilities=handoff)
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )

    events = []
    original_register = AttemptContext.register_decode_canceller

    def _register_decode_canceller(self):
        events.append("register_decode_canceller")
        return original_register(self)

    monkeypatch.setattr(AttemptContext, "register_decode_canceller", _register_decode_canceller)

    p_client = _PrefillResultClient("prefill")
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            events.append("decode_client_open")
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    await router.handle_request()

    assert events.count("register_decode_canceller") == 1
    assert events.index("decode_client_open") < events.index("register_decode_canceller")


@pytest.mark.asyncio
async def test_unified_pd_nonretryable_upstream_error_is_not_retried(monkeypatch):
    req_info = RequestInfo(
        req_id="root-reject",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    config.exception_config.transport_max_retry = 3
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _HTTPErrorClient(
        "prefill",
        400,
        {"error": {"message": "prompt exceeds maximum context length", "code": 400}},
    )
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        return None

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    with pytest.raises(UpstreamHTTPError) as exc_info:
        await router.handle_request()

    assert exc_info.value.status_code == 400
    assert len(p_client.requests) == 1
    assert len(d_client.requests) == 1


@pytest.mark.asyncio
async def test_unified_pd_stream_prefill_rejection_is_returned_before_first_decode_token(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-stream-reject",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    config.exception_config.transport_max_retry = 3
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    error_body = {"error": {"message": "prompt exceeds maximum context length", "code": 400}}
    decode_chunk_received = asyncio.Event()
    p_client = _DelayedHTTPErrorClient("prefill", 400, error_body, decode_chunk_received)
    d_client = _SignallingStreamClient("decode", decode_chunk_received)
    processed_chunks = []

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        return None

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )
    monkeypatch.setattr(
        router.rescheduler,
        "process_stream_chunk",
        lambda chunk, **_kwargs: processed_chunks.append(chunk) or chunk,
    )

    response = await router.handle_request()
    messages = await asyncio.wait_for(_invoke_asgi_response(response), timeout=1)

    assert messages[0]["status"] == 400
    assert json.loads(messages[1]["body"]) == error_body
    assert processed_chunks == []
    assert len(p_client.requests) == 1
    assert len(d_client.requests) == 1


@pytest.mark.asyncio
async def test_unified_pd_cpcd_sglang_uses_concurrent_plan(monkeypatch):
    req_info = RequestInfo(
        req_id="root-cpcd-sglang",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    concurrent = [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]
    scheduler = _Scheduler(
        prefill_engine_type="sglang",
        decode_engine_type="sglang",
        prefill_capabilities=concurrent,
        decode_capabilities=concurrent,
    )
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    await router.handle_request()

    assert len(p_client.requests) == 1
    assert len(d_client.requests) == 1
    assert MOTOR_PREFILL_RESULT_KEY not in d_client.requests[0]
    assert d_client.requests[0][MOTOR_DISPATCH_KEY]["dispatch_mode"] == "pd_pair"
    assert scheduler.update_workload.await_count == 3


@pytest.mark.asyncio
async def test_unified_pd_rejects_pair_without_shared_connector_capability(monkeypatch):
    req_info = RequestInfo(
        req_id="root-mixed",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_capabilities=[DispatchPlan.CONCURRENT_ENGINE_SYNC.value],
        decode_capabilities=[DispatchPlan.PREFILL_HANDOFF_DECODE.value],
    )
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    stop_calls = []

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        stop_calls.append((resource.instance.role, attempt.attempt_seq, reason.value))
        return None

    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    with pytest.raises(RuntimeError, match="no shared dispatch capability"):
        await router.handle_request()

    assert {call[0] for call in stop_calls} == {PDRole.ROLE_P, PDRole.ROLE_D}


@pytest.mark.asyncio
async def test_unified_pd_release_rpc_survives_waiter_cancellation():
    req_info = RequestInfo(
        req_id="root-release-shield",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    request_manager = RequestManager(config)
    scheduler = _Scheduler()
    update_started = asyncio.Event()
    allow_update = asyncio.Event()
    update_done = asyncio.Event()

    async def _update_workload(params):
        update_started.set()
        await allow_update.wait()
        update_done.set()
        return True

    scheduler.update_workload = AsyncMock(side_effect=_update_workload)
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=request_manager,
    )

    await request_manager.add_req_info(req_info)
    try:
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        release_task = asyncio.create_task(
            router._release_attempt_resource(
                attempt.prefill_resource,
                attempt.attempt_seq,
                WorkloadAction.RELEASE_TOKENS,
                attempt,
            )
        )
        await asyncio.wait_for(update_started.wait(), timeout=1)
        release_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await release_task

        duplicate = await router._release_attempt_resource(
            attempt.prefill_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt,
            wait=False,
        )
        assert duplicate is True
        assert scheduler.update_workload.await_count == 1

        allow_update.set()
        await asyncio.wait_for(update_done.wait(), timeout=1)
        await router._drain_release_tasks()
        assert attempt.release_flags.prefill_tokens
    finally:
        await request_manager.del_req_info(req_info.req_id)


@pytest.mark.asyncio
async def test_unified_pd_release_inflight_deduplicates_same_action():
    req_info = RequestInfo(
        req_id="root-release-dedupe",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    request_manager = RequestManager(config)
    scheduler = _Scheduler()
    update_started = asyncio.Event()
    allow_update = asyncio.Event()

    async def _update_workload(params):
        update_started.set()
        await allow_update.wait()
        return True

    scheduler.update_workload = AsyncMock(side_effect=_update_workload)
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=request_manager,
    )

    await request_manager.add_req_info(req_info)
    try:
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        first = await router._release_attempt_resource(
            attempt.prefill_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt,
            wait=False,
        )
        await asyncio.wait_for(update_started.wait(), timeout=1)
        second = await router._release_attempt_resource(
            attempt.prefill_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt,
            wait=False,
        )

        assert first is True
        assert second is True
        assert scheduler.update_workload.await_count == 1

        allow_update.set()
        await router._drain_release_tasks()

        assert scheduler.update_workload.await_count == 1
        assert attempt.release_flags.prefill_tokens
        assert not router._release_inflight
    finally:
        await request_manager.del_req_info(req_info.req_id)


@pytest.mark.asyncio
async def test_unified_pd_release_carries_stable_operation_id():
    """Release RPCs carry a deterministic operation_id keyed on (request, attempt, endpoint, action)
    so a retried release is de-duplicated scheduler-side instead of double-applied (which would drive
    the load ledger negative).
    """
    req_info = RequestInfo(
        req_id="root-op-id",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    request_manager = RequestManager(config)
    scheduler = _Scheduler()
    router = UnifiedPDRouter(req_info, config, scheduler=scheduler, request_manager=request_manager)

    await request_manager.add_req_info(req_info)
    try:
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        resource = attempt.prefill_resource
        item = await router._prepare_release_work_item(
            resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt=attempt,
        )
        assert item is not None
        op_id = item.params.operation_id
        assert op_id  # set (was previously None, disabling the scheduler-side dedup)
        assert str(req_info.req_id) in op_id
        assert str(resource.instance.id) in op_id
        assert str(resource.endpoint.id) in op_id
        assert WorkloadAction.RELEASE_TOKENS.value in op_id
        # A different action on the same resource must get a different id.
        item_kv = await router._prepare_release_work_item(
            resource, attempt.attempt_seq, WorkloadAction.RELEASE_KV, attempt=attempt
        )
        assert item_kv is not None
        assert item_kv.params.operation_id != op_id
    finally:
        await request_manager.del_req_info(req_info.req_id)


@pytest.mark.asyncio
async def test_unified_pd_release_retry_reuses_same_operation_id():
    """A failed release RPC is retried with the SAME operation_id, so the scheduler dedups the retry
    rather than applying the delta twice.
    """
    req_info = RequestInfo(
        req_id="root-op-id-retry",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    request_manager = RequestManager(config)
    scheduler = _Scheduler()
    seen = []

    async def _update_workload(params):
        seen.append(params.operation_id)
        return len(seen) >= 2  # fail the first send, succeed the retry

    scheduler.update_workload = AsyncMock(side_effect=_update_workload)
    router = UnifiedPDRouter(req_info, config, scheduler=scheduler, request_manager=request_manager)

    await request_manager.add_req_info(req_info)
    try:
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        ok = await router._release_attempt_resource(
            attempt.prefill_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt,
        )
        assert ok is True
        assert len(seen) == 2  # one retry happened
        assert seen[0] and seen[0] == seen[1]  # same non-empty operation_id across the retry
    finally:
        await request_manager.del_req_info(req_info.req_id)


@pytest.mark.asyncio
async def test_unified_pd_background_release_uses_single_tracked_task():
    req_info = RequestInfo(
        req_id="root-release-background-single-task",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    request_manager = RequestManager(config)
    scheduler = _Scheduler()
    update_started = asyncio.Event()
    allow_update = asyncio.Event()

    async def _update_workload(params):
        update_started.set()
        await allow_update.wait()
        return True

    scheduler.update_workload = AsyncMock(side_effect=_update_workload)
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=request_manager,
    )

    await request_manager.add_req_info(req_info)
    try:
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        router._submit_release_attempt_resource_background(
            attempt.prefill_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt,
        )
        await asyncio.wait_for(update_started.wait(), timeout=1)

        assert len(router._release_records) == 1
        assert sum(record.item is not None for record in router._release_records.values()) == 1
        assert scheduler.update_workload.await_count == 1

        router._submit_release_attempt_resource_background(
            attempt.prefill_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt,
        )
        assert len(router._release_records) == 1
        assert scheduler.update_workload.await_count == 1

        allow_update.set()
        await router._drain_release_tasks()

        assert not router._release_records
        assert not router._release_inflight
        assert attempt.release_flags.prefill_tokens
    finally:
        await request_manager.del_req_info(req_info.req_id)


@pytest.mark.asyncio
async def test_unified_pd_release_failure_keeps_local_release_and_is_drained(caplog):
    req_info = RequestInfo(
        req_id="root-release-failure",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    request_manager = RequestManager(config)
    scheduler = _Scheduler()
    scheduler.update_workload = AsyncMock(return_value=False)
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=request_manager,
    )

    await request_manager.add_req_info(req_info)
    try:
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        original = await request_manager.get_req_attempt_workload(
            req_info.req_id,
            attempt.attempt_seq,
            PDRole.ROLE_P,
        )
        assert original is not None
        original_active_kv_cache = original.active_kv_cache

        submitted = await router._release_attempt_resource(
            attempt.prefill_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_KV,
            attempt,
            wait=False,
        )
        assert submitted is True
        await router._drain_release_tasks()

        current = await request_manager.get_req_attempt_workload(
            req_info.req_id,
            attempt.attempt_seq,
            PDRole.ROLE_P,
        )
        assert current is not None
        assert current.active_kv_cache < original_active_kv_cache
        assert not attempt.release_flags.prefill_kv
        assert scheduler.update_workload.await_count == 3
        assert "Release workload background task failed" in caplog.text
        assert "Release workload rolled back locally" not in caplog.text
    finally:
        await request_manager.del_req_info(req_info.req_id)


@pytest.mark.asyncio
async def test_unified_pd_release_cancel_propagates_and_cleans_tracking(caplog):
    req_info = RequestInfo(
        req_id="root-release-cancel-propagates",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    request_manager = RequestManager(config)
    scheduler = _Scheduler()

    async def _cancelled_update(_params):
        raise asyncio.CancelledError

    scheduler.update_workload = AsyncMock(side_effect=_cancelled_update)
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=request_manager,
    )

    await request_manager.add_req_info(req_info)
    try:
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        with pytest.raises(asyncio.CancelledError):
            await router._release_attempt_resource(
                attempt.prefill_resource,
                attempt.attempt_seq,
                WorkloadAction.RELEASE_TOKENS,
                attempt,
            )

        assert scheduler.update_workload.await_count == 1
        assert not attempt.release_flags.prefill_tokens
        assert not router._release_records
        assert not router._release_inflight
        assert "Release workload task cancelled stage=release_p_tokens" in caplog.text
    finally:
        await request_manager.del_req_info(req_info.req_id)


@pytest.mark.asyncio
async def test_unified_pd_background_release_cancel_is_logged_and_drained(caplog):
    req_info = RequestInfo(
        req_id="root-release-background-cancel",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    request_manager = RequestManager(config)
    scheduler = _Scheduler()

    async def _cancelled_update(_params):
        raise asyncio.CancelledError

    scheduler.update_workload = AsyncMock(side_effect=_cancelled_update)
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=request_manager,
    )

    await request_manager.add_req_info(req_info)
    try:
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        submitted = await router._release_attempt_resource(
            attempt.prefill_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt,
            wait=False,
        )
        assert submitted is True

        await router._drain_release_tasks()

        assert scheduler.update_workload.await_count == 1
        assert not attempt.release_flags.prefill_tokens
        assert not router._release_records
        assert not router._release_inflight
        assert "Release workload background task cancelled stage=release_p_tokens" in caplog.text
    finally:
        await request_manager.del_req_info(req_info.req_id)


@pytest.mark.asyncio
async def test_unified_pd_drain_double_cancel_keeps_release_cleanup():
    req_info = RequestInfo(
        req_id="root-release-drain-double-cancel",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    request_manager = RequestManager(config)
    scheduler = _Scheduler()
    update_started = asyncio.Event()
    allow_update = asyncio.Event()
    update_done = asyncio.Event()

    async def _update_workload(_params):
        update_started.set()
        await allow_update.wait()
        update_done.set()
        return True

    scheduler.update_workload = AsyncMock(side_effect=_update_workload)
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=request_manager,
    )

    await request_manager.add_req_info(req_info)
    try:
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
        submitted = await router._release_attempt_resource(
            attempt.prefill_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt,
            wait=False,
        )
        assert submitted is True
        await asyncio.wait_for(update_started.wait(), timeout=1)

        drain_task = asyncio.create_task(router._drain_release_tasks())
        await asyncio.sleep(0)
        drain_task.cancel()
        drain_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await drain_task

        assert router._release_records

        allow_update.set()
        await asyncio.wait_for(update_done.wait(), timeout=1)
        for _ in range(20):
            if not router._release_records:
                break
            await asyncio.sleep(0)

        assert scheduler.update_workload.await_count == 1
        assert attempt.release_flags.prefill_tokens
        assert not router._release_records
        assert not router._release_inflight
    finally:
        await request_manager.del_req_info(req_info.req_id)


@pytest.mark.asyncio
async def test_unified_pd_concurrent_stream_tail_release_survives_iterator_cancellation(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-stream-tail-cancel",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    release_started = asyncio.Event()
    allow_release = asyncio.Event()

    async def _update_workload(params):
        release_started.set()
        await allow_release.wait()
        return True

    scheduler.update_workload = AsyncMock(side_effect=_update_workload)
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _StreamClient("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    chunks = []
    first_chunk_seen = asyncio.Event()

    async def _consume_response():
        chunks.append(await anext(response.body_iterator))
        first_chunk_seen.set()
        await anext(response.body_iterator)

    consumer_task = asyncio.create_task(_consume_response())
    await asyncio.wait_for(first_chunk_seen.wait(), timeout=1)
    assert chunks == [b'data: {"choices":[{"delta":{"content":"A"},"index":0}]}\n\n']
    await asyncio.wait_for(release_started.wait(), timeout=1)
    for _ in range(20):
        if len(router._release_records) == 3:
            break
        await asyncio.sleep(0)
    assert len(router._release_records) == 3

    consumer_task.cancel()
    allow_release.set()
    with pytest.raises((asyncio.CancelledError, StopAsyncIteration)):
        await consumer_task

    await router._drain_release_tasks()

    assert scheduler.update_workload.await_count == 3
    assert not router._release_records


@pytest.mark.asyncio
async def test_unified_pd_handoff_stream_yields_before_prefill_kv_release_finishes(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-handoff-kv-background",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    handoff = [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
    scheduler = _Scheduler(prefill_capabilities=handoff, decode_capabilities=handoff)
    kv_release_compute_started = asyncio.Event()
    allow_kv_release_compute = asyncio.Event()
    allow_kv_release = asyncio.Event()
    kv_release_done = asyncio.Event()

    async def _update_workload(params):
        if params.role == PDRole.ROLE_P and params.workload_action == WorkloadAction.RELEASE_KV:
            await allow_kv_release.wait()
            kv_release_done.set()
        return True

    scheduler.update_workload = AsyncMock(side_effect=_update_workload)
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    compute_and_update = router._workload_action_handler.compute_and_update

    async def _compute_and_update(resource, req_id, action, req_info_arg, **kwargs):
        if resource.instance.role == PDRole.ROLE_P and action == WorkloadAction.RELEASE_KV:
            kv_release_compute_started.set()
            await allow_kv_release_compute.wait()
        return await compute_and_update(resource, req_id, action, req_info_arg, **kwargs)

    monkeypatch.setattr(router._workload_action_handler, "compute_and_update", _compute_and_update)
    p_client = _PrefillResultClient("prefill")
    d_client = _StreamClient("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    first_chunk = await asyncio.wait_for(anext(response.body_iterator), timeout=1)

    assert first_chunk == b'data: {"choices":[{"delta":{"content":"A"},"index":0}]}\n\n'
    await asyncio.wait_for(kv_release_compute_started.wait(), timeout=1)
    assert not kv_release_done.is_set()

    allow_kv_release_compute.set()
    allow_kv_release.set()
    await asyncio.wait_for(kv_release_done.wait(), timeout=1)
    await response.body_iterator.aclose()
    await router._drain_release_tasks()


@pytest.mark.asyncio
async def test_unified_pd_handoff_stream_yields_before_prefill_token_release_finishes(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-handoff-token-background",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    handoff = [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
    scheduler = _Scheduler(prefill_capabilities=handoff, decode_capabilities=handoff)
    token_release_compute_started = asyncio.Event()
    allow_token_release_compute = asyncio.Event()
    token_release_started = asyncio.Event()
    allow_token_release = asyncio.Event()
    token_release_done = asyncio.Event()
    d_selected = asyncio.Event()
    select_and_allocate = scheduler.select_and_allocate

    async def _select_and_allocate(role, req_info, **kwargs):
        result = await select_and_allocate(role, req_info, **kwargs)
        if role == PDRole.ROLE_D:
            d_selected.set()
        return result

    async def _update_workload(params):
        if params.role == PDRole.ROLE_P and params.workload_action == WorkloadAction.RELEASE_TOKENS:
            token_release_started.set()
            await allow_token_release.wait()
            token_release_done.set()
        return True

    scheduler.select_and_allocate = AsyncMock(side_effect=_select_and_allocate)
    scheduler.update_workload = AsyncMock(side_effect=_update_workload)
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    compute_and_update = router._workload_action_handler.compute_and_update

    async def _compute_and_update(resource, req_id, action, req_info_arg, **kwargs):
        if resource.instance.role == PDRole.ROLE_P and action == WorkloadAction.RELEASE_TOKENS:
            token_release_compute_started.set()
            await allow_token_release_compute.wait()
        return await compute_and_update(resource, req_id, action, req_info_arg, **kwargs)

    monkeypatch.setattr(router._workload_action_handler, "compute_and_update", _compute_and_update)
    p_client = _PrefillResultClient("prefill")
    d_client = _StreamClient("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    first_chunk_task = asyncio.create_task(anext(response.body_iterator))

    await asyncio.wait_for(token_release_compute_started.wait(), timeout=1)
    await asyncio.wait_for(d_selected.wait(), timeout=1)
    assert not token_release_started.is_set()
    assert not token_release_done.is_set()

    allow_token_release_compute.set()
    await asyncio.wait_for(token_release_started.wait(), timeout=1)
    allow_token_release.set()
    first_chunk = await asyncio.wait_for(first_chunk_task, timeout=1)
    assert first_chunk == b'data: {"choices":[{"delta":{"content":"A"},"index":0}]}\n\n'
    await asyncio.wait_for(token_release_done.wait(), timeout=1)
    await response.body_iterator.aclose()
    await router._drain_release_tasks()


@pytest.mark.asyncio
async def test_unified_pd_stream_dispatches_context_and_yields_visible_chunk(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-stream",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _Client("prefill")
    d_client = _StreamClient("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == [b'data: {"choices":[{"delta":{"content":"A"},"index":0}]}\n\n']
    assert len(p_client.requests) == 1
    assert len(d_client.requests) == 1
    assert d_client.requests[0][MOTOR_DISPATCH_KEY]["role"] == "decode"
    assert p_client.requests[0][MOTOR_DISPATCH_KEY]["pair_id"] == d_client.requests[0][MOTOR_DISPATCH_KEY]["pair_id"]
    assert scheduler.update_workload.await_count == 3
    assert ReqState.PREFILL_END in req_info.status


@pytest.mark.asyncio
async def test_unified_pd_concurrent_stream_prefill_release_does_not_block_first_chunk(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-concurrent-prefill-release-background",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_token_compute_started = asyncio.Event()
    p_kv_compute_started = asyncio.Event()
    allow_p_release_compute = asyncio.Event()
    p_token_rpc_done = asyncio.Event()
    p_kv_rpc_done = asyncio.Event()
    compute_and_update = router._workload_action_handler.compute_and_update

    async def _compute_and_update(resource, req_id, action, req_info_arg, **kwargs):
        if resource.instance.role == PDRole.ROLE_P and action in {
            WorkloadAction.RELEASE_TOKENS,
            WorkloadAction.RELEASE_KV,
        }:
            if action == WorkloadAction.RELEASE_TOKENS:
                p_token_compute_started.set()
            else:
                p_kv_compute_started.set()
            await allow_p_release_compute.wait()
        return await compute_and_update(resource, req_id, action, req_info_arg, **kwargs)

    async def _update_workload(params):
        if params.role == PDRole.ROLE_P and params.workload_action == WorkloadAction.RELEASE_TOKENS:
            p_token_rpc_done.set()
        if params.role == PDRole.ROLE_P and params.workload_action == WorkloadAction.RELEASE_KV:
            p_kv_rpc_done.set()
        return True

    monkeypatch.setattr(router._workload_action_handler, "compute_and_update", _compute_and_update)
    scheduler.update_workload = AsyncMock(side_effect=_update_workload)
    p_client = _Client("prefill")
    d_client = _StreamClient("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    first_chunk = await asyncio.wait_for(anext(response.body_iterator), timeout=1)

    assert first_chunk == b'data: {"choices":[{"delta":{"content":"A"},"index":0}]}\n\n'
    await asyncio.wait_for(p_token_compute_started.wait(), timeout=1)
    await asyncio.wait_for(p_kv_compute_started.wait(), timeout=1)
    assert not p_token_rpc_done.is_set()
    assert not p_kv_rpc_done.is_set()

    allow_p_release_compute.set()
    await response.body_iterator.aclose()
    await router._drain_release_tasks()
    await asyncio.wait_for(p_token_rpc_done.wait(), timeout=1)
    await asyncio.wait_for(p_kv_rpc_done.wait(), timeout=1)


@pytest.mark.asyncio
async def test_unified_pd_concurrent_nonstream_releases_prefill_tokens_before_decode_finishes(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-concurrent-nonstream-prefill-release",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/chat/completions",
        entry_api="v1/chat/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    allow_decode = asyncio.Event()
    p_token_compute_started = asyncio.Event()
    allow_p_release_compute = asyncio.Event()
    compute_and_update = router._workload_action_handler.compute_and_update

    class _DelayedDecodeClient(_Client):
        async def post(self, path, json=None, headers=None, timeout=None):
            self.requests.append(json)
            self.headers.append(headers or {})
            await allow_decode.wait()
            request = httpx.Request("POST", path, headers=headers or {}, json=json)
            return httpx.Response(
                status_code=200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
                request=request,
            )

    async def _compute_and_update(resource, req_id, action, req_info_arg, **kwargs):
        if resource.instance.role == PDRole.ROLE_P and action == WorkloadAction.RELEASE_TOKENS:
            p_token_compute_started.set()
            await allow_p_release_compute.wait()
        return await compute_and_update(resource, req_id, action, req_info_arg, **kwargs)

    monkeypatch.setattr(router._workload_action_handler, "compute_and_update", _compute_and_update)
    p_client = _Client("prefill")
    d_client = _DelayedDecodeClient("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response_task = asyncio.create_task(router.handle_request())
    await asyncio.wait_for(p_token_compute_started.wait(), timeout=1)
    assert not response_task.done()

    allow_p_release_compute.set()
    allow_decode.set()
    response = await asyncio.wait_for(response_task, timeout=1)

    assert json.loads(response.body)["choices"][0]["message"]["content"] == "ok"


@pytest.mark.asyncio
async def test_unified_pd_client_disconnect_cancels_tasks_and_stops_engine(monkeypatch):
    req_info = RequestInfo(
        req_id="root-stream-disconnect",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    config.exception_config.transport_max_retry = 3
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _Client("prefill")
    d_response = _BlockingStreamResponse()
    d_client = _BlockingStreamClient("decode", d_response)
    attempts = []
    stop_calls = []
    original_create_attempt = router._create_attempt

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    async def _create_attempt(session):
        attempt = await original_create_attempt(session)
        attempts.append(attempt)
        return attempt

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        stop_calls.append((resource.instance.role, attempt.attempt_seq, reason))
        return None

    async def receive():
        await d_response.started.wait()
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(router, "_create_attempt", _create_attempt)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    response = await router.handle_request()
    await asyncio.wait_for(response(_asgi_scope(), receive, send), timeout=1)

    assert len(attempts) == 1
    attempt = attempts[0]
    await asyncio.wait_for(d_response.closed.wait(), timeout=1)
    assert attempt.state == AttemptState.STOPPED
    assert attempt.stop_sent is True
    assert attempt.prefill_task.done()
    assert attempt.decode_task.done()
    assert set(stop_calls) == {
        (PDRole.ROLE_P, 1, DispatchStopReason.CLIENT_DISCONNECT),
        (PDRole.ROLE_D, 1, DispatchStopReason.CLIENT_DISCONNECT),
    }
    assert scheduler.update_workload.await_count == 3
    assert not any(
        task.get_name() == "unified-pd-queue-root-stream-disconnect-a1" and not task.done()
        for task in asyncio.all_tasks()
    )


@pytest.mark.asyncio
async def test_unified_pd_stop_attempt_drains_release_failures(monkeypatch, caplog):
    req_info = RequestInfo(
        req_id="root-stop-drain-release-failure",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    request_manager = RequestManager(config)
    scheduler = _Scheduler()
    scheduler.update_workload = AsyncMock(return_value=False)
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=request_manager,
    )

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        return None

    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    await request_manager.add_req_info(req_info)
    try:
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))

        await router._stop_attempt(attempt, DispatchStopReason.CLIENT_DISCONNECT)

        assert attempt.state == AttemptState.STOPPED
        assert scheduler.update_workload.await_count == 9
        assert "Release workload background task failed stage=release_p_tokens" in caplog.text
        assert "Release workload background task failed stage=release_p_kv" in caplog.text
        assert "Release workload background task failed stage=release_d_tokens" in caplog.text
        assert not router._release_records
        assert not router._release_inflight
    finally:
        await request_manager.del_req_info(req_info.req_id)


@pytest.mark.asyncio
async def test_unified_pd_send_failure_closes_decode_and_stops_engine(monkeypatch):
    req_info = RequestInfo(
        req_id="root-stream-send-failure",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    scheduler = _Scheduler()
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _Client("prefill")
    d_response = _BlockingStreamResponse(
        chunk=b'data: {"choices":[{"text":"A","index":0}]}\n\n',
    )
    d_client = _BlockingStreamClient("decode", d_response)
    attempts = []
    stop_calls = []
    original_create_attempt = router._create_attempt

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    async def _create_attempt(session):
        attempt = await original_create_attempt(session)
        attempts.append(attempt)
        return attempt

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        stop_calls.append((resource.instance.role, attempt.attempt_seq, reason))
        return None

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("client socket closed")

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(router, "_create_attempt", _create_attempt)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    response = await router.handle_request()
    with pytest.raises(ClientDisconnect):
        await asyncio.wait_for(response(_asgi_scope(), _never_disconnect, send), timeout=1)

    assert len(attempts) == 1
    attempt = attempts[0]
    await asyncio.wait_for(d_response.closed.wait(), timeout=1)
    assert attempt.state == AttemptState.STOPPED
    assert attempt.stop_sent is True
    assert attempt.prefill_task.done()
    assert attempt.decode_task.done()
    assert set(stop_calls) == {
        (PDRole.ROLE_P, 1, DispatchStopReason.CLIENT_DISCONNECT),
        (PDRole.ROLE_D, 1, DispatchStopReason.CLIENT_DISCONNECT),
    }
    assert scheduler.update_workload.await_count == 3


@pytest.mark.asyncio
async def test_unified_pd_stream_error_after_visible_chunk_reschedules_with_token_replay(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-stream-reschedule",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    config = _config()
    config.exception_config.transport_max_retry = 2
    config.exception_config.reschedule_enabled = True
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    build_plan_calls = []
    original_build_retry_plan = router.rescheduler.build_retry_plan

    def _build_retry_plan(req_data):
        build_plan_calls.append(req_data)
        return original_build_retry_plan(req_data)

    monkeypatch.setattr(router.rescheduler, "build_retry_plan", _build_retry_plan)
    p_client = _Client("prefill")
    d_client = _SequenceStreamClient(
        "decode",
        [
            _StreamResponse(
                [
                    b'data: {"choices":[{"text":"A","index":0,"prompt_token_ids":[1,2],"token_ids":[10]}]}\n\n',
                ],
                exc_after_chunks=httpx.ReadError("after chunk"),
            ),
            _StreamResponse(
                [
                    b'data: {"choices":[{"text":"B","index":0,"token_ids":[11],"finish_reason":"stop"}]}\n\n',
                ]
            ),
        ],
    )

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        return None

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    response = await router.handle_request()
    messages = await _invoke_asgi_response(response)
    body = b"".join(message["body"] for message in messages if message["type"] == "http.response.body")

    assert messages[0]["status"] == 200
    assert len(build_plan_calls) == 1
    assert len(p_client.requests) == 2
    assert len(d_client.requests) == 2
    assert d_client.requests[0]["return_token_ids"] is True
    assert p_client.requests[1]["prompt"] == [1, 2, 10]
    assert p_client.requests[1]["max_tokens"] == 1
    assert p_client.requests[1]["stream"] is False
    assert d_client.requests[1]["prompt"] == [1, 2, 10]
    assert d_client.requests[1]["max_tokens"] == 7
    assert b'"text":"A"' in body
    assert b'"text":"B"' in body
    assert b"token_ids" not in body
    assert b"ReadError" not in body


@pytest.mark.asyncio
async def test_unified_pd_retry_plan_validation_fails_before_new_attempt_allocation(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-stream-invalid-replay",
        req_data={
            "model": "m",
            "prompt": "hello",
            "stream": True,
            "max_tokens": 8,
            "n": 2,
        },
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    config = _config()
    config.exception_config.transport_max_retry = 3
    config.exception_config.reschedule_enabled = True
    scheduler = _Scheduler()
    select_and_allocate = scheduler.select_and_allocate
    scheduler.select_and_allocate = AsyncMock(side_effect=select_and_allocate)
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    stop_calls = []
    build_plan_calls = []
    original_build_retry_plan = router.rescheduler.build_retry_plan

    async def _run_stream_attempt(_attempt, _dispatch_plan):
        req_info.prompt_token_ids = [1, 2]
        req_info.cached_token_ids = [10]
        raise httpx.ReadError("after token cache")
        yield b""  # pylint: disable=unreachable

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        stop_calls.append((resource.instance.role, attempt.attempt_seq, reason))
        return None

    def _build_retry_plan(req_data):
        build_plan_calls.append(req_data)
        return original_build_retry_plan(req_data)

    monkeypatch.setattr(router, "_run_stream_attempt", _run_stream_attempt)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )
    monkeypatch.setattr(router.rescheduler, "build_retry_plan", _build_retry_plan)

    response = await router.handle_request()
    messages = await _invoke_asgi_response(response)
    body = json.loads(messages[1]["body"])

    assert messages[0]["status"] == 502
    assert "parallel sampling" in body["detail"]
    assert len(build_plan_calls) == 1
    assert scheduler.select_and_allocate.await_count == 2
    assert set(stop_calls) == {
        (PDRole.ROLE_P, 1, DispatchStopReason.PEER_FAILED),
        (PDRole.ROLE_D, 1, DispatchStopReason.PEER_FAILED),
    }


@pytest.mark.asyncio
async def test_unified_pd_handoff_stream_retry_replays_same_prompt_through_prefill(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-handoff-reschedule",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    handoff = [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
    scheduler = _Scheduler(prefill_capabilities=handoff, decode_capabilities=handoff)
    config = _config()
    config.exception_config.transport_max_retry = 2
    config.exception_config.reschedule_enabled = True
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _PrefillResultClient("prefill")
    d_client = _SequenceStreamClient(
        "decode",
        [
            _StreamResponse(
                [
                    b'data: {"choices":[{"text":"A","index":0,"prompt_token_ids":[1,2],"token_ids":[10]}]}\n\n',
                ],
                exc_after_chunks=httpx.ReadError("after chunk"),
            ),
            _StreamResponse(
                [
                    b'data: {"choices":[{"text":"B","index":0,"token_ids":[11],"finish_reason":"stop"}]}\n\n',
                ]
            ),
        ],
    )

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        return None

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    response = await router.handle_request()
    messages = await _invoke_asgi_response(response)
    body = b"".join(message["body"] for message in messages if message["type"] == "http.response.body")

    assert messages[0]["status"] == 200
    assert len(p_client.requests) == 2
    assert len(d_client.requests) == 2
    assert p_client.requests[1]["prompt"] == [1, 2, 10]
    assert p_client.requests[1]["max_tokens"] == 1
    assert p_client.requests[0][MOTOR_DISPATCH_KEY]["prefill_context_budget"] == {
        "max_output_tokens": 8,
        "parameter": "max_tokens",
    }
    assert p_client.requests[1][MOTOR_DISPATCH_KEY]["prefill_context_budget"] == {
        "max_output_tokens": 7,
        "parameter": "max_tokens",
    }
    assert d_client.requests[1]["prompt"] == [1, 2, 10]
    assert d_client.requests[1]["max_tokens"] == 7
    retry_prefill_result = d_client.requests[1][MOTOR_PREFILL_RESULT_KEY]
    assert retry_prefill_result["attempt_seq"] == 2
    assert retry_prefill_result["pair_id"] == d_client.requests[1][MOTOR_DISPATCH_KEY]["pair_id"]
    assert b'"text":"A"' in body
    assert b'"text":"B"' in body
    assert b"ReadError" not in body


@pytest.mark.asyncio
async def test_unified_pd_pool_node_fault_reschedules_and_stops_as_peer_failure(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-pool-node-fault",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    config = _config()
    config.exception_config.transport_max_retry = 2
    config.exception_config.reschedule_enabled = True
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    decode_started = asyncio.Event()
    decode_calls = []
    prefill_calls = []
    stop_calls = []

    async def _forward_prefill(self, api, req_data, client, timeout):
        prefill_calls.append(req_data.copy())
        request = httpx.Request("POST", f"/{api}", json=req_data)
        return httpx.Response(
            status_code=200,
            json={"status": "cached", "id": req_data["request_id"]},
            request=request,
        )

    async def _forward_decode(self, api, req_data, client, timeout, *, on_response_ready=None):
        decode_calls.append(req_data.copy())
        if on_response_ready is not None:
            on_response_ready()
        if len(decode_calls) == 1:
            yield (b'data: {"choices":[{"text":"A","index":0,"prompt_token_ids":[1,2],"token_ids":[10]}]}\n\n')
            decode_started.set()
            await asyncio.Event().wait()
        yield b'data: {"choices":[{"text":"B","index":0,"token_ids":[11],"finish_reason":"stop"}]}\n\n'

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        stop_calls.append((resource.instance.role, attempt.attempt_seq, reason))
        return None

    monkeypatch.setattr(UnifiedPDRouter, "forward_request", _forward_prefill)
    monkeypatch.setattr(UnifiedPDRouter, "forward_stream_request", _forward_decode)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    pool = HTTPClientPool()
    p_endpoint = next(iter(next(iter(scheduler.p.endpoints.values())).values()))
    d_endpoint = next(iter(next(iter(scheduler.d.endpoints.values())).values()))
    await pool.get_client(
        p_endpoint.ip,
        p_endpoint.business_port,
        tls_config=config.infer_tls_config,
    )
    d_client = await pool.get_client(
        d_endpoint.ip,
        d_endpoint.business_port,
        tls_config=config.infer_tls_config,
    )

    try:
        response = await router.handle_request()
        response_task = asyncio.create_task(_invoke_asgi_response(response))
        await asyncio.wait_for(decode_started.wait(), timeout=5)
        while len(d_client._cancellers) != 1:
            await asyncio.sleep(0)

        first_pair_id = next(iter(d_client._cancellers))
        await d_client.cancel_all()

        messages = await asyncio.wait_for(response_task, timeout=5)
        body = b"".join(message["body"] for message in messages if message["type"] == "http.response.body")

        assert first_pair_id not in d_client._cancellers
        assert len(prefill_calls) == 2
        assert len(decode_calls) == 2
        assert decode_calls[1]["prompt"] == [1, 2, 10]
        assert b'"text":"A"' in body
        assert b'"text":"B"' in body
        assert len(stop_calls) == 2
        assert {call[0] for call in stop_calls} == {PDRole.ROLE_P, PDRole.ROLE_D}
        assert all(call[1] == 1 for call in stop_calls)
        assert all(call[2] == DispatchStopReason.PEER_FAILED for call in stop_calls)
        assert req_info.state == ReqState.DECODE_END
    finally:
        await pool.close_client(
            p_endpoint.ip,
            p_endpoint.business_port,
            tls_config=config.infer_tls_config,
        )
        await pool.close_client(
            d_endpoint.ip,
            d_endpoint.business_port,
            tls_config=config.infer_tls_config,
        )


@pytest.mark.asyncio
async def test_unified_pd_stream_error_after_visible_chunk_without_replay_does_not_retry(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-stream-error",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    config = _config()
    config.exception_config.transport_max_retry = 3
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _Client("prefill")
    d_client = _StreamClient("decode", exc_after_chunks=httpx.ReadError("after chunk"))
    stop_calls = []

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        stop_calls.append((resource.instance.role, attempt.attempt_seq, reason.value))
        return None

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    response = await router.handle_request()
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks[0] == b'data: {"choices":[{"delta":{"content":"A"},"index":0}]}\n\n'
    error_chunk = chunks[1].decode("utf-8") if isinstance(chunks[1], bytes) else chunks[1]
    assert "ReadError" in error_chunk
    assert len(d_client.requests) == 1
    assert len(stop_calls) == 2
    await router._drain_release_tasks()
    assert scheduler.update_workload.await_count == 3


@pytest.mark.asyncio
async def test_unified_pd_stream_error_before_first_body_retries_without_token_replay(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-stream-prebody-retry",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler()
    config = _config()
    config.exception_config.transport_max_retry = 2
    config.exception_config.reschedule_enabled = False
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _Client("prefill")
    d_client = _SequenceStreamClient(
        "decode",
        [
            _StreamResponse([], exc_after_chunks=httpx.ReadError("before first body")),
            _StreamResponse(
                [
                    b'data: {"choices":[{"delta":{"content":"B"},"index":0,"finish_reason":"stop"}]}\n\n',
                ]
            ),
        ],
    )

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    async def _stop(self, resource, attempt, reason, timeout=1.0):
        return None

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(
        "motor.coordinator.router.stop_client.DispatchStopClient.stop",
        _stop,
    )

    response = await router.handle_request()
    chunks = [chunk async for chunk in response.body_iterator]

    assert len(d_client.requests) == 2
    assert chunks == [b'data: {"choices":[{"delta":{"content":"B"},"index":0,"finish_reason":"stop"}]}\n\n']


def test_dispatch_carries_effective_output_budget_to_prefill_leg():
    session = PDDispatchSession(
        "request-1",
        prefill_context_budget=PrefillContextBudget(
            max_output_tokens=24,
            parameter="max_completion_tokens",
        ),
    )
    attempt = session.new_attempt(None, None, config=None, consumed_output_tokens=5)

    dispatch = attempt.dispatch_for(PDRole.ROLE_P, "prefill_handoff_decode")

    assert dispatch.prefill_context_budget == PrefillContextBudget(
        max_output_tokens=19,
        parameter="max_completion_tokens",
    )


def test_unified_pd_prefers_max_completion_tokens_and_preserves_parameter():
    router = SimpleNamespace(req_info=SimpleNamespace(req_data={"max_tokens": 32, "max_completion_tokens": 24}))

    assert UnifiedPDRouter._prefill_context_budget(router) == PrefillContextBudget(
        max_output_tokens=24,
        parameter="max_completion_tokens",
    )


def test_retry_plan_preserves_max_completion_tokens_precedence_for_completion_replay():
    plan = RetryRequestPlan(
        prompt_token_ids=(1, 2, 10),
        api="v1/completions",
        remove_chat_fields=True,
        cached_output_tokens=1,
    )
    request = {
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 32,
        "max_completion_tokens": 8,
    }

    decode_request, api = Rescheduler.apply_retry_plan(request, plan)

    assert api == "v1/completions"
    assert "messages" not in decode_request
    assert "max_completion_tokens" not in decode_request
    assert decode_request["max_tokens"] == 7
