# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Select and assemble how Render output is consumed by vLLM execution paths."""

from collections.abc import AsyncIterator, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from motor.coordinator.render.models import TokenizedRequest, TokenizerSource
from motor.coordinator.render.streaming_response import merge_derender_streams
from motor.coordinator.router.adapters.stream import encode_stream_chunk_bytes, parse_stream_chunk_json
from motor.coordinator.router.token_only import token_only_request_id, token_only_requests_for_attempt

if TYPE_CHECKING:
    from motor.config.coordinator import CoordinatorConfig
    from motor.coordinator.models.request import RequestInfo
    from motor.coordinator.router.rescheduler.rescheduler import RetryRequestPlan


class RenderExecutionMode(str, Enum):
    """Request shape used between Coordinator and the native engine."""

    NATIVE_FULL = "native_full"
    RENDER_PREFILL_NATIVE_DECODE = "render_prefill_native_decode"
    TOKEN_ONLY_DERENDER = "token_only_derender"  # nosec B105 -- execution mode label, not a credential


@dataclass(frozen=True)
class RenderPromptPlan:
    """One Render-tokenized prompt and its stable engine request identity."""

    index: int
    engine_request_id: str
    tokenized_request: TokenizedRequest


@dataclass(frozen=True)
class RenderAttemptPlan:
    """Immutable Render decisions and prompt inputs for one routing attempt."""

    mode: RenderExecutionMode
    prompts: tuple[RenderPromptPlan, ...]
    protected: bool
    attempt_seq: int
    generation: int = 0

    @property
    def tokenized_requests(self) -> list[TokenizedRequest]:
        """Return the ordered prompt inputs expected by existing request builders."""
        return [prompt.tokenized_request for prompt in self.prompts]

    def with_mode(self, request_id: str, mode: RenderExecutionMode) -> "RenderAttemptPlan":
        """Start a new whole-batch generation after an engine capability fallback."""
        generation = self.generation + 1
        prompts = tuple(
            RenderPromptPlan(
                index=prompt.index,
                engine_request_id=token_only_request_id(
                    request_id,
                    attempt_seq=self.attempt_seq,
                    generation=generation,
                    prompt_index=prompt.index,
                ),
                tokenized_request=prompt.tokenized_request,
            )
            for prompt in self.prompts
        )
        return RenderAttemptPlan(
            mode=mode,
            prompts=prompts,
            protected=self.protected,
            attempt_seq=self.attempt_seq,
            generation=generation,
        )


def can_fallback_to_native(plan: RenderAttemptPlan | None) -> bool:
    """Return whether an attempt may retry through a native OpenAI request shape."""
    return plan is None or not plan.protected


def is_render_protected(req_info: "RequestInfo", config: "CoordinatorConfig") -> bool:
    """Return whether Render fallback must avoid native plaintext request shapes."""
    obfuscation = config.token_obfuscation_config
    return bool(req_info._token_obfuscation_service is not None or obfuscation.enable)


def _is_text_only_chat_request(request: dict[str, Any]) -> bool:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, dict):
            return False
        content = message.get("content")
        if content is None or isinstance(content, str):
            continue
        if not isinstance(content, list) or any(
            not isinstance(part, dict) or part.get("type") != "text" for part in content
        ):
            return False
    return True


def resolve_render_execution_mode(
    req_info: "RequestInfo",
    config: "CoordinatorConfig",
    engine_type: str,
) -> RenderExecutionMode:
    """Return one shared Render/Decode mode for PD, Union, and fallback paths."""
    api = req_info.effective_entry_api().strip("/")
    rendered = bool(req_info.tokenized_requests) and all(
        item.tokenizer_source is TokenizerSource.RENDER for item in req_info.tokenized_requests
    )
    if str(engine_type).strip().lower() != "vllm" or api not in {
        "v1/chat/completions",
        "v1/completions",
    }:
        return RenderExecutionMode.NATIVE_FULL
    if not rendered:
        return RenderExecutionMode.NATIVE_FULL

    if is_render_protected(req_info, config):
        return RenderExecutionMode.TOKEN_ONLY_DERENDER
    if api == "v1/chat/completions":
        if req_info.req_data.get("echo", False) or not _is_text_only_chat_request(req_info.req_data):
            return RenderExecutionMode.NATIVE_FULL
    if getattr(config.render_config, "enable_token_only_decode", False):
        return RenderExecutionMode.TOKEN_ONLY_DERENDER
    return RenderExecutionMode.RENDER_PREFILL_NATIVE_DECODE


