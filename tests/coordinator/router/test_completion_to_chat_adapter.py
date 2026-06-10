# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

from motor.coordinator.router.adapters.completion_to_chat import (
    adapt_completion_nonstream_to_chat,
    adapt_completion_stream_chunk_to_chat,
    is_completion_like_stream_chunk,
)


def test_is_completion_like_stream_chunk():
    assert is_completion_like_stream_chunk({"object": "text_completion", "choices": [{"text": "a"}]})
    assert is_completion_like_stream_chunk({"choices": [{"text": "a"}]})
    assert not is_completion_like_stream_chunk({"choices": [{"delta": {"content": "a"}}]})


def test_adapt_completion_stream_chunk_to_chat():
    chunk = {
        "object": "text_completion",
        "id": "cmpl-old",
        "choices": [
            {
                "index": 0,
                "text": "Hi",
                "finish_reason": None,
                "prompt_token_ids": [1, 2],
            }
        ],
    }
    st: dict = {}
    adapt_completion_stream_chunk_to_chat(chunk, req_id="req1", stream_state=st)
    assert chunk["object"] == "chat.completion.chunk"
    assert chunk["id"].startswith("chatcmpl-")
    assert chunk["prompt_token_ids"] == [1, 2]
    assert chunk["choices"][0]["delta"]["role"] == "assistant"
    assert chunk["choices"][0]["delta"]["content"] == "Hi"
    assert st["stream_role_sent"] is True


def test_adapt_completion_nonstream_to_chat():
    body = {
        "object": "text_completion",
        "id": "cmpl-x",
        "choices": [
            {
                "index": 0,
                "text": "Hello",
                "finish_reason": "stop",
                "prompt_token_ids": [9, 8],
            }
        ],
    }
    adapt_completion_nonstream_to_chat(body, req_id="rid")
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["prompt_token_ids"] == [9, 8]
    assert body["choices"][0]["message"]["content"] == "Hello"


def test_adapt_stream_preserves_remapped_logprobs():
    """Completion logprobs must be mapped to Chat content[] shape, not dropped."""
    chunk = {
        "object": "text_completion",
        "id": "cmpl-1",
        "choices": [
            {
                "index": 0,
                "text": "Hi",
                "logprobs": {
                    "token_logprobs": [-0.1, -0.2],
                    "top_logprobs": [
                        {"token_id:101": -0.1, "token_id:102": -0.5},
                        {"token_id:103": -0.2, "token_id:104": -0.6},
                    ],
                },
            }
        ],
    }
    st: dict = {}
    adapt_completion_stream_chunk_to_chat(chunk, req_id="req1", stream_state=st)
    lp = chunk["choices"][0]["logprobs"]
    assert isinstance(lp, dict)
    assert "content" in lp
    assert lp["content"] == [
        {
            "logprob": -0.1,
            "top_logprobs": [
                {"token": "token_id:101", "logprob": -0.1},
                {"token": "token_id:102", "logprob": -0.5},
            ],
        },
        {
            "logprob": -0.2,
            "top_logprobs": [
                {"token": "token_id:103", "logprob": -0.2},
                {"token": "token_id:104", "logprob": -0.6},
            ],
        },
    ]


def test_adapt_stream_drops_logprobs_when_token_logprobs_all_null():
    """If every entry is null, do not fabricate an empty content[]."""
    chunk = {
        "object": "text_completion",
        "id": "cmpl-2",
        "choices": [
            {
                "index": 0,
                "text": "x",
                "logprobs": {"token_logprobs": [None, None]},
            }
        ],
    }
    adapt_completion_stream_chunk_to_chat(chunk, req_id="r", stream_state={})
    assert "logprobs" not in chunk["choices"][0]


def test_adapt_stream_logprobs_absent_is_noop():
    """If engine returns no logprobs field at all, adapt does not invent one."""
    chunk = {
        "object": "text_completion",
        "id": "cmpl-3",
        "choices": [{"index": 0, "text": "ok"}],
    }
    adapt_completion_stream_chunk_to_chat(chunk, req_id="r", stream_state={})
    assert "logprobs" not in chunk["choices"][0]


def test_adapt_nonstream_preserves_remapped_logprobs():
    body = {
        "object": "text_completion",
        "id": "cmpl-4",
        "choices": [
            {
                "index": 0,
                "text": "Hi",
                "logprobs": {"token_logprobs": [-0.7]},
            }
        ],
    }
    adapt_completion_nonstream_to_chat(body, req_id="rid")
    lp = body["choices"][0]["logprobs"]
    assert lp == {"content": [{"logprob": -0.7}]}
