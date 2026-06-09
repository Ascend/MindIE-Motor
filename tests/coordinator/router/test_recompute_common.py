# -*- coding: utf-8 -*-
"""Unit tests for motor.coordinator.router.recompute."""

import json

import pytest
from fastapi import HTTPException

import motor.coordinator.router.recompute as recompute_common
from motor.common.logger import get_logger
from tests.coordinator.router.mock_openai_request import mock_nostream_response

logger = get_logger(__name__)


def test_modify_req_id_retry_segment_bumps_two_digits():
    rid = "0123456789012345" + "00" + "tail"
    assert recompute_common.modify_req_id_retry_segment(rid) == "0123456789012345" + "01" + "tail"


def test_modify_req_id_retry_segment_rolls_99():
    rid = "0123456789012345" + "99" + "x"
    assert recompute_common.modify_req_id_retry_segment(rid) == "0123456789012345" + "00" + "x"


def test_modify_req_id_short_unchanged():
    assert recompute_common.modify_req_id_retry_segment("short") == "short"


def test_modify_req_id_non_digit_segment_unchanged():
    rid = "0123456789012345" + "ab" + "z"
    assert recompute_common.modify_req_id_retry_segment(rid) == rid


def test_bump_req_id_after_recompute_prepare_updates_req_id():
    class _RI:
        def __init__(self):
            self.req_id = "012345678901234500suffix"

    ri = _RI()
    recompute_common.bump_req_id_after_recompute_prepare(ri, retry_count=2, logger=logger)
    assert ri.req_id == "012345678901234501suffix"


def test_copy_req_data_for_engine_strips_origin_cap():
    inner = {"a": 1}
    req = {
        "messages": [inner],
        "max_tokens": 10,
        "_origin_max_tokens": 100,
        recompute_common._CUMULATIVE_COMPLETION_TOKENS_KEY: 42,
    }
    out = recompute_common.copy_req_data_for_engine(req)
    assert out is not req
    assert "_origin_max_tokens" not in out
    assert recompute_common._CUMULATIVE_COMPLETION_TOKENS_KEY not in out
    assert out["max_tokens"] == 10
    assert out["messages"][0] is inner
    plain = {"max_tokens": 5}
    assert recompute_common.copy_req_data_for_engine(plain) is plain


def test_extract_request_info_chat():
    req = {
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "max_tokens": 64,
    }
    info = recompute_common.extract_request_info(req)
    assert info["chat_flag"] is True
    assert info["stream_flag"] is True
    assert info["origin_prompt"] == "hi"
    assert info["origin_max_tokens"] == 64
    assert info["completion_tokens"] == 0
    assert info["generated_token"] == ""
    assert info["cached_prompt_token_ids"] is None
    assert not info["cached_output_token_ids"]


def test_update_token_id_cache_prompt_once_and_extend_output():
    ri = recompute_common.extract_request_info({"messages": [{"role": "u", "content": "a"}], "stream": True})
    recompute_common.update_token_id_cache(
        ri,
        {"prompt_token_ids": [1, 2], "choices": [{"token_ids": [10]}]},
    )
    assert ri["cached_prompt_token_ids"] == [1, 2]
    assert ri["cached_output_token_ids"] == [10]
    recompute_common.update_token_id_cache(
        ri,
        {"prompt_token_ids": [99, 99], "choices": [{"token_ids": [20]}]},
    )
    assert ri["cached_prompt_token_ids"] == [1, 2]
    assert ri["cached_output_token_ids"] == [10, 20]


def test_parse_stream_chunk_json_sse_prefix():
    raw = b'data: {"choices": [{"delta": {"content": "a"}}]}'
    obj = recompute_common.parse_stream_chunk_json(raw, logger=None)
    assert obj["choices"][0]["delta"]["content"] == "a"


