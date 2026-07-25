# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Equivalence tests for SimpleRateLimitMiddleware body-size check and rate limiting.

Covers request body size validation, skip-path bypass, middleware disable bypass,
body-size-priority-over-rate-limit, 413 response structure, and runtime threshold
update via RateLimitConfigHolder.
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch

from motor.coordinator.middleware.fastapi_middleware import (
    SimpleRateLimitMiddleware,
    RateLimitConfigHolder,
)
from motor.coordinator.middleware.rate_limiter import SimpleRateLimiter


KB = 1024
MB = 1024 * 1024
KB_AS_MB = 1 / 1024
DEFAULT_MAX_BODY_SIZE = 10  # 10 MB


@pytest.fixture(autouse=True)
def _patch_report_alarms():
    """Patch ControllerApiClient.report_alarms to avoid network calls during tests."""
    with patch(
        "motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms",
        return_value=True,
    ):
        yield


def _build_app_and_middleware(
    max_request_body_size: float = DEFAULT_MAX_BODY_SIZE,
    max_requests: int = 1000,
    window_size: int = 60,
    skip_paths: list | None = None,
    enabled: bool = True,
) -> tuple[FastAPI, SimpleRateLimitMiddleware, RateLimitConfigHolder]:
    """Create a FastAPI app with SimpleRateLimitMiddleware for testing.

    Returns the app, middleware instance, and config holder for assertions.
    """
    app = FastAPI()

    @app.post("/test")
    async def test_endpoint():
        """Simple test endpoint returning 200."""
        return {"status": "ok"}

    @app.get("/liveness")
    async def liveness_endpoint():
        """Health-check endpoint used for skip-path tests."""
        return {"status": "healthy"}

    @app.post("/liveness")
    async def liveness_post_endpoint():
        """POST variant for skip-path body-size tests."""
        return {"status": "healthy"}

    holder = RateLimitConfigHolder(
        skip_paths=skip_paths if skip_paths is not None else [],
        max_request_body_size=max_request_body_size,
        enabled=enabled,
    )
    rate_limiter = SimpleRateLimiter(max_requests=max_requests, window_size=window_size)
    holder.rate_limiter = rate_limiter

    middleware = SimpleRateLimitMiddleware(
        app=app,
        rate_limiter=rate_limiter,
        config_holder=holder,
    )

    return app, middleware, holder


async def _echo_app(scope, receive, send):
    """Dummy ASGI app: read the full request body and echo it back as a 200 response.

    Used to validate that the middleware's replay receive correctly hands the
    buffered body to the downstream app.
    """
    body = b""
    while True:
        message = await receive()
        if message.get("type") == "http.request":
            body += message.get("body", b"") or b""
            if not message.get("more_body", False):
                break
        elif message.get("type") == "http.disconnect":
            break
        else:
            break
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/octet-stream")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _recording_app(record):
    """Return a dummy ASGI app that records whether it was ever invoked."""

    async def app(scope, receive, send):
        record["called"] = True
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/octet-stream")],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    return app


def _make_scope(method="POST", path="/test", headers=None):
    """Build a minimal but complete ASGI http scope for raw driving.

    `headers` is a list of (name_bytes, value_bytes) tuples. Omit content-length
    to simulate chunked / no-Content-Length requests.
    """
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "headers": list(headers) if headers else [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


def _make_body_receive(chunks):
    """Build a receive callable yielding `chunks` as http.request messages, then http.disconnect.

    Each chunk is sent as a separate http.request message; all but the last have
    more_body=True, the last has more_body=False. After exhausting chunks,
    http.disconnect is returned indefinitely.
    """
    idx = [0]

    async def receive():
        if idx[0] < len(chunks):
            i = idx[0]
            idx[0] += 1
            more = i < len(chunks) - 1
            return {"type": "http.request", "body": chunks[i], "more_body": more}
        return {"type": "http.disconnect"}

    return receive


def _drive(middleware, scope, receive):
    """Run the middleware as an ASGI app synchronously and return the sent messages."""
    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(scope, receive, send))
    return sent


