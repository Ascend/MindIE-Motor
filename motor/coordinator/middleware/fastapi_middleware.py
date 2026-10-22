# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import time
from dataclasses import dataclass, field
from typing import Any

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from motor.common.logger import get_logger
from motor.coordinator.domain.agent_hint_wire import (
    extract_priority_from_hint_headers,
    extract_strict_priority_from_hint_headers,
    strict_priority_trusted,
)
from motor.config.coordinator import default_rate_limit_skip_paths

from .rate_limiter import SimpleRateLimiter

logger = get_logger(__name__)


@dataclass
class RateLimitConfigHolder:
    """Hot-reloadable rate limit configuration holder.

    Centralizes rate limit middleware configuration and the rate_limiter instance reference.
    External code (e.g. _apply_config_changes) can directly modify attributes or call methods
    to apply changes immediately.
    """

    skip_paths: list = field(default_factory=default_rate_limit_skip_paths)
    strict_priority_enabled: bool = False
    strict_priority_tokens: list = field(default_factory=list)
    error_message: str = "Request too frequent, please try again later"
    error_status_code: int = 429
    enabled: bool = True
    # Maximum request body size, in MB (1 MB = 1024*1024 bytes), supports decimal values; <= 0 means no limit.
    max_request_body_size: float = 0
    rate_limiter: SimpleRateLimiter = None