def build_render_attempt_plan(
    req_info: "RequestInfo",
    config: "CoordinatorConfig",
    engine_type: str,
    *,
    attempt_seq: int,
    retry_plan: "RetryRequestPlan | None" = None,
    generation: int = 0,
) -> RenderAttemptPlan | None:
    """Materialize the shared Render mode and tokens once for an attempt."""
    mode = resolve_render_execution_mode(req_info, config, engine_type)
    if mode is RenderExecutionMode.NATIVE_FULL:
        return None
    tokenized_requests = token_only_requests_for_attempt(
        req_info,
        None,
        allow_streaming=True,
        retry_plan=retry_plan,
        require_render_client=False,
    )
    if not tokenized_requests:
        return None
    prompts = tuple(
        RenderPromptPlan(
            index=index,
            engine_request_id=token_only_request_id(
                req_info.req_id,
                attempt_seq=attempt_seq,
                generation=generation,
                prompt_index=index,
            ),
            tokenized_request=tokenized,
        )
        for index, tokenized in enumerate(tokenized_requests)
    )
    return RenderAttemptPlan(
        mode=mode,
        prompts=prompts,
        protected=is_render_protected(req_info, config),
        attempt_seq=attempt_seq,
        generation=generation,
    )


def _merge_usage(target: dict[str, Any], incoming: dict[str, Any]) -> None:
    for key, value in incoming.items():
        if isinstance(value, bool):
            target.setdefault(key, value)
        elif isinstance(value, int | float):
            target[key] = target.get(key, 0) + value
        elif isinstance(value, dict):
            nested = target.setdefault(key, {})
            if isinstance(nested, dict):
                _merge_usage(nested, value)
        else:
            target.setdefault(key, deepcopy(value))


def merge_native_completion_responses(
    responses: Sequence[dict[str, Any]],
    *,
    request_id: str,
    choices_per_prompt: int,
) -> dict[str, Any]:
    """Merge ordered per-prompt native Completion responses into one response."""
    if not responses:
        raise ValueError("Native Completion batch requires at least one response")
    merged = {key: deepcopy(value) for key, value in responses[0].items() if key not in {"choices", "usage"}}
    merged["id"] = request_id
    merged["choices"] = []
    usage: dict[str, Any] = {}
    choices_per_prompt = max(choices_per_prompt, 1)
    for prompt_index, response in enumerate(responses):
        choices = response.get("choices")
        if not isinstance(choices, list):
            raise ValueError("Native Completion response contains invalid choices")
        for local_index, choice in enumerate(choices):
            if not isinstance(choice, dict):
                raise ValueError("Native Completion response contains an invalid choice")
            choice = deepcopy(choice)
            index = choice.get("index", local_index)
            if not isinstance(index, int) or isinstance(index, bool):
                index = local_index
            choice["index"] = prompt_index * choices_per_prompt + index
            merged["choices"].append(choice)
        response_usage = response.get("usage")
        if isinstance(response_usage, dict):
            _merge_usage(usage, response_usage)
    if usage:
        merged["usage"] = usage
    return merged


class _NativeCompletionStreamProcessor:
    def __init__(self, request_id: str, choice_index_offset: int) -> None:
        self._request_id = request_id
        self._choice_index_offset = choice_index_offset

    async def process(self, raw_chunk: bytes) -> bytes:
        chunk = parse_stream_chunk_json(raw_chunk)
        if chunk is None:
            raise ValueError("Native Completion stream chunk is not valid JSON")
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                raise ValueError("Native Completion stream chunk contains an invalid choice")
            index = choice.get("index", 0)
            if isinstance(index, int) and not isinstance(index, bool):
                choice["index"] = self._choice_index_offset + index
        chunk["id"] = self._request_id
        return encode_stream_chunk_bytes(raw_chunk, chunk)


async def merge_native_completion_streams(
    streams: Sequence[AsyncIterator[bytes]],
    *,
    request_id: str,
    model: str | None,
    choices_per_prompt: int,
    before_merge: Callable[[int, bytes], bytes] | None = None,
) -> AsyncIterator[bytes]:
    """Merge per-prompt native Completion streams through the shared SSE fan-in."""
    choices_per_prompt = max(choices_per_prompt, 1)
    processors = [
        _NativeCompletionStreamProcessor(request_id, index * choices_per_prompt) for index in range(len(streams))
    ]
    merged = merge_derender_streams(
        streams,
        processors,
        api="v1/completions",
        request_id=request_id,
        model=model,
        prompt_token_ids=[[] for _ in streams],
        emit_prompt_token_ids=False,
        before_derender=before_merge,
    )
    async for chunk in merged:
        yield chunk