class TestRequestBodySizeCheck:
    """Tests for request body size validation in SimpleRateLimitMiddleware."""

    def test_body_within_limit_returns_200(self):
        """Content-Length = 1KB, max = 10KB -> 200, body_size_rejected_requests = 0."""
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=10 * KB_AS_MB)
        client = TestClient(middleware)

        response = client.post("/test", content=b"x" * KB)

        assert response.status_code == 200
        assert middleware.stats["body_size_rejected_requests"] == 0

    def test_body_exceeds_limit_returns_413(self):
        """Content-Length = 2KB, max = 1KB -> 413, body_size_rejected_requests = 1."""
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=1 * KB_AS_MB)
        client = TestClient(middleware)

        response = client.post("/test", content=b"x" * (2 * KB))

        assert response.status_code == 413
        assert middleware.stats["body_size_rejected_requests"] == 1

    def test_body_equal_limit_returns_200(self):
        """Content-Length = 1KB, max = 1KB -> 200 (only strictly greater is rejected)."""
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=1 * KB_AS_MB)
        client = TestClient(middleware)

        response = client.post("/test", content=b"x" * KB)

        assert response.status_code == 200
        assert middleware.stats["body_size_rejected_requests"] == 0

    def test_no_content_length_header_returns_200(self):
        """GET with no body and no Content-Length -> 200 (pre-reads empty body, within limit).

        Note: this covers the GET-no-body case, NOT a POST with a body but absent
        Content-Length (the latter is enforced by actual-byte pre-read, covered in
        TestChunkedAndActualByteEnforcement).
        """
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=1 * KB_AS_MB)
        client = TestClient(middleware)

        # GET requests typically have no body and no Content-Length header
        response = client.get("/liveness")

        assert response.status_code == 200
        assert middleware.stats["body_size_rejected_requests"] == 0

    def test_invalid_content_length_value_returns_200(self):
        """Content-Length = "not-a-number" -> 200 (falls back to actual-byte pre-read).

        _get_content_length returns -1 for an unparseable header, so the fast path
        is skipped; the body is then pre-read. For a GET with no body the pre-read
        yields an empty body, which is within the limit, so the request is allowed.
        """
        # Test the static method directly since TestClient auto-sets Content-Length
        scope = {
            "type": "http",
            "headers": [(b"content-length", b"not-a-number")],
        }
        result = SimpleRateLimitMiddleware._get_content_length(scope)
        assert result == -1

        # Verify that -1 does not trigger the body size rejection
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=1 * KB_AS_MB)
        client = TestClient(middleware)

        # Use GET (no body) to simulate a request where Content-Length is absent
        response = client.get("/liveness")
        assert response.status_code == 200

    def test_disabled_body_size_check_returns_200(self):
        """max_request_body_size = 0 -> 200 (size check completely skipped)."""
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=0)
        client = TestClient(middleware)

        response = client.post("/test", content=b"x" * (10 * KB))

        assert response.status_code == 200
        assert middleware.stats["body_size_rejected_requests"] == 0


class TestBodySizePriorityAndBypass:
    """Tests for body-size priority over rate limiting and bypass scenarios."""

    def test_body_size_priority_over_rate_limit(self):
        """Token exhausted + body exceeds limit -> 413 (not 429), token not consumed."""
        # max_requests=1: first request consumes the only token
        app, middleware, _ = _build_app_and_middleware(
            max_request_body_size=1 * KB_AS_MB,
            max_requests=1,
            window_size=60,
        )
        client = TestClient(middleware)

        # First request: normal (within body limit, consumes the only token)
        resp1 = client.post("/test", content=b"x" * 100)
        assert resp1.status_code == 200

        # Second request: body exceeds limit AND tokens exhausted
        resp2 = client.post("/test", content=b"x" * (2 * KB))
        assert resp2.status_code == 413, "Body size check should take priority over rate limit"
        assert middleware.stats["body_size_rejected_requests"] == 1
        assert middleware.stats["blocked_requests"] == 0, "Rate limiter should not be invoked"

    def test_skip_path_bypasses_body_size_check(self):
        """Skip path + oversized body -> 200 (all checks skipped)."""
        app, middleware, _ = _build_app_and_middleware(
            max_request_body_size=1 * KB_AS_MB,
            skip_paths=["/liveness"],
        )
        client = TestClient(middleware)

        response = client.post("/liveness", content=b"x" * (10 * KB))

        assert response.status_code == 200
        assert middleware.stats["body_size_rejected_requests"] == 0

    def test_middleware_disabled_bypasses_body_size_check(self):
        """enabled = False + oversized body -> 200 (all checks skipped)."""
        app, middleware, _ = _build_app_and_middleware(
            max_request_body_size=1 * KB_AS_MB,
            enabled=False,
        )
        client = TestClient(middleware)

        response = client.post("/test", content=b"x" * (10 * KB))

        assert response.status_code == 200
        assert middleware.stats["body_size_rejected_requests"] == 0


class TestResponse413Structure:
    """Tests for 413 response body structure."""

    def test_413_response_body_structure(self):
        """Verify 413 JSON response contains 'error' and 'message' fields."""
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=1 * KB_AS_MB)
        client = TestClient(middleware)

        response = client.post("/test", content=b"x" * (2 * KB))

        assert response.status_code == 413
        body = response.json()
        assert "error" in body, "Response must contain 'error' field"
        assert body["error"] == "request_body_too_large"
        assert "message" in body, "Response must contain 'message' field"
        assert "bytes" in body["message"], "Message should mention the body size"


