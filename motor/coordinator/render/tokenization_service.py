# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Render-first tokenization with the existing Coordinator tokenizer as fallback."""

import time
from typing import Any, Protocol

from fastapi import HTTPException

from motor.common.logger import get_logger
from motor.config.coordinator import CONTEXT_BUDGET_OFF, CONTEXT_BUDGET_ON, RenderConfig
from motor.coordinator.render.api_spec import RenderApiSpec, get_render_api_spec
from motor.coordinator.render.image_obfuscation_service import ImageObfuscationError, ImageObfuscationService
from motor.coordinator.render.models import TokenizedRequest, TokenizerSource
from motor.coordinator.render.token_obfuscation_service import TokenObfuscationError, TokenObfuscationService
from motor.coordinator.render.vllm_render_client import (
    RenderClientError,
    RenderRequestError,
    RenderUnavailableError,
    VLLMRenderClient,
)

logger = get_logger(__name__)

_MESSAGES = "messages"
_FEATURES = "features"
_MAX_COMPLETION_TOKENS = "max_completion_tokens"
_MAX_TOKENS = "max_tokens"
_MODEL = "model"
_PROMPT = "prompt"
_TEMPERATURE = "temperature"
_TOOLS = "tools"


class LocalTokenizer(Protocol):
    """Existing Coordinator tokenizer operations used by the fallback path."""

    def apply_chat_template(
        self,
        messages: list,
        tools: list | None = None,
        req_data: dict | None = None,
    ) -> list[int]: ...

    def encode(self, prompt: str) -> list[int]: ...


