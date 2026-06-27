# Copyright 2023-2024 SGLang Team
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

"""OpenAI Completions serving for SGLang engine (EngineServer openai/sglang)."""

from typing import TYPE_CHECKING

from fastapi import Request

from sglang.srt.entrypoints.openai.serving_completions import (
    OpenAIServingCompletion as SglangOpenAIServingCompletion,
)
from sglang.srt.entrypoints.openai.protocol import CompletionRequest

if TYPE_CHECKING:
    from sglang.srt.managers.template_manager import TemplateManager
    from sglang.srt.managers.tokenizer_manager import TokenizerManager


class OpenAIServingCompletion:
    """EngineServer wrapper for SGLang OpenAI Completions; same handle_request contract as vllm."""

    def __init__(
        self,
        tokenizer_manager: "TokenizerManager",
        template_manager: "TemplateManager",
    ) -> None:
        self._sglang_serving_completion = SglangOpenAIServingCompletion(tokenizer_manager, template_manager)

    async def handle_request(self, request: CompletionRequest, raw_request: Request):
        return await self._sglang_serving_completion.handle_request(request, raw_request)