def test_process_stream_chunk_recompute_disabled_sets_policy_no_kv_transfer():
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    ri = recompute_common.extract_request_info(req_data)
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
    out = recompute_common.process_stream_chunk(
        chunk, ri, req_data, retry_count=0, logger=None, recompute_enabled=False
    )
    assert out is None
    assert ri.get(recompute_common.RECOMPUTE_BLOCKED_BY_POLICY_KEY) is True
    assert "recompute_kv_transfer" not in ri


def test_recompute_disabled_http_exception_status():
    exc = recompute_common.recompute_disabled_http_exception()
    assert exc.status_code == 503


def test_process_stream_chunk_recomputed():
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    ri = recompute_common.extract_request_info(req_data)
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
    out = recompute_common.process_stream_chunk(
        chunk, ri, req_data, retry_count=0, logger=None, nonstream_retry_patch=None
    )
    assert out is None
    assert ri["recompute_kv_transfer"]["all_token_ids"] == [1, 2, 10, 20]
    assert ri["recompute_kv_transfer"]["prompt_token_ids"] == [1, 2]


def test_process_stream_chunk_strips_token_ids_for_client():
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    ri = recompute_common.extract_request_info(req_data)
    chunk = json.dumps(
        {
            "prompt_token_ids": [1, 2],
            "choices": [{"delta": {"content": "a"}, "token_ids": [9]}],
        }
    ).encode()
    out = recompute_common.process_stream_chunk(
        chunk, ri, req_data, retry_count=0, logger=None, nonstream_retry_patch=None
    )
    assert out is not None
    parsed = json.loads(out.decode())
    assert "prompt_token_ids" not in parsed
    assert "token_ids" not in parsed["choices"][0]
    assert ri["cached_prompt_token_ids"] == [1, 2]
    assert ri["cached_output_token_ids"] == [9]


def test_process_stream_chunk_adapts_text_completion_chunk_for_chat_entry_without_recompute_mode():
    """AISBench / OpenAI clients expect delta; first decode may still be Completion-shaped."""
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    ri = recompute_common.extract_request_info(req_data)
    chunk = json.dumps(
        {
            "object": "text_completion",
            "id": "cmpl-test",
            "choices": [{"index": 0, "text": "6", "finish_reason": None, "logprobs": None}],
        }
    ).encode()
    st: dict = {}
    out = recompute_common.process_stream_chunk(
        chunk,
        ri,
        req_data,
        retry_count=0,
        logger=None,
        entry_api="v1/chat/completions",
        stream_adapter_state=st,
        req_id="cmpl-ingress-01",
        recompute_enabled=True,
    )
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
    out = recompute_common.strip_stream_chunk_bytes_for_client(raw)
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
    recompute_common.strip_nonstream_response_body_for_client(body)
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
    recompute_common.strip_nonstream_response_body_for_client(body)
    ch0 = body["choices"][0]
    assert "prompt_token_ids" not in body
    assert "prompt_token_ids" not in ch0
    assert "token_ids" not in ch0


def test_strip_nonstream_maps_recomputed_stop_reason():
    body = {"choices": [{"message": {"content": "x"}, "stop_reason": "recomputed"}]}
    recompute_common.strip_nonstream_response_body_for_client(body)
    assert body["choices"][0]["stop_reason"] == "stop"


def test_process_stream_chunk_drops_unparseable_chunk():
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    ri = recompute_common.extract_request_info(req_data)
    out = recompute_common.process_stream_chunk(
        b"not valid json {{{", ri, req_data, retry_count=0, logger=None, nonstream_retry_patch=None
    )
    assert out == b""


def test_process_stream_chunk_preserves_done_marker():
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    ri = recompute_common.extract_request_info(req_data)
    done_line = b"data: [DONE]\n\n"
    out = recompute_common.process_stream_chunk(
        done_line, ri, req_data, retry_count=0, logger=None, nonstream_retry_patch=None
    )
    assert out == done_line


