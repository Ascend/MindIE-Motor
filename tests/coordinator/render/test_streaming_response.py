# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from motor.coordinator.render.models import (
    DerenderedStreamChunk,
    TokenizedRequest,
    TokenizerSource,
)
from motor.coordinator.render.streaming_response import (
    StreamingDerenderProcessor,
    StreamingRenderSession,
    merge_derender_streams,
)
from motor.coordinator.render.vllm_render_client import (
    RenderRequestError,
    RenderTimeoutError,
    RenderUnavailableError,
    RenderUnsupportedError,
)

pytestmark = pytest.mark.asyncio


def _decode_sse(chunk: bytes) -> dict:
    return json.loads(chunk.removeprefix(b"data: ").strip())


def _derendered(choice: dict, step: int = 1) -> DerenderedStreamChunk:
    return DerenderedStreamChunk(chunk={"choices": [{"index": 0, **choice}]}, stream_state={"step": step})


def _processor(client: AsyncMock, obfuscation_service: Mock | None = None) -> StreamingDerenderProcessor:
    return StreamingDerenderProcessor(
        client,
        api="v1/chat/completions",
        request_id="request-1",
        request_data={
            "model": "model",
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
        tokenized_request=TokenizedRequest(
            prompt_token_ids=[10, 20],
            tokenizer_source=TokenizerSource.RENDER,
            metadata={"sampling_params": {"max_tokens": 8}},
        ),
        obfuscation_service=obfuscation_service,
    )


def _batch_processor(client: AsyncMock, prompt_token_id: int, choice_index_offset: int):
    return StreamingDerenderProcessor(
        client,
        api="v1/completions",
        request_id="request-batch",
        request_data={"model": "model", "stream": True, "prompt": ["first", "second"]},
        tokenized_request=TokenizedRequest(
            prompt_token_ids=[prompt_token_id],
            tokenizer_source=TokenizerSource.RENDER,
        ),
        choice_index_offset=choice_index_offset,
        emit_prompt_token_ids=False,
    )


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        pytest.param(RenderRequestError("Streaming Derender", 422), 422, id="request"),
        pytest.param(RenderUnsupportedError("unsupported"), 501, id="unsupported"),
        pytest.param(RenderUnavailableError("unavailable"), 502, id="unavailable"),
        pytest.param(RenderTimeoutError("timeout"), 504, id="timeout"),
    ],
)
async def test_streaming_derender_maps_render_errors_to_http_errors(error, status_code):
    client = AsyncMock()
    client.derender_stream_chunk.side_effect = error

    with pytest.raises(HTTPException) as exc_info:
        await _processor(client).process(b'data: {"choices":[{"index":0,"token_ids":[30]}]}\n\n')

    assert exc_info.value.status_code == status_code
    client.derender_stream_chunk.assert_awaited_once()


async def test_streaming_derender_maps_invalid_generate_chunk_to_bad_gateway():
    with pytest.raises(HTTPException) as exc_info:
        await _processor(AsyncMock()).process(b"not-json")

    assert exc_info.value.status_code == 502


async def test_streaming_derender_deobfuscates_engine_tokens_before_rpc():
    client = AsyncMock()
    client.derender_stream_chunk.return_value = _derendered({"delta": {"content": "A"}})
    obfuscation_service = Mock()
    obfuscation_service.deobfuscate_generate_responses.return_value = [{"choices": [{"index": 0, "token_ids": [30]}]}]

    result = await _processor(client, obfuscation_service).process(
        b'data: {"choices":[{"index":0,"token_ids":[130]}]}\n\n'
    )

    payload = client.derender_stream_chunk.await_args.args[1]
    assert payload["generate_chunk"]["choices"][0]["token_ids"] == [30]
    assert _decode_sse(result)["choices"][0]["token_ids"] == [30]
    obfuscation_service.deobfuscate_generate_responses.assert_called_once_with(
        [{"choices": [{"index": 0, "token_ids": [130]}]}]
    )


async def test_streaming_derender_carries_state_and_replay_token_ids():
    client = AsyncMock()
    client.derender_stream_chunk.side_effect = [
        _derendered({"delta": {"content": "A"}}),
        _derendered({"delta": {"content": "B"}}, step=2),
    ]
    processor = _processor(client)

    first = await processor.process(b'data: {"choices":[{"index":0,"token_ids":[30]}]}\n\n')
    second = await processor.process(b'data: {"choices":[{"index":0,"token_ids":[31]}]}\n\n')

    first_payload = _decode_sse(first)
    second_payload = _decode_sse(second)
    assert first_payload["id"] == second_payload["id"] == "request-1"
    assert first_payload["prompt_token_ids"] == [10, 20]
    assert "prompt_token_ids" not in second_payload
    assert first_payload["choices"][0]["token_ids"] == [30]
    assert second_payload["choices"][0]["token_ids"] == [31]
    first_call, second_call = client.derender_stream_chunk.await_args_list
    assert first_call.args[1]["stream_state"] is None
    assert second_call.args[1]["stream_state"] == {"step": 1}
    assert first_call.args[1]["prompt_token_ids"] == [10, 20]
    assert first_call.args[1]["prompt_tokens"] == 2
    assert first_call.args[1]["chat_request"]["messages"][0]["content"] == "hi"