class SimpleRateLimitMiddleware:
    """
    FastAPI rate limiting middleware.
    """

    def __init__(
        self,
        app: ASGIApp,
        rate_limiter: SimpleRateLimiter | None = None,
        skip_paths: list | None = None,
        error_message: str = "Request too frequent, please try again later",
        error_status_code: int = 429,
        max_request_body_size: float = 0,
        config_holder: "RateLimitConfigHolder | None" = None,
        strict_priority_enabled: bool = False,
        strict_priority_tokens: list | None = None,
    ):
        """
        Initialize the rate limiting middleware.

        Args:
            app: Downstream ASGI application (FastAPI instance or next middleware layer).
            rate_limiter: Rate limiter instance; uses default SimpleRateLimiter if None.
            skip_paths: List of paths to skip rate limiting.
            error_message: Rate limit error message.
            error_status_code: Rate limit error status code.
            max_request_body_size: Maximum request body size in MB (1MB = 1024*1024 bytes);
                requests exceeding this are rejected. Supports decimals (e.g. 0.5). <= 0 means no limit.
            config_holder: Hot-reloadable configuration holder. If None, created internally
                from the other parameters.
        """
        self.app = app
        self.rate_limiter = rate_limiter or SimpleRateLimiter()

        if config_holder is not None:
            self._config_holder = config_holder
        else:
            self._config_holder = RateLimitConfigHolder(
                skip_paths=skip_paths,
                error_message=error_message,
                error_status_code=error_status_code,
                max_request_body_size=max_request_body_size,
            )
        # WP-E2 strict trust inputs: explicit params win; fall back to the holder
        # so hot-reload paths can refresh them via the same object.
        self.config_holder = config_holder
        self.strict_priority_enabled = bool(
            strict_priority_enabled or getattr(config_holder, "strict_priority_enabled", False)
        )
        self.strict_priority_tokens = list(
            strict_priority_tokens or getattr(config_holder, "strict_priority_tokens", None) or []
        )

        self.stats = {
            "total_requests": 0,
            "allowed_requests": 0,
            "blocked_requests": 0,
            "body_size_rejected_requests": 0,
            "start_time": time.time(),
        }

    def _extract_request_data(self, scope: Scope) -> dict[str, Any]:
        """Extract basic request info (path, method, timestamp, hint priority) from the ASGI scope.

        Priority comes from the X-Agent-Hint header only (spec protocol is
        header-only); the body is never read here — the receive channel must be
        forwarded to the downstream app untouched. ``strict_priority`` is the
        hard-priority level and is only exposed when the request passes the
        WP-E2 trust rule (enabled + matching X-Strict-Priority-Token);
        untrusted claims never reach the limiter.
        """
        headers = {}
        for name, value in scope.get("headers", []):
            key = name.decode("latin-1").lower()
            if key in ("x-agent-hint", "x-strict-priority-token"):
                headers[key] = value.decode("latin-1")
        strict_level = None
        # Live-read the holder so config hot-reload (disable / token rotation)
        # takes effect immediately; the constructor snapshot is only a fallback
        # for middlewares built without a holder.
        strict_enabled = bool(getattr(self.config_holder, "strict_priority_enabled", self.strict_priority_enabled))
        strict_tokens = getattr(self.config_holder, "strict_priority_tokens", None) or self.strict_priority_tokens
        if strict_priority_trusted(
            headers,
            enabled=strict_enabled,
            tokens=strict_tokens,
        ):
            strict = extract_strict_priority_from_hint_headers(headers)
            strict_level = strict[0] if strict else None
        return {
            "endpoint": scope.get("path", ""),
            "method": scope.get("method", ""),
            "timestamp": time.time(),
            "priority": extract_priority_from_hint_headers(headers),
            "strict_priority": strict_level,
        }

    @staticmethod
    def _get_content_length(scope: Scope) -> int:
        """
        Extract the Content-Length header from the ASGI scope.

        Args:
            scope: ASGI scope.

        Returns:
            int: Content-Length value, or -1 if not found or parsing failed.
        """
        for header_name, header_value in scope.get("headers", []):
            if header_name == b"content-length":
                try:
                    return int(header_value.decode("latin-1"))
                except (ValueError, TypeError):
                    return -1
        return -1

    @staticmethod
    async def _read_body_with_limit(receive: Receive, max_bytes: int) -> tuple[bytes, bool]:
        """Read the full request body while enforcing a byte limit.

        Iterates ASGI http.request messages from `receive` and accumulates body
        bytes. Stops as soon as the accumulated size exceeds `max_bytes`.

        Args:
            receive: Original ASGI receive callable.
            max_bytes: Maximum allowed body size in bytes (inclusive; equal is allowed).

        Returns:
            Tuple (body, over_limit). When over_limit is True, body is discarded
            (returns b"") and the caller should reject with 413 without consuming
            more of the body. On http.disconnect the loop terminates and the
            partial body collected so far is returned with over_limit=False.
        """
        chunks: list[bytes] = []
        received = 0
        while True:
            message = await receive()
            mtype = message.get("type")
            if mtype == "http.request":
                chunk = message.get("body", b"") or b""
                received += len(chunk)
                if received > max_bytes:
                    return b"", True
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            elif mtype == "http.disconnect":
                break
            else:
                break
        return b"".join(chunks), False

    @staticmethod
    def _make_replay_receive(original_receive: Receive, body: bytes) -> Receive:
        """Build a receive callable that replays a buffered body then delegates.

        The first call returns a single http.request message containing the full
        `body` with more_body=False. Subsequent calls delegate to
        `original_receive` so that downstream messages (e.g. http.disconnect)
        are delivered normally.
        """
        replayed = [False]

        async def replay_receive() -> Message:
            if not replayed[0]:
                replayed[0] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await original_receive()

        return replay_receive

    @staticmethod
    def _create_rate_limit_headers(limit_info: dict[str, Any]) -> dict[str, str]:
        """Build response headers from the rate limiter info dictionary."""
        headers = {}

        if "available" in limit_info:
            headers["X-RateLimit-Remaining"] = str(limit_info["available"])
        if "limit" in limit_info:
            headers["X-RateLimit-Limit"] = str(limit_info["limit"])
        if "window_size" in limit_info:
            headers["X-RateLimit-Window"] = str(limit_info["window_size"])

        return headers

    async def _enforce_body_size_limit(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        path: str,
    ) -> Receive | None:
        """Enforce the request body size limit and return the receive to forward.

        Two mutually exclusive paths based on Content-Length presence:
        - Content-Length present (>=0): trust it (the ASGI server delivers
          exactly CL bytes to the app), reject by header value without
          reading the body.
        - Content-Length absent/invalid (-1, e.g. chunked): pre-read the
          actual body bytes and enforce the limit.

        Runs BEFORE the rate limiter so body rejection does not consume a token.

        Args:
            scope: ASGI scope.
            receive: Original ASGI receive callable.
            send: ASGI send callable (used to emit 413 when rejected).
            path: Request path (for logging).

        Returns:
            The receive callable to forward to the downstream app when the
            request is allowed (the original `receive` for the Content-Length
            path, or a replay receive for the pre-read path); None when the
            request has been rejected with a 413 response already sent via
            `send`, in which case the caller must return immediately.
        """
        max_body_size_mb = self._config_holder.max_request_body_size
        # <= 0 means the body size check is disabled; pass through unchanged.
        if max_body_size_mb <= 0:
            return receive

        # max_request_body_size is configured in MB (float); convert to bytes for comparison.
        max_body_size_bytes = int(max_body_size_mb * 1024 * 1024)
        content_length = self._get_content_length(scope)

        if content_length >= 0:
            # Path A: Content-Length present. Trust the header value; do not pre-read.
            if content_length > max_body_size_bytes:
                self.stats["body_size_rejected_requests"] += 1
                logger.warning(
                    f"Request body size too large (Content-Length): "
                    f"{content_length} > {max_body_size_bytes}, path={path}"
                )
                error_response = {
                    "error": "request_body_too_large",
                    "message": f"Request body size ({content_length} bytes) exceeds maximum",
                }
                response = JSONResponse(status_code=413, content=error_response)
                await response(scope, receive, send)
                return None
            # CL within limit: pass through the original receive (no pre-read, no replay).
            return receive

        # Path B: no Content-Length (chunked / missing / invalid). Pre-read actual bytes.
        body, over_limit = await self._read_body_with_limit(receive, max_body_size_bytes)
        if over_limit:
            self.stats["body_size_rejected_requests"] += 1
            logger.warning(f"Request body size too large (actual bytes): exceeds {max_body_size_bytes}, path={path}")
            error_response = {
                "error": "request_body_too_large",
                "message": f"Request body size exceeds maximum ({max_body_size_bytes} bytes)",
            }
            response = JSONResponse(status_code=413, content=error_response)
            await response(scope, receive, send)
            return None
        # Body within limit: replay it to the downstream app via a synthetic receive.
        return self._make_replay_receive(receive, body)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point: enforce body-size and rate limits for HTTP requests."""
        # Non-HTTP scopes (lifespan, websocket, etc.) are passed through unchanged
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        # HTTP request counter
        self.stats["total_requests"] += 1

        # Rate limiting disabled at runtime, pass through
        if not self._config_holder.enabled:
            await self.app(scope, receive, send)
            return

        # Skip-listed path, pass through
        path = scope.get("path", "")
        if self._should_skip_path(path):
            await self.app(scope, receive, send)
            return

        # Request body size check (runs before the rate limiter so a rejection
        # does not consume a token). Returns the receive to forward, or None
        # when a 413 has already been sent.
        effective_receive = await self._enforce_body_size_limit(scope, receive, send, path)
        if effective_receive is None:
            return

        # Check rate limiting — only the limiter call is guarded so that
        # downstream exceptions (including those raised mid-stream) propagate
        # naturally instead of being caught and triggering a second response.
        request_data = self._extract_request_data(scope)
        try:
            allowed, limit_info = self.rate_limiter.is_allowed(request_data)
        except Exception as e:
            logger.error(f"Error in rate limiting middleware processing request: {e}")
            # Allow request by default when error occurs
            self.stats["allowed_requests"] += 1
            await self.app(scope, effective_receive, send)
            return

        if allowed:
            # Request allowed, increment counter
            self.stats["allowed_requests"] += 1

            # Pre-build rate limiting response headers (bytes form, required by ASGI)
            rate_limit_headers = self._create_rate_limit_headers(limit_info)
            header_pairs = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in rate_limit_headers.items()]

            if not header_pairs:
                # No headers to inject, skip the send wrapper to minimize overhead
                await self.app(scope, effective_receive, send)
                return

            async def send_with_rate_limit_headers(message: Message) -> None:
                """Wrap send to inject rate limiting headers into the response start message."""
                if message["type"] == "http.response.start":
                    existing = list(message.get("headers") or [])
                    existing.extend(header_pairs)
                    message = {**message, "headers": existing}
                await send(message)

            await self.app(scope, effective_receive, send_with_rate_limit_headers)
            return
        else:
            # Request rate limited, increment counter
            self.stats["blocked_requests"] += 1

            # Build rate limiting response headers and error body
            rate_limit_headers = self._create_rate_limit_headers(limit_info)
            error_response = {
                "error": "rate_limit_exceeded",
                "message": self._config_holder.error_message,
                "details": {
                    "available": limit_info.get("available", 0),
                    "limit": limit_info.get("limit", 0),
                    "window_size": limit_info.get("window_size", 0),
                },
            }

            logger.warning(f"Request rate limited: {request_data['endpoint']}")

            # Send JSONResponse directly via ASGI, bypassing the downstream app
            response = JSONResponse(
                status_code=self._config_holder.error_status_code, content=error_response, headers=rate_limit_headers
            )
            await response(scope, receive, send)
            return

    def _should_skip_path(self, path: str) -> bool:
        """Return True if the given path matches any skip-listed prefix."""
        skip_paths = self._config_holder.skip_paths
        if not skip_paths:
            return False
        return any(path.startswith(skip_path) for skip_path in skip_paths)
