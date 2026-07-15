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
from vllm.entrypoints.openai.completion.serving import OpenAIServingCompletion as VllmOpenAIServingCompletion
from vllm.engine.protocol import EngineClient
from vllm.entrypoints.openai.models.serving import OpenAIServingModels
from vllm.entrypoints.openai.completion.protocol import CompletionRequest, CompletionResponse

from motor.engine_server.core.vllm.vllm_openai_compat import (
    RequestLogger,
    call_openai_serving,
    kwargs_matching_signature,
)
from motor.engine_server.core.vllm.prefill_context_validation import (
    activate_prefill_context_check,
    install_completion_render_validator,
    reset_prefill_context_check,
)


class OpenAIServingCompletion:
    def __init__(
        self,
        engine_client: EngineClient,
        models: OpenAIServingModels,
        *,
        request_logger: RequestLogger | None,
        return_tokens_as_token_ids: bool = False,
        enable_prompt_tokens_details: bool = False,
        enable_force_include_usage: bool = False,
        openai_serving_render: Any | None = None,
    ):
        comp_kw: dict[str, Any] = {
            "request_logger": request_logger,
            "return_tokens_as_token_ids": return_tokens_as_token_ids,
            "enable_prompt_tokens_details": enable_prompt_tokens_details,
            "enable_force_include_usage": enable_force_include_usage,
        }
        if openai_serving_render is not None:
            comp_kw["openai_serving_render"] = openai_serving_render
        comp_kw = kwargs_matching_signature(VllmOpenAIServingCompletion.__init__, comp_kw)
        self._vllm_serving_completion = VllmOpenAIServingCompletion(
            engine_client,
            models,
            **comp_kw,
        )
        install_completion_render_validator(self._vllm_serving_completion)

    async def handle_request(self, request: CompletionRequest, raw_request: Request):
        check = getattr(raw_request.state, "motor_prefill_context_check", None)
        token = activate_prefill_context_check(check)
        try:
            return await call_openai_serving(
                self._vllm_serving_completion,
                lambda: self._vllm_serving_completion.create_completion(request, raw_request),
                CompletionResponse,
            )
        finally:
            reset_prefill_context_check(token)
