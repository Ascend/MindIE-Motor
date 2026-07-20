#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""End-to-end test: verify that rate limit middleware hot reload takes effect via RateLimitConfigHolder.

This test covers the real path of build_simple_rate_limit + _apply_config_changes,
ensuring that the middleware immediately reads the latest config after holder attributes are modified.
"""
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from motor.coordinator.middleware.fastapi_middleware import (
    SimpleRateLimitMiddleware,
    RateLimitConfigHolder,
)
from motor.coordinator.middleware.rate_limiter import SimpleRateLimiter


def _build_app_with_rate_limit(max_request_body_size: int):
    """Build a FastAPI app with rate limit middleware, returning (app, config_holder)."""
    app = FastAPI()

    @app.post("/v1/test")
    async def test_endpoint():
        """Test endpoint, returns 200"""
        return {"status": "ok"}

    holder = RateLimitConfigHolder(
        skip_paths=["/liveness"],
        error_message="too many requests",
        error_status_code=429,
        max_request_body_size=max_request_body_size,
    )
    app.add_middleware(
        SimpleRateLimitMiddleware,
        rate_limiter=SimpleRateLimiter(max_requests=100, window_size=60),
        config_holder=holder,
    )
    return app, holder


@pytest.mark.asyncio
async def test_hot_reload_max_request_body_size_takes_effect():
    """After hot-reloading max_request_body_size, previously rejected 413 requests should be allowed."""
    app, holder = _build_app_with_rate_limit(max_request_body_size=1024)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Small request body: allowed
        resp = await client.post("/v1/test", json={"data": "small"})
        assert resp.status_code == 200

        # Large request body (>1024): rejected with 413
        large_body = {"data": "x" * 5000}
        resp = await client.post("/v1/test", json=large_body)
        assert resp.status_code == 413

        # Hot reload: directly modify holder attribute
        holder.max_request_body_size = 10240

        # Same large request body: should be allowed now
        resp = await client.post("/v1/test", json=large_body)
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_hot_reload_skip_paths_takes_effect():
    """After hot-reloading skip_paths, requests to newly skipped paths should bypass rate limiting."""
    app, holder = _build_app_with_rate_limit(max_request_body_size=1024)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # /v1/blocked is not in skip_paths initially, large body should be rejected with 413
        large_body = {"data": "x" * 5000}
        resp = await client.post("/v1/blocked", json=large_body)
        assert resp.status_code == 413

        # Hot reload: add /v1/blocked to skip_paths
        holder.skip_paths = ["/liveness", "/v1/blocked"]

        # Same request should now bypass rate limit check (route not found returns 404, not 413)
        resp = await client.post("/v1/blocked", json=large_body)
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_hot_reload_enabled_takes_effect():
    """After hot-reloading enabled to False, rate limiting should be bypassed."""
    app, holder = _build_app_with_rate_limit(max_request_body_size=1024)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Large request body: rejected with 413
        large_body = {"data": "x" * 5000}
        resp = await client.post("/v1/test", json=large_body)
        assert resp.status_code == 413

        # Hot reload: disable rate limiting
        holder.enabled = False

        # Same request: allowed
        resp = await client.post("/v1/test", json=large_body)
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_hot_reload_max_requests_takes_effect():
    """After hot-reloading max_requests, the rate limit threshold should increase."""
    limiter = SimpleRateLimiter(max_requests=3, window_size=60)
    holder = RateLimitConfigHolder(
        skip_paths=["/liveness"],
        error_message="too many requests",
        error_status_code=429,
        max_request_body_size=10240,
    )
    holder.rate_limiter = limiter

    app = FastAPI()

    @app.get("/v1/test")
    async def test_endpoint():
        return {"status": "ok"}

    app.add_middleware(
        SimpleRateLimitMiddleware,
        rate_limiter=limiter,
        config_holder=holder,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Send 3 requests, all allowed (initial max_requests=3)
        for _ in range(3):
            resp = await client.get("/v1/test")
            assert resp.status_code == 200

        # The 4th request should be rate limited
        resp = await client.get("/v1/test")
        assert resp.status_code == 429

        # Hot reload: increase max_requests to 5
        limiter.update_config(max_requests=5)

        # Send 2 more requests, should be allowed (2 additional token capacity)
        resp = await client.get("/v1/test")
        assert resp.status_code == 200
        resp = await client.get("/v1/test")
        assert resp.status_code == 200

        # The 7th request should be rate limited again
        resp = await client.get("/v1/test")
        assert resp.status_code == 429


@pytest.mark.asyncio
async def test_hot_reload_max_requests_via_config_holder():
    """Config updates via the rate_limiter reference in RateLimitConfigHolder should take effect."""
    limiter = SimpleRateLimiter(max_requests=3, window_size=60)
    holder = RateLimitConfigHolder(
        skip_paths=["/liveness"],
        error_message="too many requests",
        error_status_code=429,
        max_request_body_size=10240,
        rate_limiter=limiter,
    )

    app = FastAPI()

    @app.get("/v1/test")
    async def test_endpoint():
        return {"status": "ok"}

    app.add_middleware(
        SimpleRateLimitMiddleware,
        rate_limiter=limiter,
        config_holder=holder,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Send 3 requests, all allowed (initial max_requests=3)
        for _ in range(3):
            resp = await client.get("/v1/test")
            assert resp.status_code == 200

        # The 4th request should be rate limited
        resp = await client.get("/v1/test")
        assert resp.status_code == 429

        # Update config via holder.rate_limiter (simulating _apply_config_changes)
        holder.rate_limiter.update_config(max_requests=5)

        # Send 2 more requests, should be allowed now
        resp = await client.get("/v1/test")
        assert resp.status_code == 200
        resp = await client.get("/v1/test")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_hot_reload_window_size_takes_effect():
    """After hot-reloading window_size, refill_rate should be updated correctly."""
    limiter = SimpleRateLimiter(max_requests=10, window_size=60)
    holder = RateLimitConfigHolder(
        skip_paths=["/liveness"],
        error_message="too many requests",
        error_status_code=429,
        max_request_body_size=10240,
        rate_limiter=limiter,
    )

    app = FastAPI()

    @app.get("/v1/test")
    async def test_endpoint():
        return {"status": "ok"}

    app.add_middleware(
        SimpleRateLimitMiddleware,
        rate_limiter=limiter,
        config_holder=holder,
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Initial config: window_size=60, refill_rate=10/60
        assert holder.rate_limiter.window_size == 60
        assert holder.rate_limiter._bucket.refill_rate == 10 / 60

        # Send requests to verify rate limiting works normally
        for _ in range(5):
            resp = await client.get("/v1/test")
            assert resp.status_code == 200

        # Hot reload: shrink window_size to 10 (refill_rate increases from 10/60 to 1/s)
        holder.rate_limiter.update_config(window_size=10)

        # Verify config is updated
        assert holder.rate_limiter.window_size == 10
        assert holder.rate_limiter._bucket.refill_rate == 1.0

        # Continue sending requests to verify it still works correctly
        resp = await client.get("/v1/test")
        assert resp.status_code == 200