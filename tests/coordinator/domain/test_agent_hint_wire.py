# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Unit tests for the X-Agent-Hint wire-format adapter."""

import base64
import json

import pytest

from motor.coordinator.domain.agent_hint import parse_agent_hint
from motor.coordinator.domain.agent_hint_wire import (
    AGENT_HINT_WIRE_HEADER,
    STRICT_PRIORITY_TOKEN_HEADER,
    apply_agent_hint_header,
    decode_agent_hint_header,
    extract_strict_priority_from_hint_headers,
    strict_priority_trusted,
)

# Golden vector: plugins/ascend-llm/models/llm/hint.py::encode_agent_hint
# (AscendAgent-Dify) over the fixed hint below — cross-repo interop is pinned
# by this constant. Decoded payload:
# {"session":{"session_id":"s-7f3a2c","agent_type":"researcher","epoch":0},
#  "priority":{"level":1,"reason":"user_waiting"},
#  "prediction":{"latency_ms":4100,"remaining_calls":3,"output_tokens":420}}
GOLDEN_HINT_B64 = (
    "eyJzZXNzaW9uIjp7InNlc3Npb25faWQiOiJzLTdmM2EyYyIsImFnZW50X3R5cGUiOiJyZXNlYXJjaGVy"
    "IiwiZXBvY2giOjB9LCJwcmlvcml0eSI6eyJsZXZlbCI6MSwicmVhc29uIjoidXNlcl93YWl0aW5nIn0s"
    "InByZWRpY3Rpb24iOnsibGF0ZW5jeV9tcyI6NDEwMCwicmVtYWluaW5nX2NhbGxzIjozLCJvdXRwdXRf"
    "dG9rZW5zIjo0MjB9fQ"
)


def _b64(obj: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


def test_decode_golden_vector_matches_dify_plugin_output():
    """A real Dify-plugin X-Agent-Hint value must decode to the documented hint."""
    assert decode_agent_hint_header(GOLDEN_HINT_B64) == {
        "session": {"session_id": "s-7f3a2c", "agent_type": "researcher", "epoch": 0},
        "priority": {"level": 1, "reason": "user_waiting"},
        "prediction": {"latency_ms": 4100, "remaining_calls": 3, "output_tokens": 420},
    }


def test_decode_accepts_padded_and_unpadded_values():
    """The Dify plugin strips base64 padding; both forms must decode identically."""
    padded = GOLDEN_HINT_B64 + "=" * (-len(GOLDEN_HINT_B64) % 4)
    assert decode_agent_hint_header(GOLDEN_HINT_B64) == decode_agent_hint_header(padded)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "not-base64!!",
        _b64(["not", "an", "object"]),
        base64.urlsafe_b64encode(b"\xff\xfe\xfd").decode("ascii"),
        "x" * 20000,
    ],
)
def test_decode_malformed_values_return_none(value):
    """Broken or hostile header values are dropped with a warning, never raised."""
    assert decode_agent_hint_header(value) is None


def test_apply_merges_legacy_fields_into_empty_body_and_returns_payload():
    """Only legacy-representable fields enter the body; the decoded payload is returned."""
    request_json: dict = {}
    header_hint = apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: GOLDEN_HINT_B64})

    assert request_json == {
        "agent_hint": {
            "session_id": "s-7f3a2c",
            "priority_control": {"priority": 1, "reason": "user_waiting"},
            "latency_control": {"latency_sensitivity": 4100},
        }
    }
    assert header_hint == {
        "session": {"session_id": "s-7f3a2c", "agent_type": "researcher", "epoch": 0},
        "priority": {"level": 1, "reason": "user_waiting"},
        "prediction": {"latency_ms": 4100, "remaining_calls": 3, "output_tokens": 420},
    }


