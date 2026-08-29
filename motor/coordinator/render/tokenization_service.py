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

from motor.common.logger import get_logger
from motor.config.coordinator import CONTEXT_BUDGET_OFF, CONTEXT_BUDGET_ON, RenderConfig
from motor.coordinator.render.api_spec import RenderApiSpec, get_render_api_spec
from motor.coordinator.render.models import TokenizedRequest, TokenizerSource
from motor.coordinator.render.vllm_render_client import (
    RenderClientError,
    RenderUnavailableError,
    VLLMRenderClient,
)

logger = get_logger(__name__)

_MESSAGES = "messages"
_MAX_COMPLETION_TOKENS = "max_completion_tokens"
_MAX_TOKENS = "max_tokens"
_MODEL = "model"
_PROMPT = "prompt"
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
    ) -> None:
        self._config = config
        self._render_client = render_client
        self._context_budget_enabled = context_budget_mode == CONTEXT_BUDGET_ON
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
        if self._config.enabled and spec is not None:
            start = time.perf_counter()
            try:
                if self._render_client is None:
                    raise RenderUnavailableError("Render client is not initialized")
                output_budget = self._select_output_budget(spec, request_data)
                render_request = self._prepare_render_request(request_data, output_budget)
                result = await self._render_client.render(api, render_request)
                latency_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    "render tokenize success request_id=%s model=%s prompt_length=%d "
                    "latency_ms=%.2f tokenizer_source=%s",
                    request_id,
                    request_data.get(_MODEL, ""),
                    sum(len(item.prompt_token_ids) for item in result),
                    latency_ms,
                    result[0].tokenizer_source.value,
                )

                return result
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

    @staticmethod
    def sync_sampling_params(
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
        if effective_tokens is None:
            return

        for result in results:
            sampling_params = result.metadata.get("sampling_params")
            if isinstance(sampling_params, dict):
                sampling_params[_MAX_TOKENS] = effective_tokens
