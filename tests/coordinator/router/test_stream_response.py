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
import contextvars
import json

import httpx
import pytest
from fastapi import HTTPException
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect

import motor.common.utils.error as cancel_error
from motor.config.coordinator import CoordinatorConfig as _CoordinatorConfig
from motor.coordinator.domain.request_manager import RequestManager as _RequestManager
from motor.coordinator.router.stream_response import (
    CommitAwareStreamingResponse,
    StreamCommitController,
)
from motor.coordinator.router.upstream_error import UpstreamHTTPError

_IMPORT_ORDER_GUARD = (_CoordinatorConfig, _RequestManager)


def _scope() -> dict:
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


async def _never_disconnect() -> dict:
    await asyncio.Event().wait()
    return {"type": "http.disconnect"}


@pytest.mark.asyncio
async def test_response_commits_after_acceptance_without_waiting_for_first_token():
    messages = []
    source_started = asyncio.Event()
    release_first_token = asyncio.Event()
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        source_started.set()
        await release_first_token.wait()
        yield b"first"

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    response_task = asyncio.create_task(response(_scope(), _never_disconnect, send))
    await source_started.wait()

    controller.mark_ready("engine", 1)
    while not messages:
        await asyncio.sleep(0)

    assert messages == [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": response.raw_headers,
        }
    ]

    release_first_token.set()
    await response_task
    assert messages[1]["body"] == b"first"
    assert messages[-1]["more_body"] is False


@pytest.mark.asyncio
async def test_response_preserves_precommit_upstream_status_body_and_headers():
    messages = []
    error_body = b'{"error":{"message":"maximum context length exceeded","code":400}}'
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        raise UpstreamHTTPError(
            status_code=400,
            body=error_body,
            headers={"content-type": "application/json", "retry-after": "3"},
            phase="stream",
        )
        yield b""  # pylint: disable=unreachable

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    assert messages[0]["status"] == 400
    assert messages[1]["body"] == error_body
    headers = dict(messages[0]["headers"])
    assert headers[b"content-type"] == b"application/json"
    assert headers[b"retry-after"] == b"3"
    assert controller.committed is False


@pytest.mark.asyncio
async def test_response_preserves_upstream_json_as_sse_after_commit():
    messages = []
    error_body = b'{"error":{"message":"decode failed","code":503}}'
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        raise UpstreamHTTPError(
            status_code=503,
            body=error_body,
            headers={"content-type": "application/json"},
            phase="stream",
        )

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    assert messages[0]["status"] == 200
    assert messages[1]["body"].startswith(b"data: ")
    assert json.loads(messages[2]["body"].decode().removeprefix("data: ").strip()) == json.loads(error_body)
    assert messages[-1]["more_body"] is False


@pytest.mark.asyncio
async def test_response_closes_stream_when_client_disconnects_before_commit():
    messages = []
    source_started = asyncio.Event()
    closed = asyncio.Event()
    cancellation_reasons = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        source_started.set()
        try:
            await asyncio.Event().wait()
            yield b"never"
        except asyncio.CancelledError as error:
            cancellation_reasons.extend(error.args)
            raise
        finally:
            closed.set()

    async def receive():
        await source_started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), receive, send)

    await asyncio.wait_for(closed.wait(), timeout=1)
    assert messages == []
    assert controller.committed is False
    assert cancellation_reasons == [cancel_error.CLIENT_DISCONNECT]


@pytest.mark.asyncio
async def test_stream_generator_runs_in_one_task_for_context_lifecycle():
    messages = []
    current = contextvars.ContextVar("stream-context", default="")
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        token = current.set("active")
        try:
            controller.mark_ready("engine", 1)
            yield current.get().encode()
            await asyncio.sleep(0)
            yield current.get().encode()
        finally:
            current.reset(token)

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    bodies = [message["body"] for message in messages if message["type"] == "http.response.body"]
    assert bodies[:2] == [b"active", b"active"]


