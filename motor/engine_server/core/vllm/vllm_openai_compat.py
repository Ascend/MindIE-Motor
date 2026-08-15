# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
#
# MindIE is licensed under both the Mulan PSL v2 and the Apache License, Version 2.0.
# Portions derived from vLLM are licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Helpers to support multiple vLLM OpenAI serving API shapes (with/without OpenAIServingRender)."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from importlib import import_module
from typing import Any

from motor.engine_server.core.errors.sanitizer import sanitize_error_message

try:
    from motor.common.logger import get_logger

    logger = get_logger(__name__)
except (ImportError, ModuleNotFoundError):
    # Keep the compatibility module importable in minimal test/build
    # environments. Production uses the project logger whenever available.
    logger = logging.getLogger(__name__)


def _import_vllm_attr(candidates: tuple[tuple[str, str], ...], unavailable: Any) -> Any:
    for module_path, attr_name in candidates:
        try:
            return getattr(import_module(module_path), attr_name)
        except ImportError:
            continue
    return unavailable


class _UnavailableRequestLogger:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("vLLM RequestLogger is not available in this environment")


def _unavailable_cli_env_setup() -> None:
    raise RuntimeError("vLLM cli_env_setup is not available in this environment")


def _unavailable_process_lora_modules(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError("vLLM process_lora_modules is not available in this environment")


RequestLogger = _import_vllm_attr(
    (
        ("vllm.entrypoints.serve.utils.request_logger", "RequestLogger"),
        ("vllm.entrypoints.logger", "RequestLogger"),
    ),
    _UnavailableRequestLogger,
)
process_lora_modules = _import_vllm_attr(
    (
        ("vllm.entrypoints.serve.utils.api_utils", "process_lora_modules"),
        ("vllm.entrypoints.utils", "process_lora_modules"),
    ),
    _unavailable_process_lora_modules,
)
cli_env_setup = _import_vllm_attr(
    (
        ("vllm.entrypoints.serve.utils.api_utils", "cli_env_setup"),
        ("vllm.entrypoints.utils", "cli_env_setup"),
    ),
    _unavailable_cli_env_setup,
)

__all__ = [
    "RequestLogger",
    "process_lora_modules",
    "cli_env_setup",
    "kwargs_matching_signature",
    "call_openai_serving",
    "openai_http_response_from_exception",
    "openai_http_response_from_generator",
    "vllm_stream_error_json",
    "register_vllm_openai_error_handlers",
    "vllm_openai_chat_needs_render",
    "build_openai_serving_render_kwargs",
    "create_openai_serving_render",
]


def openai_http_response_from_generator(
    generator: Any,
    json_response_type: type | tuple[type, ...],
) -> Any:
    """Map vLLM OpenAI serving output to a FastAPI HTTP response."""
    from fastapi.responses import JSONResponse, StreamingResponse
    from vllm.entrypoints.openai.engine.protocol import ErrorResponse

    if isinstance(generator, ErrorResponse):
        return JSONResponse(
            content=generator.model_dump(),
            status_code=generator.error.code,
        )

    response_types = json_response_type if isinstance(json_response_type, tuple) else (json_response_type,)
    if isinstance(generator, response_types):
        return JSONResponse(content=generator.model_dump())

    return StreamingResponse(content=generator, media_type="text/event-stream")


def _create_vllm_error_response(exc: Exception) -> Any:
    try:
        from vllm.entrypoints.serve.utils.error_response import create_error_response
    except ImportError:
        create_error_response = None

    if create_error_response is not None:
        try:
            return create_error_response(exc)
        except (AttributeError, TypeError, ValueError):
            # Keep the server able to return a safe error if a supported vLLM
            # version changes the helper contract unexpectedly.
            logger.exception("vLLM create_error_response failed; using compatibility fallback")

    err_type, status_code = _fallback_error_classification(exc)
    payload = {
        "error": {
            "message": sanitize_error_message(str(exc)),
            "type": err_type,
            "param": getattr(exc, "parameter", None) or getattr(exc, "param", None),
            "code": status_code,
        }
    }

    class _FallbackErrorResponse:
        def __init__(self) -> None:
            self.error = type("_FallbackErrorInfo", (), {"code": status_code})()

        def model_dump(self) -> dict[str, Any]:
            return payload

    return _FallbackErrorResponse()


def _fallback_error_classification(exc: Exception) -> tuple[str, int]:
    """Best-effort mirror of vLLM create_error_response when the helper is absent."""
    name = type(exc).__name__
    if name == "VLLMValidationError" or isinstance(exc, ValueError | TypeError | OverflowError):
        return "BadRequestError", HTTPStatus.BAD_REQUEST.value
    if name == "VLLMUnprocessableEntityError":
        return "UnprocessableEntityError", HTTPStatus.UNPROCESSABLE_ENTITY.value
    if name == "VLLMNotFoundError":
        return "NotFoundError", HTTPStatus.NOT_FOUND.value
    if isinstance(exc, NotImplementedError):
        return "NotImplementedError", HTTPStatus.NOT_IMPLEMENTED.value
    if name == "GenerationError":
        code = getattr(exc, "status_code", None)
        try:
            status_code = int(code) if code is not None else HTTPStatus.INTERNAL_SERVER_ERROR.value
        except (TypeError, ValueError):
            status_code = HTTPStatus.INTERNAL_SERVER_ERROR.value
        return "InternalServerError", status_code
    # Mirror vLLM: jinja2.TemplateError (and subclasses) are client input errors.
    if any(cls.__name__ == "TemplateError" for cls in type(exc).__mro__):
        return "BadRequestError", HTTPStatus.BAD_REQUEST.value
    return "InternalServerError", HTTPStatus.INTERNAL_SERVER_ERROR.value


def _vllm_error_response_to_json_response(error_response: Any) -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        content=error_response.model_dump(),
        status_code=error_response.error.code,
    )


