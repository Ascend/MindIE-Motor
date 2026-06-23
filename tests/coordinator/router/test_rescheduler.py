# -*- coding: utf-8 -*-
"""Unit tests for motor.coordinator.router.recompute."""

import json

import pytest
from fastapi import HTTPException

from motor.coordinator.router.adapters.stream import (
    parse_stream_chunk_json,
    strip_nonstream_response_body_for_client,
    strip_stream_chunk_bytes_for_client,
)
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.router.rescheduler.rescheduler import Rescheduler
from motor.common.logger import get_logger

logger = get_logger(__name__)


def _make_request_info(
    req_data: dict | None = None,
    *,
    req_id: str = "test-req",
    api: str = "v1/chat/completions",
    entry_api: str | None = None,
    req_len: int = 1,
    **kwargs,
) -> RequestInfo:
    if req_data is None:
        req_data = {
            "messages": [{"role": "user", "content": "x"}],
            "stream": True,
            "max_tokens": 10,
        }
    return RequestInfo(
        req_id=req_id,
        req_data=req_data,
        req_len=req_len,
        api=api,
        entry_api=entry_api if entry_api is not None else api,
        **kwargs,
    )


def test_update_token_id_cache_prompt_once_and_extend_output():
    req = _make_request_info()
    req.update_token_id_cache({"prompt_token_ids": [1, 2], "choices": [{"token_ids": [10]}]})
    assert req.prompt_token_ids == [1, 2]
    assert req.cached_token_ids == [10]

    req.update_token_id_cache({"prompt_token_ids": [99, 99], "choices": [{"token_ids": [20]}]})
    assert req.prompt_token_ids == [1, 2]
    assert req.cached_token_ids == [10, 20]


def test_parse_stream_chunk_json_sse_prefix():
    raw = b'data: {"choices": [{"delta": {"content": "a"}}]}'
    obj = parse_stream_chunk_json(raw, logger=None)
    assert obj["choices"][0]["delta"]["content"] == "a"


def test_process_stream_chunk_recompute_disabled_sets_policy_no_kv_transfer():
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    chunk = json.dumps(
        {
            "prompt_token_ids": [1, 2],
            "choices": [
                {
                    "delta": {"content": "tok"},
                    "token_ids": [10, 20],
                    "stop_reason": "recomputed",
                }
            ],
        }
    ).encode()
    req = _make_request_info(req_data)
    resch = Rescheduler(False, req, logger=logger)
    out = resch.process_stream_chunk(chunk)
    assert out is not None
    assert req.prompt_token_ids == []
    assert req.cached_token_ids == []


def test_process_stream_chunk_recomputed():
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    chunk = json.dumps(
        {
            "prompt_token_ids": [1, 2],
            "choices": [
                {
                    "delta": {"content": "tok"},
                    "token_ids": [10, 20],
                    "stop_reason": "recomputed",
                }
            ],
        }
    ).encode()
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    out = resch.process_stream_chunk(chunk)
    assert out is not None
    parsed = json.loads(out.decode())
    ch0 = parsed["choices"][0]
    assert ch0["stop_reason"] == "stop"
    assert "prompt_token_ids" not in parsed
    assert "token_ids" not in ch0
    assert req.prompt_token_ids == [1, 2]
    assert req.cached_token_ids == [10, 20]


def test_process_stream_chunk_strips_token_ids_for_client():
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    chunk = json.dumps(
        {
            "prompt_token_ids": [1, 2],
            "choices": [{"delta": {"content": "a"}, "token_ids": [9]}],
        }
    ).encode()
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    out = resch.process_stream_chunk(chunk)
    assert out is not None
    parsed = json.loads(out.decode())
    assert "prompt_token_ids" not in parsed
    assert "token_ids" not in parsed["choices"][0]
    assert req.prompt_token_ids == [1, 2]
    assert req.cached_token_ids == [9]