@pytest.mark.asyncio
async def test_response_rejects_body_iterator_after_asgi_claims_stream():
    source_started = asyncio.Event()
    release_source = asyncio.Event()
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        source_started.set()
        await release_source.wait()
        controller.mark_ready("engine", 1)
        yield b"done"

    async def send(message):
        return None

    response = CommitAwareStreamingResponse(source(), controller)
    response_task = asyncio.create_task(response(_scope(), _never_disconnect, send))
    await source_started.wait()

    with pytest.raises(RuntimeError, match="asgi already claimed"):
        await anext(response.body_iterator)

    release_source.set()
    await response_task


@pytest.mark.asyncio
async def test_response_rejects_asgi_after_body_iterator_claims_stream():
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        yield b"direct"

    async def send(message):
        return None

    response = CommitAwareStreamingResponse(source(), controller)
    assert await anext(response.body_iterator) == b"direct"

    with pytest.raises(RuntimeError, match="body_iterator already claimed"):
        await response(_scope(), _never_disconnect, send)

    await response.body_iterator.aclose()


def test_controller_ignores_stale_attempt_readiness():
    controller = StreamCommitController.requiring({"prefill", "decode"})
    controller.begin_attempt(1)
    controller.mark_ready("decode", 1)
    controller.begin_attempt(2)
    controller.mark_ready("prefill", 1)
    controller.mark_ready("decode", 2)

    assert controller.ready_to_commit is False

    controller.mark_ready("prefill", 2)
    assert controller.ready_to_commit is True


@pytest.mark.asyncio
async def test_response_timeout_cancels_upstream_with_infer_timeout_reason():
    messages = []
    source_started = asyncio.Event()
    cancellation_reasons = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        source_started.set()
        try:
            await asyncio.Event().wait()
            yield b"never"
        except asyncio.CancelledError as error:
            cancellation_reasons.extend(error.args)
            raise

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller, timeout=0.05)
    await response(_scope(), _never_disconnect, send)

    assert messages[0]["status"] == 504
    body = json.loads(messages[1]["body"])
    assert body["error"]["code"] == 504
    assert controller.committed is False
    assert cancellation_reasons == [cancel_error.INFER_TIMEOUT]


@pytest.mark.asyncio
async def test_response_timeout_after_commit_sends_504_sse():
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        await asyncio.Event().wait()
        yield b"never"

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller, timeout=0.05)
    await response(_scope(), _never_disconnect, send)

    assert messages[0]["status"] == 200
    bodies = [m["body"] for m in messages if m["type"] == "http.response.body"]
    last_data = [b for b in bodies if b.startswith(b"data:")][-1]
    payload = json.loads(last_data.decode().removeprefix("data: ").strip())
    assert payload["error"]["code"] == 504
    assert messages[-1]["more_body"] is False


@pytest.mark.asyncio
async def test_response_precommit_request_cancelled_infer_timeout_returns_504():
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        raise cancel_error.RequestCancelledError(cancel_error.INFER_TIMEOUT)
        yield b""  # pylint: disable=unreachable

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    assert messages[0]["status"] == 504
    body = json.loads(messages[1]["body"])
    assert body["error"]["code"] == 504


@pytest.mark.asyncio
async def test_response_committed_request_cancelled_infer_timeout_returns_504_sse():
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        raise cancel_error.RequestCancelledError(cancel_error.INFER_TIMEOUT)

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    bodies = [m["body"] for m in messages if m["type"] == "http.response.body"]
    last_data = [b for b in bodies if b.startswith(b"data:")][-1]
    payload = json.loads(last_data.decode().removeprefix("data: ").strip())
    assert payload["error"]["code"] == 504


def test_controller_requires_at_least_one_readiness_part():
    with pytest.raises(ValueError, match="at least one readiness part"):
        StreamCommitController.requiring([])


def test_controller_rejects_new_attempt_after_commit_boundary():
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)
    controller.mark_ready("engine", 1)

    with pytest.raises(RuntimeError, match="commit boundary"):
        controller.begin_attempt(2)


