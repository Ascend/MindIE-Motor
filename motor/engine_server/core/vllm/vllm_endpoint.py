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

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from vllm import envs
from vllm.entrypoints.chat_utils import load_chat_template
from vllm.entrypoints.openai.chat_completion.protocol import ChatCompletionRequest
from vllm.entrypoints.openai.completion.protocol import CompletionRequest
from vllm.entrypoints.openai.models.protocol import BaseModelPath
from vllm.entrypoints.openai.models.serving import OpenAIServingModels

from motor.common.logger import get_logger
from motor.engine_server.core.infer_endpoint import InferEndpoint, CONFIG_KEY
from motor.engine_server.core.vllm.vllm_engine import VLLMEngine
from motor.engine_server.core.vllm.openai.serving_chat import OpenAIServingChat
from motor.engine_server.core.vllm.openai.serving_completion import OpenAIServingCompletion
from motor.engine_server.core.vllm.vllm_openai_compat import (
    RequestLogger,
    create_openai_serving_render,
    process_lora_modules,
    vllm_openai_chat_needs_render,
)

logger = get_logger(__name__)

# argparse / EngineArgs field names (optional; getattr for older vLLM)
ATTR_DEFAULT_CHAT_TEMPLATE_KWARGS = "default_chat_template_kwargs"
ATTR_ENABLE_AUTO_TOOL_CHOICE = "enable_auto_tool_choice"
ATTR_EXCLUDE_TOOLS_WHEN_TOOL_CHOICE_NONE = "exclude_tools_when_tool_choice_none"
ATTR_TOOL_CALL_PARSER = "tool_call_parser"
ATTR_TRUST_REQUEST_CHAT_TEMPLATE = "trust_request_chat_template"


