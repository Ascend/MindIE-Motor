# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
#
# MindIE is licensed under both the Mulan PSL v2 and the Apache License, Version 2.0.

"""Helpers to support multiple vLLM OpenAI serving API shapes (with/without OpenAIServingRender)."""

from __future__ import annotations

import inspect
from typing import Any, Callable


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
