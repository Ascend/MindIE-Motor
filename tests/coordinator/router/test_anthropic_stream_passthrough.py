# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for Anthropic SSE passthrough and protocol-gated OpenAI mutations."""

import json

import pytest

from motor.coordinator.models.request import RequestInfo
from motor.coordinator.router.adapters.stream import strip_stream_chunk_bytes_for_client
from motor.coordinator.router.rescheduler.rescheduler import Rescheduler
from motor.coordinator.router.strategies.unified_pd import UnifiedPDRouter
from motor.common.logger import get_logger

logger = get_logger(__name__)

ANTHROPIC_ENTRY_API = "v1/messages"

# Representative Anthropic SSE frames (event: + data: lines, \n\n terminated).
ANTHROPIC_FRAMES = [
    b'event: message_start\ndata: {"type": "message_start", "message": {"id": "msg_01", "type": "message", '
    b'"role": "assistant", "content": [], "model": "test-model", "stop_reason": null, "usage": '
    b'{"input_tokens": 10, "output_tokens": 1}}}\n\n',
    b'event: content_block_start\ndata: {"type": "content_block_start", "index": 0, '
    b'"content_block": {"type": "text", "text": ""}}\n\n',
    b'event: ping\ndata: {"type": "ping"}\n\n',
    b'event: content_block_delta\ndata: {"type": "content_block_delta", "index": 0, '
    b'"delta": {"type": "text_delta", "text": "Hello"}}\n\n',
    b'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, '
    b'"usage": {"output_tokens": 3}}\n\n',
    b'event: message_stop\ndata: {"type": "message_stop"}\n\n',
]


def _make_anthropic_request_info(**kwargs) -> RequestInfo:
    req_data = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "x"}],
        "stream": True,
        "max_tokens": 10,
    }
    kwargs.setdefault("api", ANTHROPIC_ENTRY_API)
    kwargs.setdefault("entry_api", ANTHROPIC_ENTRY_API)
    return RequestInfo(
        req_id="test-anthropic-req",
        req_data=req_data,
        req_len=1,
        # Anthropic bodies contain "messages"; dispatch sets this for any such body.
        client_expects_chat_shape=True,
        **kwargs,
    )


def test_is_anthropic_entry_api():
    req = _make_anthropic_request_info()
    assert req.is_anthropic_entry

    count_tokens_req = _make_anthropic_request_info(
        api="v1/messages/count_tokens", entry_api="v1/messages/count_tokens"
    )
    assert count_tokens_req.is_anthropic_entry

    openai_req = RequestInfo(
        req_id="test-openai-req",
        req_data={"messages": [{"role": "user", "content": "x"}], "stream": True},
        req_len=1,
        api="v1/chat/completions",
        entry_api="v1/chat/completions",
    )
    assert not openai_req.is_anthropic_entry


@pytest.mark.parametrize("reschedule_enabled", [True, False])
def test_rescheduler_passes_anthropic_frames_byte_identical(reschedule_enabled):
    """Anthropic frames pass the rescheduler path unmodified, no token-id caching attempted."""
    req = _make_anthropic_request_info()
    resch = Rescheduler(reschedule_enabled, req, logger=logger)
    for frame in ANTHROPIC_FRAMES:
        assert resch.process_stream_chunk(frame) == frame
    assert req.prompt_token_ids == []
    assert req.cached_token_ids == []


def test_rescheduler_never_adapts_anthropic_frames_to_chat_shape():
    """Even mid-retry with chat shape expected, Anthropic frames are never rewritten."""
    req = _make_anthropic_request_info()
    resch = Rescheduler(True, req, logger=logger)
    resch.is_rescheduling = True
    for frame in ANTHROPIC_FRAMES:
        assert resch.process_stream_chunk(frame) == frame


@pytest.mark.parametrize("client_return_token_ids", [True, False])
def test_strip_path_passes_anthropic_frames_byte_identical(client_return_token_ids):
    """The non-rescheduler strip path never drops or reformats Anthropic frames."""
    for frame in ANTHROPIC_FRAMES:
        out = strip_stream_chunk_bytes_for_client(frame, client_return_token_ids=client_return_token_ids)
        assert out == frame


def test_strip_path_passes_anthropic_frames_with_crlf():
    frame = b'event: ping\r\ndata: {"type": "ping"}\r\n\r\n'
    assert strip_stream_chunk_bytes_for_client(frame) == frame


