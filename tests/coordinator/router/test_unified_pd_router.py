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
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

import motor.common.utils.error as cancel_error
from motor.common.http import HTTPClientPool
from motor.common.logger.logger import _resolve_logger_name
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
from motor.coordinator.router.adapters.pd_protocol import EngineProtocolError
from motor.coordinator.router.dispatch_session import (
    AttemptContext,
    AttemptState,
    AttemptStopReason,
    PDDispatchSession,
)
from motor.common.utils.error import RequestCancelledError
from motor.coordinator.router.rescheduler.rescheduler import Rescheduler, RetryRequestPlan
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter
from motor.coordinator.router.upstream_error import UpstreamHTTPError

_ROUTER_LOGGER = _resolve_logger_name("motor.coordinator.router.strategies.base")


def _instance(
    instance_id: int,
    role: PDRole,
    *,
    engine_type: str = "sglang",
    bootstrap_port: int | None = None,
) -> Instance:
    endpoint = Endpoint(
        id=instance_id,
        ip="127.0.0.1",
        business_port=str(8100 + instance_id),
        bootstrap_port=bootstrap_port,
        status=EndpointStatus.NORMAL,
    )
    return Instance(
        job_name=f"job-{instance_id}",
        model_name=engine_type,
        engine_type=engine_type,
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
        prefill_engine_type: str = "sglang",
        decode_engine_type: str = "sglang",
        prefill_bootstrap_port: int | None = 20001,
    ):
        self.p = _instance(
            1,
            PDRole.ROLE_P,
            engine_type=prefill_engine_type,
            bootstrap_port=prefill_bootstrap_port,
        )
        self.d = _instance(
            2,
            PDRole.ROLE_D,
            engine_type=decode_engine_type,
        )
        self.update_workload = AsyncMock(return_value=True)

    async def select_and_allocate(self, role, req_info, **_kwargs):
        instance = self.p if role == PDRole.ROLE_P else self.d
        required_engine_type = _kwargs.get("required_engine_type")
        if required_engine_type and instance.engine_type != required_engine_type:
            return None
        endpoint = next(iter(next(iter(instance.endpoints.values())).values()))
        return instance, endpoint, Workload(active_tokens=1)

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
        self.abort_requests = []
        self.headers = []
        self.base_url = f"http://{name}"
        self.timeout = 1

    async def post(self, path, json=None, headers=None, timeout=None):
        if str(path).rstrip("/").endswith("abort_request"):
            self.abort_requests.append(json)
            request = httpx.Request("POST", path, headers=headers or {}, json=json)
            return httpx.Response(status_code=200, json={}, request=request)
        self.requests.append(json)
        self.headers.append(headers or {})
        if self.exc is not None:
            raise self.exc
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        if self.name == "prefill":
            return httpx.Response(
                status_code=200,
                json={"status": "cached", "id": json.get("request_id") or json.get("rid")},
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


class _NativeHandoffPrefillClient(_Client):
    async def post(self, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        return httpx.Response(
            status_code=200,
            json={
                "kv_transfer_params": {
                    "do_remote_prefill": True,
                    "remote_request_id": json["request_id"],
                    "remote_host": "10.0.0.1",
                    "remote_port": 9000,
                    "connector_private": {"opaque": "kv"},
                },
                "usage": {
                    "prompt_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
            },
            request=request,
        )


class _NativeSglangPrefillClient(_Client):
    async def post(self, path, json=None, headers=None, timeout=None):
        self.requests.append(json)
        self.headers.append(headers or {})
        request = httpx.Request("POST", path, headers=headers or {}, json=json)
        if json.get("stream"):
            return httpx.Response(
                status_code=200,
                content=(
                    b'data: {"choices":[],"usage":{"prompt_tokens":5,'
                    b'"prompt_tokens_details":{"cached_tokens":2}}}\n\n'
                    b"data: [DONE]\n\n"
                ),
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        return httpx.Response(
            status_code=200,
            json={
                "id": json["rid"],
                "usage": {
                    "prompt_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 2},
                },
            },
            request=request,
        )


def test_collect_logprobs_removes_null_field_when_client_did_not_request_it():
    router = UnifiedPDRouter.__new__(UnifiedPDRouter)
    router.logger = MagicMock()
    sampling_state = {
        "enabled": True,
        "client_logprobs": False,
        "lp_count": 1,
        "info": {},
    }
    chunk = b'data: {"choices":[{"logprobs":null,"token_ids":[1]}]}\n\n'

    out = router._collect_logprobs_from_stream_chunk(chunk, sampling_state)

    assert out is not chunk
    assert b'"logprobs"' not in out


def test_collect_logprobs_skips_parse_for_plain_content_chunk():
    router = UnifiedPDRouter.__new__(UnifiedPDRouter)
    router.logger = MagicMock()
    sampling_state = {
        "enabled": True,
        "client_logprobs": False,
        "lp_count": 1,
        "info": {},
    }
    chunk = b'data: {"choices":[{"delta":{"content":"hello"},"index":0}]}\n\n'

    out = router._collect_logprobs_from_stream_chunk(chunk, sampling_state)

    assert out is chunk


def test_collect_logprobs_keeps_original_bytes_when_only_token_ids_present():
    router = UnifiedPDRouter.__new__(UnifiedPDRouter)
    router.logger = MagicMock()
    sampling_state = {
        "enabled": True,
        "client_logprobs": False,
        "lp_count": 1,
        "info": {},
    }
    chunk = b'data: {"choices":[{"token_ids":[1,2,3]}]}\n\n'

    out = router._collect_logprobs_from_stream_chunk(chunk, sampling_state)

    assert out is chunk
    assert sampling_state["info"]["cached_output_token_ids"] == [1, 2, 3]


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
            AttemptStopReason.PEER_FAILED,
        ),
        (cancel_error.CLIENT_DISCONNECT, AttemptStopReason.CLIENT_DISCONNECT),
        (cancel_error.DISPATCH_ABORT, AttemptStopReason.OTHER),
        (cancel_error.SCOPE_ABORT, AttemptStopReason.OTHER),
    ],
)
def test_unified_pd_cancel_stop_reason_mapping(reason, expected):
    assert UnifiedPDRouter._cancel_stop_reason(reason) == expected


@pytest.mark.asyncio
async def test_unified_pd_process_response_error_wraps_cancelled_as_request_cancelled_error(
    monkeypatch,
    caplog,
):
    caplog.set_level(
        logging.WARNING,
        logger=_resolve_logger_name("motor.coordinator.router.strategies.unified_pd"),
    )

    req_info = RequestInfo(
        req_id="root-cancel-wrap",
        req_data={"model": "m", "prompt": "hi"},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=3,
    )
    config = _config()
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=_Scheduler(),
        request_manager=RequestManager(config),
    )
    monkeypatch.setattr(router, "_stop_attempt", AsyncMock(return_value=None))

    attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))
    error, retry = await router._process_response_error(
        attempt,
        0,
        asyncio.CancelledError(cancel_error.CLIENT_DISCONNECT),
    )

    assert isinstance(error, RequestCancelledError)
    assert error.reason == cancel_error.CLIENT_DISCONNECT
    assert retry is False
    assert "Unified PD cancelled" in caplog.text
    assert cancel_error.CLIENT_DISCONNECT in caplog.text