def test_prepare_retry_request_req_len_ignores_internal_keys():
    req_data = {"messages": [{"role": "user", "content": "a"}], "max_tokens": 100, "stream": True}
    ri = recompute_common.extract_request_info(req_data)
    ri["recompute_kv_transfer"] = {"all_token_ids": [1, 2], "prompt_token_ids": [1]}

    class _RI:
        req_len = 0
        req_data = None

    stub = _RI()
    recompute_common.prepare_retry_request(req_data, ri, new_retry_count=1, req_id="r", logger=logger, req_info=stub)
    assert "_origin_max_tokens" in req_data
    assert stub.req_len == len(json.dumps(recompute_common.copy_req_data_for_engine(req_data)).encode("utf-8"))


def test_process_stream_chunk_recomputed_missing_prompt_token_ids_raises():
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    ri = recompute_common.extract_request_info(req_data)
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
    with pytest.raises(HTTPException) as exc_info:
        recompute_common.process_stream_chunk(
            chunk, ri, req_data, retry_count=0, logger=None, nonstream_retry_patch=None
        )
    assert exc_info.value.status_code == 502


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
    ri = recompute_common.extract_request_info(req_data)
    ri["recompute_kv_transfer"] = {"all_token_ids": [1, 2, 3], "prompt_token_ids": [1]}
    recompute_common.prepare_retry_request(req_data, ri, new_retry_count=1, req_id="r", logger=logger, req_info=None)
    assert "messages" not in req_data
    assert req_data["prompt"] == [1, 2, 3]


def test_prepare_retry_request_chat_eligible_uses_completions_prompt():
    req_data = {"messages": [{"role": "user", "content": "a"}], "max_tokens": 100, "stream": True}
    ri = recompute_common.extract_request_info(req_data)
    ri["recompute_kv_transfer"] = {"all_token_ids": [1, 2, 3], "prompt_token_ids": [1]}
    recompute_common.prepare_retry_request(
        req_data,
        ri,
        new_retry_count=2,
        req_id="rid",
        logger=logger,
        req_info=None,
    )
    assert "messages" not in req_data
    assert req_data["prompt"] == [1, 2, 3]
    assert req_data["max_tokens"] == 100 - 2 + 1
    assert req_data["_origin_max_tokens"] == 100


def test_prepare_retry_request_multi_round_max_tokens_uses_origin_cap():
    """Each recompute leg adds to cumulative completion; max_tokens stays under origin cap."""
    req_data = {"messages": [{"role": "user", "content": "a"}], "max_tokens": 100, "stream": True}
    ri1 = recompute_common.extract_request_info(req_data)
    ri1["recompute_kv_transfer"] = {"all_token_ids": list(range(10)), "prompt_token_ids": [0]}
    recompute_common.prepare_retry_request(req_data, ri1, new_retry_count=1, req_id="r", logger=logger, req_info=None)
    assert req_data["max_tokens"] == 100 - 9 + 1
    assert req_data[recompute_common._CUMULATIVE_COMPLETION_TOKENS_KEY] == 9
    ri2 = recompute_common.extract_request_info(req_data)
    ri2["recompute_kv_transfer"] = {"all_token_ids": list(range(15)), "prompt_token_ids": [0]}
    recompute_common.prepare_retry_request(req_data, ri2, new_retry_count=2, req_id="r", logger=logger, req_info=None)
    # Leg2 adds 14 tokens; cumulative 9+14=23 — not 14 alone (would wrongly inflate max_tokens).
    assert req_data[recompute_common._CUMULATIVE_COMPLETION_TOKENS_KEY] == 23
    assert req_data["max_tokens"] == 100 - 23 + 1


def test_prepare_retry_request_missing_kv_raises():
    req_data = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 50, "stream": True}
    ri = recompute_common.extract_request_info(req_data)
    ri["recompute_kv_transfer"] = {}
    with pytest.raises(HTTPException) as exc_info:
        recompute_common.prepare_retry_request(
            req_data, ri, new_retry_count=1, req_id="r", logger=logger, req_info=None
        )
    assert exc_info.value.status_code == 502