class TestRuntimeThresholdUpdate:
    """Tests for runtime threshold update via RateLimitConfigHolder."""

    def test_runtime_update_threshold_takes_effect(self):
        """Modify max_request_body_size via config_holder -> new threshold takes effect."""
        app, middleware, holder = _build_app_and_middleware(max_request_body_size=10 * KB_AS_MB)
        client = TestClient(middleware)

        # Initially: 2KB is within the 10KB limit
        resp1 = client.post("/test", content=b"x" * (2 * KB))
        assert resp1.status_code == 200

        # Update threshold to 1KB at runtime
        holder.max_request_body_size = 1 * KB_AS_MB

        # Now: 2KB exceeds the new 1KB limit
        resp2 = client.post("/test", content=b"x" * (2 * KB))
        assert resp2.status_code == 413
        assert middleware.stats["body_size_rejected_requests"] == 1

    def test_runtime_disable_body_check_takes_effect(self):
        """Set max_request_body_size = 0 at runtime -> body check disabled."""
        app, middleware, holder = _build_app_and_middleware(max_request_body_size=1 * KB_AS_MB)
        client = TestClient(middleware)

        # Initially: 2KB exceeds 1KB limit
        resp1 = client.post("/test", content=b"x" * (2 * KB))
        assert resp1.status_code == 413

        # Disable body size check at runtime
        holder.max_request_body_size = 0

        # Now: same request passes
        resp2 = client.post("/test", content=b"x" * (2 * KB))
        assert resp2.status_code == 200

    def test_runtime_enable_middleware_takes_effect(self):
        """Toggle enabled flag at runtime -> middleware respects new state."""
        app, middleware, holder = _build_app_and_middleware(
            max_request_body_size=1 * KB_AS_MB,
            enabled=False,
        )
        client = TestClient(middleware)

        # Initially disabled: oversized body passes
        resp1 = client.post("/test", content=b"x" * (2 * KB))
        assert resp1.status_code == 200

        # Enable middleware at runtime
        holder.enabled = True

        # Now: oversized body is rejected
        resp2 = client.post("/test", content=b"x" * (2 * KB))
        assert resp2.status_code == 413


class TestShouldSkipPath:
    """Tests for _should_skip_path defensive logic."""

    def test_should_skip_path_with_none_skip_paths(self):
        """skip_paths = None -> _should_skip_path returns False (no crash)."""
        app = FastAPI()
        holder = RateLimitConfigHolder(skip_paths=None)
        middleware = SimpleRateLimitMiddleware(app=app, config_holder=holder)

        assert middleware._should_skip_path("/test") is False

    def test_should_skip_path_with_empty_list(self):
        """skip_paths = [] -> _should_skip_path returns False."""
        app = FastAPI()
        holder = RateLimitConfigHolder(skip_paths=[])
        middleware = SimpleRateLimitMiddleware(app=app, config_holder=holder)

        assert middleware._should_skip_path("/test") is False

    def test_should_skip_path_matching_prefix(self):
        """skip_paths contains matching prefix -> returns True."""
        app = FastAPI()
        holder = RateLimitConfigHolder(skip_paths=["/liveness", "/metrics"])
        middleware = SimpleRateLimitMiddleware(app=app, config_holder=holder)

        assert middleware._should_skip_path("/liveness") is True
        assert middleware._should_skip_path("/liveness/sub") is True
        assert middleware._should_skip_path("/metrics") is True
        assert middleware._should_skip_path("/test") is False


