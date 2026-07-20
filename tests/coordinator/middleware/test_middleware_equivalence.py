#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Behavioural equivalence test for the two rate limiting middleware implementations.

Compares fastapi_middleware (native ASGI) against fastapi_middleware_legacy
(BaseHTTPMiddleware) across every code path: allowed, blocked, skip path,
disabled, exception safety, and non-HTTP scope passthrough.

Usage (from repo root):
    .\\.venv\\Scripts\\python.exe tests\\coordinator\\test_middleware_equivalence.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List, Tuple

# Ensure the repository root is on sys.path
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Stub out controller alarm reporting before importing the middlewares
from motor.coordinator.api_client import controller_api_client as _cac

_cac.ControllerApiClient.report_alarms = staticmethod(lambda *a, **kw: None)

from motor.coordinator.middleware.fastapi_middleware import (
    SimpleRateLimitMiddleware as NativeMW,
    load_rate_limit_config,
    ENV_RATE_LIMIT_MAX_REQUEST_BODY_SIZE,
)
from motor.coordinator.middleware.fastapi_middleware_legacy import (
    SimpleRateLimitMiddleware as LegacyMW,
)
from motor.coordinator.middleware.rate_limiter import SimpleRateLimiter

# ---------------------------------------------------------------------------
# ASGI helpers
# ---------------------------------------------------------------------------

_HTTP_SCOPE: Dict[str, Any] = {
    "type": "http",
    "method": "GET",
    "path": "/v1/chat/completions",
    "headers": [],
}

_LIFESPAN_SCOPE: Dict[str, Any] = {"type": "lifespan"}


async def _empty_receive() -> Dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _collecting_send(messages: List[Dict[str, Any]]) -> callable:
    async def _send(message: Dict[str, Any]) -> None:
        messages.append(message)

    return _send


async def _dummy_app(scope, receive, send):
    """Minimal ASGI app that returns 200 with a plain-text body."""
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


def _parse_response(
    messages: List[Dict[str, Any]],
) -> Tuple[int, Dict[str, str], bytes]:
    """Extract status, headers dict, and body from collected ASGI messages."""
    start = next(m for m in messages if m["type"] == "http.response.start")
    status = start["status"]
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start.get("headers", [])}
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, headers, body


def _make_limiter(max_requests: int, tokens: int | None = None) -> SimpleRateLimiter:
    """Create a limiter and optionally set its token count directly."""
    limiter = SimpleRateLimiter(max_requests=max_requests, window_size=60)
    if tokens is not None:
        limiter._bucket.tokens = tokens
    return limiter


# ---------------------------------------------------------------------------
# Test helpers that run both implementations and compare
# ---------------------------------------------------------------------------


async def _run_native(
    limiter: SimpleRateLimiter,
    scope: Dict[str, Any] | None = None,
    skip_paths: list | None = None,
    enabled: bool = True,
    error_message: str = "too fast",
    error_status_code: int = 429,
    max_request_body_size: int = 10 * 1024 * 1024,
) -> Tuple[List[Dict[str, Any]], Any]:
    """Run the native ASGI middleware and return (messages, middleware_instance)."""
    mw = NativeMW(
        app=_dummy_app,
        rate_limiter=limiter,
        skip_paths=skip_paths or [],
        error_message=error_message,
        error_status_code=error_status_code,
        max_request_body_size=max_request_body_size,
    )
    mw._config_holder.enabled = enabled
    messages: List[Dict[str, Any]] = []
    await mw(
        scope or dict(_HTTP_SCOPE),
        _empty_receive,
        await _collecting_send(messages),
    )
    return messages, mw