def test_update_token_id_cache_prompt_from_completion_choice():
    """Completion streams may put ``prompt_token_ids`` on ``choices[0]`` only."""
    ri = {"cached_prompt_token_ids": None, "cached_output_token_ids": []}
    chunk = {
        "choices": [{"prompt_token_ids": [5, 6, 7], "token_ids": [1], "text": "x"}],
    }
    recompute_common.update_token_id_cache(ri, chunk)
    assert ri["cached_prompt_token_ids"] == [5, 6, 7]


def test_prepare_retry_request_completions_engine_switches_api():
    req_data = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "stream": True,
    }
    ri = recompute_common.extract_request_info(req_data)
    ri["recompute_kv_transfer"] = {"all_token_ids": [1, 2, 3], "prompt_token_ids": [1]}

    class _RI:
        req_len = 0
        req_data = None
        api = "v1/chat/completions"
        recompute_engine_mode = None

    stub = _RI()
    recompute_common.prepare_retry_request(
        req_data,
        ri,
        new_retry_count=1,
        req_id="r",
        logger=logger,
        req_info=stub,
    )
    assert "messages" not in req_data
    assert req_data["prompt"] == [1, 2, 3]
    assert stub.api == "v1/completions"
    assert stub.recompute_engine_mode == "completions"


def test_prepare_retry_request_nonstream_no_output_ids_budget_is_zero():
    """Without output ids in KV, completion_from_tokens is 0 (no usage fallback)."""
    req_data = {
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 100,
        "stream": False,
    }
    ri = recompute_common.extract_request_info(req_data)
    ri["completion_tokens"] = 4
    ri["recompute_kv_transfer"] = {
        "all_token_ids": [10, 11, 12],
        "prompt_token_ids": [10, 11, 12],
    }
    recompute_common.prepare_retry_request(req_data, ri, new_retry_count=1, req_id="r", logger=logger, req_info=None)
    assert req_data["max_tokens"] == 100 - 0 + 1


def test_prepare_retry_request_clamps_max_tokens_when_budget_non_positive():
    req_data = {
        "messages": [{"role": "user", "content": "x"}],
        "max_tokens": 10,
        "stream": True,
    }
    ri = recompute_common.extract_request_info(req_data)
    ri["recompute_kv_transfer"] = {
        "all_token_ids": list(range(25)),
        "prompt_token_ids": [0],
    }
    recompute_common.prepare_retry_request(req_data, ri, new_retry_count=1, req_id="r", logger=logger, req_info=None)
    assert req_data["max_tokens"] == 1


def test_prepare_retry_request_n_greater_than_one_raises():
    req_data = {
        "messages": [{"role": "user", "content": "hi"}],
        "n": 2,
        "max_tokens": 100,
        "stream": True,
    }
    ri = recompute_common.extract_request_info(req_data)
    ri["recompute_kv_transfer"] = {"all_token_ids": [1, 2], "prompt_token_ids": [1]}
    with pytest.raises(HTTPException) as exc_info:
        recompute_common.prepare_retry_request(
            req_data,
            ri,
            new_retry_count=1,
            req_id="r",
            logger=logger,
            req_info=None,
        )
    assert exc_info.value.status_code == 502


def test_prepare_retry_request_response_format_json_mode_raises():
    req_data = {
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_object"},
        "max_tokens": 100,
        "stream": True,
    }
    ri = recompute_common.extract_request_info(req_data)
    ri["recompute_kv_transfer"] = {"all_token_ids": [1, 2], "prompt_token_ids": [1]}
    with pytest.raises(HTTPException) as exc_info:
        recompute_common.prepare_retry_request(
            req_data,
            ri,
            new_retry_count=1,
            req_id="r",
            logger=logger,
            req_info=None,
        )
    assert exc_info.value.status_code == 502