def test_apply_body_fields_win_and_header_fills_gaps():
    request_json = {
        "agent_hint": {
            "session_id": "body-session",
            "priority_control": {"priority": 9},
        }
    }
    header_hint = apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: GOLDEN_HINT_B64})

    assert request_json["agent_hint"]["session_id"] == "body-session"
    assert request_json["agent_hint"]["priority_control"]["priority"] == 9
    assert request_json["agent_hint"]["priority_control"]["reason"] == "user_waiting"
    assert request_json["agent_hint"]["latency_control"]["latency_sensitivity"] == 4100
    assert header_hint is not None


def test_apply_accepts_exact_case_header_fallback():
    """Plain dict callers may pass the exact-case header name."""
    request_json: dict = {}
    header_hint = apply_agent_hint_header(request_json, {"X-Agent-Hint": GOLDEN_HINT_B64})

    assert request_json["agent_hint"]["session_id"] == "s-7f3a2c"
    assert header_hint is not None


def test_apply_without_header_is_noop():
    request_json: dict = {}
    assert apply_agent_hint_header(request_json, {}) is None
    assert apply_agent_hint_header(request_json, None) is None

    assert request_json == {}


def test_apply_malformed_header_is_noop():
    request_json: dict = {}
    assert apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: "@@@bad@@@"}) is None

    assert request_json == {}


def test_apply_replaces_non_dict_body_hint():
    request_json = {"agent_hint": "garbage"}
    header_hint = apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: GOLDEN_HINT_B64})

    assert request_json == {
        "agent_hint": {
            "session_id": "s-7f3a2c",
            "priority_control": {"priority": 1, "reason": "user_waiting"},
            "latency_control": {"latency_sensitivity": 4100},
        }
    }
    assert header_hint is not None


# Full AgentHint spec example (docs/Agent Hint字段&实现定义.md §1.1) as sent
# by a header-only client.
SPEC_HINT = {
    "session": {
        "session_id": "s-7f3a2c",
        "parent_session_id": "s-root01",
        "derivation": {"relation": "spawn", "join": "sync"},
        "agent_type": "researcher",
        "call_index": 2,
        "epoch": 0,
    },
    "agent_status": {
        "state": "running",
        "state_since_ms": 1790000000000,
    },
    "prediction": {
        "remaining_calls": 3,
        "output_tokens": 420,
        "interval_ms": 11800,
        "latency_ms": 4100,
        "next_action": {
            "state": "tool",
            "tools": [{"name": "web_search", "expected_duration_s": 8}],
            "prob": 0.85,
        },
    },
    "priority": {"level": 3, "reason": "critical_path"},
    "strict_priority": {"level": 3, "reason": "critical_path"},
    "cache_control": [
        {"type": "pin", "range": {"end": 1}},
        {"type": "ephemeral", "ttl_s": 12, "range": {"end": 10}},
    ],
}


def test_apply_never_leaks_spec_blocks_into_body():
    """Protocol boundary: spec blocks stay in the returned payload, not the body."""
    request_json: dict = {}
    header_hint = apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: _b64(SPEC_HINT)})

    body_hint = request_json["agent_hint"]
    assert set(body_hint) == {"session_id", "parent_session_id", "priority_control", "latency_control"}
    assert body_hint == {
        "session_id": "s-7f3a2c",
        "parent_session_id": "s-root01",
        "priority_control": {"priority": 3, "reason": "critical_path"},
        "latency_control": {"latency_sensitivity": 4100},
    }
    assert header_hint == SPEC_HINT


def test_wire_full_spec_reaches_parse_agent_hint_via_header_hint():
    """Header-only spec client yields a fully populated AgentHintInfo."""
    request_json: dict = {}
    header_hint = apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: _b64(SPEC_HINT)})

    hint = parse_agent_hint(request_json, header_hint=header_hint)

    assert hint.session_id == "s-7f3a2c"
    assert hint.parent_session_id == "s-root01"
    assert hint.session_hint is not None
    assert hint.session_hint.agent_type == "researcher"
    assert hint.session_hint.call_index == 2
    assert hint.session_hint.epoch == 0
    assert hint.session_hint.derivation is not None
    assert hint.session_hint.derivation.relation == "spawn"
    assert hint.agent_status is not None
    assert hint.agent_status.state == "running"
    assert hint.prediction is not None
    assert hint.prediction.output_tokens == 420
    assert hint.prediction.next_action is not None
    assert hint.prediction.next_action.prob == 0.85
    assert hint.prediction.next_action.tools[0].name == "web_search"
    assert hint.strict_priority is not None
    assert hint.strict_priority.level == 3
    # spec cache_control (list form → first entry) with message-range mapping
    assert hint.cache_control is not None
    assert hint.cache_control.type == "pin"
    assert hint.cache_control.msg_offset == 1
    assert hint.raw_extra == {}