def test_process_stream_chunk_adapts_text_completion_chunk_for_chat_entry_without_recompute_mode():
    """AISBench / OpenAI clients expect delta; first decode may still be Completion-shaped."""
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    chunk = json.dumps(
        {
            "object": "text_completion",
            "id": "cmpl-test",
            "choices": [{"index": 0, "text": "6", "finish_reason": None, "logprobs": None}],
        }
    ).encode()
    req = _make_request_info(
        req_data,
        req_id="cmpl-ingress-01",
        entry_api="v1/chat/completions",
        client_expects_chat_shape=True,
    )
    resch = Rescheduler(True, req, logger=logger)
    resch.is_rescheduling = True
    out = resch.process_stream_chunk(chunk)
    assert out is not None
    parsed = json.loads(out.decode())
    assert parsed["object"] == "chat.completion.chunk"
    assert parsed["id"].startswith("chatcmpl-")
    c0 = parsed["choices"][0]
    assert "delta" in c0
    assert c0["delta"].get("content") == "6"
    assert "text" not in c0


def test_strip_stream_chunk_bytes_for_client_sse_prefix():
    raw = b'data: {"prompt_token_ids": [1], "choices": [{"token_ids": [2], "delta": {}}]}\n\n'
    out = strip_stream_chunk_bytes_for_client(raw)
    line = out.decode().strip()
    assert line.startswith("data: ")
    parsed = json.loads(line[len("data: ") :])
    assert "prompt_token_ids" not in parsed
    assert "token_ids" not in parsed["choices"][0]


def test_strip_nonstream_response_body_for_client():
    body = {
        "prompt_token_ids": [10],
        "choices": [{"message": {"content": "hi"}, "token_ids": [20]}],
    }
    strip_nonstream_response_body_for_client(body)
    assert "prompt_token_ids" not in body
    assert "token_ids" not in body["choices"][0]


def test_strip_nonstream_removes_prompt_token_ids_nested_in_choices():
    """vLLM may echo prompt_token_ids under choices[0]; clients must not see it."""
    body = {
        "choices": [
            {
                "message": {"content": "hi"},
                "prompt_token_ids": [1, 2, 3],
                "token_ids": [4],
            }
        ],
    }
    strip_nonstream_response_body_for_client(body)
    ch0 = body["choices"][0]
    assert "prompt_token_ids" not in body
    assert "prompt_token_ids" not in ch0
    assert "token_ids" not in ch0


def test_strip_nonstream_maps_recomputed_stop_reason():
    body = {"choices": [{"message": {"content": "x"}, "stop_reason": "recomputed"}]}
    strip_nonstream_response_body_for_client(body)
    assert body["choices"][0]["stop_reason"] == "stop"


def test_process_stream_chunk_drops_unparseable_chunk():
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    out = resch.process_stream_chunk(b"not valid json {{{")
    assert out == b""


def test_process_stream_chunk_preserves_done_marker():
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    done_line = b"data: [DONE]\n\n"
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    out = resch.process_stream_chunk(done_line)
    assert out == done_line


def test_prepare_retry_request_req_len_ignores_internal_keys():
    req_data = {"messages": [{"role": "user", "content": "a"}], "max_tokens": 100, "stream": True}
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    req.prompt_token_ids = [1]
    req.cached_token_ids = [2]
    retry_req, retry_api = resch.prepare_retry_request(req_data)
    assert retry_req["prompt"] == [1, 2]
    assert retry_api == "v1/completions"


def test_process_stream_chunk_recomputed_missing_prompt_token_ids_skips_retry_body():
    """Without prompt_token_ids, cache only gets output ids; prepare_retry_request no-ops."""
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    chunk = json.dumps(
        {
            "choices": [
                {
                    "delta": {"content": "tok"},
                    "token_ids": [10],
                    "stop_reason": "recomputed",
                }
            ],
        }
    ).encode()
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    out = resch.process_stream_chunk(chunk)
    assert out is not None
    assert req.prompt_token_ids == []
    assert req.cached_token_ids == [10]
    retry_req, retry_api = resch.prepare_retry_request(dict(req_data))
    assert retry_req == req_data
    assert retry_api == req.api