def test_completions_retry_eligible_false_for_json_response_format():
    req_data = {
        "messages": [{"role": "user", "content": "hi"}],
        "response_format": {"type": "json_object"},
    }
    assert not recompute_common.completions_retry_eligible_for_chat_request(req_data)


def test_prepare_retry_request_tools_not_eligible_raises():
    req_data = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "x"}}],
        "max_tokens": 100,
        "stream": True,
    }
    ri = recompute_common.extract_request_info(req_data)
    ri["recompute_kv_transfer"] = {"all_token_ids": [1, 2], "prompt_token_ids": [1]}
    with pytest.raises(HTTPException) as exc_info:
        recompute_common.prepare_retry_request(
            req_data,
            ri,
            new_retry_count=1,
            req_id="r",
            logger=logger,
            req_info=None,
        )
    assert exc_info.value.status_code == 502


def test_is_recomputed_nonstream_response():
    assert recompute_common.is_recomputed_nonstream_response({"choices": [{"stop_reason": "recomputed"}]})
    assert not recompute_common.is_recomputed_nonstream_response({"choices": [{}]})


def test_mock_nostream_recomputed_with_return_token_ids():
    req_data = {
        "messages": [{"role": "user", "content": "0"}],
        "max_tokens": 5,
        "stream": False,
        "return_token_ids": True,
    }
    chunks = list(mock_nostream_response(req_data, max_num=20, recomputed=True))
    assert chunks
    body = json.loads(chunks[0])
    assert body["choices"][0]["stop_reason"] == "recomputed"
    assert isinstance(body.get("prompt_token_ids"), list)
    assert isinstance(body["choices"][0].get("token_ids"), list)
    ri = recompute_common.build_request_info_for_nonstream_recompute(req_data, body)
    kv = ri["recompute_kv_transfer"]
    assert kv["prompt_token_ids"] == body["prompt_token_ids"]
    assert kv["all_token_ids"] == body["prompt_token_ids"] + body["choices"][0]["token_ids"]


def test_build_request_info_for_nonstream_recompute():
    req_data = {"messages": [{"role": "user", "content": "hello"}], "stream": False, "max_tokens": 50}
    body = {
        "prompt_token_ids": [7, 8],
        "choices": [
            {
                "message": {"content": " partial"},
                "stop_reason": "recomputed",
                "token_ids": [9, 10],
            }
        ],
        "usage": {"completion_tokens": 2},
    }
    ri = recompute_common.build_request_info_for_nonstream_recompute(req_data, body)
    assert ri["generated_token"] == " partial"
    assert ri["completion_tokens"] == 2
    assert ri["recompute_kv_transfer"]["all_token_ids"] == [7, 8, 9, 10]
    assert ri["recompute_kv_transfer"]["prompt_token_ids"] == [7, 8]


def test_validate_nonstream_recompute_body_ok_without_output_token_ids():
    body = {
        "prompt_token_ids": [7, 8],
        "choices": [{"stop_reason": "recomputed"}],
    }
    recompute_common.validate_nonstream_recompute_body("rid", body, logger=None)


def test_validate_nonstream_recompute_body_usage_without_token_ids_raises():
    body = {
        "prompt_token_ids": [7, 8],
        "usage": {"completion_tokens": 3},
        "choices": [{"stop_reason": "recomputed"}],
    }
    with pytest.raises(HTTPException) as exc_info:
        recompute_common.validate_nonstream_recompute_body("rid", body, logger=None)
    assert exc_info.value.status_code == 502


def test_validate_nonstream_recompute_body_missing_prompt_ids_raises():
    body = {
        "choices": [{"stop_reason": "recomputed"}],
    }
    with pytest.raises(HTTPException) as exc_info:
        recompute_common.validate_nonstream_recompute_body("rid", body, logger=None)
    assert exc_info.value.status_code == 502


