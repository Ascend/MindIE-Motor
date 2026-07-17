# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import logging

import pytest

from motor.coordinator.router.adapters.stream import (
    chunk_has_recomputed_stop_reason,
    chunk_has_usage_field,
    merge_prompt_tokens_details_into_usage,
    parse_stream_chunk_json,
    stream_chunk_needs_client_strip_parse,
    stream_chunk_needs_sampling_parse,
    strip_openai_token_id_fields_for_client,
    strip_stream_chunk_bytes_for_client,
)


def test_strip_openai_token_id_fields_returns_false_when_noop():
    body = {"choices": [{"delta": {"content": "hi"}}]}
    assert strip_openai_token_id_fields_for_client(body) is False


def test_merge_prompt_tokens_details_skips_when_already_present():
    details = {"cached_tokens": 3}
    chunk = {"usage": {"prompt_tokens": 10, "prompt_tokens_details": details}}
    assert merge_prompt_tokens_details_into_usage(chunk, details) is False


def test_stream_chunk_needs_client_strip_parse_plain_delta():
    chunk = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
    assert stream_chunk_needs_client_strip_parse(chunk) is False


def test_stream_chunk_needs_client_strip_parse_ignores_token_ids_in_content():
    chunk = b'data: {"choices":[{"delta":{"content":"see \\"token_ids\\" in prose"}}]}\n\n'
    assert stream_chunk_needs_client_strip_parse(chunk) is False


def test_stream_chunk_needs_client_strip_parse_detects_field_with_whitespace():
    chunk = b'data: {"choices":[{"delta":{}, "token_ids" : [1]}]}\n\n'
    assert stream_chunk_needs_client_strip_parse(chunk) is True


def test_stream_chunk_with_done_text_still_strips_token_ids():
    chunk = b'data: {"choices":[{"delta":{"content":"[DONE]"},"token_ids":[1]}]}\n\n'
    out = strip_stream_chunk_bytes_for_client(chunk)
    assert out is not chunk
    assert b'"token_ids"' not in out
    assert b'"content":"[DONE]"' in out


def test_stream_chunk_strips_null_token_id_fields():
    chunk = b'data: {"prompt_token_ids":null,"choices":[{"token_ids":null}]}\n\n'
    out = strip_stream_chunk_bytes_for_client(chunk)
    assert out is not chunk
    assert b"prompt_token_ids" not in out
    assert b"token_ids" not in out


def test_recomputed_detection_ignores_content_text():
    chunk = b'data: {"choices":[{"delta":{"content":"\\"stop_reason\\" recomputed"}}]}\n\n'
    assert chunk_has_recomputed_stop_reason(chunk) is False


def test_stream_chunk_needs_client_strip_parse_detects_json_token_ids_field():
    chunk = b'data: {"choices":[{"delta":{"content":"x"},"token_ids":[1]}]}\n\n'
    assert stream_chunk_needs_client_strip_parse(chunk) is True


def test_stream_chunk_needs_client_strip_parse_with_return_token_ids_only_for_recomputed():
    plain = b'data: {"choices":[{"delta":{"content":"x"},"token_ids":[1]}]}\n\n'
    assert stream_chunk_needs_client_strip_parse(plain, client_return_token_ids=True) is False

    recomputed = b'data: {"choices":[{"stop_reason":"recomputed","token_ids":[1]}]}\n\n'
    assert stream_chunk_needs_client_strip_parse(recomputed, client_return_token_ids=True) is True


def test_chunk_has_recomputed_stop_reason_requires_stop_reason_field():
    assert chunk_has_recomputed_stop_reason(b'"stop_reason":"recomputed"') is True
    assert chunk_has_recomputed_stop_reason(b'the word recomputed alone') is False


def test_chunk_has_usage_field():
    assert chunk_has_usage_field(b'data: {"choices":[],"usage":{"prompt_tokens":1}}\n\n') is True
    assert chunk_has_usage_field(b'data: {"choices":[{"delta":{"content":"disk usage"}}]}\n\n') is False


def test_stream_chunk_needs_sampling_parse():
    plain = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
    assert stream_chunk_needs_sampling_parse(plain) is False
    with_logprobs = b'data: {"choices":[{"delta":{"content":"x"},"logprobs":null}]}\n\n'
    assert stream_chunk_needs_sampling_parse(with_logprobs) is True


def test_parse_stream_chunk_json_bytes_native():
    chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    parsed = parse_stream_chunk_json(chunk, logger=None)
    assert parsed is not None
    assert parsed["choices"][0]["delta"]["content"] == "hi"


def test_parse_stream_chunk_json_propagates_type_errors():
    """Payload extraction bugs must not be disguised as invalid upstream data."""
    with pytest.raises((TypeError, AttributeError)):
        parse_stream_chunk_json(None, logger=None)  # type: ignore[arg-type]


def test_strip_stream_chunk_drops_invalid_utf8_when_parse_required():
    chunk = b'data: \xff"token_ids":[1]'
    assert parse_stream_chunk_json(chunk, logger=None) is None
    assert strip_stream_chunk_bytes_for_client(chunk) == b""


def test_strip_stream_chunk_logs_invalid_chunk(caplog):
    logger = logging.getLogger("test_stream_adapter.invalid_chunk")
    chunk = b'data: {"token_ids": ['
    with caplog.at_level(logging.WARNING, logger=logger.name):
        assert strip_stream_chunk_bytes_for_client(chunk, logger=logger) == b""
    assert "Dropping invalid stream chunk after client normalization" in caplog.text


def test_done_sentinel_is_preserved_exactly():
    chunk = b"data: [DONE]\n\n"
    assert strip_stream_chunk_bytes_for_client(chunk) is chunk


def test_strip_stream_chunk_passthrough_malformed_json_with_choices_marker():
    """Fast path trusts upstream; malformed-but-shaped chunks are not parsed on the default strip path."""
    chunk = b'data: {"choices": ['
    assert strip_stream_chunk_bytes_for_client(chunk) is chunk


def test_strip_stream_chunk_passthrough_valid_plain_delta():
    chunk = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
    assert strip_stream_chunk_bytes_for_client(chunk) is chunk


def test_strip_stream_chunk_passthrough_return_token_ids_without_recomputed():
    chunk = b'data: {"prompt_token_ids":[1],"choices":[{"token_ids":[2],"delta":{}}]}\n\n'
    out = strip_stream_chunk_bytes_for_client(chunk, client_return_token_ids=True)
    assert out is chunk


def test_strip_stream_chunk_normalizes_recomputed_when_return_token_ids():
    chunk = b'data: {"choices":[{"stop_reason":"recomputed","token_ids":[1]}]}\n\n'
    out = strip_stream_chunk_bytes_for_client(chunk, client_return_token_ids=True)
    assert out is not chunk
    assert b'"stop_reason":"stop"' in out
    assert b'"token_ids":[1]' in out