def test_controller_rejects_unknown_part_and_ignores_readiness_after_seal():
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    with pytest.raises(ValueError, match="Unexpected stream readiness part"):
        controller.mark_ready("bogus", 1)

    controller.mark_commit_started()
    controller.mark_ready("engine", 1)
    assert controller.ready_to_commit is False
    assert controller.committed is False


@pytest.mark.asyncio
async def test_response_runs_background_task_after_stream_completes():
    messages = []
    background_ran = False
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b"chunk"

    def mark_done():
        nonlocal background_ran
        background_ran = True

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller, background=BackgroundTask(mark_done))
    await response(_scope(), _never_disconnect, send)

    assert background_ran is True


@pytest.mark.asyncio
async def test_response_returns_500_when_stream_ends_before_commit_barrier():
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        return
        yield b""  # pylint: disable=unreachable

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    assert messages[0]["status"] == 500
    body = json.loads(messages[1]["body"])
    assert body["error"]["code"] == 500
    assert body["error"]["type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_response_raises_client_disconnect_when_start_send_fails():
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b"chunk"

    async def send(message):
        raise OSError("broken pipe")

    response = CommitAwareStreamingResponse(source(), controller)
    with pytest.raises(ClientDisconnect):
        await response(_scope(), _never_disconnect, send)


@pytest.mark.asyncio
async def test_response_raises_client_disconnect_when_precommit_error_send_fails():
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        raise ValueError("boom")
        yield b""  # pylint: disable=unreachable

    async def send(message):
        raise OSError("broken pipe")

    response = CommitAwareStreamingResponse(source(), controller)
    with pytest.raises(ClientDisconnect):
        await response(_scope(), _never_disconnect, send)


@pytest.mark.asyncio
async def test_response_raises_client_disconnect_when_committed_error_send_fails():
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        raise ValueError("mid-stream boom")

    async def send(message):
        if message["type"] == "http.response.body" and b'"error"' in message.get("body", b""):
            raise OSError("broken pipe")
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    with pytest.raises(ClientDisconnect):
        await response(_scope(), _never_disconnect, send)


@pytest.mark.asyncio
async def test_response_committed_transport_timeout_maps_to_504_sse():
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        raise httpx.ReadTimeout("upstream read timed out")

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    bodies = [m["body"] for m in messages if m["type"] == "http.response.body"]
    last_data = [b for b in bodies if b.startswith(b"data:")][-1]
    payload = json.loads(last_data.decode().removeprefix("data: ").strip())
    assert payload["error"]["code"] == 504
    assert payload["error"]["type"] == "ReadTimeout"


@pytest.mark.asyncio
async def test_response_committed_http_status_error_uses_upstream_status_sse():
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)
    request = httpx.Request("POST", "http://engine/v1/completions")
    status_error = httpx.HTTPStatusError(
        "unexpected status", request=request, response=httpx.Response(418, request=request)
    )

    async def source():
        controller.mark_ready("engine", 1)
        yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        raise status_error

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    bodies = [m["body"] for m in messages if m["type"] == "http.response.body"]
    last_data = [b for b in bodies if b.startswith(b"data:")][-1]
    payload = json.loads(last_data.decode().removeprefix("data: ").strip())
    assert payload["error"]["code"] == 418


@pytest.mark.asyncio
async def test_response_tolerates_terminal_frame_send_failure_after_commit():
    """A failed terminal write must not emit an error event or retry the terminal frame."""
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b"chunk"

    async def send(message):
        # Record every attempt, including rejected frames, so retries cannot pass unnoticed.
        messages.append(message)
        is_terminal_frame = message["type"] == "http.response.body" and not message["more_body"]
        if is_terminal_frame:
            raise RuntimeError("client write failed")

    response = CommitAwareStreamingResponse(source(), controller)
    await asyncio.wait_for(response(_scope(), _never_disconnect, send), timeout=1)

    assert messages == [
        {"type": "http.response.start", "status": 200, "headers": response.raw_headers},
        {"type": "http.response.body", "body": b"chunk", "more_body": True},
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]


