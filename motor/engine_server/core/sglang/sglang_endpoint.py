# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from sglang.srt.entrypoints.openai.protocol import ChatCompletionRequest
from sglang.srt.entrypoints.openai.protocol import CompletionRequest

from motor.common.logger import get_logger
from motor.engine_server.core.infer_endpoint import InferEndpoint, CONFIG_KEY
from motor.engine_server.core.sglang.sglang_engine import SGLangEngine
from motor.engine_server.core.sglang.openai.serving_chat import OpenAIServingChat
from motor.engine_server.core.sglang.openai.serving_completion import OpenAIServingCompletion
from motor.engine_server.core.sglang.openai.serving_models import OpenAIServingModels

logger = get_logger(__name__)


async def _sglang_health_checker() -> bool:
    """SGLang has no separate engine client; engine runs in same process."""
    return True


@asynccontextmanager
async def _sglang_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Set app.state in lifespan (same process as request handling)."""
    config = app.extra.get(CONFIG_KEY)
    engine = SGLangEngine(config)
    (tokenizer_manager, template_manager) = engine.launch()
    if not tokenizer_manager or not template_manager:
        raise ValueError("SGLang lifespan: tokenizer_manager or template_manager not found.")
    logger.info("InferEndpoint lifespan: SGLang serving components created.")

    app.state.health_checker = _sglang_health_checker
    app.state.tokenizer_manager = tokenizer_manager
    app.state.openai_serving_models = OpenAIServingModels(
        tokenizer_manager=tokenizer_manager,
    )
    app.state.openai_serving_chat = OpenAIServingChat(
        tokenizer_manager=tokenizer_manager,
        template_manager=template_manager,
    )
    app.state.openai_serving_completion = OpenAIServingCompletion(
        tokenizer_manager=tokenizer_manager,
        template_manager=template_manager,
    )
    logger.info("InferEndpoint: SGLang serving components created.")
    yield
    engine.shutdown()
    logger.info("InferEndpoint lifespan: SGLang serving components shutdown.")


class SGLangEndpoint(InferEndpoint):
    def get_lifespan(self):
        return _sglang_lifespan

    def init_request_handlers(self) -> None:
        self.chat_completion_request = ChatCompletionRequest
        self.completion_request = CompletionRequest
