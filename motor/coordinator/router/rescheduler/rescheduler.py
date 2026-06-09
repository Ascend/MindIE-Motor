# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from typing import Any

from fastapi import HTTPException, status

from motor.coordinator.models import RequestInfo
from motor.coordinator.models.constants import OpenAIField
from motor.coordinator.router.adapters.completion_to_chat import (
    adapt_completion_stream_chunk_to_chat,
    is_completion_like_stream_chunk,
)

from motor.coordinator.router.adapters.stream import (
    parse_stream_chunk_json,
    strip_openai_token_id_fields_for_client,
    encode_stream_chunk_bytes,
)

# Chat-only fields removed when switching recompute retry to OpenAI Completions body.
_CHAT_ONLY_KEYS_RECOMPUTE = frozenset(
    {
        OpenAIField.MESSAGES,
        "tools",
        "tool_choice",
        "functions",
        "function_call",
        "modalities",
        "parallel_tool_calls",
        "response_format",
        "reasoning_effort",
        "include_reasoning",
        "audio",
        "metadata",
    }
)


class Rescheduler:
    """Normalize decode responses and build Completions-style retry bodies after recompute."""

    def __init__(self, enable, req: RequestInfo, logger):
        # settings
        self.enable = enable
        self.req = req
        # variables
        self.is_rescheduling = False
        self.retry_count = 0
        # functions
        self.logger = logger

    def process_stream_chunk(
        self,
        chunk: bytes,
        *,
        stream_adapter_state: dict[str, Any] | None = None,
    ) -> bytes:
        """Process one decode stream chunk.

        When ``enable`` is true and ``stop_reason`` is ``recomputed``, returns ``None`` so
        the router can reschedule. When recompute is disabled, recomputed chunks are still
        forwarded to the client (with ``recomputed`` mapped to ``stop``).

        Returns:
            Bytes to forward to the client
        """
        chunk_json = parse_stream_chunk_json(chunk, self.logger)
        if chunk_json is None:
            try:
                text = chunk.decode("utf-8", errors="replace").strip()
            except Exception:
                text = ""
            if "[DONE]" in text:
                return chunk
            if self.logger is not None:
                self.logger.debug("Dropping non-JSON decode stream chunk (Coordinator safety)")
            return b""

        if self.req.prompt_tokens_details:
            if chunk_json.get(OpenAIField.USAGE, {}):
                chunk_json[OpenAIField.USAGE]["prompt_tokens_details"] = self.req.prompt_tokens_details

        if self.enable:
            self.req.update_token_id_cache(chunk_json)

        sta = stream_adapter_state if stream_adapter_state is not None else {}
        # Chat clients expect chat.completion.chunk; adapt Completion-shaped engine chunks.
        if self.is_rescheduling and self.req.client_expects_chat_shape and is_completion_like_stream_chunk(chunk_json):
            adapt_completion_stream_chunk_to_chat(
                chunk_json,
                req_id=self.req.req_id,
                stream_state=sta,
            )

        choices = chunk_json.get(OpenAIField.CHOICES, [])
        if not choices:
            strip_openai_token_id_fields_for_client(
                chunk_json, client_return_token_ids=self.req.client_expects_token_ids
            )
            return encode_stream_chunk_bytes(chunk, chunk_json)

        strip_openai_token_id_fields_for_client(chunk_json, client_return_token_ids=self.req.client_expects_token_ids)
        return encode_stream_chunk_bytes(chunk, chunk_json)

    def prepare_retry_request(self, req_data: dict) -> (dict, str):
        """Build the retry request body and target API after a recompute signal.

        Chat ingress is retried via Completions (``prompt: list[int]``, API
        ``v1/completions``). Ineligible chat requests (tools, logprobs, multimodal parts,
        etc.) raise HTTP 502 because vLLM Chat cannot replay raw token-id prompts in
        ``messages``.

        ``max_tokens`` is derived from the client's original budget minus cumulative
        output token ids from prior legs, with a +1 adjustment when output tokens exist.
        """
        self.retry_count += 1
        if len(self.req.prompt_token_ids) == 0 or len(self.req.cached_token_ids) == 0:
            return (req_data, self.req.api)

        try:
            n_val = int(req_data.get("n", 1))
        except (TypeError, ValueError):
            self.logger.error("Rescheduling aborted for request %s: invalid n", self.req.req_id)
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail="Rescheduling aborted: invalid n parameter.",
            ) from None
        if n_val != 1:
            self.logger.error(
                "Rescheduling aborted for request %s: parallel sampling n=%s not supported",
                self.req.req_id,
                n_val,
            )
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail="Rescheduling does not support parallel sampling (n>1).",
            )

        if OpenAIField.MAX_TOKENS in req_data:
            try:
                max_tokens = int(req_data[OpenAIField.MAX_TOKENS])
                max_tokens -= len(self.req.cached_token_ids)
                req_data[OpenAIField.MAX_TOKENS] = max(1, max_tokens)
            except (TypeError, ValueError):
                pass

        all_ids = list(self.req.prompt_token_ids)
        all_ids.extend(self.req.cached_token_ids)
        is_chat = OpenAIField.MESSAGES in req_data
        if is_chat:
            eligible = self.completions_retry_eligible_for_chat_request(req_data)
            if not eligible:
                self.logger.error(
                    "Rescheduling aborted for request %s: Chat ingress ineligible for "
                    "Completions-style token-id replay.",
                    self.req.req_id,
                )
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        "Rescheduling is not supported for this Chat request (tools, logprobs, "
                        "top_logprobs, non-text multimodal content, structured "
                        "response_format, or n>1). vLLM does not accept token-id arrays in "
                        "Chat messages."
                    ),
                )
            for k in _CHAT_ONLY_KEYS_RECOMPUTE:
                req_data.pop(k, None)
            req_data[OpenAIField.PROMPT] = all_ids
            reschedule_api = "v1/completions"
            self.logger.info(
                "Rescheduling request %s (completions engine): retry=%d all_len=%d prompt_len=%d "
                "cumulative_completion=%d max_tokens=%s",
                self.req.req_id,
                self.retry_count,
                len(all_ids),
                len(self.req.prompt_token_ids),
                len(self.req.cached_token_ids),
                req_data.get(OpenAIField.MAX_TOKENS, 0),
            )
        else:
            req_data[OpenAIField.PROMPT] = all_ids
            reschedule_api = self.req.api
            self.logger.info(
                "Rescheduling request %s (token-id completion retry): retry=%d all_len=%d prompt_len=%d "
                "cumulative_completion=%d max_tokens=%s",
                self.req.req_id,
                self.retry_count,
                len(all_ids),
                len(self.req.prompt_token_ids),
                len(self.req.cached_token_ids),
                req_data.get(OpenAIField.MAX_TOKENS, 0),
            )

        return (req_data, reschedule_api)

    def completions_retry_eligible_for_chat_request(self, req_data: dict) -> bool:
        """Return whether a chat request may be retried as Completions with token-id prompt.

        When this returns ``False``, :func:`prepare_retry_request` raises HTTP 502 because
        vLLM Chat does not accept a raw token-id list in ``messages[].content``.
        """
        rf = req_data.get("response_format")
        if rf is not None:
            if isinstance(rf, dict):
                rtype = rf.get("type")
                if rtype is not None and rtype != "text":
                    return False
            else:
                return False

        if req_data.get("tools"):
            return False
        if req_data.get("logprobs"):
            return False
        if req_data.get("top_logprobs"):
            return False
        messages = req_data.get(OpenAIField.MESSAGES) or []
        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype and ptype != "text":
                    return False
        return True
