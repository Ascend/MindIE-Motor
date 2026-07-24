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

        self.stats = {
            "total_requests": 0,
            "allowed_requests": 0,
            "blocked_requests": 0,
            "body_size_rejected_requests": 0,
            "start_time": time.time(),
        }

    @staticmethod
    def _extract_request_data(scope: Scope) -> dict[str, Any]:
        """Extract basic request info (path, method, timestamp) from the ASGI scope."""
        return {"endpoint": scope.get("path", ""), "method": scope.get("method", ""), "timestamp": time.time()}

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

        # Request body size check (based on Content-Length header, without reading the body)
        max_body_size_mb = self._config_holder.max_request_body_size
        if max_body_size_mb > 0:
            # max_request_body_size is configured in MB (float); when comparing, it is converted to bytes.
            max_body_size_bytes = int(max_body_size_mb * 1024 * 1024)
            content_length = self._get_content_length(scope)
            if content_length > max_body_size_bytes:
                self.stats["body_size_rejected_requests"] += 1
                logger.warning(f"Request body size too large: {content_length} > {max_body_size_bytes}, path={path}")
                error_response = {
                    "error": "request_body_too_large",
                    "message": f"Request body size ({content_length} bytes) exceeds maximum",
                }
                response = JSONResponse(status_code=413, content=error_response)
                await response(scope, receive, send)
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
            await self.app(scope, receive, send)
            return

        if allowed:
            # Request allowed, increment counter
            self.stats["allowed_requests"] += 1

            # Pre-build rate limiting response headers (bytes form, required by ASGI)
            rate_limit_headers = self._create_rate_limit_headers(limit_info)
            header_pairs = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in rate_limit_headers.items()]

            if not header_pairs:
                # No headers to inject, skip the send wrapper to minimize overhead
                await self.app(scope, receive, send)
                return

            async def send_with_rate_limit_headers(message: Message) -> None:
                """Wrap send to inject rate limiting headers into the response start message."""
                if message["type"] == "http.response.start":
                    existing = list(message.get("headers") or [])
                    existing.extend(header_pairs)
                    message = {**message, "headers": existing}
                await send(message)

            await self.app(scope, receive, send_with_rate_limit_headers)
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
