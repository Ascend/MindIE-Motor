# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Stateful client-side orchestration for stateless streaming Derender calls."""

import asyncio
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import aclosing
from copy import deepcopy
from typing import Any

from motor.coordinator.render.api_spec import get_render_api_spec
from motor.coordinator.render.models import TokenizedRequest
from motor.coordinator.render.response import derender_http_exception
from motor.coordinator.render.token_obfuscation_service import TokenObfuscationService
from motor.coordinator.render.vllm_render_client import (
    RenderClientError,
    RenderInvalidResponseError,
    VLLMRenderClient,
)
from motor.coordinator.router.adapters.stream import (
    encode_stream_chunk_bytes,
    parse_stream_chunk_json,
)

_SSE_TEMPLATE = b"data: {}\n\n"


class StreamingDerenderProcessor:
    """Convert Generate SSE chunks to OpenAI SSE while carrying state per choice."""

    def __init__(
        self,
        render_client: VLLMRenderClient,
        *,
        api: str,
        request_id: str,
        request_data: dict[str, Any],
        tokenized_request: TokenizedRequest,
        choice_index_offset: int = 0,
        emit_prompt_token_ids: bool = True,
        obfuscation_service: TokenObfuscationService | None = None,
    ) -> None:
        spec = get_render_api_spec(api)
        if spec is None:
            raise ValueError(f"Unsupported streaming Derender API: {api}")
        self._render_client = render_client
        self._api = spec.api
        self._request_id = request_id
        self._request_data = deepcopy(request_data)
        self._request_payload_field = spec.request_payload_field
        self._prompt_token_ids = list(tokenized_request.prompt_token_ids)
        self._states: dict[int, dict[str, Any]] = {}
        self._choice_index_offset = choice_index_offset
        self._emit_prompt_token_ids = emit_prompt_token_ids
        self._prompt_ids_emitted = False
        self._obfuscation_service = obfuscation_service

    async def process(self, raw_chunk: bytes) -> bytes:
        """Process one complete upstream SSE frame."""
        try:
            return await self._process(raw_chunk)
        except RenderClientError as error:
            raise derender_http_exception(error) from error

    async def _process(self, raw_chunk: bytes) -> bytes:
        generate_chunk = parse_stream_chunk_json(raw_chunk)
        if generate_chunk is None:
            if b"[DONE]" in raw_chunk:
                return raw_chunk
            raise RenderInvalidResponseError("Generate stream chunk is not valid JSON")

        choices = generate_chunk.get("choices")
        if not isinstance(choices, list):
            raise RenderInvalidResponseError("Generate stream chunk contains invalid choices")
        if not choices:
            return self._usage_chunk(raw_chunk, generate_chunk)
        if self._obfuscation_service is not None:
            generate_chunk = self._obfuscation_service.deobfuscate_generate_responses([generate_chunk])[0]
            choices = generate_chunk["choices"]

        if len(choices) != 1 or not isinstance(choices[0], dict):
            raise RenderInvalidResponseError("Streaming Derender requires exactly one choice per chunk")

        generate_choice = choices[0]
        choice_index = generate_choice.get("index", 0)
        if not isinstance(choice_index, int) or isinstance(choice_index, bool) or choice_index < 0:
            raise RenderInvalidResponseError("Generate stream chunk contains invalid choice index")
        payload = {
            "stream": True,
            "model": self._request_data.get("model"),
            "generate_chunk": generate_chunk,
            "stream_state": self._states.get(choice_index),
            "prompt_token_ids": list(self._prompt_token_ids),
            "prompt_tokens": len(self._prompt_token_ids),
            self._request_payload_field: self._request_data,
        }
        result = await self._render_client.derender_stream_chunk(self._api, payload)
        self._states[choice_index] = result.stream_state
        chunk = result.chunk
        chunk["id"] = self._request_id
        self._inject_token_ids(chunk, generate_choice, choice_index)
        self._remap_choice_indices(chunk)
        if self._emit_prompt_token_ids and not self._prompt_ids_emitted:
            chunk["prompt_token_ids"] = list(self._prompt_token_ids)
            self._prompt_ids_emitted = True
        return encode_stream_chunk_bytes(_SSE_TEMPLATE, chunk)

    def snapshot(self) -> tuple[dict[int, dict[str, Any]], bool]:
        """Capture mutable Derender state at an attempt boundary."""
        return deepcopy(self._states), self._prompt_ids_emitted

    def restore(self, snapshot: tuple[dict[int, dict[str, Any]], bool]) -> None:
        """Restore state when an attempt fails before the response is committed."""
        states, prompt_ids_emitted = snapshot
        self._states = deepcopy(states)
        self._prompt_ids_emitted = prompt_ids_emitted

    @staticmethod
    def _inject_token_ids(
        chunk: dict[str, Any],
        generate_choice: dict[str, Any],
        choice_index: int,
    ) -> None:
        token_ids = generate_choice.get("token_ids")
        if not isinstance(token_ids, list):
            return
        for choice in chunk.get("choices") or []:
            if isinstance(choice, dict) and choice.get("index", 0) == choice_index:
                choice["token_ids"] = list(token_ids)
                return

    def _remap_choice_indices(self, chunk: dict[str, Any]) -> None:
        for choice in chunk.get("choices") or []:
            if isinstance(choice, dict):
                index = choice.get("index", 0)
                if isinstance(index, int) and not isinstance(index, bool):
                    choice["index"] = self._choice_index_offset + index

    def _usage_chunk(self, raw_chunk: bytes, generate_chunk: dict[str, Any]) -> bytes:
        chunk = {
            key: deepcopy(generate_chunk[key])
            for key in ("created", "usage", "system_fingerprint")
            if key in generate_chunk
        }
        chunk.update(
            {
                "id": self._request_id,
                "object": "chat.completion.chunk" if "chat" in self._api else "text_completion",
                "model": self._request_data.get("model"),
                "choices": [],
            }
        )
        return encode_stream_chunk_bytes(raw_chunk, chunk)