@pytest.mark.asyncio
async def test_unified_pd_vllm_uses_native_handoff_before_selecting_decode(monkeypatch):
    req_info = RequestInfo(
        req_id="root-cpcd",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )
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

    class _RecordingPrefillClient(_NativeHandoffPrefillClient):
        async def post(self, path, json=None, headers=None, timeout=None):
            response = await super().post(path, json=json, headers=headers, timeout=timeout)
            events.append(("native_prefill_result",))
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
    p_request = p_client.requests[0]
    d_request = d_client.requests[0]
    assert "_motor_dispatch" not in p_request
    assert "_motor_dispatch" not in d_request
    assert "_motor_prefill_result" not in d_request
    assert p_request["request_id"] == d_request["request_id"] == "root-cpcd#a1"
    assert p_request["stream"] is False
    assert p_request["max_tokens"] == 1
    assert p_request["min_tokens"] == 1
    assert d_request["max_tokens"] == 8
    assert d_request["kv_transfer_params"] == {
        "do_remote_prefill": True,
        "remote_request_id": "root-cpcd#a1",
        "remote_host": "10.0.0.1",
        "remote_port": 9000,
        "connector_private": {"opaque": "kv"},
    }
    assert p_client.headers[0]["X-Request-Id"] == "root-cpcd#a1"
    assert d_client.headers[0]["X-Request-Id"] == "root-cpcd#a1"
    assert scheduler.update_workload.await_count == 2
    assert [event for event in events if event[0] == "select"] == [
        ("select", PDRole.ROLE_P),
        ("select", PDRole.ROLE_D),
    ]
    assert events.index(("native_prefill_result",)) < events.index(("select", PDRole.ROLE_D))
    assert ("release", PDRole.ROLE_P, WorkloadAction.RELEASE_TOKENS) in events
    assert req_info.prompt_tokens_details == {"cached_tokens": 2}
    assert ReqState.PREFILL_END in req_info.status
    assert req_info.status[ReqState.P_ALLOCATED] <= req_info.status[ReqState.PREFILL_END]
    assert req_info.status[ReqState.PREFILL_END] <= req_info.status[ReqState.DECODE_END]


