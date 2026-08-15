# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
#
# MindIE is licensed under both the Mulan PSL v2 and the Apache License, Version 2.0.
# You may choose to use this software under the terms of either license.
#
# ---------------------------------------------------------------------------
# Mulan PSL v2:
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
#
# Apache License, Version 2.0:
# You may obtain a copy of the License at:
#         http://www.apache.org/licenses/LICENSE-2.0
# ---------------------------------------------------------------------------
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the respective licenses for more details.

"""Anthropic error envelope helpers for the engine-side Anthropic routes.

Engine serving errors default to the OpenAI envelope (``{"error": {...}}``) or
FastAPI's ``{"detail": ...}``; Anthropic SDK clients only surface the Anthropic
envelope ``{"type": "error", "error": {"type": ..., "message": ...}}``.
"""

import json
from http import HTTPStatus
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from motor.engine_server.core.errors.sanitizer import sanitize_error_message

_STATUS_TO_ERROR_TYPE = {
    HTTPStatus.BAD_REQUEST.value: "invalid_request_error",
    HTTPStatus.UNAUTHORIZED.value: "authentication_error",
    HTTPStatus.FORBIDDEN.value: "permission_error",
    HTTPStatus.NOT_FOUND.value: "not_found_error",
    HTTPStatus.REQUEST_TIMEOUT.value: "timeout_error",
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE.value: "request_too_large",
    HTTPStatus.UNPROCESSABLE_ENTITY.value: "invalid_request_error",
    HTTPStatus.TOO_MANY_REQUESTS.value: "rate_limit_error",
}


def anthropic_error_type_for_status(status_code: int) -> str:
    """Map an HTTP status code to an Anthropic error type."""
    if status_code in _STATUS_TO_ERROR_TYPE:
        return _STATUS_TO_ERROR_TYPE[status_code]
    if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR.value:
        return "api_error"
    return "invalid_request_error"


def anthropic_error_payload(
    *,
    status_code: int,
    message: str,
    error_type: str | None = None,
) -> dict[str, Any]:
    """Build the Anthropic error envelope body."""
    return {
        "type": "error",
        "error": {
            "type": error_type or anthropic_error_type_for_status(status_code),
            "message": sanitize_error_message(message),
        },
    }


def anthropic_error_response(
    *,
    status_code: int,
    message: str,
    error_type: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Render an Anthropic-envelope error as a JSON response."""
    return JSONResponse(
        content=anthropic_error_payload(
            status_code=status_code,
            message=message,
            error_type=error_type,
        ),
        status_code=status_code,
        headers=headers,
    )


def _detail_to_message(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        return str(detail["error"].get("message", ""))
    if isinstance(detail, list):
        count = len(detail)
        label = "error" if count == 1 else "errors"
        message = f"{count} validation {label}:\n"
        message += "".join(f"  {error}\n" for error in detail).rstrip()
        return message
    return str(detail)


def anthropic_http_response_from_exception(exc: Exception) -> JSONResponse:
    """Map an HTTP-layer exception on an Anthropic route to the Anthropic envelope."""
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        return anthropic_error_response(
            status_code=exc.status_code,
            message=_detail_to_message(exc.detail),
            headers=getattr(exc, "headers", None),
        )
    return anthropic_error_response(
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
        message=str(exc),
    )


def anthropic_response_from_engine_error_body(
    body: dict[str, Any],
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Convert an engine error body to the Anthropic envelope.

    Idempotent: bodies already in the Anthropic envelope pass through with
    their original status code.
    """
    if body.get("type") == "error" and isinstance(body.get("error"), dict):
        return JSONResponse(content=body, status_code=status_code, headers=headers)
    error = body.get("error")
    if isinstance(error, dict):
        message = str(error.get("message", ""))
    elif "detail" in body:
        message = _detail_to_message(body["detail"])
    else:
        message = json.dumps(body, ensure_ascii=False)
    return anthropic_error_response(status_code=status_code, message=message, headers=headers)


def anthropic_stream_error_frame(exc: Exception) -> str:
    """Serialize a mid-stream failure as one Anthropic ``error`` SSE frame.

    Anthropic streams carry errors as ``event: error`` frames and have no
    ``[DONE]`` sentinel.
    """
    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        status_code = exc.status_code
        message = _detail_to_message(exc.detail)
    else:
        status_code = HTTPStatus.INTERNAL_SERVER_ERROR.value
        message = str(exc)
    payload = anthropic_error_payload(status_code=status_code, message=message)
    return f"event: error\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
