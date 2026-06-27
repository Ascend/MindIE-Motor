import asyncio
import contextvars
import json

import pytest

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
