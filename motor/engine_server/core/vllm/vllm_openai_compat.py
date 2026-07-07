# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
#
# MindIE is licensed under both the Mulan PSL v2 and the Apache License, Version 2.0.
# Portions derived from vLLM are licensed under the Apache License, Version 2.0.
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Helpers to support multiple vLLM OpenAI serving API shapes (with/without OpenAIServingRender)."""

from __future__ import annotations

import inspect
from importlib import import_module
from typing import Any, Callable


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
    "openai_http_response_from_generator",
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