async def _run_legacy(
    limiter: SimpleRateLimiter,
    scope: Dict[str, Any] | None = None,
    skip_paths: list | None = None,
    enabled: bool = True,
    error_message: str = "too fast",
    error_status_code: int = 429,
) -> Tuple[List[Dict[str, Any]], Any]:
    """Run the legacy BaseHTTPMiddleware middleware and return (messages, middleware_instance).

    Because BaseHTTPMiddleware.dispatch expects a Starlette Request object,
    we build a minimal FastAPI app, mount the middleware, and drive it via
    httpx.ASGITransport to keep the test self-contained.
    """
    import httpx
    from fastapi import FastAPI

    app = FastAPI()

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def _catch_all(full_path: str):
        return {"ok": True}

    mw = LegacyMW(
        app=app,
        rate_limiter=limiter,
        skip_paths=skip_paths or [],
        error_message=error_message,
        error_status_code=error_status_code,
    )
    mw.enabled = enabled
    mw.app = app

    transport = httpx.ASGITransport(app=mw)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            (scope or _HTTP_SCOPE)["path"],
            timeout=10.0,
        )

    # Convert httpx response into ASGI-message-like structure for comparison
    messages = [
        {
            "type": "http.response.start",
            "status": resp.status_code,
            "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in resp.headers.items()],
        },
        {
            "type": "http.response.body",
            "body": resp.content,
        },
    ]
    return messages, mw


# ---------------------------------------------------------------------------
# The actual equivalence checks
# ---------------------------------------------------------------------------


def _assert_stats_equal(
    native_mw: NativeMW,
    legacy_mw: LegacyMW,
    label: str,
) -> None:
    """Assert that the stats dicts are equivalent (ignoring start_time and native-only keys)."""
    n = dict(native_mw.stats)
    l = dict(legacy_mw.stats)
    del n["start_time"]
    del l["start_time"]
    # 只比较两个中间件共有的 key（native 可能包含 legacy 没有的额外统计项）
    common_keys = set(n.keys()) & set(l.keys())
    for key in common_keys:
        assert n[key] == l[key], f"[{label}] stats mismatch for key '{key}':\n  native: {n[key]}\n  legacy: {l[key]}"


def _assert_response_equal(
    native_msgs: List[Dict[str, Any]],
    legacy_msgs: List[Dict[str, Any]],
    label: str,
) -> None:
    """Assert that status, body, and rate-limit headers match."""
    n_status, n_headers, n_body = _parse_response(native_msgs)
    l_status, l_headers, l_body = _parse_response(legacy_msgs)

    assert n_status == l_status, f"[{label}] status mismatch: native={n_status} legacy={l_status}"

    # Compare rate-limit headers only (content-type / content-length may differ)
    for hdr in ("x-ratelimit-remaining", "x-ratelimit-limit", "x-ratelimit-window"):
        n_val = n_headers.get(hdr)
        l_val = l_headers.get(hdr)
        assert n_val == l_val, f"[{label}] header {hdr} mismatch: native={n_val!r} legacy={l_val!r}"

    # Body comparison (for blocked case the body is JSON)
    if n_status >= 400:
        try:
            n_json = json.loads(n_body)
            l_json = json.loads(l_body)
            assert n_json == l_json, f"[{label}] error body mismatch:\n  native: {n_json}\n  legacy: {l_json}"
        except json.JSONDecodeError:
            assert n_body == l_body, f"[{label}] raw body mismatch: native={n_body!r} legacy={l_body!r}"


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


async def test_allowed_request():
    """Both should allow the request, inject X-RateLimit-* headers, and return 200."""
    limiter_n = _make_limiter(100)
    limiter_l = _make_limiter(100)

    n_msgs, n_mw = await _run_native(limiter_n)
    l_msgs, l_mw = await _run_legacy(limiter_l)

    _assert_response_equal(n_msgs, l_msgs, "allowed")
    _assert_stats_equal(n_mw, l_mw, "allowed")
    assert n_mw.stats["allowed_requests"] == 1
    assert n_mw.stats["blocked_requests"] == 0
    print("  [PASS] allowed_request")


async def test_blocked_request():
    """Both should return 429 with the structured error body."""
    limiter_n = _make_limiter(1, tokens=0)
    limiter_l = _make_limiter(1, tokens=0)

    n_msgs, n_mw = await _run_native(limiter_n)
    l_msgs, l_mw = await _run_legacy(limiter_l)

    _assert_response_equal(n_msgs, l_msgs, "blocked")
    _assert_stats_equal(n_mw, l_mw, "blocked")
    assert n_mw.stats["allowed_requests"] == 0
    assert n_mw.stats["blocked_requests"] == 1
    print("  [PASS] blocked_request")