@pytest.mark.asyncio
async def test_unified_pd_vllm_rejects_missing_native_ticket_without_decode_or_peer_stop(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-missing-ticket",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )
    select_and_allocate = AsyncMock(side_effect=scheduler.select_and_allocate)
    scheduler.select_and_allocate = select_and_allocate
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

    with pytest.raises(UpstreamHTTPError) as exc_info:
        await router.handle_request()

    assert exc_info.value.status_code == 502
    assert b"Missing kv_transfer_params" in exc_info.value.body
    assert select_and_allocate.await_count == 1
    assert len(p_client.requests) == 1
    assert d_client.requests == []
    assert scheduler.update_workload.await_count == 1
    release = scheduler.update_workload.await_args.args[0]
    assert release.role == PDRole.ROLE_P
    assert release.workload_action == WorkloadAction.RELEASE_TOKENS


@pytest.mark.asyncio
async def test_unified_pd_vllm_strips_native_internal_fields_from_nonstream_response(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-native-strip",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _NativeHandoffPrefillClient("prefill")

    class _DecodeClient(_Client):
        async def post(self, path, json=None, headers=None, timeout=None):
            self.requests.append(json)
            self.headers.append(headers or {})
            request = httpx.Request("POST", path, headers=headers or {}, json=json)
            return httpx.Response(
                status_code=200,
                json={
                    "choices": [{"text": "ok", "index": 0}],
                    "kv_transfer_params": {"remote_host": "10.0.0.1"},
                },
                request=request,
            )

    d_client = _DecodeClient("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    body = json.loads(response.body)

    assert body["choices"][0]["text"] == "ok"
    assert "kv_transfer_params" not in body
    assert d_client.requests[0]["kv_transfer_params"]["remote_host"] == "10.0.0.1"


@pytest.mark.asyncio
async def test_unified_pd_vllm_native_handoff_preserves_chat_output_budget_and_shape(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-native-chat",
        req_data={
            "model": "m",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
            "max_completion_tokens": 6,
        },
        api="v1/chat/completions",
        entry_api="v1/chat/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _NativeHandoffPrefillClient("prefill")

    class _CompletionDecodeClient(_Client):
        async def post(self, path, json=None, headers=None, timeout=None):
            self.requests.append(json)
            self.headers.append(headers or {})
            request = httpx.Request("POST", path, headers=headers or {}, json=json)
            return httpx.Response(
                status_code=200,
                json={
                    "object": "text_completion",
                    "choices": [{"text": "ok", "index": 0, "finish_reason": "stop"}],
                },
                request=request,
            )

    d_client = _CompletionDecodeClient("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    body = json.loads(response.body)

    assert p_client.requests[0]["max_tokens"] == 1
    assert p_client.requests[0]["max_completion_tokens"] == 1
    assert d_client.requests[0]["max_completion_tokens"] == 6
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "ok"}


@pytest.mark.asyncio
async def test_unified_pd_vllm_strips_native_internal_fields_from_stream_response(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-native-stream-strip",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _NativeHandoffPrefillClient("prefill")
    d_client = _SequenceStreamClient(
        "decode",
        [
            _StreamResponse(
                [(b'data: {"choices":[{"text":"A","index":0}],"kv_transfer_params":{"remote_host":"10.0.0.1"}}\n\n')]
            )
        ],
    )

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    chunks = [chunk async for chunk in response.body_iterator]

    assert len(chunks) == 1
    assert b'"text":"A"' in chunks[0]
    assert b"kv_transfer_params" not in chunks[0]


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
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )
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

    p_client = _NativeHandoffPrefillClient("prefill")
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

    monkeypatch.setattr(router, "_client_for", _client_for)

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

    monkeypatch.setattr(router, "_client_for", _client_for)
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
async def test_unified_pd_sglang_uses_native_bootstrap_concurrently(monkeypatch):
    req_info = RequestInfo(
        req_id="root-cpcd-sglang",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="sglang",
        decode_engine_type="sglang",
        prefill_bootstrap_port=8998,
    )
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _NativeSglangPrefillClient("prefill")
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
    p_request = p_client.requests[0]
    d_request = d_client.requests[0]
    assert "_motor_dispatch" not in p_request
    assert "_motor_dispatch" not in d_request
    assert "_motor_prefill_result" not in d_request
    assert p_request["rid"] == d_request["rid"] == "root-cpcd-sglang#a1"
    for request in (p_request, d_request):
        assert request["bootstrap_host"] == "127.0.0.1"
        assert request["bootstrap_port"] == 8998
    assert p_request["bootstrap_room"] == d_request["bootstrap_room"]
    assert p_client.headers[0]["X-Request-Id"] == "root-cpcd-sglang#a1"
    assert d_client.headers[0]["X-Request-Id"] == "root-cpcd-sglang#a1"
    assert scheduler.update_workload.await_count == 2


@pytest.mark.asyncio
async def test_unified_pd_sglang_rejects_missing_bootstrap_port_before_dispatch(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-sglang-no-port",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="sglang",
        decode_engine_type="sglang",
    )
    next(iter(next(iter(scheduler.p.endpoints.values())).values())).bootstrap_port = None
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _NativeSglangPrefillClient("prefill")
    d_client = _Client("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    with pytest.raises(EngineProtocolError, match="bootstrap port"):
        await router.handle_request()

    assert p_client.requests == []
    assert d_client.requests == []
    assert scheduler.update_workload.await_count == 2


@pytest.mark.asyncio
async def test_unified_pd_sglang_rejects_mixed_engine_pair_without_dispatch(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-sglang-mixed",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="sglang",
        decode_engine_type="vllm",
        prefill_bootstrap_port=8998,
    )
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    with pytest.raises(HTTPException) as exc_info:
        await router.handle_request()

    assert exc_info.value.status_code == 503
    assert "engine_type=sglang" in exc_info.value.detail
    assert scheduler.update_workload.await_count == 1


@pytest.mark.asyncio
async def test_unified_pd_sglang_decode_failure_cancels_prefill_without_peer_stop(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-sglang-cancel",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="sglang",
        decode_engine_type="sglang",
        prefill_bootstrap_port=8998,
    )
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    prefill_started = asyncio.Event()
    prefill_cancelled = asyncio.Event()

    class _BlockingPrefillClient(_Client):
        async def post(self, path, json=None, headers=None, timeout=None):
            if str(path).rstrip("/").endswith("abort_request"):
                return await super().post(path, json=json, headers=headers, timeout=timeout)
            self.requests.append(json)
            self.headers.append(headers or {})
            prefill_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                prefill_cancelled.set()

    class _FailingDecodeClient(_Client):
        async def post(self, path, json=None, headers=None, timeout=None):
            if str(path).rstrip("/").endswith("abort_request"):
                return await super().post(path, json=json, headers=headers, timeout=timeout)
            self.requests.append(json)
            self.headers.append(headers or {})
            await prefill_started.wait()
            raise httpx.ConnectError("decode down")

    p_client = _BlockingPrefillClient("prefill")
    d_client = _FailingDecodeClient("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    with pytest.raises(httpx.ConnectError, match="decode down"):
        await router.handle_request()

    await asyncio.wait_for(prefill_cancelled.wait(), timeout=1)
    assert p_client.requests[0]["bootstrap_room"] == d_client.requests[0]["bootstrap_room"]
    assert p_client.abort_requests == [{"rid": "root-sglang-cancel#a1"}]
    assert d_client.abort_requests == [{"rid": "root-sglang-cancel#a1"}]
    assert scheduler.update_workload.await_count == 2


@pytest.mark.asyncio
async def test_unified_pd_sglang_retry_uses_new_matched_bootstrap_room(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-sglang-retry",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="sglang",
        decode_engine_type="sglang",
        prefill_bootstrap_port=8998,
    )
    config = _config()
    config.exception_config.transport_max_retry = 2
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _NativeSglangPrefillClient("prefill")

    class _FailOnceDecodeClient(_Client):
        async def post(self, path, json=None, headers=None, timeout=None):
            if str(path).rstrip("/").endswith("abort_request"):
                return await super().post(path, json=json, headers=headers, timeout=timeout)
            self.requests.append(json)
            self.headers.append(headers or {})
            if len(self.requests) == 1:
                raise httpx.ConnectError("retry decode")
            request = httpx.Request("POST", path, headers=headers or {}, json=json)
            return httpx.Response(
                status_code=200,
                json={"choices": [{"text": "ok", "index": 0}]},
                request=request,
            )

    d_client = _FailOnceDecodeClient("decode")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()

    assert json.loads(response.body)["choices"][0]["text"] == "ok"
    assert len(p_client.requests) == len(d_client.requests) == 2
    for attempt_seq, (p_request, d_request) in enumerate(
        zip(p_client.requests, d_client.requests, strict=True),
        start=1,
    ):
        assert p_request["rid"] == d_request["rid"] == f"root-sglang-retry#a{attempt_seq}"
        assert p_request["bootstrap_room"] == d_request["bootstrap_room"]
    assert p_client.requests[0]["bootstrap_room"] != p_client.requests[1]["bootstrap_room"]


@pytest.mark.asyncio
async def test_unified_pd_sglang_chat_stream_uses_native_fields_and_strips_them(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-sglang-chat-stream",
        req_data={
            "model": "m",
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
            "max_completion_tokens": 6,
        },
        api="v1/chat/completions",
        entry_api="v1/chat/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="sglang",
        decode_engine_type="sglang",
        prefill_bootstrap_port=8998,
    )
    router = UnifiedPDRouter(
        req_info,
        _config(),
        scheduler=scheduler,
        request_manager=RequestManager(_config()),
    )
    p_client = _NativeSglangPrefillClient("prefill")
    d_client = _SequenceStreamClient(
        "decode",
        [
            _StreamResponse(
                [
                    (
                        b'data: {"choices":[{"delta":{"content":"A"},"index":0}],'
                        b'"bootstrap_host":"127.0.0.1","bootstrap_port":8998,'
                        b'"bootstrap_room":123}\n\n'
                    )
                ]
            )
        ],
    )

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    chunks = [chunk async for chunk in response.body_iterator]

    assert len(chunks) == 1
    assert b'"content":"A"' in chunks[0]
    assert b"bootstrap_" not in chunks[0]
    assert p_client.requests[0]["stream"] is False
    assert d_client.requests[0]["stream"] is True
    for request in (p_client.requests[0], d_client.requests[0]):
        assert request["max_completion_tokens"] == 6
        assert "_motor_dispatch" not in request
    assert p_client.requests[0]["bootstrap_room"] == d_client.requests[0]["bootstrap_room"]


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
        # A different endpoint on the same attempt must get a different id.
        item_decode = await router._prepare_release_work_item(
            attempt.decode_resource,
            attempt.attempt_seq,
            WorkloadAction.RELEASE_TOKENS,
            attempt=attempt,
        )
        assert item_decode is not None
        assert item_decode.params.operation_id != op_id
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
        original_active_tokens = original.active_tokens

        with caplog.at_level(logging.DEBUG, logger=_ROUTER_LOGGER):
            submitted = await router._release_attempt_resource(
                attempt.prefill_resource,
                attempt.attempt_seq,
                WorkloadAction.RELEASE_TOKENS,
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
            assert current.active_tokens == original_active_tokens
            assert not attempt.release_flags.prefill_tokens
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
        with caplog.at_level(logging.DEBUG, logger=_ROUTER_LOGGER):
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
        with caplog.at_level(logging.DEBUG, logger=_ROUTER_LOGGER):
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
async def test_unified_pd_bootstrap_stream_tail_release_survives_iterator_cancellation(
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
        if len(router._release_records) == 2:
            break
        await asyncio.sleep(0)
    assert len(router._release_records) == 2

    consumer_task.cancel()
    allow_release.set()
    with pytest.raises((asyncio.CancelledError, StopAsyncIteration)):
        await consumer_task

    await router._drain_release_tasks()

    assert scheduler.update_workload.await_count == 2
    assert not router._release_records


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
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )
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
    p_client = _NativeHandoffPrefillClient("prefill")
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
    assert d_client.requests[0]["rid"] == p_client.requests[0]["rid"]
    assert p_client.requests[0]["bootstrap_room"] == d_client.requests[0]["bootstrap_room"]
    assert scheduler.update_workload.await_count == 2
    assert ReqState.PREFILL_END in req_info.status


@pytest.mark.asyncio
async def test_unified_pd_bootstrap_stream_prefill_release_does_not_block_first_chunk(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-bootstrap-prefill-release-background",
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
    allow_p_release_compute = asyncio.Event()
    p_token_rpc_done = asyncio.Event()
    compute_and_update = router._workload_action_handler.compute_and_update

    async def _compute_and_update(resource, req_id, action, req_info_arg, **kwargs):
        if resource.instance.role == PDRole.ROLE_P and action == WorkloadAction.RELEASE_TOKENS:
            p_token_compute_started.set()
            await allow_p_release_compute.wait()
        return await compute_and_update(resource, req_id, action, req_info_arg, **kwargs)

    async def _update_workload(params):
        if params.role == PDRole.ROLE_P and params.workload_action == WorkloadAction.RELEASE_TOKENS:
            p_token_rpc_done.set()
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
    assert not p_token_rpc_done.is_set()

    allow_p_release_compute.set()
    await response.body_iterator.aclose()
    await router._drain_release_tasks()
    await asyncio.wait_for(p_token_rpc_done.wait(), timeout=1)


@pytest.mark.asyncio
async def test_unified_pd_bootstrap_nonstream_holds_prefill_tokens_until_decode_finishes(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-bootstrap-nonstream-prefill-release",
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
    decode_started = asyncio.Event()
    p_token_compute_started = asyncio.Event()
    allow_p_release_compute = asyncio.Event()
    compute_and_update = router._workload_action_handler.compute_and_update

    class _DelayedDecodeClient(_Client):
        async def post(self, path, json=None, headers=None, timeout=None):
            self.requests.append(json)
            self.headers.append(headers or {})
            decode_started.set()
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
    await asyncio.wait_for(decode_started.wait(), timeout=1)
    assert not p_token_compute_started.is_set()
    assert not response_task.done()

    allow_decode.set()
    await asyncio.wait_for(p_token_compute_started.wait(), timeout=1)
    assert not response_task.done()

    allow_p_release_compute.set()
    response = await asyncio.wait_for(response_task, timeout=1)

    assert json.loads(response.body)["choices"][0]["message"]["content"] == "ok"


@pytest.mark.asyncio
async def test_unified_pd_client_disconnect_cancels_tasks_and_releases_resources(monkeypatch):
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

    async def receive():
        await d_response.started.wait()
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(router, "_create_attempt", _create_attempt)

    response = await router.handle_request()
    await asyncio.wait_for(response(_asgi_scope(), receive, send), timeout=1)

    assert len(attempts) == 1
    attempt = attempts[0]
    await asyncio.wait_for(d_response.closed.wait(), timeout=1)
    assert attempt.state == AttemptState.STOPPED
    assert attempt.prefill_task.done()
    assert attempt.decode_task.done()
    assert scheduler.update_workload.await_count == 2
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

    await request_manager.add_req_info(req_info)
    try:
        attempt = await router._create_attempt(PDDispatchSession(req_info.req_id))

        with caplog.at_level(logging.DEBUG, logger=_ROUTER_LOGGER):
            await router._stop_attempt(attempt, AttemptStopReason.CLIENT_DISCONNECT)

            assert attempt.state == AttemptState.STOPPED
            assert scheduler.update_workload.await_count == 6
            assert "Release workload background task failed stage=release_p_tokens" in caplog.text
            assert "Release workload background task failed stage=release_d_tokens" in caplog.text
            assert not router._release_records
            assert not router._release_inflight
    finally:
        await request_manager.del_req_info(req_info.req_id)


@pytest.mark.asyncio
async def test_unified_pd_send_failure_closes_decode_and_releases_resources(monkeypatch):
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

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise OSError("client socket closed")

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(router, "_create_attempt", _create_attempt)

    response = await router.handle_request()
    with pytest.raises(ClientDisconnect):
        await asyncio.wait_for(response(_asgi_scope(), _never_disconnect, send), timeout=1)

    assert len(attempts) == 1
    attempt = attempts[0]
    await asyncio.wait_for(d_response.closed.wait(), timeout=1)
    assert attempt.state == AttemptState.STOPPED
    assert attempt.prefill_task.done()
    assert attempt.decode_task.done()
    assert scheduler.update_workload.await_count == 2


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

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    messages = await _invoke_asgi_response(response)
    body = b"".join(message["body"] for message in messages if message["type"] == "http.response.body")

    assert messages[0]["status"] == 200
    assert len(build_plan_calls) == 1
    assert len(p_client.requests) == 2
    assert len(d_client.requests) == 2
    assert d_client.requests[0]["return_token_ids"] is True
    assert p_client.requests[1]["prompt"] == [1, 2, 10]
    assert p_client.requests[1]["max_tokens"] == 8
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
    build_plan_calls = []
    original_build_retry_plan = router.rescheduler.build_retry_plan

    async def _run_stream_attempt(_attempt, _dispatch_plan):
        req_info.prompt_token_ids = [1, 2]
        req_info.cached_token_ids = [10]
        raise httpx.ReadError("after token cache")
        yield b""  # pylint: disable=unreachable

    def _build_retry_plan(req_data):
        build_plan_calls.append(req_data)
        return original_build_retry_plan(req_data)

    monkeypatch.setattr(router, "_run_stream_attempt", _run_stream_attempt)
    monkeypatch.setattr(router.rescheduler, "build_retry_plan", _build_retry_plan)

    response = await router.handle_request()
    messages = await _invoke_asgi_response(response)
    body = json.loads(messages[1]["body"])

    assert messages[0]["status"] == 502
    assert "parallel sampling" in body["detail"]
    assert len(build_plan_calls) == 1
    assert scheduler.select_and_allocate.await_count == 2


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
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )
    config = _config()
    config.exception_config.transport_max_retry = 2
    config.exception_config.reschedule_enabled = True
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _NativeHandoffPrefillClient("prefill")
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

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    messages = await _invoke_asgi_response(response)
    body = b"".join(message["body"] for message in messages if message["type"] == "http.response.body")

    assert messages[0]["status"] == 200
    assert len(p_client.requests) == 2
    assert len(d_client.requests) == 2
    assert p_client.requests[1]["prompt"] == [1, 2, 10]
    assert p_client.requests[1]["max_tokens"] == 1
    assert all("_motor_dispatch" not in request for request in p_client.requests)
    assert all("_motor_dispatch" not in request for request in d_client.requests)
    assert all("_motor_prefill_result" not in request for request in d_client.requests)
    assert p_client.requests[0]["request_id"] == d_client.requests[0]["request_id"] == "root-handoff-reschedule#a1"
    assert p_client.requests[1]["request_id"] == d_client.requests[1]["request_id"] == "root-handoff-reschedule#a2"
    assert d_client.requests[1]["prompt"] == [1, 2, 10]
    assert d_client.requests[1]["max_tokens"] == 7
    assert d_client.requests[1]["kv_transfer_params"]["remote_request_id"] == "root-handoff-reschedule#a2"
    assert b'"text":"A"' in body
    assert b'"text":"B"' in body
    assert b"ReadError" not in body


@pytest.mark.asyncio
async def test_unified_pd_pool_node_fault_reschedules_without_peer_stop(
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

    async def _forward_prefill(self, api, req_data, client, timeout):
        prefill_calls.append(req_data.copy())
        request = httpx.Request("POST", f"/{api}", json=req_data)
        return httpx.Response(
            status_code=200,
            json={"status": "cached", "id": req_data.get("request_id") or req_data.get("rid")},
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

    monkeypatch.setattr(UnifiedPDRouter, "forward_request", _forward_prefill)
    monkeypatch.setattr(UnifiedPDRouter, "forward_stream_request", _forward_decode)

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

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks[0] == b'data: {"choices":[{"delta":{"content":"A"},"index":0}]}\n\n'
    error_chunk = chunks[1].decode("utf-8") if isinstance(chunks[1], bytes) else chunks[1]
    assert "ReadError" in error_chunk
    assert len(d_client.requests) == 1
    await router._drain_release_tasks()
    assert scheduler.update_workload.await_count == 2


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

    monkeypatch.setattr(router, "_client_for", _client_for)

    response = await router.handle_request()
    chunks = [chunk async for chunk in response.body_iterator]

    assert len(d_client.requests) == 2
    assert chunks == [b'data: {"choices":[{"delta":{"content":"B"},"index":0,"finish_reason":"stop"}]}\n\n']


@pytest.mark.asyncio
async def test_unified_pd_nonstream_falls_back_to_hybrid_when_decode_pool_exhausted(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-nonstream-fallback",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )
    select_and_allocate = scheduler.select_and_allocate

    async def _select_and_allocate(role, request_info, **kwargs):
        if role == PDRole.ROLE_D:
            return None
        return await select_and_allocate(role, request_info, **kwargs)

    async def _get_unblocked_instances(role):
        if role == PDRole.ROLE_D:
            return []
        return [scheduler.p.id]

    scheduler.select_and_allocate = AsyncMock(side_effect=_select_and_allocate)
    scheduler.get_unblocked_instances = _get_unblocked_instances
    config = _config()
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _NativeHandoffPrefillClient("prefill")
    fallback_calls = []

    class _FallbackRouter:
        async def handle_request(self, *, manage_request_context):
            fallback_calls.append(manage_request_context)
            return JSONResponse({"choices": [{"text": "hybrid"}]})

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        assert resource.instance.role == PDRole.ROLE_P
        yield p_client

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(router, "_build_hybrid_fallback_router", _FallbackRouter)

    response = await router.handle_request()

    assert json.loads(response.body)["choices"][0]["text"] == "hybrid"
    assert fallback_calls == [False]
    assert len(p_client.requests) == 1
    assert scheduler.select_and_allocate.await_count == 2
    await router._drain_release_tasks()
    assert scheduler.update_workload.await_count == 1
    release = scheduler.update_workload.await_args.args[0]
    assert release.role == PDRole.ROLE_P
    assert release.workload_action == WorkloadAction.RELEASE_TOKENS


@pytest.mark.asyncio
async def test_unified_pd_runtime_fallback_respects_disabled_switch(monkeypatch):
    req_info = RequestInfo(
        req_id="root-fallback-disabled",
        req_data={"model": "m", "prompt": "hello", "stream": False, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )
    select_and_allocate = scheduler.select_and_allocate

    async def _select_and_allocate(role, request_info, **kwargs):
        if role == PDRole.ROLE_D:
            return None
        return await select_and_allocate(role, request_info, **kwargs)

    scheduler.select_and_allocate = AsyncMock(side_effect=_select_and_allocate)
    scheduler.get_unblocked_instances = AsyncMock(return_value=[scheduler.p.id])
    config = _config()
    config.scheduler_config.enable_pd_separation_fallback_to_hybrid = False
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _NativeHandoffPrefillClient("prefill")

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        assert resource.instance.role == PDRole.ROLE_P
        yield p_client

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(
        router,
        "_build_hybrid_fallback_router",
        lambda: pytest.fail("disabled runtime fallback must not build PDHybridRouter"),
    )

    with pytest.raises(HTTPException, match="No instance available for role"):
        await router.handle_request()

    scheduler.get_unblocked_instances.assert_not_awaited()


@pytest.mark.asyncio
async def test_unified_pd_stream_restarts_on_hybrid_before_commit_when_decode_pool_exhausted(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-stream-fallback-restart",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )
    select_and_allocate = scheduler.select_and_allocate

    async def _select_and_allocate(role, request_info, **kwargs):
        if role == PDRole.ROLE_D:
            return None
        return await select_and_allocate(role, request_info, **kwargs)

    async def _get_unblocked_instances(role):
        if role == PDRole.ROLE_D:
            return []
        return [scheduler.p.id]

    scheduler.select_and_allocate = AsyncMock(side_effect=_select_and_allocate)
    scheduler.get_unblocked_instances = _get_unblocked_instances
    config = _config()
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _NativeHandoffPrefillClient("prefill")
    fallback_calls = []

    class _FallbackRouter:
        async def stream_fallback_from_existing_context(self, **kwargs):
            fallback_calls.append(kwargs)
            kwargs["mark_unified_ready"]()
            yield b'data: {"choices":[{"text":"hybrid","finish_reason":"stop"}]}\n\n'

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        assert resource.instance.role == PDRole.ROLE_P
        yield p_client

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(router, "_build_hybrid_fallback_router", _FallbackRouter)

    response = await router.handle_request()
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks == [b'data: {"choices":[{"text":"hybrid","finish_reason":"stop"}]}\n\n']
    assert len(fallback_calls) == 1
    assert fallback_calls[0]["attempt_id"] == 2
    assert fallback_calls[0]["req_data"]["prompt"] == "hello"
    assert fallback_calls[0]["mark_unified_ready"] is not None


@pytest.mark.asyncio
async def test_unified_pd_stream_resumes_on_hybrid_with_token_replay_after_commit(
    monkeypatch,
):
    req_info = RequestInfo(
        req_id="root-stream-fallback-resume",
        req_data={"model": "m", "prompt": "hello", "stream": True, "max_tokens": 8},
        api="v1/completions",
        entry_api="v1/completions",
        req_len=10,
    )
    scheduler = _Scheduler(
        prefill_engine_type="vllm",
        decode_engine_type="vllm",
    )

    async def _get_unblocked_instances(role):
        if role == PDRole.ROLE_D:
            return []
        return [scheduler.p.id]

    scheduler.get_unblocked_instances = _get_unblocked_instances
    config = _config()
    config.exception_config.transport_max_retry = 2
    config.exception_config.reschedule_enabled = True
    router = UnifiedPDRouter(
        req_info,
        config,
        scheduler=scheduler,
        request_manager=RequestManager(config),
    )
    p_client = _NativeHandoffPrefillClient("prefill")
    d_client = _SequenceStreamClient(
        "decode",
        [
            _StreamResponse(
                [
                    b'data: {"choices":[{"text":"A","index":0,"prompt_token_ids":[1,2],"token_ids":[10]}]}\n\n',
                ],
                exc_after_chunks=httpx.ReadError("decode disappeared"),
            )
        ],
    )
    fallback_calls = []

    class _FallbackRouter:
        async def stream_fallback_from_existing_context(self, **kwargs):
            fallback_calls.append(kwargs)
            yield b'data: {"choices":[{"text":"B","token_ids":[11],"finish_reason":"stop"}]}\n\n'

    @asynccontextmanager
    async def _client_for(resource: ScheduledResource):
        if resource.instance.role == PDRole.ROLE_P:
            yield p_client
        else:
            yield d_client

    monkeypatch.setattr(router, "_client_for", _client_for)
    monkeypatch.setattr(router, "_build_hybrid_fallback_router", _FallbackRouter)

    response = await router.handle_request()
    chunks = [chunk async for chunk in response.body_iterator]
    body = b"".join(chunk if isinstance(chunk, bytes) else chunk.encode() for chunk in chunks)

    assert b'"text":"A"' in body
    assert b'"text":"B"' in body
    assert len(fallback_calls) == 1
    assert fallback_calls[0]["is_resume"] is True
    assert fallback_calls[0]["api"] == "v1/completions"
    assert fallback_calls[0]["req_data"]["prompt"] == [1, 2, 10]
    assert fallback_calls[0]["req_data"]["max_tokens"] == 7


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