@asynccontextmanager
async def _vllm_lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Set app.state in lifespan (runs in the process that serves requests)."""
    config = app.extra.get(CONFIG_KEY)
    if not config:
        raise ValueError("VLLM lifespan: app.extra[CONFIG_KEY] not set (init_request_handlers not called).")
    args = config.get_args()
    # vLLM ParserManager.get_tool_parser only loads a parser when both
    # enable_auto_tool_choice and tool_call_parser are set; mirror common
    # --tool-call-parser-only usage so model output is parsed into tool_calls.
    if getattr(args, ATTR_TOOL_CALL_PARSER, None) and not getattr(args, ATTR_ENABLE_AUTO_TOOL_CHOICE, False):
        setattr(args, ATTR_ENABLE_AUTO_TOOL_CHOICE, True)
        logger.info(
            "tool_call_parser is set but enable_auto_tool_choice was false; "
            "enabling enable_auto_tool_choice so chat completions parse tool calls."
        )

    engine = VLLMEngine(config)
    engine_client = engine.launch()
    if engine_client is None:
        raise ValueError("VLLM lifespan: engine_client not found.")
    logger.info("InferEndpoint lifespan: Initializing engine_client...")

    # Headless PCP follower: no engine core, only MultiprocExecutor workers.
    # Check actual worker process liveness instead of returning True unconditionally.
    headless_follower = engine.is_headless_follower()
    if headless_follower:
        logger.info("InferEndpoint lifespan: headless follower mode, skipping API server setup.")
        app.state.engine_client = engine_client  # MultiprocExecutor, not AsyncLLM

        async def headless_health_checker() -> bool:
            executor = app.state.engine_client
            if not hasattr(executor, "workers"):
                return False
            return all(worker.proc.is_alive() for worker in executor.workers)

        app.state.health_checker = headless_health_checker

        try:
            yield
        finally:
            engine.shutdown()
        logger.info("InferEndpoint lifespan: headless follower cleanup completed")
        return

    try:
        await engine_client.reset_mm_cache()
        logger.info("InferEndpoint lifespan: Engine_client initialized successfully")

        async def vllm_health_checker() -> bool:
            try:
                await engine_client.check_health()
                return True
            except Exception:
                logger.error("VLLM health check failed")
                return False

        app.state.health_checker = vllm_health_checker
        app.state.engine_client = engine_client
        app.state.log_stats = not getattr(args, "disable_log_stats", False)

        supported_tasks = await engine_client.get_supported_tasks()
        resolved_chat_template = load_chat_template(args.chat_template)
        vllm_config = engine_client.vllm_config

        default_mm_loras = vllm_config.lora_config.default_mm_loras if vllm_config.lora_config is not None else {}
        lora_modules = process_lora_modules(args.lora_modules, default_mm_loras)

        if args.served_model_name is not None:
            served_model_names = args.served_model_name
        else:
            served_model_names = [args.model]

        if args.enable_log_requests:
            request_logger = RequestLogger(max_log_len=args.max_log_len)
        else:
            request_logger = None

        base_model_paths = [BaseModelPath(name=name, model_path=args.model) for name in served_model_names]

        openai_serving_models = OpenAIServingModels(
            engine_client=engine_client,
            base_model_paths=base_model_paths,
            lora_modules=lora_modules,
        )
        app.state.openai_serving_models = openai_serving_models

        openai_serving_render = None
        if vllm_openai_chat_needs_render():
            try:
                openai_serving_render = create_openai_serving_render(
                    engine_client,
                    {
                        "model_registry": openai_serving_models.registry,
                        "request_logger": request_logger,
                        "chat_template": resolved_chat_template,
                        "chat_template_content_format": args.chat_template_content_format,
                        ATTR_TRUST_REQUEST_CHAT_TEMPLATE: getattr(args, ATTR_TRUST_REQUEST_CHAT_TEMPLATE, False),
                        "enable_auto_tools": getattr(args, ATTR_ENABLE_AUTO_TOOL_CHOICE, False),
                        ATTR_EXCLUDE_TOOLS_WHEN_TOOL_CHOICE_NONE: getattr(
                            args, ATTR_EXCLUDE_TOOLS_WHEN_TOOL_CHOICE_NONE, False
                        ),
                        "tool_parser": getattr(args, ATTR_TOOL_CALL_PARSER, None),
                        ATTR_DEFAULT_CHAT_TEMPLATE_KWARGS: getattr(args, ATTR_DEFAULT_CHAT_TEMPLATE_KWARGS, None),
                        "log_error_stack": getattr(args, "log_error_stack", False),
                    },
                )
            except ImportError as e:
                raise RuntimeError(
                    "Installed vLLM expects OpenAIServingRender (chat serving API); "
                    "use a complete matching vLLM build or an older vLLM without the render layer."
                ) from e
            except RuntimeError as e:
                logger.warning(
                    "InferEndpoint lifespan: Failed to initialize OpenAIServingRender, "
                    "continue without render compatibility layer: %s",
                    e,
                )

        try:
            app.state.openai_serving_chat = (
                OpenAIServingChat(
                    engine_client=engine_client,
                    models=openai_serving_models,
                    response_role=args.response_role,
                    request_logger=request_logger,
                    chat_template=resolved_chat_template,
                    chat_template_content_format=args.chat_template_content_format,
                    openai_serving_render=openai_serving_render,
                    trust_request_chat_template=getattr(args, ATTR_TRUST_REQUEST_CHAT_TEMPLATE, False),
                    return_tokens_as_token_ids=getattr(args, "return_tokens_as_token_ids", False),
                    reasoning_parser=getattr(args, "reasoning_parser", ""),
                    enable_auto_tools=getattr(args, ATTR_ENABLE_AUTO_TOOL_CHOICE, False),
                    exclude_tools_when_tool_choice_none=getattr(args, ATTR_EXCLUDE_TOOLS_WHEN_TOOL_CHOICE_NONE, False),
                    tool_parser=getattr(args, ATTR_TOOL_CALL_PARSER, None),
                    enable_prompt_tokens_details=getattr(args, "enable_prompt_tokens_details", False),
                    enable_force_include_usage=getattr(args, "enable_force_include_usage", False),
                    enable_log_outputs=getattr(args, "enable_log_outputs", False),
                    enable_log_deltas=getattr(args, "enable_log_deltas", True),
                    default_chat_template_kwargs=getattr(args, ATTR_DEFAULT_CHAT_TEMPLATE_KWARGS, None),
                )
                if "generate" in supported_tasks
                else None
            )

            app.state.openai_serving_completion = (
                OpenAIServingCompletion(
                    engine_client=engine_client,
                    models=openai_serving_models,
                    request_logger=request_logger,
                    return_tokens_as_token_ids=getattr(args, "return_tokens_as_token_ids", False),
                    enable_prompt_tokens_details=getattr(args, "enable_prompt_tokens_details", False),
                    enable_force_include_usage=getattr(args, "enable_force_include_usage", False),
                    openai_serving_render=openai_serving_render,
                )
                if "generate" in supported_tasks
                else None
            )

            logger.info("InferEndpoint lifespan: Serving components created successfully")
        except Exception as e:
            logger.error("InferEndpoint lifespan: Failed to create serving components: %s", e)
            raise

        log_stats_task: asyncio.Task[None] | None = None
        if app.state.log_stats:
            ec = engine_client

            async def _force_log_stats():
                while True:
                    await asyncio.sleep(envs.VLLM_LOG_STATS_INTERVAL)
                    await ec.do_log_stats()

            log_stats_task = asyncio.create_task(_force_log_stats())

        try:
            yield
        finally:
            if log_stats_task is not None:
                log_stats_task.cancel()

        engine.shutdown()
        logger.info("InferEndpoint lifespan: Engine_client cleanup completed")
    except Exception as e:
        logger.error("InferEndpoint lifespan: Failed to initialize or manage engine_client: %s", e)
        raise


class VLLMEndpoint(InferEndpoint):
    def get_lifespan(self):
        return _vllm_lifespan

    def init_request_handlers(self) -> None:
        self.chat_completion_request = ChatCompletionRequest
        self.completion_request = CompletionRequest