@pytest.mark.parametrize("reschedule_enabled", [True, False])
def test_openai_frames_still_stripped_and_normalized(reschedule_enabled):
    """OpenAI-shaped chunks keep exact pre-change behavior: token ids stripped, recomputed -> stop."""
    req_data = {"messages": [{"role": "user", "content": "x"}], "stream": True, "max_tokens": 10}
    chunk = json.dumps(
        {
            "prompt_token_ids": [1, 2],
            "choices": [{"delta": {"content": "tok"}, "token_ids": [10, 20], "stop_reason": "recomputed"}],
        }
    ).encode()
    req = RequestInfo(
        req_id="test-openai-req",
        req_data=req_data,
        req_len=1,
        api="v1/chat/completions",
        entry_api="v1/chat/completions",
    )
    resch = Rescheduler(reschedule_enabled, req, logger=logger)
    out = resch.process_stream_chunk(chunk)
    parsed = json.loads(out.decode())
    assert "prompt_token_ids" not in parsed
    assert "token_ids" not in parsed["choices"][0]
    assert parsed["choices"][0]["stop_reason"] == "stop"
    if reschedule_enabled:
        assert req.prompt_token_ids == [1, 2]
        assert req.cached_token_ids == [10, 20]
    else:
        assert req.prompt_token_ids == []
        assert req.cached_token_ids == []

    strip_chunk = b'data: {"prompt_token_ids": [1], "choices": [{"token_ids": [2], "delta": {}}]}\n\n'
    stripped = strip_stream_chunk_bytes_for_client(strip_chunk)
    parsed = json.loads(stripped.decode().strip().removeprefix("data: "))
    assert "prompt_token_ids" not in parsed
    assert "token_ids" not in parsed["choices"][0]


def _make_unified_pd_router(req_info: RequestInfo) -> UnifiedPDRouter:
    """Bare instance for unit-testing usage-merge helpers (no scheduler/config needed)."""
    router = UnifiedPDRouter.__new__(UnifiedPDRouter)
    router.req_info = req_info
    return router


def test_unified_pd_prompt_tokens_details_merge_skipped_for_anthropic():
    """OpenAI usage mutations never touch Anthropic-shaped bodies."""
    req = _make_anthropic_request_info()
    req.prompt_tokens_details = {"cached_tokens": 4}
    router = _make_unified_pd_router(req)
    body = {"usage": {"input_tokens": 10, "output_tokens": 3}}
    assert router._merge_prompt_tokens_details(body) is False
    assert body == {"usage": {"input_tokens": 10, "output_tokens": 3}}

    capture_body = {"usage": {"input_tokens": 10, "cache_read_input_tokens": 4}}
    router._capture_prompt_tokens_details(capture_body)
    assert req.prompt_tokens_details == {"cached_tokens": 4}


def test_unified_pd_prompt_tokens_details_merge_unchanged_for_openai():
    req = RequestInfo(
        req_id="test-openai-req",
        req_data={"prompt": "hello", "stream": False},
        req_len=1,
        api="v1/completions",
        entry_api="v1/completions",
    )
    req.prompt_tokens_details = {"cached_tokens": 4}
    router = _make_unified_pd_router(req)
    body = {"usage": {"prompt_tokens": 10, "completion_tokens": 3}}
    assert router._merge_prompt_tokens_details(body) is True
    assert body["usage"]["prompt_tokens_details"] == {"cached_tokens": 4}


# ---------------------------------------------------------------------------
# Anthropic usage cache fields (capture / non-stream merge / stream merge)
# ---------------------------------------------------------------------------

# Prefill leg hit 6 of 10 prompt tokens: input_tokens = 10 - 6, cache_creation = 0.
CAPTURED_ANTHROPIC_USAGE = {
    "input_tokens": 4,
    "cache_read_input_tokens": 6,
    "cache_creation_input_tokens": 0,
}

PREFILL_BODY_WITH_CACHE_USAGE = {
    "usage": {
        "input_tokens": 4,
        "output_tokens": 1,
        "cache_read_input_tokens": 6,
        "cache_creation_input_tokens": 0,
    }
}


def _captured_anthropic_router():
    req = _make_anthropic_request_info()
    req.update_anthropic_input_usage(dict(CAPTURED_ANTHROPIC_USAGE))
    return req, _make_unified_pd_router(req)


