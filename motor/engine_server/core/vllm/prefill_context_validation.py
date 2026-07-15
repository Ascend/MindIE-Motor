# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may obtain a copy of the License at:
#         http://license.coscl.org.cn/MulanPSL2

"""Post-tokenization validation for vLLM handoff-prefill requests.

The P leg of a handoff request is intentionally changed to a one-token
generation. The Coordinator carries the client's original output budget in
``_motor_dispatch``; store it in a request-local context so it can still be
checked after vLLM has rendered and tokenized the prompt, but before it
submits work to the engine.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Callable


_PREFILL_CONTEXT_CHECK: ContextVar["PrefillContextCheck | None"] = ContextVar(
    "motor_prefill_context_check", default=None
)


@dataclass(frozen=True)
class PrefillContextCheck:
    """The client output budget that must be checked on a handoff P leg."""

    max_output_tokens: int
    parameter: str


def activate_prefill_context_check(check: PrefillContextCheck | None) -> Token:
    return _PREFILL_CONTEXT_CHECK.set(check)


def reset_prefill_context_check(token: Token) -> None:
    _PREFILL_CONTEXT_CHECK.reset(token)


def validate_tokenized_prompts(serving: Any, engine_prompts: list[Any]) -> str | None:
    """Return a client-facing error after vLLM has produced prompt token IDs."""
    check = _PREFILL_CONTEXT_CHECK.get()
    if check is None:
        return None

    max_model_len = getattr(getattr(serving, "model_config", None), "max_model_len", None)
    extract_prompt_len: Callable[[Any], int] | None = getattr(serving, "_extract_prompt_len", None)
    if not isinstance(max_model_len, int) or max_model_len <= 0 or not callable(extract_prompt_len):
        # Do not reject a request if a future vLLM API no longer exposes the
        # tokenized prompt length. Native vLLM validation remains the fallback.
        return None

    for engine_prompt in engine_prompts:
        prompt_tokens = extract_prompt_len(engine_prompt)
        if prompt_tokens + check.max_output_tokens > max_model_len:
            return (
                f"Requested {check.parameter} ({check.max_output_tokens}) plus prompt length "
                f"({prompt_tokens}) exceeds the model context length ({max_model_len})."
            )
    return None


def install_chat_render_validator(serving: Any) -> None:
    """Validate a chat request immediately after vLLM renders/tokenizes it."""
    original_render = serving.render_chat_request

    async def render_chat_request(request: Any) -> Any:
        result = await original_render(request)
        if not isinstance(result, tuple) or len(result) != 2:
            return result
        error = validate_tokenized_prompts(serving, result[1])
        if error is None:
            return result
        check = _PREFILL_CONTEXT_CHECK.get()
        return serving.create_error_response(error, param=check.parameter if check is not None else None)

    serving.render_chat_request = render_chat_request


def install_completion_render_validator(serving: Any) -> None:
    """Validate a completion request immediately after vLLM tokenizes it."""
    original_render = serving.render_completion_request

    async def render_completion_request(request: Any) -> Any:
        result = await original_render(request)
        if not isinstance(result, list):
            return result
        error = validate_tokenized_prompts(serving, result)
        if error is None:
            return result
        check = _PREFILL_CONTEXT_CHECK.get()
        return serving.create_error_response(error, param=check.parameter if check is not None else None)

    serving.render_completion_request = render_completion_request