async def test_streaming_render_session_rolls_back_uncommitted_attempt_state():
    client = AsyncMock()
    client.derender_stream_chunk.side_effect = [
        _derendered({}),
        _derendered({}),
    ]
    session = StreamingRenderSession(
        client,
        api="v1/completions",
        request_id="request-1",
        request_data={"model": "model", "stream": True, "prompt": "hi"},
        tokenized_requests=[TokenizedRequest(prompt_token_ids=[10], tokenizer_source=TokenizerSource.RENDER)],
    )

    session.begin_attempt()
    await session.processor().process(b'data: {"choices":[{"index":0,"token_ids":[30]}]}\n\n')
    session.finish_attempt(False)
    await session.processor().process(b'data: {"choices":[{"index":0,"token_ids":[30]}]}\n\n')

    assert client.derender_stream_chunk.await_args_list[1].args[1]["stream_state"] is None


async def test_streaming_derender_passes_done_and_usage_without_rpc():
    client = AsyncMock()
    processor = _processor(client)
    done = b"data: [DONE]\n\n"

    assert await processor.process(done) == done
    usage = await processor.process(b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n')

    assert _decode_sse(usage)["usage"]["completion_tokens"] == 1
    client.derender_stream_chunk.assert_not_awaited()


async def test_merge_derender_streams_derenders_concurrently_and_preserves_choice_indices():
    client = AsyncMock()
    started = 0
    both_started = asyncio.Event()

    async def derender_concurrently(_api, _payload):
        nonlocal started
        started += 1
        if started == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        return _derendered({"text": "ok"})

    client.derender_stream_chunk.side_effect = derender_concurrently

    async def stream(prompt_tokens: int):
        yield b'data: {"choices":[{"index":0,"token_ids":[30]}]}\n\n'
        yield (
            b'data: {"choices":[],"usage":{"prompt_tokens":'
            + str(prompt_tokens).encode()
            + b',"completion_tokens":1,"total_tokens":'
            + str(prompt_tokens + 1).encode()
            + b"}}\n\n"
        )
        yield b"data: [DONE]\n\n"

    chunks = [
        chunk
        async for chunk in merge_derender_streams(
            [stream(1), stream(2)],
            [_batch_processor(client, 10, 0), _batch_processor(client, 20, 1)],
            api="v1/completions",
            request_id="request-batch",
            model="model",
            prompt_token_ids=[[10], [20]],
        )
    ]
    payloads = [_decode_sse(chunk) for chunk in chunks if b"[DONE]" not in chunk]
    choices = [payload["choices"][0] for payload in payloads if payload["choices"]]
    usage = next(payload["usage"] for payload in payloads if not payload["choices"])

    assert sorted(choice["index"] for choice in choices) == [0, 1]
    assert payloads[0]["prompt_token_ids"] == [[10], [20]]
    assert all("prompt_token_ids" not in payload for payload in payloads[1:])
    assert usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    assert chunks.count(b"data: [DONE]\n\n") == 1


async def test_merge_derender_streams_client_close_cancels_full_queue_pumps():
    client = AsyncMock()
    client.derender_stream_chunk.return_value = _derendered({"text": "ok"})
    produced = 0
    queue_saturated = asyncio.Event()
    stream_closed = [asyncio.Event(), asyncio.Event()]

    async def stream(index: int):
        nonlocal produced
        try:
            while True:
                produced += 1
                if produced >= 4:
                    queue_saturated.set()
                yield b'data: {"choices":[{"index":0,"token_ids":[30]}]}\n\n'
        finally:
            stream_closed[index].set()

    merged = merge_derender_streams(
        [stream(0), stream(1)],
        [_batch_processor(client, 10, 0), _batch_processor(client, 20, 1)],
        api="v1/completions",
        request_id="request-batch",
        model="model",
        prompt_token_ids=[[10], [20]],
    )
    await anext(merged)
    await asyncio.wait_for(queue_saturated.wait(), timeout=1)

    await asyncio.wait_for(merged.aclose(), timeout=1)

    assert all(closed.is_set() for closed in stream_closed)


async def test_merge_derender_streams_propagates_upstream_cancelled_error():
    client = AsyncMock()

    async def cancelled_stream():
        raise asyncio.CancelledError("node fault")
        yield b"unreachable"  # pylint: disable=unreachable

    merged = merge_derender_streams(
        [cancelled_stream()],
        [_batch_processor(client, 10, 0)],
        api="v1/completions",
        request_id="request-cancelled",
        model="model",
        prompt_token_ids=[[10]],
    )

    with pytest.raises(asyncio.CancelledError, match="node fault"):
        await asyncio.wait_for(anext(merged), timeout=1)


async def test_merge_derender_streams_failure_cancels_sibling_stream():
    client = AsyncMock()
    sibling_started = asyncio.Event()
    sibling_closed = asyncio.Event()

    async def failing_stream():
        await sibling_started.wait()
        yield b"not-json"

    async def sibling_stream():
        try:
            sibling_started.set()
            await asyncio.Event().wait()
            yield b"unreachable"
        finally:
            sibling_closed.set()

    merged = merge_derender_streams(
        [failing_stream(), sibling_stream()],
        [_batch_processor(client, 10, 0), _batch_processor(client, 20, 1)],
        api="v1/completions",
        request_id="request-batch",
        model="model",
        prompt_token_ids=[[10], [20]],
    )

    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(anext(merged), timeout=1)

    assert exc_info.value.status_code == 502
    assert sibling_closed.is_set()