def _http_exception_error_payload(
    *,
    status_code: int,
    detail: Any,
) -> tuple[dict[str, Any], int]:
    if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
        error = detail["error"].copy()
        error["message"] = sanitize_error_message(str(error.get("message", "")))
        error["code"] = status_code
        error.setdefault("type", _http_exception_error_type(status_code))
        error.setdefault("param", None)
        return {**detail, "error": error}, status_code

    message = sanitize_error_message(detail if isinstance(detail, str) else str(detail))
    return (
        {
            "error": {
                "message": message,
                "type": _http_exception_error_type(status_code),
                "param": _validation_error_param(detail),
                "code": status_code,
            }
        },
        status_code,
    )


def _http_exception_error_type(status_code: int) -> str:
    """Match vLLM's HTTPException handler, including phrase spacing."""
    if status_code == 499:
        return "ClientClosedRequest"
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return f"HTTP{status_code}Error"


def vllm_stream_error_json(exc: Exception) -> str:
    """Serialize a stream failure using vLLM's streaming error envelope."""
    from fastapi import HTTPException
    from starlette.exceptions import HTTPException as StarletteHTTPException

    if isinstance(exc, (HTTPException, StarletteHTTPException)):
        payload, _ = _http_exception_error_payload(
            status_code=exc.status_code,
            detail=exc.detail,
        )
        return json.dumps(payload, separators=(",", ":"))
    error_response = _create_vllm_error_response(exc)
    return json.dumps(error_response.model_dump(), separators=(",", ":"))


def _validation_error_param(detail: Any) -> str | None:
    """Mirror vLLM validation_exception_handler param extraction.

    Prefer ``VLLMValidationError.parameter`` embedded in Pydantic ``ctx``, then
    fall back to a cleaned dotted ``loc`` path.
    """
    if not isinstance(detail, list):
        return None

    for error in detail:
        if not isinstance(error, dict):
            continue
        ctx = error.get("ctx")
        if isinstance(ctx, dict):
            ctx_error = ctx.get("error")
            parameter = getattr(ctx_error, "parameter", None)
            if parameter is not None:
                return parameter

    for error in detail:
        if not isinstance(error, dict):
            continue
        loc = error.get("loc")
        if isinstance(loc, tuple | list) and loc:
            try:
                from vllm.entrypoints.serve.utils.server_utils import clean_loc_for_param

                return clean_loc_for_param(tuple(loc))
            except (ImportError, AttributeError, TypeError):
                return ".".join(str(part) for part in loc)
    return None


def _validation_error_payload(errors: Any) -> dict[str, Any]:
    errors = errors if isinstance(errors, list) else []
    count = len(errors)
    label = "error" if count == 1 else "errors"
    if errors:
        message = f"{count} validation {label}:\n"
        message += "".join(f"  {error}\n" for error in errors).rstrip()
    else:
        message = "Validation error"
    return {
        "error": {
            "message": sanitize_error_message(message),
            "type": HTTPStatus.BAD_REQUEST.phrase,
            "code": HTTPStatus.BAD_REQUEST.value,
            "param": _validation_error_param(errors),
        }
    }