async def test_skip_path():
    """Both should skip the path and NOT count it in allowed/blocked."""
    limiter_n = _make_limiter(100)
    limiter_l = _make_limiter(100)

    scope = {**_HTTP_SCOPE, "path": "/liveness"}

    n_msgs, n_mw = await _run_native(limiter_n, scope=scope, skip_paths=["/liveness"])
    l_msgs, l_mw = await _run_legacy(limiter_l, scope=scope, skip_paths=["/liveness"])

    _assert_response_equal(n_msgs, l_msgs, "skip_path")
    _assert_stats_equal(n_mw, l_mw, "skip_path")
    assert n_mw.stats["total_requests"] == 1
    assert n_mw.stats["allowed_requests"] == 0
    assert n_mw.stats["blocked_requests"] == 0
    print("  [PASS] skip_path")


async def test_disabled():
    """Both should pass through when enabled=False, not counting allowed/blocked."""
    limiter_n = _make_limiter(1, tokens=0)  # would block if enabled
    limiter_l = _make_limiter(1, tokens=0)

    n_msgs, n_mw = await _run_native(limiter_n, enabled=False)
    l_msgs, l_mw = await _run_legacy(limiter_l, enabled=False)

    _assert_response_equal(n_msgs, l_msgs, "disabled")
    _assert_stats_equal(n_mw, l_mw, "disabled")
    assert n_mw.stats["total_requests"] == 1
    assert n_mw.stats["allowed_requests"] == 0
    assert n_mw.stats["blocked_requests"] == 0
    print("  [PASS] disabled")


async def test_non_http_scope():
    """Both should pass through lifespan scopes without touching stats."""
    limiter_n = _make_limiter(100)
    limiter_l = _make_limiter(100)

    # Native: direct ASGI call with lifespan scope
    n_mw = NativeMW(app=_dummy_app, rate_limiter=limiter_n, skip_paths=[])
    n_msgs: List[Dict[str, Any]] = []
    await n_mw(_LIFESPAN_SCOPE, _empty_receive, await _collecting_send(n_msgs))

    # Legacy: BaseHTTPMiddleware only handles HTTP scopes, so lifespan is
    # transparently passed through by Starlette. We verify via stats.
    l_mw = LegacyMW(app=_dummy_app, rate_limiter=limiter_l, skip_paths=[])
    l_msgs: List[Dict[str, Any]] = []
    await l_mw(_LIFESPAN_SCOPE, _empty_receive, await _collecting_send(l_msgs))

    # Both should have empty stats for non-HTTP
    _assert_stats_equal(n_mw, l_mw, "non_http_scope")
    assert n_mw.stats["total_requests"] == 0
    print("  [PASS] non_http_scope")


async def test_custom_error():
    """Both should use the custom error_message and error_status_code."""
    limiter_n = _make_limiter(1, tokens=0)
    limiter_l = _make_limiter(1, tokens=0)

    n_msgs, n_mw = await _run_native(limiter_n, error_message="custom msg", error_status_code=503)
    l_msgs, l_mw = await _run_legacy(limiter_l, error_message="custom msg", error_status_code=503)

    n_status, _, n_body = _parse_response(n_msgs)
    l_status, _, l_body = _parse_response(l_msgs)

    assert n_status == 503, f"native status={n_status}, expected 503"
    assert l_status == 503, f"legacy status={l_status}, expected 503"
    assert "custom msg" in n_body.decode()
    assert "custom msg" in l_body.decode()
    _assert_stats_equal(n_mw, l_mw, "custom_error")
    print("  [PASS] custom_error")


async def test_update_config():
    """update_config should work identically on both."""
    limiter_n = _make_limiter(100)
    limiter_l = _make_limiter(100)

    n_mw = NativeMW(app=_dummy_app, rate_limiter=limiter_n, skip_paths=["/a"])
    l_mw = LegacyMW(app=_dummy_app, rate_limiter=limiter_l, skip_paths=["/a"])

    n_mw.update_config(skip_paths=["/b", "/c"], enabled=False, error_message="new")
    l_mw.update_config(skip_paths=["/b", "/c"], enabled=False, error_message="new")

    assert n_mw._config_holder.skip_paths == ["/b", "/c"]
    assert l_mw.skip_paths == ["/b", "/c"]
    assert n_mw._config_holder.enabled == False
    assert l_mw.enabled == False
    assert n_mw._config_holder.error_message == "new"
    assert l_mw.error_message == "new"
    print("  [PASS] update_config")