def test_parse_without_header_hint_yields_legacy_only_view():
    """Spec blocks are invisible when the caller does not pass the header payload."""
    request_json: dict = {}
    apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: _b64(SPEC_HINT)})

    hint = parse_agent_hint(request_json)

    assert hint.session_id == "s-7f3a2c"
    assert hint.session_hint is None
    assert hint.agent_status is None
    assert hint.prediction is None
    assert hint.strict_priority is None
    assert hint.cache_control is None


def test_body_session_ids_win_over_header_nested_session():
    """Body flat ids beat the header's nested session{}; nested extras still parse."""
    request_json = {"agent_hint": {"session_id": "body-session"}}
    header_hint = apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: _b64(SPEC_HINT)})

    hint = parse_agent_hint(request_json, header_hint=header_hint)

    assert hint.session_id == "body-session"
    # complement rule: only parent arrives from the header nested block
    assert hint.parent_session_id == "s-root01"
    assert hint.session_hint is not None
    assert hint.session_hint.agent_type == "researcher"


def test_body_cache_control_wins_over_header_spec_cache_control():
    """A legacy body cache_control is authoritative; the header one is dropped."""
    request_json = {
        "agent_hint": {"cache_control": {"ttl": 120}},
        "messages": [{"role": "user"} for _ in range(4)],
    }
    header_hint = apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: _b64(SPEC_HINT)})

    hint = parse_agent_hint(request_json, header_hint=header_hint)

    assert hint.cache_control is not None
    assert hint.cache_control.type == "ephemeral"
    assert hint.cache_control.ttl == 120
    # legacy defaulting: msg_offset = len(messages)
    assert hint.cache_control.msg_offset == 4


# ---------------------------------------------------------------------------
# Phase 3: middleware-side priority extraction (header-only)
# ---------------------------------------------------------------------------


def test_extract_priority_from_hint_headers():
    from motor.coordinator.domain.agent_hint_wire import extract_priority_from_hint_headers

    hint = {"priority": {"level": 3, "reason": "critical_path"}}
    assert extract_priority_from_hint_headers({AGENT_HINT_WIRE_HEADER: _b64(hint)}) == 3
    # exact-case fallback
    assert extract_priority_from_hint_headers({"X-Agent-Hint": _b64(hint)}) == 3


def test_extract_priority_malformed_or_missing_inputs():
    from motor.coordinator.domain.agent_hint_wire import extract_priority_from_hint_headers

    assert extract_priority_from_hint_headers({}) is None
    assert extract_priority_from_hint_headers(None) is None
    assert extract_priority_from_hint_headers({AGENT_HINT_WIRE_HEADER: "@@@bad@@@"}) is None
    # header without a priority block
    assert extract_priority_from_hint_headers({AGENT_HINT_WIRE_HEADER: _b64({"session": {"session_id": "s"}})}) is None
    # non-integer level
    assert extract_priority_from_hint_headers({AGENT_HINT_WIRE_HEADER: _b64({"priority": {"level": "high"}})}) is None


def test_apply_priority_reason_header_fills_and_body_wins():
    """E3: priority.reason survives the header->body merge; body reason wins."""
    request_json: dict = {}
    apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: GOLDEN_HINT_B64})
    assert request_json["agent_hint"]["priority_control"]["reason"] == "user_waiting"

    request_json = {"agent_hint": {"priority_control": {"priority": 2, "reason": "critical_path"}}}
    apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: GOLDEN_HINT_B64})
    assert request_json["agent_hint"]["priority_control"]["priority"] == 2
    assert request_json["agent_hint"]["priority_control"]["reason"] == "critical_path"