class TestChunkedAndActualByteEnforcement:
    """Tests for actual-byte body enforcement against chunked / no-Content-Length
    / under-reported Content-Length requests.

    These drive the middleware directly as an ASGI app (via _drive) to precisely
    control the scope headers and the receive message stream, independent of
    httpx/TestClient normalization.
    """

    def test_no_content_length_post_exceeds_limit_returns_413(self):
        """No Content-Length + 2KB body (max 1KB) -> 413, app not called, no token consumed."""
        record = {"called": False}
        rate_limiter = SimpleRateLimiter(max_requests=10, window_size=60)
        middleware = SimpleRateLimitMiddleware(
            app=_recording_app(record),
            rate_limiter=rate_limiter,
            max_request_body_size=1 * KB_AS_MB,
        )

        scope = _make_scope(headers=[])  # no content-length header
        receive = _make_body_receive([b"x" * KB, b"x" * KB])  # 2KB total
        sent = _drive(middleware, scope, receive)

        assert sent[0]["status"] == 413
        assert record["called"] is False, "Downstream app must not be called on oversized body"
        assert middleware.stats["body_size_rejected_requests"] == 1
        assert middleware.stats["blocked_requests"] == 0

    def test_content_length_under_reported_body_exceeds_returns_413(self):
        """Content-Length=100 but actual body 2KB (max 1KB) -> 413 (defends against lying CL)."""
        record = {"called": False}
        rate_limiter = SimpleRateLimiter(max_requests=10, window_size=60)
        middleware = SimpleRateLimitMiddleware(
            app=_recording_app(record),
            rate_limiter=rate_limiter,
            max_request_body_size=1 * KB_AS_MB,
        )

        scope = _make_scope(headers=[(b"content-length", b"100")])
        receive = _make_body_receive([b"x" * KB, b"x" * KB])  # actual 2KB, claims 100
        sent = _drive(middleware, scope, receive)

        assert sent[0]["status"] == 413
        assert record["called"] is False, "Downstream app must not be called on oversized body"
        assert middleware.stats["body_size_rejected_requests"] == 1
        assert middleware.stats["blocked_requests"] == 0

    def test_pre_read_replays_body_within_limit(self):
        """No Content-Length + 500B body (max 1KB) -> 200, app receives the full body via replay."""
        rate_limiter = SimpleRateLimiter(max_requests=10, window_size=60)
        middleware = SimpleRateLimitMiddleware(
            app=_echo_app,
            rate_limiter=rate_limiter,
            max_request_body_size=1 * KB_AS_MB,
        )

        body = b"x" * 500
        scope = _make_scope(headers=[])  # no content-length header
        receive = _make_body_receive([body])
        sent = _drive(middleware, scope, receive)

        assert sent[0]["status"] == 200
        body_messages = [m for m in sent if m.get("type") == "http.response.body"]
        assert body_messages, "Expected an http.response.body message"
        assert body_messages[-1].get("body") == body, "Downstream app must receive the replayed body"
        assert middleware.stats["body_size_rejected_requests"] == 0

    def test_chunked_multi_chunk_within_limit_returns_200(self):
        """No Content-Length + multi-chunk 700B body (max 1KB) -> 200, body replayed correctly."""
        rate_limiter = SimpleRateLimiter(max_requests=10, window_size=60)
        middleware = SimpleRateLimitMiddleware(
            app=_echo_app,
            rate_limiter=rate_limiter,
            max_request_body_size=1 * KB_AS_MB,
        )

        chunk_a, chunk_b, chunk_c = b"x" * 300, b"y" * 200, b"z" * 200
        scope = _make_scope(headers=[])  # no content-length header
        receive = _make_body_receive([chunk_a, chunk_b, chunk_c])
        sent = _drive(middleware, scope, receive)

        assert sent[0]["status"] == 200
        body_messages = [m for m in sent if m.get("type") == "http.response.body"]
        assert body_messages, "Expected an http.response.body message"
        assert body_messages[-1].get("body") == chunk_a + chunk_b + chunk_c
        assert middleware.stats["body_size_rejected_requests"] == 0

    def test_chunked_priority_over_rate_limit(self):
        """Token exhausted + 2KB chunked body (max 1KB) -> 413, token not consumed.

        Extends the body-size-priority-over-rate-limit contract to the chunked /
        no-Content-Length path: the over-limit body is rejected before the rate
        limiter is invoked, so blocked_requests stays 0.
        """
        record = {"called": False}
        rate_limiter = SimpleRateLimiter(max_requests=1, window_size=60)
        middleware = SimpleRateLimitMiddleware(
            app=_recording_app(record),
            rate_limiter=rate_limiter,
            max_request_body_size=1 * KB_AS_MB,
        )

        # First request: 100B with Content-Length, consumes the only token.
        scope1 = _make_scope(headers=[(b"content-length", b"100")])
        receive1 = _make_body_receive([b"x" * 100])
        sent1 = _drive(middleware, scope1, receive1)
        assert sent1[0]["status"] == 200
        assert record["called"] is True

        # Second request: 2KB chunked (no Content-Length), tokens exhausted.
        record["called"] = False
        scope2 = _make_scope(headers=[])  # no content-length header
        receive2 = _make_body_receive([b"x" * KB, b"x" * KB])
        sent2 = _drive(middleware, scope2, receive2)

        assert sent2[0]["status"] == 413, "Chunked oversized body should take priority over rate limit"
        assert record["called"] is False
        assert middleware.stats["body_size_rejected_requests"] == 1
        assert middleware.stats["blocked_requests"] == 0, "Rate limiter must not be invoked"