async def test_multiple_requests_stats():
    """Stats should accumulate identically across multiple requests."""
    limiter_n = _make_limiter(100)
    limiter_l = _make_limiter(100)

    n_mw = NativeMW(app=_dummy_app, rate_limiter=limiter_n, skip_paths=[])
    l_mw = LegacyMW(app=_dummy_app, rate_limiter=limiter_l, skip_paths=[])

    for _ in range(5):
        await n_mw(_HTTP_SCOPE, _empty_receive, await _collecting_send([]))

    # For legacy, we need to drive it through httpx each time
    import httpx
    from fastapi import FastAPI

    app = FastAPI()

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def _catch_all(full_path: str):
        return {"ok": True}

    l_mw.app = app
    transport = httpx.ASGITransport(app=l_mw)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(5):
            await client.get("/v1/chat/completions", timeout=10.0)

    _assert_stats_equal(n_mw, l_mw, "multiple_requests")
    assert n_mw.stats["total_requests"] == 5
    assert n_mw.stats["allowed_requests"] == 5
    assert n_mw.stats["blocked_requests"] == 0
    print("  [PASS] multiple_requests_stats")


async def test_exception_safety():
    """When the rate limiter raises, both should pass through and count as allowed."""

    class _FaultyLimiter:
        def is_allowed(self, *a, **kw):
            raise RuntimeError("simulated limiter failure")

    fl_n = _FaultyLimiter()
    fl_l = _FaultyLimiter()

    n_msgs, n_mw = await _run_native(fl_n)
    l_msgs, l_mw = await _run_legacy(fl_l)

    n_status, _, _ = _parse_response(n_msgs)
    l_status, _, _ = _parse_response(l_msgs)

    assert n_status == 200, f"native should pass through on error, got {n_status}"
    assert l_status == 200, f"legacy should pass through on error, got {l_status}"
    _assert_stats_equal(n_mw, l_mw, "exception_safety")
    assert n_mw.stats["allowed_requests"] == 1
    assert n_mw.stats["blocked_requests"] == 0
    print("  [PASS] exception_safety")


# ---------------------------------------------------------------------------
# Body size limit tests (native-only feature, no legacy equivalence)
# ---------------------------------------------------------------------------


def _make_scope_with_content_length(path: str, content_length: int) -> Dict[str, Any]:
    """构造带 Content-Length 头的 ASGI HTTP scope"""
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-length", str(content_length).encode("latin-1"))],
    }


def _make_scope_without_content_length(path: str) -> Dict[str, Any]:
    """构造不带 Content-Length 头的 ASGI HTTP scope"""
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
    }


def _make_scope_with_invalid_content_length(path: str, raw_value: str) -> Dict[str, Any]:
    """构造带非法 Content-Length 头的 ASGI HTTP scope"""
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-length", raw_value.encode("latin-1"))],
    }


async def test_body_size_within_limit():
    """请求体大小在限制范围内，应正常放行返回 200"""
    limiter = _make_limiter(100)
    scope = _make_scope_with_content_length("/v1/chat/completions", 1024)

    n_msgs, n_mw = await _run_native(limiter, scope=scope, max_request_body_size=10 * 1024)

    status, _, _ = _parse_response(n_msgs)
    assert status == 200, f"expected 200, got {status}"
    assert n_mw.stats["body_size_rejected_requests"] == 0
    assert n_mw.stats["allowed_requests"] == 1
    print("  [PASS] body_size_within_limit")