def test_prepare_retry_request_multi_message_becomes_completions_prompt():
    """Multi-turn chat is folded into ``all_ids``; retry uses Completions (BUG-4 / BUG-5)."""
    req_data = {
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hi"},
        ],
        "max_tokens": 100,
        "stream": True,
    }
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    req.prompt_token_ids = [1]
    req.cached_token_ids = [2, 3]
    retry_req, retry_api = resch.prepare_retry_request(req_data)
    assert "messages" not in retry_req
    assert retry_req["prompt"] == [1, 2, 3]


def test_prepare_retry_request_chat_eligible_uses_completions_prompt():
    req_data = {"messages": [{"role": "user", "content": "a"}], "max_tokens": 100, "stream": True}
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    req.prompt_token_ids = [1]
    req.cached_token_ids = [2, 3]
    retry_req, retry_api = resch.prepare_retry_request(req_data)
    assert "messages" not in retry_req
    assert retry_req["prompt"] == [1, 2, 3]
    assert retry_api == "v1/completions"
    # prepare_retry_request: max_tokens -= len(cached_token_ids), not +1
    assert retry_req["max_tokens"] == 100 - len(req.cached_token_ids)


def test_prepare_retry_request_multi_round_max_tokens_uses_origin_cap():
    """max_tokens is reduced by len(cached_token_ids) each prepare_retry_request call."""
    req_data = {"messages": [{"role": "user", "content": "a"}], "max_tokens": 100, "stream": True}
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    req.prompt_token_ids = [0]
    leg1_cached = list(range(1, 10))  # 9 output ids
    req.cached_token_ids = leg1_cached
    retry_req, _ = resch.prepare_retry_request(req_data)
    assert retry_req["max_tokens"] == 100 - len(leg1_cached)

    req_data["max_tokens"] = 100
    req.prompt_token_ids = [0]
    leg2_cached = list(range(1, 15))  # 14 output ids
    req.cached_token_ids = leg2_cached
    retry_req, _ = resch.prepare_retry_request(req_data)
    assert retry_req["max_tokens"] == 100 - len(leg2_cached)


def test_prepare_retry_request_missing_kv_noops():
    """Empty prompt_token_ids and cached_token_ids: early return, no 502."""
    req_data = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 50, "stream": True}
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    assert resch.retry_count == 0
    retry_req, retry_api = resch.prepare_retry_request(req_data)
    # Missing KV is an early return: request body unchanged and retry_count not bumped.
    assert resch.retry_count == 0
    assert "messages" in retry_req
    assert retry_req["messages"] == [{"role": "user", "content": "hello"}]
    assert "prompt" not in retry_req
    assert retry_api == req.api


def test_retry_plan_applies_shared_prompt_with_role_specific_budget():
    req_data = {
        "model": "m",
        "prompt": "hello",
        "stream": True,
        "max_tokens": 8,
    }
    req = _make_request_info(req_data, api="v1/completions")
    req.prompt_token_ids = [1, 2]
    req.cached_token_ids = [10]
    resch = Rescheduler(True, req, logger=logger)

    plan = resch.build_retry_plan(req_data)
    p_req, p_api = resch.apply_retry_plan(
        {**req_data, "stream": False, "max_tokens": 1},
        plan,
        prefill=True,
    )
    d_req, d_api = resch.apply_retry_plan(req_data.copy(), plan)

    assert p_api == d_api == "v1/completions"
    assert p_req["prompt"] == d_req["prompt"] == [1, 2, 10]
    assert p_req["max_tokens"] == 1
    assert d_req["max_tokens"] == 7