def register_vllm_openai_error_handlers(app: Any) -> None:
    """Register FastAPI handlers that mirror vLLM api_server OpenAI error responses."""
    from fastapi import HTTPException, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return openai_http_response_from_exception(exc)

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
        return openai_http_response_from_exception(exc)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            content=_validation_error_payload(exc.errors()),
            status_code=HTTPStatus.BAD_REQUEST.value,
        )


def openai_http_response_from_exception(exc: Exception) -> Any:
    """Map any engine exception to vLLM OpenAI ErrorResponse semantics."""
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse
    from starlette.exceptions import HTTPException as StarletteHTTPException

    if isinstance(exc, HTTPException):
        content, status_code = _http_exception_error_payload(
            status_code=exc.status_code,
            detail=exc.detail,
        )
        return JSONResponse(content=content, status_code=status_code, headers=exc.headers)
    if isinstance(exc, StarletteHTTPException):
        content, status_code = _http_exception_error_payload(
            status_code=exc.status_code,
            detail=exc.detail,
        )
        return JSONResponse(content=content, status_code=status_code, headers=exc.headers)

    return _vllm_error_response_to_json_response(_create_vllm_error_response(exc))


async def call_openai_serving(
    serving: Any,
    create_fn: Callable[[], Awaitable[Any]],
    json_response_type: type | tuple[type, ...],
) -> Any:
    """Invoke vLLM create_* and leave exception conversion to the adapter.

    vLLM methods commonly return ``ErrorResponse`` as a value.  Those values
    are rendered here; raised exceptions deliberately propagate so the
    endpoint has exactly one engine-specific conversion and logging point.
    """
    generator = await create_fn()
    return openai_http_response_from_generator(generator, json_response_type)


def kwargs_matching_signature(fn: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Drop keys not accepted by *fn* so one code path works across vLLM versions."""
    params = inspect.signature(fn).parameters
    return {k: v for k, v in kwargs.items() if k in params}


def vllm_openai_chat_needs_render() -> bool:
    from vllm.entrypoints.openai.chat_completion.serving import OpenAIServingChat

    return "openai_serving_render" in inspect.signature(OpenAIServingChat.__init__).parameters


def _first_existing_attr_value(target: Any, candidates: tuple[str, ...]) -> tuple[bool, Any | None]:
    for attr_name in candidates:
        if hasattr(target, attr_name):
            return True, getattr(target, attr_name)
    return False, None


def build_openai_serving_render_kwargs(
    ctor: Callable[..., Any],
    engine_client: Any,
    base_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Build ctor kwargs for OpenAIServingRender across vLLM API changes."""
    dynamic_candidates: dict[str, tuple[str, ...]] = {
        "model_config": ("model_config",),
        "renderer": ("renderer",),
        # vLLM 0.20+ may no longer expose io_processor on AsyncLLM.
        "io_processor": ("io_processor", "processor", "input_processor"),
    }

    dynamic_kwargs: dict[str, Any] = {}
    for kw_name, attr_candidates in dynamic_candidates.items():
        found, value = _first_existing_attr_value(engine_client, attr_candidates)
        # Keep kwargs when attribute exists even if value is None.
        if found:
            dynamic_kwargs[kw_name] = value

    merged_kwargs = {**base_kwargs, **dynamic_kwargs}
    ctor_kwargs = kwargs_matching_signature(ctor, merged_kwargs)

    signature = inspect.signature(ctor)
    required_kwargs = [
        name
        for name, param in signature.parameters.items()
        if name != "self"
        and param.default is inspect._empty
        and param.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    missing_required = [name for name in required_kwargs if name not in ctor_kwargs]
    if missing_required:
        raise RuntimeError(
            "Cannot initialize OpenAIServingRender due to missing required kwargs: "
            f"{missing_required}. This usually means the installed vLLM changed AsyncLLM "
            "attributes and the compatibility mapping needs updating."
        )

    return ctor_kwargs


def create_openai_serving_render(engine_client: Any, base_kwargs: dict[str, Any]) -> Any:
    from vllm.entrypoints.serve.render.serving import OpenAIServingRender

    ctor_kwargs = build_openai_serving_render_kwargs(
        OpenAIServingRender.__init__,
        engine_client,
        base_kwargs,
    )
    return OpenAIServingRender(**ctor_kwargs)