class TestAnthropicUsageCacheFields:
    def test_capture_from_prefill_body_with_cache_keys(self):
        """Top-level usage with cache_read_input_tokens is captured (all three keys)."""
        req = _make_anthropic_request_info()
        router = _make_unified_pd_router(req)
        router._capture_prompt_tokens_details(PREFILL_BODY_WITH_CACHE_USAGE)
        assert req.anthropic_input_usage == CAPTURED_ANTHROPIC_USAGE
        # OpenAI storage is never populated by Anthropic capture.
        assert req.prompt_tokens_details == {}

    def test_capture_from_prefill_result_payload(self):
        """Handoff PrefillResult-shaped bodies are probed under payload as well."""
        req = _make_anthropic_request_info()
        router = _make_unified_pd_router(req)
        body = {"object": "motor.prefill_result", "payload": dict(PREFILL_BODY_WITH_CACHE_USAGE)}
        router._capture_prompt_tokens_details(body)
        assert req.anthropic_input_usage == CAPTURED_ANTHROPIC_USAGE

    def test_capture_absent_cache_info_leaves_storage_empty(self):
        """No cache_read_input_tokens -> nothing captured (absent-info case)."""
        req = _make_anthropic_request_info()
        router = _make_unified_pd_router(req)
        router._capture_prompt_tokens_details({"usage": {"input_tokens": 10, "output_tokens": 1}})
        assert req.anthropic_input_usage == {}

    def test_nonstream_merge_overwrites_input_usage(self):
        """Decode-leg usage gets the prefill leg's three fields; output_tokens preserved."""
        req, router = _captured_anthropic_router()
        body = {
            "id": "msg_01",
            "usage": {
                "input_tokens": 10,
                "output_tokens": 3,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
        assert router._merge_prompt_tokens_details(body) is True
        assert body["usage"] == {
            "input_tokens": 4,
            "output_tokens": 3,
            "cache_read_input_tokens": 6,
            "cache_creation_input_tokens": 0,
        }

    def test_nonstream_merge_without_capture_untouched(self):
        """No captured info -> body untouched, no keys added, no nulls."""
        req = _make_anthropic_request_info()
        router = _make_unified_pd_router(req)
        body = {"usage": {"input_tokens": 10, "output_tokens": 3}}
        assert router._merge_prompt_tokens_details(body) is False
        assert body == {"usage": {"input_tokens": 10, "output_tokens": 3}}

    @pytest.mark.parametrize("path", ["rescheduler", "unified_pd"])
    def test_stream_message_start_merged(self, path):
        """message_start carries the captured usage on both stream-processing paths."""
        req, router = _captured_anthropic_router()
        if path == "rescheduler":
            out = Rescheduler(True, req, logger=logger).process_stream_chunk(ANTHROPIC_FRAMES[0])
        else:
            out = router._merge_prompt_tokens_details_into_stream_chunk(ANTHROPIC_FRAMES[0])
        assert out != ANTHROPIC_FRAMES[0]
        assert out.startswith(b"event: message_start\n")
        assert out.endswith(b"\n\n")
        payload = json.loads(out.decode().split("data: ", 1)[1])
        assert payload["type"] == "message_start"
        assert payload["message"]["usage"] == {
            "input_tokens": 4,
            "output_tokens": 1,
            "cache_read_input_tokens": 6,
            "cache_creation_input_tokens": 0,
        }

    @pytest.mark.parametrize("path", ["rescheduler", "unified_pd"])
    def test_stream_other_frames_byte_identical(self, path):
        """content_block_delta / message_delta / message_stop etc. never change."""
        req, router = _captured_anthropic_router()
        resch = Rescheduler(True, req, logger=logger)
        for frame in ANTHROPIC_FRAMES[1:]:
            if path == "rescheduler":
                assert resch.process_stream_chunk(frame) == frame
            else:
                assert router._merge_prompt_tokens_details_into_stream_chunk(frame) == frame

    @pytest.mark.parametrize("path", ["rescheduler", "unified_pd"])
    def test_stream_message_start_without_event_line(self, path):
        """A bare data: message_start frame is merged and stays event-line-free."""
        req, router = _captured_anthropic_router()
        frame = (
            b'data: {"type": "message_start", "message": {"id": "msg_01", "usage": '
            b'{"input_tokens": 10, "output_tokens": 1}}}\n\n'
        )
        if path == "rescheduler":
            out = Rescheduler(True, req, logger=logger).process_stream_chunk(frame)
        else:
            out = router._merge_prompt_tokens_details_into_stream_chunk(frame)
        assert not out.startswith(b"event:")
        assert out.endswith(b"\n\n")
        payload = json.loads(out.decode().strip().removeprefix("data: "))
        assert payload["message"]["usage"]["input_tokens"] == 4
        assert payload["message"]["usage"]["cache_read_input_tokens"] == 6

    @pytest.mark.parametrize("path", ["rescheduler", "unified_pd"])
    def test_stream_absent_cache_info_passthrough(self, path):
        """No captured info -> every frame byte-identical, no keys added, no nulls."""
        req = _make_anthropic_request_info()
        router = _make_unified_pd_router(req)
        resch = Rescheduler(True, req, logger=logger)
        for frame in ANTHROPIC_FRAMES:
            if path == "rescheduler":
                assert resch.process_stream_chunk(frame) == frame
            else:
                assert router._merge_prompt_tokens_details_into_stream_chunk(frame) == frame

    def test_openai_capture_and_merge_unchanged(self):
        """OpenAI path never touches anthropic_input_usage and still merges details."""
        req = RequestInfo(
            req_id="test-openai-req",
            req_data={"prompt": "hello", "stream": False},
            req_len=1,
            api="v1/completions",
            entry_api="v1/completions",
        )
        router = _make_unified_pd_router(req)
        router.logger = logger  # OpenAI stream-merge path logs via self.logger
        router._capture_prompt_tokens_details(
            {"usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 4}}}
        )
        assert req.prompt_tokens_details == {"cached_tokens": 4}
        assert req.anthropic_input_usage == {}

        chunk = b'data: {"usage": {"prompt_tokens": 10, "completion_tokens": 3}}\n\n'
        out = router._merge_prompt_tokens_details_into_stream_chunk(chunk)
        payload = json.loads(out.decode().strip().removeprefix("data: "))
        assert payload["usage"]["prompt_tokens_details"] == {"cached_tokens": 4}
        # An OpenAI-shaped message_start-lookalike chunk is never Anthropic-merged.
        tricky = b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 1}}}\n\n'
        assert router._merge_prompt_tokens_details_into_stream_chunk(tricky) == tricky
