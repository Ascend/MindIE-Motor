# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from types import SimpleNamespace

import pytest

from motor.coordinator.models.request import RequestInfo
from motor.coordinator.render.models import TokenizedRequest, TokenizerSource
from motor.coordinator.router.adapters.stream import parse_stream_chunk_json
from motor.coordinator.router.render_execution import (
    RenderExecutionMode,
    build_render_attempt_plan,
    can_fallback_to_native,
    merge_native_completion_responses,
    merge_native_completion_streams,
    resolve_render_execution_mode,
)
from motor.coordinator.router.rescheduler.rescheduler import RetryRequestPlan


NATIVE = RenderExecutionMode.NATIVE_FULL
RENDER_NATIVE = RenderExecutionMode.RENDER_PREFILL_NATIVE_DECODE
TOKEN_ONLY = RenderExecutionMode.TOKEN_ONLY_DERENDER
CHAT_API = "v1/chat/completions"


def _case(
    data,
    expected,
    *,
    api="v1/completions",
    enable_token_only_decode=False,
    image_obfuscation=False,
    engine_type="vllm",
):
    render_config = SimpleNamespace()
    if enable_token_only_decode is not None:
        render_config.enable_token_only_decode = enable_token_only_decode
    prompt = data.get("prompt")
    req_info = RequestInfo(
        req_id="request-1",
        req_data=data,
        req_len=1,
        api=api,
        entry_api=api,
        tokenized_requests=[
            TokenizedRequest(prompt_token_ids=[index + 1], tokenizer_source=TokenizerSource.RENDER)
            for index in range(len(prompt) if isinstance(prompt, list) else 1)
        ],
    )
    config = SimpleNamespace(
        render_config=render_config,
        token_obfuscation_config=SimpleNamespace(
            enable=False,
            image_config=SimpleNamespace(enable=image_obfuscation),
        ),
    )
    return req_info, config, engine_type, expected


@pytest.mark.parametrize(
    ("req_info", "config", "engine_type", "expected"),
    [
        _case(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            RENDER_NATIVE,
            api=CHAT_API,
            enable_token_only_decode=None,
        ),
        _case(
            {"messages": [{"role": "user", "content": "hi"}], "stream": True},
            TOKEN_ONLY,
            api=CHAT_API,
            enable_token_only_decode=True,
        ),
        _case(
            {"messages": [{"role": "user", "content": "hi"}]}, TOKEN_ONLY, api=CHAT_API, enable_token_only_decode=True
        ),
        _case({"prompt": "hi"}, TOKEN_ONLY, enable_token_only_decode=True),
        _case({"prompt": ["a", "b"]}, RENDER_NATIVE),
        _case(
            {"messages": [{"role": "user", "content": "hi"}], "echo": True},
            NATIVE,
            api=CHAT_API,
            enable_token_only_decode=True,
        ),
        _case(
            {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": "x"}]}]},
            NATIVE,
            api=CHAT_API,
        ),
        _case({"prompt": "hi"}, TOKEN_ONLY, image_obfuscation=True),
        _case({"prompt": "hi"}, NATIVE, engine_type="sglang"),
    ],
)
def test_resolve_render_execution_mode(req_info, config, engine_type, expected) -> None:
    assert resolve_render_execution_mode(req_info, config, engine_type) is expected


@pytest.mark.parametrize(
    ("image_obfuscation", "expected_mode", "protected"),
    [(False, RENDER_NATIVE, False), (True, TOKEN_ONLY, True)],
)
def test_build_render_attempt_plan_materializes_retry_tokens_and_prompt_identity(
    image_obfuscation: bool,
    expected_mode: RenderExecutionMode,
    protected: bool,
) -> None:
    req_info, config, engine_type, _ = _case(
        {"prompt": "hi", "stream": True},
        expected_mode,
        image_obfuscation=image_obfuscation,
    )
    plan = build_render_attempt_plan(
        req_info,
        config,
        engine_type,
        attempt_seq=2,
        retry_plan=RetryRequestPlan(
            prompt_token_ids=(7, 8, 9),
            api="v1/completions",
            remove_chat_fields=False,
            cached_output_tokens=1,
        ),
    )

    assert plan is not None
    assert plan.mode is expected_mode
    assert plan.protected is protected
    assert can_fallback_to_native(plan) is not protected
    assert plan.prompts[0].engine_request_id == "request-1#a2#g0#p0"
    assert plan.tokenized_requests[0].prompt_token_ids == [7, 8, 9]


def test_build_render_attempt_plan_bypasses_native_full() -> None:
    req_info, config, engine_type, _ = _case({"prompt": "hi"}, NATIVE, engine_type="sglang")

    assert build_render_attempt_plan(req_info, config, engine_type, attempt_seq=1) is None
    assert can_fallback_to_native(None)


def test_merge_native_completion_responses_owns_batch_semantics():
    merged = merge_native_completion_responses(
        [
            {"choices": [{"index": 0, "text": "first"}], "usage": {"prompt_tokens": 1, "total_tokens": 2}},
            {"choices": [{"index": 0, "text": "second"}], "usage": {"prompt_tokens": 2, "total_tokens": 3}},
        ],
        request_id="request-batch",
        choices_per_prompt=1,
    )

    assert merged["id"] == "request-batch"
    assert [(choice["index"], choice["text"]) for choice in merged["choices"]] == [(0, "first"), (1, "second")]
    assert merged["usage"] == {"prompt_tokens": 3, "total_tokens": 5}


@pytest.mark.asyncio
async def test_merge_native_completion_streams_owns_batch_semantics():
    async def stream(text: str, prompt_tokens: int):
        yield f'data: {{"choices":[{{"index":0,"text":"{text}"}}]}}\n\n'.encode()
        yield f'data: {{"choices":[],"usage":{{"prompt_tokens":{prompt_tokens},"total_tokens":2}}}}\n\n'.encode()
        yield b"data: [DONE]\n\n"

    chunks = [
        chunk
        async for chunk in merge_native_completion_streams(
            [stream("first", 1), stream("second", 2)],
            request_id="request-batch",
            model="model",
            choices_per_prompt=1,
        )
    ]
    payloads = [parse_stream_chunk_json(chunk) for chunk in chunks if b"[DONE]" not in chunk]
    choices = [payload["choices"][0] for payload in payloads if payload["choices"]]

    assert sorted((choice["index"], choice["text"]) for choice in choices) == [(0, "first"), (1, "second")]
    assert all(payload["id"] == "request-batch" for payload in payloads)
    assert next(payload["usage"] for payload in payloads if not payload["choices"]) == {
        "prompt_tokens": 3,
        "total_tokens": 4,
    }
    assert chunks.count(b"data: [DONE]\n\n") == 1
