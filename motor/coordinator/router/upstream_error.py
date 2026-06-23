# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the License at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
from dataclasses import dataclass

import httpx
from fastapi import status
from fastapi.responses import JSONResponse, Response


RETRYABLE_UPSTREAM_STATUS_CODES = frozenset(
    {
        status.HTTP_408_REQUEST_TIMEOUT,
        status.HTTP_425_TOO_EARLY,
        status.HTTP_429_TOO_MANY_REQUESTS,
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        status.HTTP_502_BAD_GATEWAY,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        status.HTTP_504_GATEWAY_TIMEOUT,
    }
)
FORWARDED_ERROR_HEADERS = frozenset({"content-type", "retry-after"})
ERROR_MESSAGE_PREVIEW_BYTES = 1024


@dataclass
class UpstreamHTTPError(Exception):
    """HTTP error returned by an inference engine."""

    status_code: int
    body: bytes
    headers: dict[str, str]
    phase: str
    # True when ``body`` was capped at ``upstream_error_body_max_bytes`` and the engine's error
    # was larger, so ``body`` may be cut mid-payload and must not be forwarded verbatim.
    truncated: bool = False

    def __str__(self) -> str:
        preview = self.body[:ERROR_MESSAGE_PREVIEW_BYTES]
        text = preview.decode("utf-8", errors="replace").strip()
        if len(self.body) > ERROR_MESSAGE_PREVIEW_BYTES:
            text = f"{text}..."
        if text:
            return f"Upstream {self.phase} request failed with HTTP {self.status_code}: {text}"
        return f"Upstream {self.phase} request failed with HTTP {self.status_code}"

    @classmethod
    def from_response(
        cls,
        response: httpx.Response,
        *,
        body: bytes,
        phase: str,
        truncated: bool = False,
    ) -> "UpstreamHTTPError":
        headers = {
            name.lower(): value for name, value in response.headers.items() if name.lower() in FORWARDED_ERROR_HEADERS
        }
        return cls(
            status_code=response.status_code,
            body=body,
            headers=headers,
            phase=phase,
            truncated=truncated,
        )


def is_retryable_upstream_error(error: BaseException) -> bool:
    if isinstance(error, UpstreamHTTPError):
        return error.status_code in RETRYABLE_UPSTREAM_STATUS_CODES
    if isinstance(error, asyncio.CancelledError):
        return False
    return isinstance(
        error,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.ReadError,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.WriteError,
            httpx.WriteTimeout,
        ),
    )


def render_upstream_error(error: UpstreamHTTPError) -> Response:
    # Forward the engine's error body verbatim only when it is complete. A truncated body (engine
    # error exceeded upstream_error_body_max_bytes) may be cut mid-JSON, so forwarding it under the
    # engine's content-type would hand the client an invalid payload; fall back to a self-contained
    # JSON error that still preserves the upstream status code and a bounded preview.
    if error.body and not error.truncated:
        return Response(
            content=error.body,
            status_code=error.status_code,
            headers=dict(error.headers),
        )
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {
                "message": str(error),
                "type": "upstream_http_error",
                "code": error.status_code,
            }
        },
    )


def render_transport_error(error: httpx.RequestError) -> JSONResponse:
    status_code = (
        status.HTTP_504_GATEWAY_TIMEOUT if isinstance(error, httpx.TimeoutException) else status.HTTP_502_BAD_GATEWAY
    )
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": str(error),
                "type": type(error).__name__,
                "code": status_code,
            }
        },
    )