def test_validate_nonstream_recompute_body_invalid_output_token_ids_raises():
    body = {
        "prompt_token_ids": [1],
        "choices": [{"stop_reason": "recomputed", "token_ids": "bad"}],
    }
    with pytest.raises(HTTPException) as exc_info:
        recompute_common.validate_nonstream_recompute_body("rid", body, logger=None)
    assert exc_info.value.status_code == 502


def test_extract_request_info_propagates_client_return_token_ids():
    req = {"messages": [{"role": "user", "content": "hi"}], "stream": True, "max_tokens": 64}
    info = recompute_common.extract_request_info(req)
    assert info["client_return_token_ids"] is False

    req2 = {**req, "_client_return_token_ids": True}
    info2 = recompute_common.extract_request_info(req2)
    assert info2["client_return_token_ids"] is True


def test_copy_req_data_for_engine_strips_client_return_token_ids():
    req = {"max_tokens": 10, "_client_return_token_ids": True}
    out = recompute_common.copy_req_data_for_engine(req)
    assert "_client_return_token_ids" not in out


def test_strip_nonstream_preserves_token_ids_when_client_requested():
    body = {
        "prompt_token_ids": [10],
        "choices": [{"message": {"content": "hi"}, "token_ids": [20], "prompt_token_ids": [10]}],
    }
    recompute_common.strip_nonstream_response_body_for_client(body, client_return_token_ids=True)
    assert body["prompt_token_ids"] == [10]
    assert body["choices"][0]["token_ids"] == [20]
    assert body["choices"][0]["prompt_token_ids"] == [10]


def test_strip_stream_chunk_preserves_token_ids_when_client_requested():
    raw = b'data: {"prompt_token_ids": [1], "choices": [{"token_ids": [2], "delta": {}}]}\n\n'
    out = recompute_common.strip_stream_chunk_bytes_for_client(raw, client_return_token_ids=True)
    parsed = json.loads(out.decode().strip().removeprefix("data: "))
    assert parsed["prompt_token_ids"] == [1]
    assert parsed["choices"][0]["token_ids"] == [2]


def test_strip_still_normalizes_recomputed_stop_reason_when_client_requested():
    body = {"choices": [{"message": {"content": "x"}, "stop_reason": "recomputed", "token_ids": [1]}]}
    recompute_common.strip_nonstream_response_body_for_client(body, client_return_token_ids=True)
    assert body["choices"][0]["stop_reason"] == "stop"
    assert body["choices"][0]["token_ids"] == [1]


def test_process_stream_chunk_preserves_token_ids_when_client_requested():
    req_data = {
        "messages": [{"role": "user", "content": "x"}],
        "stream": True,
        "max_tokens": 10,
        "_client_return_token_ids": True,
    }
    ri = recompute_common.extract_request_info(req_data)
    chunk = json.dumps(
        {
            "prompt_token_ids": [1, 2],
            "choices": [{"delta": {"content": "a"}, "token_ids": [9]}],
        }
    ).encode()
    out = recompute_common.process_stream_chunk(
        chunk, ri, req_data, retry_count=0, logger=None, nonstream_retry_patch=None
    )
    assert out is not None
    parsed = json.loads(out.decode())
    assert parsed["prompt_token_ids"] == [1, 2]
    assert parsed["choices"][0]["token_ids"] == [9]
    # Internal cache still works
    assert ri["cached_prompt_token_ids"] == [1, 2]
    assert ri["cached_output_token_ids"] == [9]


def test_prepare_retry_request_is_engine_agnostic():
    """Recompute retry only rewrites OpenAI request state; engine fields live in adapters."""
    req_data = {
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
        "stream": True,
    }
    ri = recompute_common.extract_request_info(req_data)
    ri["recompute_kv_transfer"] = {"all_token_ids": [1, 2, 3], "prompt_token_ids": [1]}
    recompute_common.prepare_retry_request(req_data, ri, new_retry_count=1, req_id="r", logger=logger, req_info=None)
    assert req_data["prompt"] == [1, 2, 3]