async def test_body_size_exceeds_limit():
    """请求体大小超过限制，应返回 413 并递增拒绝计数"""
    limiter = _make_limiter(100)
    scope = _make_scope_with_content_length("/v1/chat/completions", 2048)

    n_msgs, n_mw = await _run_native(limiter, scope=scope, max_request_body_size=1024)

    status, _, _ = _parse_response(n_msgs)
    assert status == 413, f"expected 413, got {status}"
    assert n_mw.stats["body_size_rejected_requests"] == 1
    assert n_mw.stats["allowed_requests"] == 0
    assert n_mw.stats["blocked_requests"] == 0
    print("  [PASS] body_size_exceeds_limit")


async def test_body_size_exact_boundary():
    """请求体大小恰好等于限制值，应放行（仅 > 才拒绝，>= 放行）"""
    limiter = _make_limiter(100)
    scope = _make_scope_with_content_length("/v1/chat/completions", 1024)

    n_msgs, n_mw = await _run_native(limiter, scope=scope, max_request_body_size=1024)

    status, _, _ = _parse_response(n_msgs)
    assert status == 200, f"expected 200 at exact boundary, got {status}"
    assert n_mw.stats["body_size_rejected_requests"] == 0
    print("  [PASS] body_size_exact_boundary")


async def test_body_size_no_content_length():
    """请求未携带 Content-Length 头，应跳过大小检查并放行"""
    limiter = _make_limiter(100)
    scope = _make_scope_without_content_length("/v1/chat/completions")

    n_msgs, n_mw = await _run_native(limiter, scope=scope, max_request_body_size=1024)

    status, _, _ = _parse_response(n_msgs)
    assert status == 200, f"expected 200 without content-length, got {status}"
    assert n_mw.stats["body_size_rejected_requests"] == 0
    print("  [PASS] body_size_no_content_length")


async def test_body_size_invalid_content_length():
    """Content-Length 头值为非法整数，应跳过大小检查并放行"""
    limiter = _make_limiter(100)
    scope = _make_scope_with_invalid_content_length("/v1/chat/completions", "not-a-number")

    n_msgs, n_mw = await _run_native(limiter, scope=scope, max_request_body_size=1024)

    status, _, _ = _parse_response(n_msgs)
    assert status == 200, f"expected 200 with invalid content-length, got {status}"
    assert n_mw.stats["body_size_rejected_requests"] == 0
    print("  [PASS] body_size_invalid_content_length")


async def test_body_size_disabled_when_zero():
    """max_request_body_size <= 0 时应完全跳过大小检查"""
    limiter = _make_limiter(100)
    scope = _make_scope_with_content_length("/v1/chat/completions", 999 * 1024 * 1024)

    n_msgs, n_mw = await _run_native(limiter, scope=scope, max_request_body_size=0)

    status, _, _ = _parse_response(n_msgs)
    assert status == 200, f"expected 200 when body size check disabled, got {status}"
    assert n_mw.stats["body_size_rejected_requests"] == 0
    print("  [PASS] body_size_disabled_when_zero")


async def test_body_size_check_before_rate_limit():
    """请求体超限时应优先返回 413，不消耗令牌、不返回 429"""
    limiter = _make_limiter(1, tokens=0)  # 令牌已耗尽，正常会返回 429
    scope = _make_scope_with_content_length("/v1/chat/completions", 2048)

    n_msgs, n_mw = await _run_native(limiter, scope=scope, max_request_body_size=1024)

    status, _, body = _parse_response(n_msgs)
    assert status == 413, f"expected 413 (body size before rate limit), got {status}"
    assert n_mw.stats["body_size_rejected_requests"] == 1
    assert n_mw.stats["blocked_requests"] == 0, "should not count as rate-limited"
    # 令牌不应被消耗
    assert limiter._bucket.tokens == 0, "token should not be consumed on body size reject"
    print("  [PASS] body_size_check_before_rate_limit")


async def test_body_size_skip_path_bypass():
    """skip_paths 中的路径应绕过请求体大小检查"""
    limiter = _make_limiter(100)
    scope = _make_scope_with_content_length("/liveness", 999 * 1024 * 1024)

    n_msgs, n_mw = await _run_native(limiter, scope=scope, skip_paths=["/liveness"], max_request_body_size=1024)

    status, _, _ = _parse_response(n_msgs)
    assert status == 200, f"expected 200 for skip path, got {status}"
    assert n_mw.stats["body_size_rejected_requests"] == 0
    print("  [PASS] body_size_skip_path_bypass")


