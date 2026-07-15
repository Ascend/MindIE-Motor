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

from typing import Any

from fastapi import Request
from vllm.entrypoints.openai.chat_completion.serving import OpenAIServingChat as VllmOpenAIServingChat
from vllm.engine.protocol import EngineClient
from vllm.entrypoints.openai.models.serving import OpenAIServingModels
from vllm.entrypoints.chat_utils import ChatTemplateContentFormatOption
from vllm.entrypoints.openai.chat_completion.protocol import ChatCompletionRequest, ChatCompletionResponse

from motor.engine_server.core.vllm.vllm_openai_compat import (
    RequestLogger,
    call_openai_serving,
    kwargs_matching_signature,
)
from motor.engine_server.core.vllm.prefill_context_validation import (
    activate_prefill_context_check,
    install_chat_render_validator,
    reset_prefill_context_check,
)


class OpenAIServingChat:
    def __init__(
        self,
        engine_client: EngineClient,
        models: OpenAIServingModels,
        response_role: str,
        *,
        request_logger: RequestLogger | None,
        chat_template: str | None,
        chat_template_content_format: ChatTemplateContentFormatOption,
        openai_serving_render: Any | None = None,
        trust_request_chat_template: bool = False,
        return_tokens_as_token_ids: bool = False,
        reasoning_parser: str = "",
        enable_auto_tools: bool = False,
        exclude_tools_when_tool_choice_none: bool = False,
        tool_parser: str | None = None,
        enable_prompt_tokens_details: bool = False,
        enable_force_include_usage: bool = False,
        enable_log_outputs: bool = False,
        enable_log_deltas: bool = True,
        default_chat_template_kwargs: dict[str, Any] | None = None,
    ) -> None:
        chat_kw: dict[str, Any] = {
            "request_logger": request_logger,
            "chat_template": chat_template,
            "chat_template_content_format": chat_template_content_format,
            "trust_request_chat_template": trust_request_chat_template,
            "return_tokens_as_token_ids": return_tokens_as_token_ids,
            "reasoning_parser": reasoning_parser,
            "enable_auto_tools": enable_auto_tools,
            "exclude_tools_when_tool_choice_none": exclude_tools_when_tool_choice_none,
            "tool_parser": tool_parser,
            "enable_prompt_tokens_details": enable_prompt_tokens_details,
            "enable_force_include_usage": enable_force_include_usage,
            "enable_log_outputs": enable_log_outputs,
            "enable_log_deltas": enable_log_deltas,
            "default_chat_template_kwargs": default_chat_template_kwargs,
        }
        if openai_serving_render is not None:
            chat_kw["openai_serving_render"] = openai_serving_render
        chat_kw = kwargs_matching_signature(VllmOpenAIServingChat.__init__, chat_kw)
        self._vllm_serving_chat = VllmOpenAIServingChat(
            engine_client,
            models,
            response_role,
            **chat_kw,
        )
        install_chat_render_validator(self._vllm_serving_chat)

    async def handle_request(self, request: ChatCompletionRequest, raw_request: Request):
        check = getattr(raw_request.state, "motor_prefill_context_check", None)
        token = activate_prefill_context_check(check)
        try:
            return await call_openai_serving(
                self._vllm_serving_chat,
                lambda: self._vllm_serving_chat.create_chat_completion(request, raw_request),
                ChatCompletionResponse,
            )
        finally:
            reset_prefill_context_check(token)