class StreamingRenderSession:
    """Own Derender state for every logical stream across engine attempts."""

    def __init__(
        self,
        render_client: VLLMRenderClient,
        *,
        api: str,
        request_id: str,
        request_data: dict[str, Any],
        tokenized_requests: Sequence[TokenizedRequest],
        choices_per_prompt: int = 1,
        obfuscation_service: TokenObfuscationService | None = None,
    ) -> None:
        if not tokenized_requests:
            raise ValueError("Streaming Render session requires tokenized requests")
        choices_per_prompt = max(choices_per_prompt, 1)
        self._processors = [
            StreamingDerenderProcessor(
                render_client,
                api=api,
                request_id=request_id,
                request_data=request_data,
                tokenized_request=tokenized,
                choice_index_offset=index * choices_per_prompt,
                emit_prompt_token_ids=len(tokenized_requests) == 1,
                obfuscation_service=obfuscation_service,
            )
            for index, tokenized in enumerate(tokenized_requests)
        ]
        self._attempt_snapshot: list[tuple[dict[int, dict[str, Any]], bool]] | None = None

    def begin_attempt(self) -> None:
        """Snapshot state before an engine attempt starts producing chunks."""
        self._attempt_snapshot = [processor.snapshot() for processor in self._processors]

    def commit_attempt(self) -> None:
        """Keep state produced by a successful or client-visible attempt."""
        self._attempt_snapshot = None

    def rollback_attempt(self) -> None:
        """Discard Derender progress that was never committed to the client."""
        if self._attempt_snapshot is None:
            return
        for processor, snapshot in zip(self._processors, self._attempt_snapshot):
            processor.restore(snapshot)
        self._attempt_snapshot = None

    def finish_attempt(self, committed: bool) -> None:
        """Commit visible progress or restore an uncommitted attempt."""
        if committed:
            self.commit_attempt()
        else:
            self.rollback_attempt()

    def processor(self, prompt_index: int = 0) -> StreamingDerenderProcessor:
        """Return the stable processor for one logical prompt stream."""
        if prompt_index < 0 or prompt_index >= len(self._processors):
            raise IndexError("Streaming Render prompt index is out of range")
        return self._processors[prompt_index]

    @property
    def processors(self) -> Sequence[StreamingDerenderProcessor]:
        """Return processors in Render prompt order."""
        return self._processors


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