async def test_body_size_disabled_middleware_bypass():
    """中间件被禁用时，请求体大小检查也应被跳过"""
    limiter = _make_limiter(100)
    scope = _make_scope_with_content_length("/v1/chat/completions", 999 * 1024 * 1024)

    n_msgs, n_mw = await _run_native(limiter, scope=scope, enabled=False, max_request_body_size=1024)

    status, _, _ = _parse_response(n_msgs)
    assert status == 200, f"expected 200 when middleware disabled, got {status}"
    assert n_mw.stats["body_size_rejected_requests"] == 0
    print("  [PASS] body_size_disabled_middleware_bypass")


async def test_body_size_413_response_body():
    """413 响应体应包含结构化的错误信息"""
    limiter = _make_limiter(100)
    scope = _make_scope_with_content_length("/v1/chat/completions", 5000)

    n_msgs, n_mw = await _run_native(limiter, scope=scope, max_request_body_size=1024)

    status, _, body = _parse_response(n_msgs)
    assert status == 413
    payload = json.loads(body)
    assert payload["error"] == "request_body_too_large"
    assert payload["details"]["content_length"] == 5000
    assert payload["details"]["max_allowed"] == 1024
    print("  [PASS] body_size_413_response_body")


async def test_body_size_update_config():
    """update_config 应能运行时更新 max_request_body_size"""
    limiter = _make_limiter(100)
    n_mw = NativeMW(
        app=_dummy_app,
        rate_limiter=limiter,
        skip_paths=[],
        max_request_body_size=1024,
    )

    # 初始限制 1024，5000 应被拒绝
    scope_big = _make_scope_with_content_length("/v1/test", 5000)
    msgs: List[Dict[str, Any]] = []
    await n_mw(scope_big, _empty_receive, await _collecting_send(msgs))
    status_before, _, _ = _parse_response(msgs)
    assert status_before == 413

    # 运行时更新为 10240，5000 应被放行
    n_mw.update_config(max_request_body_size=10240)
    msgs2: List[Dict[str, Any]] = []
    await n_mw(scope_big, _empty_receive, await _collecting_send(msgs2))
    status_after, _, _ = _parse_response(msgs2)
    assert status_after == 200
    print("  [PASS] body_size_update_config")


async def test_load_config_max_request_body_size_env():
    """环境变量 RATE_LIMIT_MAX_REQUEST_BODY_SIZE 应正确覆盖配置"""
    original = os.environ.get(ENV_RATE_LIMIT_MAX_REQUEST_BODY_SIZE)
    try:
        os.environ[ENV_RATE_LIMIT_MAX_REQUEST_BODY_SIZE] = "2048"
        config = load_rate_limit_config()
        assert config.max_request_body_size == 2048, f"expected 2048 from env, got {config.max_request_body_size}"
    finally:
        if original is None:
            os.environ.pop(ENV_RATE_LIMIT_MAX_REQUEST_BODY_SIZE, None)
        else:
            os.environ[ENV_RATE_LIMIT_MAX_REQUEST_BODY_SIZE] = original
    print("  [PASS] load_config_max_request_body_size_env")


async def test_load_config_max_request_body_size_env_invalid():
    """环境变量值为非法整数时，应回退到默认值且不抛异常"""
    original = os.environ.get(ENV_RATE_LIMIT_MAX_REQUEST_BODY_SIZE)
    try:
        os.environ[ENV_RATE_LIMIT_MAX_REQUEST_BODY_SIZE] = "not-a-number"
        config = load_rate_limit_config()
        assert config.max_request_body_size == 10 * 1024 * 1024, (
            f"expected default 10MB on invalid env, got {config.max_request_body_size}"
        )
    finally:
        if original is None:
            os.environ.pop(ENV_RATE_LIMIT_MAX_REQUEST_BODY_SIZE, None)
        else:
            os.environ[ENV_RATE_LIMIT_MAX_REQUEST_BODY_SIZE] = original
    print("  [PASS] load_config_max_request_body_size_env_invalid")