def test_apply_priority_reason_non_string_ignored():
    """E3: a non-string reason never enters priority_control."""
    request_json: dict = {}
    hint = {"priority": {"level": 3, "reason": 42}}

    raw = base64.urlsafe_b64encode(json.dumps(hint).encode()).decode().rstrip("=")
    apply_agent_hint_header(request_json, {AGENT_HINT_WIRE_HEADER: raw})
    assert request_json["agent_hint"]["priority_control"]["priority"] == 3
    assert "reason" not in request_json["agent_hint"]["priority_control"]


def test_extract_strict_priority_from_hint_headers():
    """WP-E2: strict_priority{level, reason} parses from the hint header."""
    hint = {"session": {"session_id": "s1"}, "strict_priority": {"level": 5, "reason": "critical_path"}}
    raw = base64.urlsafe_b64encode(json.dumps(hint).encode()).decode().rstrip("=")
    assert extract_strict_priority_from_hint_headers({AGENT_HINT_WIRE_HEADER: raw}) == (5, "critical_path")
    hint_noreason = {"strict_priority": {"level": 2}}
    raw2 = base64.urlsafe_b64encode(json.dumps(hint_noreason).encode()).decode().rstrip("=")
    assert extract_strict_priority_from_hint_headers({AGENT_HINT_WIRE_HEADER: raw2}) == (2, None)
    assert extract_strict_priority_from_hint_headers({AGENT_HINT_WIRE_HEADER: "!!!"}) is None
    assert extract_strict_priority_from_hint_headers(None) is None


def test_strict_priority_trust_rule():
    """WP-E2: trust = enabled AND non-empty tokens AND matching token header."""
    headers = {STRICT_PRIORITY_TOKEN_HEADER: "tok-1"}
    assert strict_priority_trusted(headers, enabled=True, tokens=["tok-1"]) is True
    assert strict_priority_trusted(headers, enabled=False, tokens=["tok-1"]) is False
    assert strict_priority_trusted(headers, enabled=True, tokens=[]) is False
    assert strict_priority_trusted({"x-strict-priority-token": "other"}, enabled=True, tokens=["tok-1"]) is False
    # exact-case fallback for plain-dict callers
    assert strict_priority_trusted({"X-Strict-Priority-Token": "tok-1"}, enabled=True, tokens=["tok-1"]) is True


def test_strict_priority_token_uses_constant_time_compare(monkeypatch):
    """Review 194909399: token match must use hmac.compare_digest and evaluate
    every configured token, so timing does not leak the shared prefix or the
    match position (no short-circuit).
    """
    import hmac as hmac_mod

    real = hmac_mod.compare_digest
    calls: list[tuple[bytes, bytes]] = []

    def _spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr("motor.coordinator.domain.agent_hint_wire.hmac.compare_digest", _spy)
    tokens = ["tok-1", "tok-2", "tok-3"]

    # First token matches, but every candidate is still compared.
    assert strict_priority_trusted({STRICT_PRIORITY_TOKEN_HEADER: "tok-1"}, enabled=True, tokens=tokens) is True
    assert len(calls) == len(tokens)

    calls.clear()
    # No token matches: still one comparison per candidate.
    assert strict_priority_trusted({STRICT_PRIORITY_TOKEN_HEADER: "tok-9"}, enabled=True, tokens=tokens) is False
    assert len(calls) == len(tokens)


def test_strict_priority_token_prefix_shared_values_match_exactly():
    tokens = ["secret-abc", "secret-def"]
    assert strict_priority_trusted({STRICT_PRIORITY_TOKEN_HEADER: "secret-abc"}, enabled=True, tokens=tokens) is True
    assert strict_priority_trusted({STRICT_PRIORITY_TOKEN_HEADER: "secret-a"}, enabled=True, tokens=tokens) is False
    assert strict_priority_trusted({STRICT_PRIORITY_TOKEN_HEADER: "secret-abcd"}, enabled=True, tokens=tokens) is False