@pytest.mark.asyncio
async def test_response_raises_client_disconnect_when_body_send_fails_midstream():
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b"chunk"

    async def send(message):
        if message["type"] == "http.response.body":
            raise OSError("broken pipe")

    response = CommitAwareStreamingResponse(source(), controller)
    with pytest.raises(ClientDisconnect):
        await response(_scope(), _never_disconnect, send)


@pytest.mark.asyncio
async def test_response_precommit_transport_error_returns_502():
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        raise httpx.ConnectError("connection refused")
        yield b""  # pylint: disable=unreachable

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    assert messages[0]["status"] == 502
    body = json.loads(messages[1]["body"])
    assert body["error"]["code"] == 502
    assert body["error"]["type"] == "ConnectError"


@pytest.mark.asyncio
async def test_response_precommit_http_exception_preserves_status_detail_and_headers():
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        raise HTTPException(status_code=429, detail="engine busy", headers={"Retry-After": "5"})
        yield b""  # pylint: disable=unreachable

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    assert messages[0]["status"] == 429
    assert json.loads(messages[1]["body"]) == {"detail": "engine busy"}
    headers = dict(messages[0]["headers"])
    assert headers[b"retry-after"] == b"5"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_error", "expected_code"),
    [
        (httpx.ConnectError("connection refused"), 502),
        (HTTPException(status_code=429, detail="engine busy"), 429),
    ],
)
async def test_response_committed_error_maps_status_into_sse_envelope(source_error, expected_code):
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        raise source_error

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    bodies = [m["body"] for m in messages if m["type"] == "http.response.body"]
    last_data = [b for b in bodies if b.startswith(b"data:")][-1]
    payload = json.loads(last_data.decode().removeprefix("data: ").strip())
    assert payload["error"]["code"] == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize("error_body", [b"not json", b""])
async def test_response_committed_upstream_error_without_parseable_body_wraps_envelope(error_body):
    """A truncated/empty engine error body must not be forwarded verbatim mid-stream."""
    messages = []
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
        raise UpstreamHTTPError(status_code=503, body=error_body, headers={}, phase="stream")

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller)
    await response(_scope(), _never_disconnect, send)

    bodies = [m["body"] for m in messages if m["type"] == "http.response.body"]
    last_data = [b for b in bodies if b.startswith(b"data:")][-1]
    payload = json.loads(last_data.decode().removeprefix("data: ").strip())
    assert payload["error"]["code"] == 503
    assert payload["error"]["type"] == "UpstreamHTTPError"


@pytest.mark.asyncio
async def test_body_iterator_yields_error_chunk_when_source_fails():
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        yield b"chunk"
        raise ValueError("mid-stream boom")

    response = CommitAwareStreamingResponse(source(), controller)
    assert await anext(response.body_iterator) == b"chunk"

    error_chunk = await anext(response.body_iterator)
    payload = json.loads(error_chunk.removeprefix("data: ").strip())
    assert payload["error"]["code"] == 500

    with pytest.raises(StopAsyncIteration):
        await anext(response.body_iterator)


@pytest.mark.asyncio
async def test_on_first_body_sent_callback_fires_exactly_once():
    messages = []
    first_body_calls = 0
    controller = StreamCommitController.requiring({"engine"})
    controller.begin_attempt(1)

    async def source():
        controller.mark_ready("engine", 1)
        yield b"one"
        yield b"two"

    def on_first_body():
        nonlocal first_body_calls
        first_body_calls += 1

    async def send(message):
        messages.append(message)

    response = CommitAwareStreamingResponse(source(), controller, on_first_body_sent=on_first_body)
    await response(_scope(), _never_disconnect, send)

    assert first_body_calls == 1
