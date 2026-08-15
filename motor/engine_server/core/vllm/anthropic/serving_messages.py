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

"""Composition wrapper around vLLM's AnthropicServingMessages."""

import inspect
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from vllm.engine.protocol import EngineClient
from vllm.entrypoints.anthropic.protocol import (
    AnthropicCountTokensRequest,
    AnthropicCountTokensResponse,
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
)
from vllm.entrypoints.anthropic.serving import AnthropicServingMessages as VllmAnthropicServingMessages
from vllm.entrypoints.chat_utils import ChatTemplateContentFormatOption
from vllm.entrypoints.openai.engine.protocol import ErrorResponse
from vllm.entrypoints.openai.models.serving import OpenAIServingModels

from motor.engine_server.core.anthropic.errors import anthropic_error_response
from motor.engine_server.core.vllm.prefill_context_validation import (
    activate_prefill_context_check,
    install_chat_render_validator,
    reset_prefill_context_check,
)
from motor.engine_server.core.vllm.vllm_openai_compat import (
    RequestLogger,
    kwargs_matching_signature,
)

# The deployed fork renamed the renderer kwarg from ``openai_serving_render``
# to ``online_renderer``; resolve whichever the installed fork accepts.
_RENDERER_KWARG_CANDIDATES = ("online_renderer", "openai_serving_render")


class AnthropicServingMessages:
    """Wrap vLLM AnthropicServingMessages behind the engine-server serving interface.

    Mirrors the OpenAI serving wrappers: constructor kwargs are filtered
    against the installed vLLM signature, and vLLM ``ErrorResponse`` values
    are rendered as Anthropic-envelope HTTP responses while raised exceptions
    propagate so the endpoint keeps a single conversion/logging point.
    """

    def __init__(
        self,
        engine_client: EngineClient,
        models: OpenAIServingModels,
        response_role: str,
        *,
        request_logger: RequestLogger | None,
        chat_template: str | None,
        chat_template_content_format: ChatTemplateContentFormatOption,
        renderer: Any | None = None,
        return_tokens_as_token_ids: bool = False,
        reasoning_parser: str = "",
        enable_auto_tools: bool = False,
        tool_parser: str | None = None,
        enable_prompt_tokens_details: bool = False,
        enable_force_include_usage: bool = False,
        default_chat_template_kwargs: dict[str, Any] | None = None,
    ) -> None:
        serving_kw: dict[str, Any] = {
            "request_logger": request_logger,
            "chat_template": chat_template,
            "chat_template_content_format": chat_template_content_format,
            "return_tokens_as_token_ids": return_tokens_as_token_ids,
            "reasoning_parser": reasoning_parser,
            "enable_auto_tools": enable_auto_tools,
            "tool_parser": tool_parser,
            "enable_prompt_tokens_details": enable_prompt_tokens_details,
            "enable_force_include_usage": enable_force_include_usage,
            "default_chat_template_kwargs": default_chat_template_kwargs,
        }
        parameters = inspect.signature(VllmAnthropicServingMessages.__init__).parameters
        renderer_kwarg = next((name for name in _RENDERER_KWARG_CANDIDATES if name in parameters), None)
        if renderer_kwarg is not None:
            if renderer is None:
                raise RuntimeError(
                    f"Installed vLLM AnthropicServingMessages requires `{renderer_kwarg}`; "
                    "the renderer compatibility layer failed to provide one."
                )
            serving_kw[renderer_kwarg] = renderer
        serving_kw = kwargs_matching_signature(VllmAnthropicServingMessages.__init__, serving_kw)
        self._vllm_serving_messages = VllmAnthropicServingMessages(
            engine_client,
            models,
            response_role,
            **serving_kw,
        )
        install_chat_render_validator(self._vllm_serving_messages)

    async def handle_request(self, request: AnthropicMessagesRequest, raw_request: Request):
        """Serve a Messages request; returns a JSON or SSE-streaming response."""
        check = getattr(raw_request.state, "motor_prefill_context_check", None)
        token = activate_prefill_context_check(check)
        try:
            result = await self._vllm_serving_messages.create_messages(request, raw_request)
        finally:
            reset_prefill_context_check(token)
        if isinstance(result, ErrorResponse):
            return anthropic_error_response(
                status_code=result.error.code,
                message=result.error.message,
            )
        if isinstance(result, AnthropicMessagesResponse):
            # Mirror the fork's api_router serialization: unset (e.g.
            # recompute-rewritten) fields are dropped; the endpoint's
            # normalization restores a spec-compliant stop_reason.
            return JSONResponse(content=result.model_dump(exclude_none=True))
        return StreamingResponse(content=result, media_type="text/event-stream")

    async def count_tokens(self, request: AnthropicCountTokensRequest, raw_request: Request):
        """Tokenize the request messages and return the Anthropic token count."""
        result = await self._vllm_serving_messages.count_tokens(request, raw_request)
        if isinstance(result, ErrorResponse):
            return anthropic_error_response(
                status_code=result.error.code,
                message=result.error.message,
            )
        if isinstance(result, AnthropicCountTokensResponse):
            return JSONResponse(content=result.model_dump(exclude_none=True))
        return result