def test_can_resume_after_visible_output_requires_replay_progress():
    req_data = {"model": "m", "prompt": "hello", "stream": True}
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)

    assert not resch.can_resume_after_visible_output(req_data)

    req.prompt_token_ids = [1, 2]
    req.cached_token_ids = [10]
    assert resch.can_resume_after_visible_output(req_data)
    assert not Rescheduler(False, req, logger=logger).can_resume_after_visible_output(req_data)


def test_can_resume_after_visible_output_rejects_ineligible_chat_request():
    req_data = {
        "model": "m",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"type": "function", "function": {"name": "lookup"}}],
        "stream": True,
    }
    req = _make_request_info(req_data)
    req.prompt_token_ids = [1, 2]
    req.cached_token_ids = [10]
    resch = Rescheduler(True, req, logger=logger)

    assert not resch.can_resume_after_visible_output(req_data)


def test_can_resume_after_visible_output_rejects_incomplete_token_tracking():
    req_data = {"model": "m", "prompt": "hello", "stream": True}
    req = _make_request_info(req_data, api="v1/completions")
    resch = Rescheduler(True, req, logger=logger)
    resch.process_stream_chunk(b'data: {"prompt_token_ids":[1,2],"choices":[{"text":"A","token_ids":[10]}]}\n\n')
    resch.process_stream_chunk(b'data: {"choices":[{"text":"B"}]}\n\n')

    assert not resch.can_resume_after_visible_output(req_data)


def test_can_resume_after_visible_output_rejects_finished_stream():
    req_data = {"model": "m", "prompt": "hello", "stream": True}
    req = _make_request_info(req_data, api="v1/completions")
    resch = Rescheduler(True, req, logger=logger)
    resch.process_stream_chunk(
        b'data: {"prompt_token_ids":[1,2],"choices":[{"text":"A","token_ids":[10],"finish_reason":"stop"}]}\n\n'
    )

    assert not resch.can_resume_after_visible_output(req_data)


def test_update_token_id_cache_prompt_from_completion_choice():
    """Completion streams may put ``prompt_token_ids`` on ``choices[0]`` only."""
    req = _make_request_info()
    req.update_token_id_cache(
        {
            "choices": [{"prompt_token_ids": [5, 6, 7], "token_ids": [1], "text": "x"}],
        }
    )
    assert req.prompt_token_ids == [5, 6, 7]
    assert req.cached_token_ids == [1]


def test_prepare_retry_request_completions_engine_switches_api():
    req_data = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "stream": True,
    }
    req = _make_request_info(req_data, api="v1/chat/completions")
    resch = Rescheduler(True, req, logger=logger)
    req.prompt_token_ids = [1]
    req.cached_token_ids = [2, 3]
    retry_req, retry_api = resch.prepare_retry_request(req_data)
    assert "messages" not in retry_req
    assert retry_req["prompt"] == [1, 2, 3]
    assert retry_api == "v1/completions"


def test_prepare_retry_request_nonstream_no_output_ids_budget_is_zero():
    """Without output ids in KV, completion_from_tokens is 0 (no usage fallback)."""
    req_data = {
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 100,
        "stream": False,
    }
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    req.prompt_token_ids = [10, 11, 12]
    req.cached_token_ids = []
    retry_req, retry_api = resch.prepare_retry_request(req_data)
    assert retry_req["max_tokens"] == 100


def test_prepare_retry_request_clamps_max_tokens_when_budget_non_positive():
    req_data = {
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 10,
        "stream": True,
    }
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    req.prompt_token_ids = [0]
    req.cached_token_ids = list(range(1, 25))
    retry_req, retry_api = resch.prepare_retry_request(req_data)
    assert retry_req["max_tokens"] == 1


def test_prepare_retry_request_n_greater_than_one_raises():
    req_data = {
        "messages": [{"role": "user", "content": "hi"}],
        "n": 2,
        "max_tokens": 100,
        "stream": True,
    }
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    req.prompt_token_ids = [1]
    req.cached_token_ids = [2]
    with pytest.raises(HTTPException) as exc_info:
        resch.prepare_retry_request(req_data)
    assert exc_info.value.status_code == 502