async def merge_derender_streams(
    streams: Sequence[AsyncIterator[bytes]],
    processors: Sequence[StreamingDerenderProcessor],
    *,
    api: str,
    request_id: str,
    model: str | None,
    prompt_token_ids: Sequence[Sequence[int]],
    emit_prompt_token_ids: bool = True,
    before_derender: Callable[[int, bytes], bytes] | None = None,
) -> AsyncIterator[bytes]:
    """Merge one token-only stream per prompt into one OpenAI SSE response."""
    if not streams or len(streams) != len(processors) or len(streams) != len(prompt_token_ids):
        raise ValueError("Streaming Derender batches require matching non-empty inputs")

    queue: asyncio.Queue[tuple[str, int, Any]] = asyncio.Queue(maxsize=len(streams))

    async def pump(index: int, stream: AsyncIterator[bytes]) -> None:
        try:
            async with aclosing(stream):
                async for raw_chunk in stream:
                    chunk_json = parse_stream_chunk_json(raw_chunk)
                    if chunk_json is None:
                        if b"[DONE]" in raw_chunk:
                            continue
                        raise RenderInvalidResponseError("Generate stream chunk is not valid JSON")
                    choices = chunk_json.get("choices")
                    if not isinstance(choices, list):
                        raise RenderInvalidResponseError("Generate stream chunk contains invalid choices")
                    if not choices:
                        await queue.put(("usage", index, chunk_json))
                        continue
                    if before_derender is not None:
                        raw_chunk = before_derender(index, raw_chunk)
                    rendered = await processors[index].process(raw_chunk)
                    await queue.put(("chunk", index, rendered))
        except asyncio.CancelledError as error:
            current_task = asyncio.current_task()
            if current_task is not None and current_task.cancelling():
                raise
            await queue.put(("error", index, error))
        except Exception as error:
            await queue.put(("error", index, error))
        else:
            await queue.put(("done", index, None))

    tasks = [asyncio.create_task(pump(index, stream)) for index, stream in enumerate(streams)]
    active = len(tasks)
    usage: dict[str, Any] = {}
    usage_metadata: dict[str, Any] = {}
    prompt_ids_emitted = not emit_prompt_token_ids
    try:
        while active:
            event, index, value = await queue.get()
            if event == "done":
                active -= 1
                continue
            if event == "error":
                if isinstance(value, RenderClientError):
                    raise derender_http_exception(value) from value
                raise value

            if event == "usage":
                chunk_usage = value.get("usage")
                if isinstance(chunk_usage, dict):
                    _merge_usage(usage, chunk_usage)
                for key in ("created", "system_fingerprint"):
                    if key in value:
                        usage_metadata.setdefault(key, deepcopy(value[key]))
                continue

            rendered = value
            if not prompt_ids_emitted:
                rendered_json = parse_stream_chunk_json(rendered)
                if rendered_json is None:
                    error = RenderInvalidResponseError("Derender stream chunk is not valid JSON")
                    raise derender_http_exception(error) from error
                prompt_ids: list[int] | list[list[int]]
                if len(prompt_token_ids) == 1:
                    prompt_ids = list(prompt_token_ids[0])
                else:
                    prompt_ids = [list(ids) for ids in prompt_token_ids]
                rendered_json["prompt_token_ids"] = prompt_ids
                rendered = encode_stream_chunk_bytes(rendered, rendered_json)
                prompt_ids_emitted = True
            yield rendered

        if usage:
            usage_chunk = {
                **usage_metadata,
                "id": request_id,
                "object": "chat.completion.chunk" if "chat" in api else "text_completion",
                "model": model,
                "choices": [],
                "usage": usage,
            }
            yield encode_stream_chunk_bytes(_SSE_TEMPLATE, usage_chunk)
        yield b"data: [DONE]\n\n"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
