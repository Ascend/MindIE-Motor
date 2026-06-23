# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the License at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import contextlib
import inspect
import math
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException

from motor.common.http.security_utils import sanitize_error_message


_CLIENT_ERROR_EXCEPTION_NAMES = frozenset(
    {
        "BadRequestError",
        "ContextLengthExceededError",
        "InputTooLongError",
        "InvalidRequestError",
        "RequestValidationError",
    }
)
_CONTEXT_LENGTH_ERROR_MARKERS = (
    "maximum context length",
    "maximum model length",
    "max model length",
    "prompt is too long",
    "input is too long",
    "too many tokens",
)
_FORWARDED_EXCEPTION_HEADERS = frozenset(
    {
        "allow",
        "retry-after",
        "www-authenticate",
    }
)


def _is_json_detail(value: Any, *, depth: int = 0, seen: set[int] | None = None) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if value is None or isinstance(value, str | int | bool):
        return True
    if depth >= 8:
        return False
    if not isinstance(value, dict | list):
        return False

    seen = seen if seen is not None else set()
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    try:
        if isinstance(value, list):
            return all(_is_json_detail(item, depth=depth + 1, seen=seen) for item in value)
        return all(
            isinstance(key, str) and _is_json_detail(item, depth=depth + 1, seen=seen) for key, item in value.items()
        )
    finally:
        seen.remove(identity)


def _valid_error_status(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        status_code = int(value)
    except (TypeError, ValueError):
        return None
    return status_code if HTTPStatus.BAD_REQUEST <= status_code <= 599 else None


def _exception_status_code(exc: Exception) -> int | None:
    for attr_name in ("status_code", "http_status_code", "http_status"):
        status_code = _valid_error_status(getattr(exc, attr_name, None))
        if status_code is not None:
            return status_code

    response = getattr(exc, "response", None)
    return _valid_error_status(getattr(response, "status_code", None))


def _exception_detail(exc: Exception) -> Any:
    detail = getattr(exc, "detail", None)
    if detail is not None:
        return detail

    response = getattr(exc, "response", None)
    if response is not None:
        json_loader = getattr(response, "json", None)
        if callable(json_loader):
            try:
                payload = json_loader()
            except Exception:
                payload = None
            if inspect.isawaitable(payload):
                close = getattr(payload, "close", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        close()
                payload = None
            if payload is not None and _is_json_detail(payload):
                return payload
        text = getattr(response, "text", None)
        if isinstance(text, str) and text:
            return text

    return str(exc)


def _exception_headers(exc: Exception) -> dict[str, str] | None:
    candidates = getattr(exc, "headers", None)
    if candidates is None:
        response = getattr(exc, "response", None)
        candidates = getattr(response, "headers", None)
    if not isinstance(candidates, Mapping):
        return None

    headers = {
        str(name): str(value) for name, value in candidates.items() if str(name).lower() in _FORWARDED_EXCEPTION_HEADERS
    }
    return headers or None


def _is_client_input_error(exc: Exception) -> bool:
    if isinstance(exc, OverflowError):
        return True
    if type(exc).__name__ in _CLIENT_ERROR_EXCEPTION_NAMES:
        return True
    if not isinstance(exc, ValueError):
        return False

    message = str(exc).lower()
    if any(marker in message for marker in _CONTEXT_LENGTH_ERROR_MARKERS):
        return True
    return "sequence length" in message and ("exceed" in message or "longer than" in message)


def map_serving_exception(
    exc: Exception,
    *,
    map_unknown_to_http_500: bool = True,
) -> Exception:
    """Preserve engine HTTP errors and classify known request validation failures."""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, StarletteHTTPException):
        return HTTPException(
            status_code=exc.status_code,
            detail=exc.detail,
            headers=exc.headers,
        )

    status_code = _exception_status_code(exc)
    if status_code is not None:
        return HTTPException(
            status_code=status_code,
            detail=_exception_detail(exc),
            headers=_exception_headers(exc),
        )

    if _is_client_input_error(exc):
        return HTTPException(
            status_code=HTTPStatus.BAD_REQUEST.value,
            detail=str(exc),
        )

    if map_unknown_to_http_500:
        return HTTPException(
            status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
            detail=sanitize_error_message(str(exc)),
        )
    return exc