def test_prepare_retry_request_response_format_json_mode_raises():
    req_data = {
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_object"},
        "max_tokens": 100,
        "stream": True,
    }
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    req.prompt_token_ids = [1]
    req.cached_token_ids = [2]
    with pytest.raises(HTTPException) as exc_info:
        resch.prepare_retry_request(req_data)
    assert exc_info.value.status_code == 502


def test_completions_retry_eligible_false_for_json_response_format():
    req_data = {
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_object"},
    }
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    assert not resch.completions_retry_eligible_for_chat_request(req_data)


def test_prepare_retry_request_tools_not_eligible_raises():
    req_data = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "x"}}],
        "max_tokens": 100,
        "stream": True,
    }
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    req.prompt_token_ids = [1]
    req.cached_token_ids = [2]
    with pytest.raises(HTTPException) as exc_info:
        resch.prepare_retry_request(req_data)
    assert exc_info.value.status_code == 502


def test_strip_nonstream_preserves_token_ids_when_client_requested():
    body = {
        "prompt_token_ids": [10],
        "choices": [{"message": {"content": "hi"}, "token_ids": [20], "prompt_token_ids": [10]}],
    }
    strip_nonstream_response_body_for_client(body, client_return_token_ids=True)
    assert body["prompt_token_ids"] == [10]
    assert body["choices"][0]["token_ids"] == [20]
    assert body["choices"][0]["prompt_token_ids"] == [10]


def test_strip_stream_chunk_preserves_token_ids_when_client_requested():
    raw = b'data: {"prompt_token_ids": [1], "choices": [{"token_ids": [2], "delta": {}}]}\n\n'
    out = strip_stream_chunk_bytes_for_client(raw, client_return_token_ids=True)
    parsed = json.loads(out.decode().strip().removeprefix("data: "))
    assert parsed["prompt_token_ids"] == [1]
    assert parsed["choices"][0]["token_ids"] == [2]


def test_strip_still_normalizes_recomputed_stop_reason_when_client_requested():
    body = {"choices": [{"message": {"content": "x"}, "stop_reason": "recomputed", "token_ids": [1]}]}
    strip_nonstream_response_body_for_client(body, client_return_token_ids=True)
    assert body["choices"][0]["stop_reason"] == "stop"
    assert body["choices"][0]["token_ids"] == [1]


def test_process_stream_chunk_preserves_token_ids_when_client_requested():
    req_data = {
        "messages": [{"role": "user", "content": "x"}],
        "stream": True,
        "max_tokens": 10,
        "return_token_ids": True,
    }
    chunk = json.dumps(
        {
            "prompt_token_ids": [1, 2],
            "choices": [{"delta": {"content": "a"}, "token_ids": [9]}],
        }
    ).encode()
    req = _make_request_info(req_data, client_expects_token_ids=True)
    resch = Rescheduler(True, req, logger=logger)
    out = resch.process_stream_chunk(chunk)
    assert out is not None
    parsed = json.loads(out.decode())
    assert parsed["prompt_token_ids"] == [1, 2]
    assert parsed["choices"][0]["token_ids"] == [9]
    # Internal cache still works
    assert req.prompt_token_ids == [1, 2]
    assert req.cached_token_ids == [9]


def test_prepare_retry_request_is_engine_agnostic():
    """Recompute retry only rewrites OpenAI request state; engine fields live in adapters."""
    req_data = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "stream": True,
    }
    req = _make_request_info(req_data)
    resch = Rescheduler(True, req, logger=logger)
    req.prompt_token_ids = [1]
    req.cached_token_ids = [2, 3]
    retry_req, retry_api = resch.prepare_retry_request(req_data)
    assert retry_req["prompt"] == [1, 2, 3]