class TokenizationService:
    """Produce prompt token IDs without coupling callers to vLLM response shapes."""

    def __init__(
        self,
        config: RenderConfig,
        render_client: VLLMRenderClient | None = None,
        local_tokenizer: LocalTokenizer | None = None,
        *,
        context_budget_mode: str = CONTEXT_BUDGET_OFF,
        obfuscation_service: TokenObfuscationService | None = None,
        image_obfuscation_service: ImageObfuscationService | None = None,
    ) -> None:
        self._config = config
        self._render_client = render_client
        self._context_budget_enabled = context_budget_mode == CONTEXT_BUDGET_ON
        self._obfuscation_service = obfuscation_service
        self._image_obfuscation_service = image_obfuscation_service
        if local_tokenizer is None:
            from motor.coordinator.scheduler.policy.kv_cache_affinity import (
                TokenizerManager,
            )

            local_tokenizer = TokenizerManager()
        self._local_tokenizer = local_tokenizer

    @property
    def render_client(self) -> VLLMRenderClient | None:
        """Return the sidecar client shared by Render and Derender."""
        return self._render_client

    async def tokenize(
        self,
        request_id: str,
        api: str,
        request_data: dict[str, Any],
    ) -> list[TokenizedRequest] | None:
        """Prefer Render and fall back without making token metadata mandatory."""
        spec = get_render_api_spec(api)
        render_reason = ""
        streaming_render_disabled = request_data.get("stream", False) and not self._config.enable_streaming
        use_render = self._config.enable and not streaming_render_disabled
        if use_render and spec is not None:
            start = time.perf_counter()
            try:
                if self._render_client is None:
                    raise RenderUnavailableError("Render client is not initialized")
                self._apply_render_chat_template_defaults(request_data)
                output_budget = self._select_output_budget(spec, request_data)
                render_request = self._prepare_render_request(request_data, output_budget)
                result = await self._render_client.render(api, render_request)
                self._obfuscate(result)
                obfuscated_images = self._obfuscate_images(result)
                latency_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    "render tokenize success request_id=%s model=%s prompt_length=%d "
                    "latency_ms=%.2f tokenizer_source=%s obfuscated_images=%d",
                    request_id,
                    request_data.get(_MODEL, ""),
                    sum(len(item.prompt_token_ids) for item in result),
                    latency_ms,
                    result[0].tokenizer_source.value,
                    obfuscated_images,
                )

                return result
            except RenderRequestError as e:
                raise HTTPException(status_code=e.status_code, detail=e.detail) from e
            except RenderClientError as e:
                latency_ms = (time.perf_counter() - start) * 1000
                render_reason = str(e)
                logger.warning(
                    "render unavailable, fallback local tokenizer request_id=%s model=%s "
                    "reason=%s latency_ms=%.2f fallback=local_tokenizer",
                    request_id,
                    request_data.get(_MODEL, ""),
                    render_reason,
                    latency_ms,
                )
                if self._obfuscation_service is not None:
                    raise TokenObfuscationError("Render is required for token-obfuscated inference") from e
                if self._image_obfuscation_service is not None:
                    raise ImageObfuscationError("Render is required for image-obfuscated inference") from e

        if streaming_render_disabled:
            if self._obfuscation_service is not None:
                raise TokenObfuscationError("Streaming Render is required for token-obfuscated inference")
            if self._image_obfuscation_service is not None:
                raise ImageObfuscationError("Streaming Render is required for image-obfuscated inference")

        try:
            result = self._tokenize_local(request_data)
        except Exception as e:
            logger.warning(
                "request tokenization unavailable, continue without token metadata "
                "request_id=%s model=%s render_reason=%s local_reason=%s",
                request_id,
                request_data.get(_MODEL, ""),
                render_reason,
                type(e).__name__,
            )
            return None

        if result is None:
            logger.warning(
                "request tokenization unavailable, continue without token metadata "
                "request_id=%s model=%s render_reason=%s local_reason=empty_token_ids",
                request_id,
                request_data.get(_MODEL, ""),
                render_reason,
            )
            return None

        logger.info(
            "local tokenize success request_id=%s model=%s prompt_length=%d tokenizer_source=%s",
            request_id,
            request_data.get(_MODEL, ""),
            sum(len(item.prompt_token_ids) for item in result),
            result[0].tokenizer_source.value,
        )
        return result

    def _tokenize_local(self, request_data: dict[str, Any]) -> list[TokenizedRequest] | None:
        messages = request_data.get(_MESSAGES)
        if messages is not None:
            token_ids = self._local_tokenizer.apply_chat_template(
                messages,
                request_data.get(_TOOLS),
                req_data=request_data,
            )
        else:
            prompt = request_data.get(_PROMPT)
            if isinstance(prompt, list) and all(isinstance(token_id, int) for token_id in prompt):
                token_ids = prompt
            elif isinstance(prompt, list):
                tokenized_prompts = []
                for item in prompt:
                    if isinstance(item, str):
                        item_token_ids = self._local_tokenizer.encode(item)
                    elif isinstance(item, list):
                        item_token_ids = item
                    else:
                        return None
                    tokenized_prompts.append(
                        TokenizedRequest(
                            prompt_token_ids=item_token_ids,
                            tokenizer_source=TokenizerSource.LOCAL,
                        )
                    )
                return tokenized_prompts or None
            elif isinstance(prompt, str):
                token_ids = self._local_tokenizer.encode(prompt)
            else:
                return None

        if not token_ids:
            return None
        return [
            TokenizedRequest(
                prompt_token_ids=token_ids,
                tokenizer_source=TokenizerSource.LOCAL,
            )
        ]

    def _obfuscate(self, results: list[TokenizedRequest]) -> None:
        if self._obfuscation_service is None:
            return
        for result in results:
            result.engine_prompt_token_ids = self._obfuscation_service.obfuscate(result.prompt_token_ids)

    def _obfuscate_images(self, results: list[TokenizedRequest]) -> int:
        """Permute the Render image tensors the engine is about to consume."""
        service = self._image_obfuscation_service
        if service is None:
            return 0
        replaced = 0
        for result in results:
            features = result.metadata.get(_FEATURES)
            if not isinstance(features, dict):
                continue
            replaced += service.obfuscate_render_features(features)
        return replaced

    def _apply_render_chat_template_defaults(self, request_data: dict[str, Any]) -> None:
        """Map OpenAI thinking fields into chat_template_kwargs for Render and Derender."""
        if _MESSAGES not in request_data or self._obfuscation_service is None:
            return

        configured = request_data.get("chat_template_kwargs")
        if configured is not None and not isinstance(configured, dict):
            # Preserve malformed input so the Render endpoint remains responsible for validation.
            return
        if isinstance(configured, dict) and "enable_thinking" in configured:
            return

        template_kwargs = dict(configured or {})
        thinking = request_data.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") in {"enabled", "disabled"}:
            template_kwargs["enable_thinking"] = thinking["type"] == "enabled"
        elif request_data.get("reasoning_effort") is not None:
            template_kwargs["enable_thinking"] = request_data["reasoning_effort"] != "none"
        else:
            return
        request_data["chat_template_kwargs"] = template_kwargs

    def _select_output_budget(
        self,
        spec: RenderApiSpec,
        request_data: dict[str, Any],
    ) -> tuple[str, int] | None:
        if not self._context_budget_enabled:
            return None

        for parameter in spec.output_budget_fields:
            requested_tokens = request_data.get(parameter)
            if isinstance(requested_tokens, int) and not isinstance(requested_tokens, bool) and requested_tokens > 0:
                return str(parameter), requested_tokens
        return None

    @staticmethod
    def _prepare_render_request(
        request_data: dict[str, Any],
        output_budget: tuple[str, int] | None,
    ) -> dict[str, Any]:
        if output_budget is None:
            return request_data
        parameter, _ = output_budget
        render_request = request_data.copy()
        render_request[parameter] = 1
        return render_request

    def sync_sampling_params(
        self,
        request_data: dict[str, Any],
        results: list[TokenizedRequest],
    ) -> None:
        """Keep Render metadata aligned with the request after Coordinator budget adaptation."""
        parameters = (_MAX_COMPLETION_TOKENS, _MAX_TOKENS) if _MESSAGES in request_data else (_MAX_TOKENS,)
        effective_tokens = None
        for parameter in parameters:
            value = request_data.get(parameter)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                effective_tokens = value
                break

        for result in results:
            sampling_params = result.metadata.get("sampling_params")
            if not isinstance(sampling_params, dict):
                continue
            if effective_tokens is not None:
                sampling_params[_MAX_TOKENS] = effective_tokens
            self._guard_obfuscated_thinking_sampling(request_data, sampling_params)

    @staticmethod
    def _thinking_enabled(request_data: dict[str, Any]) -> bool:
        configured = request_data.get("chat_template_kwargs")
        if isinstance(configured, dict) and "enable_thinking" in configured:
            return bool(configured["enable_thinking"])

        thinking = request_data.get("thinking")
        if isinstance(thinking, dict):
            if thinking.get("type") == "enabled":
                return True
            if thinking.get("type") == "disabled":
                return False

        reasoning_effort = request_data.get("reasoning_effort")
        if reasoning_effort is not None:
            return reasoning_effort != "none"
        return False

    def _guard_obfuscated_thinking_sampling(
        self,
        request_data: dict[str, Any],
        sampling_params: dict[str, Any],
    ) -> None:
        """Obfuscated weights need greedy decoding to keep thinking tokens stable."""
        if self._obfuscation_service is None or not self._thinking_enabled(request_data):
            return
        if _TEMPERATURE in request_data:
            return
        sampling_params[_TEMPERATURE] = 0.0
