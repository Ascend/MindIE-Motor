# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Anthropic error envelope helpers for coordinator-synthesized errors.

Coordinator-synthesized errors (validation, scheduling failure, upstream transport
failure, mid-stream failure) on Anthropic entry APIs (``v1/messages*``) must use the
Anthropic envelope ``{"type": "error", "error": {"type": ..., "message": ...}}``;
Anthropic SDKs do not reliably surface FastAPI ``{"detail": ...}`` bodies or
OpenAI-shaped ``data: {"error": ...}`` SSE frames. Engine-supplied error bodies are
NOT routed through here — they pass through verbatim (backend error pass-through).
"""

import json
from typing import Any

from fastapi.responses import JSONResponse

from motor.common.http.security_utils import sanitize_error_message

_STATUS_TO_ERROR_TYPE = {
    400: "invalid_request_error",
    404: "not_found_error",
    429: "rate_limit_error",
    503: "overloaded_error",
}


def anthropic_error_type_for_status(status_code: int) -> str:
    """Map an HTTP status code to an Anthropic error type."""
    if status_code in _STATUS_TO_ERROR_TYPE:
        return _STATUS_TO_ERROR_TYPE[status_code]
    if status_code >= 500:
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
        status_code=status_code,
        content=anthropic_error_payload(status_code=status_code, message=message, error_type=error_type),
        headers=headers,
    )


def detail_to_message(detail: Any) -> str:
    """Flatten a FastAPI ``HTTPException.detail`` into a plain message string."""
    if isinstance(detail, str):
        return detail
    return str(detail)


def is_anthropic_error_body(body: bytes) -> dict[str, Any] | None:
    """Return the parsed body when it already carries the Anthropic error envelope."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if isinstance(payload, dict) and payload.get("type") == "error" and isinstance(payload.get("error"), dict):
        return payload
    return None


def anthropic_stream_error_frame(*, status_code: int, message: str) -> bytes:
    """Serialize a mid-stream failure as one Anthropic ``error`` SSE frame.

    Anthropic streams carry errors as ``event: error`` frames and have no
    ``[DONE]`` sentinel.
    """
    payload = anthropic_error_payload(status_code=status_code, message=message)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: error\ndata: {encoded}\n\n".encode()
