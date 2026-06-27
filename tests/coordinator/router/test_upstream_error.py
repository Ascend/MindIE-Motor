import json
from unittest.mock import MagicMock

import httpx
import pytest

from motor.config.coordinator import CoordinatorConfig, ExceptionConfig
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.router.strategies.base import BaseRouter
from motor.coordinator.router.upstream_error import (
    UpstreamHTTPError,
    is_retryable_upstream_error,
    render_upstream_error,
)


class _Router(BaseRouter):
    async def handle_request(self):
        raise NotImplementedError


class _TrackingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.read_count = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


def _router(*, body_limit: int = 64 * 1024) -> _Router:
    config = CoordinatorConfig()
    config.exception_config = ExceptionConfig(
        max_retry=1,
        upstream_error_body_max_bytes=body_limit,
    )
    req_info = RequestInfo(
        req_id="upstream-error-test",
        req_data={"model": "m", "prompt": "hello"},
        api="v1/completions",
        req_len=10,
    )
    return _Router(
        req_info,
        config,
        scheduler=MagicMock(),
        request_manager=RequestManager(config),
    )


@pytest.mark.asyncio
async def test_nonstream_upstream_error_preserves_status_body_and_retry_after():
    error_body = {
        "error": {
            "message": "This model's maximum context length is 4096 tokens",
            "type": "BadRequestError",
            "code": 400,
        }
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json=error_body,
            headers={"Retry-After": "7"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://engine",
    ) as client:
        with pytest.raises(UpstreamHTTPError) as exc_info:
            await _router().forward_request("v1/completions", {"prompt": "hello"}, client, 1)

    error = exc_info.value
    assert error.status_code == 400
    assert json.loads(error.body) == error_body
    assert error.headers["retry-after"] == "7"
    assert not is_retryable_upstream_error(error)

    response = render_upstream_error(error)
    assert response.status_code == 400
    assert json.loads(response.body) == error_body
    assert response.headers["retry-after"] == "7"


@pytest.mark.asyncio
async def test_stream_upstream_error_body_is_bounded():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            413,
            content=b"0123456789",
            headers={"Content-Type": "text/plain"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://engine",
    ) as client:
        stream = _router(body_limit=5).forward_stream_request(
            "v1/completions",
            {"prompt": "hello", "stream": True},
            client,
            1,
        )
        with pytest.raises(UpstreamHTTPError) as exc_info:
            await anext(stream)

    assert exc_info.value.status_code == 413
    assert exc_info.value.body == b"01234"


@pytest.mark.asyncio
async def test_nonstream_truncated_error_renders_valid_json_not_raw_body():
    # An engine error larger than the cap would be cut mid-JSON; forwarding it verbatim under the
    # engine's application/json content-type would hand the client an invalid payload.
    big_body = b'{"error": {"message": "' + b"x" * 256 + b'"}}'

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            content=big_body,
            headers={"Content-Type": "application/json"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://engine",
    ) as client:
        with pytest.raises(UpstreamHTTPError) as exc_info:
            await _router(body_limit=32).forward_request("v1/completions", {"prompt": "hello"}, client, 1)

    error = exc_info.value
    assert error.truncated is True
    assert len(error.body) == 32

    response = render_upstream_error(error)
    assert response.status_code == 500
    # Self-contained, valid JSON fallback instead of the cut engine body.
    payload = json.loads(response.body)
    assert payload["error"]["code"] == 500
    assert response.body != error.body


@pytest.mark.asyncio
async def test_nonstream_error_stops_reading_after_bounded_probe():
    stream = _TrackingStream([b"01234", b"56789", b"must-not-be-read"])

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            stream=stream,
            headers={"Content-Type": "text/plain"},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://engine",
    ) as client:
        with pytest.raises(UpstreamHTTPError) as exc_info:
            await _router(body_limit=5).forward_request("v1/completions", {"prompt": "hello"}, client, 1)

    assert exc_info.value.body == b"01234"
    assert exc_info.value.truncated is True
    assert stream.read_count == 2
    assert stream.closed is True


@pytest.mark.asyncio
async def test_nonstream_success_remains_readable_after_stream_context_closes():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"text": "ok"}]},
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://engine",
    ) as client:
        response = await _router().forward_request("v1/completions", {"prompt": "hello"}, client, 1)

    assert response.is_closed is True
    assert response.json() == {"choices": [{"text": "ok"}]}


def test_render_upstream_error_forwards_complete_body_but_falls_back_when_truncated():
    complete = UpstreamHTTPError(
        status_code=400,
        body=b'{"error": "bad request"}',
        headers={"content-type": "application/json"},
        phase="non-stream",
        truncated=False,
    )
    forwarded = render_upstream_error(complete)
    assert forwarded.status_code == 400
    assert forwarded.body == b'{"error": "bad request"}'

    truncated = UpstreamHTTPError(
        status_code=500,
        body=b'{"error": {"message": "partial',  # cut mid-JSON
        headers={"content-type": "application/json"},
        phase="non-stream",
        truncated=True,
    )
    response = render_upstream_error(truncated)
    assert response.status_code == 500
    payload = json.loads(response.body)  # must parse as valid JSON
    assert payload["error"]["code"] == 500


def test_retry_classification_only_retries_transient_failures():
    assert is_retryable_upstream_error(UpstreamHTTPError(status_code=503, body=b"", headers={}, phase="non-stream"))
    assert not is_retryable_upstream_error(UpstreamHTTPError(status_code=422, body=b"", headers={}, phase="non-stream"))
    assert is_retryable_upstream_error(httpx.ConnectError("connection refused"))
    assert not is_retryable_upstream_error(httpx.UnsupportedProtocol("bad protocol"))
