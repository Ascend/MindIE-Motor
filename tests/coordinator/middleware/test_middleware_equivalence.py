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
DEFAULT_MAX_BODY_SIZE = 10 * MB


@pytest.fixture(autouse=True)
def _patch_report_alarms():
    """Patch ControllerApiClient.report_alarms to avoid network calls during tests."""
    with patch(
        "motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms",
        return_value=True,
    ):
        yield


def _build_app_and_middleware(
    max_request_body_size: int = DEFAULT_MAX_BODY_SIZE,
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


class TestRequestBodySizeCheck:
    """Tests for request body size validation in SimpleRateLimitMiddleware."""

    def test_body_within_limit_returns_200(self):
        """Content-Length = 1KB, max = 10KB -> 200, body_size_rejected_requests = 0."""
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=10 * KB)
        client = TestClient(middleware)

        response = client.post("/test", content=b"x" * KB)

        assert response.status_code == 200
        assert middleware.stats["body_size_rejected_requests"] == 0

    def test_body_exceeds_limit_returns_413(self):
        """Content-Length = 2KB, max = 1KB -> 413, body_size_rejected_requests = 1."""
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=1 * KB)
        client = TestClient(middleware)

        response = client.post("/test", content=b"x" * (2 * KB))

        assert response.status_code == 413
        assert middleware.stats["body_size_rejected_requests"] == 1

    def test_body_equal_limit_returns_200(self):
        """Content-Length = 1KB, max = 1KB -> 200 (only strictly greater is rejected)."""
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=1 * KB)
        client = TestClient(middleware)

        response = client.post("/test", content=b"x" * KB)

        assert response.status_code == 200
        assert middleware.stats["body_size_rejected_requests"] == 0

    def test_no_content_length_header_returns_200(self):
        """Request without Content-Length header -> 200 (check skipped)."""
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=1 * KB)
        client = TestClient(middleware)

        # GET requests typically have no body and no Content-Length header
        response = client.get("/liveness")

        assert response.status_code == 200
        assert middleware.stats["body_size_rejected_requests"] == 0

    def test_invalid_content_length_value_returns_200(self):
        """Content-Length = "not-a-number" -> 200 (check skipped, _get_content_length returns -1)."""
        # Test the static method directly since TestClient auto-sets Content-Length
        scope = {
            "type": "http",
            "headers": [(b"content-length", b"not-a-number")],
        }
        result = SimpleRateLimitMiddleware._get_content_length(scope)
        assert result == -1

        # Verify that -1 does not trigger the body size rejection
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=1 * KB)
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
            max_request_body_size=1 * KB,
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
            max_request_body_size=1 * KB,
            skip_paths=["/liveness"],
        )
        client = TestClient(middleware)

        response = client.post("/liveness", content=b"x" * (10 * KB))

        assert response.status_code == 200
        assert middleware.stats["body_size_rejected_requests"] == 0

    def test_middleware_disabled_bypasses_body_size_check(self):
        """enabled = False + oversized body -> 200 (all checks skipped)."""
        app, middleware, _ = _build_app_and_middleware(
            max_request_body_size=1 * KB,
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
        app, middleware, _ = _build_app_and_middleware(max_request_body_size=1 * KB)
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
        app, middleware, holder = _build_app_and_middleware(max_request_body_size=10 * KB)
        client = TestClient(middleware)

        # Initially: 2KB is within the 10KB limit
        resp1 = client.post("/test", content=b"x" * (2 * KB))
        assert resp1.status_code == 200

        # Update threshold to 1KB at runtime
        holder.max_request_body_size = 1 * KB

        # Now: 2KB exceeds the new 1KB limit
        resp2 = client.post("/test", content=b"x" * (2 * KB))
        assert resp2.status_code == 413
        assert middleware.stats["body_size_rejected_requests"] == 1

    def test_runtime_disable_body_check_takes_effect(self):
        """Set max_request_body_size = 0 at runtime -> body check disabled."""
        app, middleware, holder = _build_app_and_middleware(max_request_body_size=1 * KB)
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
            max_request_body_size=1 * KB,
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
