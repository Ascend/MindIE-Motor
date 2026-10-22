# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of MulanPSL2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for agent hint parsing and normalization."""

import pytest
from pydantic import ValidationError

from motor.coordinator.domain.agent_hint import (
    CacheControl,
    ContextEdit,
    SessionControl,
    agent_hint_implies_manage_request,
    apply_session_control_autofill,
    ensure_minimum_messages_for_session_edits,
    parse_agent_hint,
    parse_manage_request,
    session_control_implies_manage_request,
)


def test_parse_agent_hint_resolves_header_ids_and_preserves_extensions():
    """Body IDs take precedence while unknown fields remain available to callers."""
    hint = parse_agent_hint(
        {"agent_hint": {"session_id": "body-session", "extension": {"enabled": True}}},
        headers={"x-session-id": "header-session", "x-parent-session-id": "header-parent"},
    )

    assert hint.session_id == "body-session"
    assert hint.parent_session_id == "header-parent"
    assert hint.raw_extra == {"extension": {"enabled": True}}


def test_parse_agent_hint_complements_single_session_id():
    """A single valid identifier is mirrored to the missing identifier field."""
    hint = parse_agent_hint({"agent_hint": {"parent_session_id": "session-1"}})

    assert (hint.session_id, hint.parent_session_id) == ("session-1", "session-1")


@pytest.mark.parametrize(
    "ttl, expected",
    [("not-a-number", 300), (0, 60), (7200, 3600), (600, 600)],
)
def test_cache_control_normalizes_ttl(ttl, expected):
    """TTL input is converted to an integer and clamped to the supported range."""
    assert CacheControl(ttl=ttl).ttl == expected


def test_parse_agent_hint_drops_invalid_cache_offset_and_server_fields():
    """Message offsets outside the request are rejected and server coordinates are ignored."""
    hint = parse_agent_hint(
        {
            "messages": [{"role": "user"}],
            "agent_hint": {
                "session_id": "s",
                "cache_control": {"msg_offset": 2, "block_offset": 99},
            },
        }
    )

    assert hint.cache_control is None


def test_parse_agent_hint_filters_invalid_context_edits_by_message_and_tool_bounds():
    """Only edits with valid half-open ranges for their target are retained."""
    hint = parse_agent_hint(
        {
            "messages": [{"role": "user"}, {"role": "assistant"}],
            "tools": [{"type": "function"}],
            "agent_hint": {
                "session_id": "s",
                "context_management": {
                    "edits": [
                        {"type": "offload", "start": 0, "end": 1},
                        {"type": "evict", "target": "tools", "start": 0, "end": 1},
                        {"type": "prefetch", "start": 2, "end": 2},
                        {"type": "offload", "start": 0, "end": 2, "target": "tools"},
                    ]
                },
            },
        }
    )

    assert hint.context_management is not None
    assert [(edit.type, edit.target) for edit in hint.context_management.edits] == [
        ("offload", "session"),
        ("evict", "tools"),
    ]


def test_context_edit_requires_supported_type():
    """Unsupported context operations fail schema validation rather than being executed."""
    with pytest.raises(ValidationError):
        ContextEdit(type="delete")


def test_ensure_minimum_messages_injects_session_edit_messages():
    """Management session edits receive safe placeholder messages when the body omits them."""
    request_json = {"agent_hint": {"context_management": {"manage_request": True, "edits": [{"type": "evict"}]}}}
    req_data = {}

    ensure_minimum_messages_for_session_edits(request_json, req_data)

    assert len(request_json["messages"]) == 2
    assert req_data["messages"] is request_json["messages"]
    assert [message["role"] for message in request_json["messages"]] == ["system", "user"]


def test_ensure_minimum_messages_does_not_change_non_management_request():
    """Ordinary requests and non-session edits keep their original message payload."""
    messages = [{"role": "user", "content": "hello"}]
    request_json = {
        "messages": messages,
        "agent_hint": {"context_management": {"manage_request": False, "edits": [{"type": "evict"}]}},
    }
    req_data = {"messages": messages}

    ensure_minimum_messages_for_session_edits(request_json, req_data)

    assert request_json["messages"] == messages
    assert req_data["messages"] == messages


def test_session_control_requires_supported_type():
    """Unsupported session lifecycle operations fail schema validation."""
    with pytest.raises(ValidationError):
        SessionControl(type="restart")


@pytest.mark.parametrize(
    "sc_type, expected_edit_type",
    [
        ("pause", "offload"),
        ("stop", "evict"),
        ("compact", "evict"),
        ("resume", "prefetch"),
    ],
)
def test_apply_session_control_autofill_injects_context_management(sc_type, expected_edit_type):
    """pause/stop/compact/resume translate into a session-targeted manage-request."""
    request_json = {"agent_hint": {"session_control": {"type": sc_type}}}

    apply_session_control_autofill(request_json)

    assert request_json["agent_hint"]["context_management"] == {
        "manage_request": True,
        "edits": [{"type": expected_edit_type, "target": "session"}],
    }
    assert request_json["agent_hint"]["session_control"] == {"type": sc_type}


def test_apply_session_control_autofill_start_injects_nothing():
    """'start' only declares a session start; no context management is synthesized."""
    request_json = {"agent_hint": {"session_control": {"type": "start"}}}

    apply_session_control_autofill(request_json)

    assert "context_management" not in request_json["agent_hint"]
    assert request_json["agent_hint"]["session_control"] == {"type": "start"}


def test_apply_session_control_autofill_conflict_keeps_context_management():
    """Mutual exclusion: an explicit context_management wins and session_control is dropped."""
    request_json = {
        "agent_hint": {
            "session_control": {"type": "stop"},
            "context_management": {"manage_request": True, "edits": [{"type": "offload"}]},
        }
    }

    apply_session_control_autofill(request_json)

    assert "session_control" not in request_json["agent_hint"]
    assert request_json["agent_hint"]["context_management"]["edits"] == [{"type": "offload"}]


@pytest.mark.parametrize(
    "cm_value",
    [
        {},
        None,
        {"manage_request": True},  # no usable edits -> _parse_context_management returns None
        {"manage_request": False},  # no edits -> _parse_context_management returns None
        {"edits": [{"type": "offload"}]},  # default target = "session" but manage_request False
        "garbage",
        42,
    ],
)
def test_apply_session_control_autofill_invalid_cm_preserves_session_control(cm_value):
    """Invalid / empty / non-dict context_management must NOT block session_control autofill.

    Regression: previously, presence of the ``context_management`` key alone
    triggered the CONFLICT branch and dropped ``session_control``, leaving a
    non-empty ``messages`` request to be executed as plain inference while the
    client intent (e.g. ``stop``) was silently ignored.
    """
    request_json = {
        "agent_hint": {
            "session_control": {"type": "stop"},
            "context_management": cm_value,
        }
    }

    apply_session_control_autofill(request_json)

    # session_control must be honored: autofill injected a session-targeted evict edit
    assert request_json["agent_hint"]["context_management"] == {
        "manage_request": True,
        "edits": [{"type": "evict", "target": "session"}],
    }
    # the original session_control hint is preserved (consistent with the
    # ACTIONABLE branch for a session_control-only request)
    assert request_json["agent_hint"]["session_control"] == {"type": "stop"}


def test_apply_session_control_autofill_drops_invalid_type():
    """Unsupported types are dropped instead of being executed."""
    request_json = {"agent_hint": {"session_control": {"type": "restart"}}}

    apply_session_control_autofill(request_json)

    assert "session_control" not in request_json["agent_hint"]
    assert "context_management" not in request_json["agent_hint"]


@pytest.mark.parametrize(
    "sc_data",
    [
        {"type": None},
        {"type": 42},
        {},
    ],
)
def test_apply_session_control_autofill_drops_malformed_type(sc_data):
    """Missing / None / non-string type degrades to a dropped hint, never a crash."""
    request_json = {"agent_hint": {"session_control": sc_data}}

    apply_session_control_autofill(request_json)

    assert "session_control" not in request_json["agent_hint"]
    assert "context_management" not in request_json["agent_hint"]


def test_apply_session_control_autofill_logs_type(caplog):
    """Every valid session_control request must log the resolved type and raw session_id."""
    request_json = {
        "agent_hint": {
            "session_id": "s-001",
            "session_control": {"type": "pause"},
        }
    }

    with caplog.at_level("INFO", logger="motor.coordinator.domain.agent_hint"):
        apply_session_control_autofill(request_json)

    assert "session_control.type=pause" in caplog.text
    assert "session_id=s-001" in caplog.text


def test_apply_session_control_autofill_logs_type_without_session_id(caplog):
    """Missing session_id is rendered as None; do not crash and keep the field name."""
    request_json = {"agent_hint": {"session_control": {"type": "pause"}}}

    with caplog.at_level("INFO", logger="motor.coordinator.domain.agent_hint"):
        apply_session_control_autofill(request_json)

    assert "session_control.type=pause" in caplog.text
    assert "session_id=None" in caplog.text


def test_apply_session_control_autofill_ignores_missing_or_malformed():
    """Requests without a dict session_control are left untouched."""
    no_agent_hint = {"messages": []}
    apply_session_control_autofill(no_agent_hint)
    assert no_agent_hint == {"messages": []}

    non_dict = {"agent_hint": {"session_control": "pause"}}
    apply_session_control_autofill(non_dict)
    assert "session_control" not in non_dict["agent_hint"]
    assert "context_management" not in non_dict["agent_hint"]


@pytest.mark.parametrize(
    "agent_hint, expected",
    [
        ({"session_control": {"type": "pause"}}, True),
        ({"session_control": {"type": "stop"}}, True),
        ({"session_control": {"type": "compact"}}, True),
        ({"session_control": {"type": "resume"}}, True),
        ({"session_control": {"type": "start"}}, False),
        ({"session_control": {"type": "restart"}}, False),
        ({"session_control": {"type": None}}, False),
        ({"session_control": {"type": 42}}, False),
        ({"session_control": {}}, False),
        ({"session_control": "pause"}, False),
        ({}, False),
        # raw context_management without a session-targeted edit does NOT block session_control
        # (the cm would be dropped downstream anyway); session_control must still imply a manage-request.
        ({"session_control": {"type": "stop"}, "context_management": {"manage_request": True}}, True),
        ({"session_control": {"type": "stop"}, "context_management": {}}, True),
        ({"session_control": {"type": "stop"}, "context_management": None}, True),
        ({"session_control": {"type": "stop"}, "context_management": "garbage"}, True),
        # a valid session-targeted manage request still wins via mutual exclusion
        (
            {
                "session_control": {"type": "stop"},
                "context_management": {"manage_request": True, "edits": [{"type": "offload"}]},
            },
            False,
        ),
    ],
)
def test_session_control_implies_manage_request(agent_hint, expected):
    """Only pause/stop/compact/resume (without a valid raw context_management) imply a manage-request."""
    assert session_control_implies_manage_request(agent_hint) is expected


@pytest.mark.parametrize(
    "agent_hint, expected",
    [
        # explicit context_management path
        ({"context_management": {"manage_request": True, "edits": [{"type": "evict", "target": "session"}]}}, True),
        ({"context_management": {"manage_request": True, "edits": [{"type": "evict"}]}}, True),
        ({"context_management": {"manage_request": True, "edits": [{"type": "evict", "target": "messages"}]}}, False),
        ({"context_management": {"manage_request": False, "edits": [{"type": "evict", "target": "session"}]}}, False),
        ({"context_management": {"manage_request": True}}, False),
        # session_control path
        ({"session_control": {"type": "pause"}}, True),
        ({"session_control": {"type": "stop"}}, True),
        ({"session_control": {"type": "start"}}, False),
        ({"session_control": {"type": "restart"}}, False),
        # invalid / empty raw cm is NOT a conflict; session_control still implies a manage-request
        (
            {
                "session_control": {"type": "stop"},
                "context_management": {"manage_request": True, "edits": []},
            },
            True,
        ),
        ({"session_control": "pause"}, False),
        ({}, False),
    ],
)
def test_agent_hint_implies_manage_request(agent_hint, expected):
    """The single predicate covers both the explicit cm path and the session_control path."""
    assert agent_hint_implies_manage_request(agent_hint) is expected


def test_parse_agent_hint_session_control_after_autofill():
    """Dispatch order (autofill then parse) populates both session_control and context_management."""
    request_json = {"agent_hint": {"session_id": "s", "session_control": {"type": "resume"}}}

    apply_session_control_autofill(request_json)
    hint = parse_agent_hint(request_json)

    assert hint.session_control is not None
    assert hint.session_control.type == "resume"
    assert hint.context_management is not None
    assert hint.context_management.manage_request is True
    assert [(edit.type, edit.target) for edit in hint.context_management.edits] == [("prefetch", "session")]
    assert "session_control" not in hint.raw_extra


def test_parse_agent_hint_drops_invalid_session_control():
    """parse_agent_hint alone drops session_control with an unsupported type."""
    hint = parse_agent_hint({"agent_hint": {"session_control": {"type": "nope"}}})

    assert hint.session_control is None
    assert hint.context_management is None


def test_session_control_autofill_then_minimum_messages_injection():
    """Autofill must run before message injection so empty session-control requests get placeholders."""
    request_json = {"agent_hint": {"session_control": {"type": "stop"}}}
    req_data = request_json.copy()

    apply_session_control_autofill(request_json)
    ensure_minimum_messages_for_session_edits(request_json, req_data)

    assert len(request_json["messages"]) == 2
    assert [message["role"] for message in request_json["messages"]] == ["system", "user"]


def test_parse_agent_hint_resolves_lowercase_headers_from_real_request():
    """Regression: Starlette normalizes header keys to lowercase; parse_agent_hint
    must read lowercase keys (matches the real Request.headers path used in dispatch).
    """
    from starlette.datastructures import Headers

    # Simulate exactly what dispatch.py passes: dict(raw_request.headers).
    # ASGI servers (uvicorn/hypercorn) deliver lowercase header bytes.
    raw_request_headers = Headers(raw=[(b"x-session-id", b"req-sess"), (b"x-parent-session-id", b"req-parent")])
    headers_dict = dict(raw_request_headers)

    hint = parse_agent_hint({"agent_hint": {}}, headers=headers_dict)

    assert hint.session_id == "req-sess"
    assert hint.parent_session_id == "req-parent"


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("false", False),
        ("True", True),
        ("FALSE", False),
        ("TrUe", True),
        (None, False),
        ("", False),
        ("yes", False),
        (2, False),
        (1.5, False),
        ([], False),
    ],
)
def test_parse_manage_request_accepts_bool_int_and_string(value, expected):
    """JSON bool / 0-1 int / case-insensitive 'true'/'false' parse to bool; everything else falls back to False."""
    assert parse_manage_request(value) is expected


# (Phase 1 spec-form tests live in the header_hint section appended below.)

# ---------------------------------------------------------------------------
# Phase 1: spec-form blocks arrive ONLY via the X-Agent-Hint header payload
# (parse_agent_hint(header_hint=...)); the body agent_hint block stays legacy.
# ---------------------------------------------------------------------------

FULL_SPEC_SESSION = {
    "session_id": "s-child",
    "parent_session_id": "s-parent",
    "derivation": {"relation": "spawn", "join": "sync"},
    "agent_type": "researcher",
    "call_index": 2,
    "epoch": 0,
}


def test_header_hint_full_spec_session_block():
    """The nested spec session block lands in session_hint and drives the flat IDs."""
    hint = parse_agent_hint({}, header_hint={"session": FULL_SPEC_SESSION})

    assert hint.session_id == "s-child"
    assert hint.parent_session_id == "s-parent"
    assert hint.session_hint is not None
    assert hint.session_hint.agent_type == "researcher"
    assert hint.session_hint.call_index == 2
    assert hint.session_hint.epoch == 0
    assert hint.session_hint.derivation is not None
    assert hint.session_hint.derivation.relation == "spawn"
    assert hint.session_hint.derivation.join == "sync"
    assert hint.session_hint.derivation.fork_point is None


def test_header_hint_nested_ids_lose_to_body_flat_ids():
    """Body flat ids beat the header nested session{} (same body-wins rule as the wire)."""
    hint = parse_agent_hint(
        {"agent_hint": {"session_id": "flat-session"}},
        header_hint={"session": {**FULL_SPEC_SESSION, "session_id": "nested-session"}},
    )

    assert hint.session_id == "flat-session"
    assert hint.parent_session_id == "s-parent"
    assert hint.session_hint is not None
    assert hint.session_hint.agent_type == "researcher"


def test_header_hint_partial_nested_session_falls_back_to_flat():
    """A nested block without IDs (status-event shape) falls back to flat fields."""
    hint = parse_agent_hint(
        {"agent_hint": {"session_id": "flat-session"}},
        header_hint={"session": {"agent_type": "researcher", "call_index": 3}},
    )

    assert hint.session_id == "flat-session"
    assert hint.session_hint is not None
    assert hint.session_hint.agent_type == "researcher"
    assert hint.session_hint.call_index == 3


def test_header_hint_nested_session_header_fallback():
    """x-session-id headers still apply when neither nested nor flat carries an ID."""
    hint = parse_agent_hint(
        {},
        headers={"x-session-id": "header-session"},
        header_hint={"session": {"agent_type": "researcher"}},
    )

    assert hint.session_id == "header-session"
    assert hint.parent_session_id == "header-session"


def test_header_hint_derivation_defaults_and_fork_point():
    """join defaults to sync; fork_point passes through for fork relations."""
    hint = parse_agent_hint(
        {},
        header_hint={
            "session": {
                "session_id": "s",
                "derivation": {"relation": "fork", "fork_point": 5},
            }
        },
    )

    assert hint.session_hint is not None
    derivation = hint.session_hint.derivation
    assert derivation is not None
    assert derivation.relation == "fork"
    assert derivation.join == "sync"
    assert derivation.fork_point == 5


def test_header_hint_drops_invalid_derivation_but_keeps_session():
    """An unsupported relation drops only the derivation, not the whole session hint."""
    hint = parse_agent_hint(
        {},
        header_hint={
            "session": {
                "session_id": "s",
                "agent_type": "researcher",
                "derivation": {"relation": "clone"},
            }
        },
    )

    assert hint.session_hint is not None
    assert hint.session_hint.agent_type == "researcher"
    assert hint.session_hint.derivation is None


def test_header_hint_normalizes_dirty_session_numbers():
    """Negative / non-numeric call_index, epoch and fork_point are unset, not rejected."""
    hint = parse_agent_hint(
        {},
        header_hint={
            "session": {
                "session_id": "s",
                "call_index": 0,
                "epoch": "x",
                "derivation": {"relation": "fork", "fork_point": -1},
            }
        },
    )

    assert hint.session_hint is not None
    assert hint.session_hint.call_index is None
    assert hint.session_hint.epoch is None
    assert hint.session_hint.derivation is not None
    assert hint.session_hint.derivation.fork_point is None


def test_header_hint_agent_status_full_and_invalid_state():
    """agent_status parses tools; an unknown state drops the whole block."""
    hint = parse_agent_hint(
        {},
        header_hint={
            "agent_status": {
                "state": "tool",
                "state_since_ms": 1790000000000,
                "reason": "await_tool",
                "tools": [{"name": "web_search", "call_id": "call-1", "child_session_id": "s-child"}],
            }
        },
    )

    assert hint.agent_status is not None
    assert hint.agent_status.state == "tool"
    assert hint.agent_status.reason == "await_tool"
    assert hint.agent_status.tools[0].name == "web_search"
    assert hint.agent_status.tools[0].child_session_id == "s-child"

    invalid = parse_agent_hint({}, header_hint={"agent_status": {"state": "waiting"}})
    assert invalid.agent_status is None


def test_header_hint_prediction_fields_and_bounds():
    """prediction parses numeric estimates; negative values and bad prob are unset."""
    hint = parse_agent_hint(
        {},
        header_hint={
            "prediction": {
                "remaining_calls": 3,
                "output_tokens": 420,
                "interval_ms": 11800,
                "latency_ms": -5,
                "next_action": {
                    "state": "tool",
                    "tools": [{"name": "web_search", "expected_duration_s": 8}],
                    "prob": 1.5,
                },
            }
        },
    )

    assert hint.prediction is not None
    assert hint.prediction.remaining_calls == 3
    assert hint.prediction.output_tokens == 420
    assert hint.prediction.interval_ms == 11800
    assert hint.prediction.latency_ms is None
    assert hint.prediction.next_action is not None
    assert hint.prediction.next_action.state == "tool"
    assert hint.prediction.next_action.prob is None
    assert hint.prediction.next_action.tools[0].expected_duration_s == 8

    empty = parse_agent_hint({}, header_hint={"prediction": {"output_tokens": "NaN"}})
    assert empty.prediction is None


def test_header_hint_strict_priority_is_stored_not_consumed():
    """strict_priority parses into PriorityHint; no scheduling consumer exists (Phase 3 deferred)."""
    hint = parse_agent_hint(
        {},
        header_hint={"strict_priority": {"level": 3, "reason": "critical_path"}},
    )

    assert hint.strict_priority is not None
    assert hint.strict_priority.level == 3
    assert hint.strict_priority.reason == "critical_path"

    malformed = parse_agent_hint({}, header_hint={"strict_priority": {"level": "high"}})
    assert malformed.strict_priority is None


def test_body_spec_blocks_are_unknown_passthrough():
    """Without a header payload, spec-form keys in the body land in raw_extra untouched."""
    hint = parse_agent_hint(
        {
            "agent_hint": {
                "session": FULL_SPEC_SESSION,
                "agent_status": {"state": "running"},
                "prediction": {"output_tokens": 1},
                "strict_priority": {"level": 1},
                "priority": {"level": 1},
            }
        }
    )

    assert hint.session_hint is None
    assert hint.agent_status is None
    assert hint.prediction is None
    assert hint.strict_priority is None
    assert hint.raw_extra == {
        "session": FULL_SPEC_SESSION,
        "agent_status": {"state": "running"},
        "prediction": {"output_tokens": 1},
        "strict_priority": {"level": 1},
        "priority": {"level": 1},
    }


def test_header_hint_spec_cache_control_pin_with_message_range():
    """pin + range{end=1} maps to msg_offset=1 via the header path."""
    hint = parse_agent_hint(
        {"messages": [{"role": "system"}, {"role": "user"}]},
        header_hint={"cache_control": {"type": "pin", "range": {"end": 1}}},
    )

    assert hint.cache_control is not None
    assert hint.cache_control.type == "pin"
    assert hint.cache_control.msg_offset == 1
    assert hint.cache_control.range_unit == "message"


def test_header_hint_spec_cache_control_ttl_s_and_list_form():
    """ttl_s wins over ttl; a list uses the first entry."""
    hint = parse_agent_hint(
        {},
        header_hint={
            "cache_control": [
                {"type": "ephemeral", "ttl_s": 600},
                {"type": "pin"},
            ]
        },
    )

    assert hint.cache_control is not None
    assert hint.cache_control.type == "ephemeral"
    assert hint.cache_control.ttl == 600


def test_header_hint_spec_cache_control_unknown_unit_drops_range():
    """``message`` is the only range.unit the protocol defines; any other unit drops the range.

    Regression guard: ``token`` used to be a whitelisted unit whose range was stored
    half-translated, which let an unset msg_offset be defaulted to len(messages) and hand a
    wrong (conversation-tail) effect point to the block-coordinate chain.
    """
    without_messages = parse_agent_hint(
        {},
        header_hint={"cache_control": {"type": "ephemeral", "range": {"unit": "token", "start": 0, "end": 512}}},
    )
    assert without_messages.cache_control is not None
    assert without_messages.cache_control.type == "ephemeral"
    assert without_messages.cache_control.range_unit is None
    assert without_messages.cache_control.range_start is None
    assert without_messages.cache_control.range_end is None
    assert without_messages.cache_control.msg_offset is None

    with_messages = parse_agent_hint(
        {"messages": [{"role": "user", "content": "hi"} for _ in range(3)]},
        header_hint={"cache_control": {"type": "pin", "range": {"unit": "token", "end": 512}}},
    )
    assert with_messages.cache_control is not None
    assert with_messages.cache_control.type == "pin"
    assert with_messages.cache_control.range_unit is None
    assert with_messages.cache_control.msg_offset == 3  # no-range default = whole conversation


def test_spec_cache_control_ttl_explicit_distinguishes_asked_from_defaulted():
    """``ttl`` defaults to 300, so only ``ttl_explicit`` separates "asked" from "defaulted"."""
    implied = parse_agent_hint({}, header_hint={"cache_control": {"type": "pin"}})
    assert implied.cache_control is not None
    assert implied.cache_control.ttl == 300
    assert implied.cache_control.ttl_explicit is False

    via_alias = parse_agent_hint({}, header_hint={"cache_control": {"type": "ephemeral", "ttl_s": 60}})
    assert via_alias.cache_control is not None
    assert via_alias.cache_control.ttl == 60
    assert via_alias.cache_control.ttl_explicit is True

    via_legacy_name = parse_agent_hint({}, header_hint={"cache_control": {"type": "pin", "ttl": 600}})
    assert via_legacy_name.cache_control is not None
    assert via_legacy_name.cache_control.ttl == 600
    assert via_legacy_name.cache_control.ttl_explicit is True

    explicit_default_value = parse_agent_hint({}, header_hint={"cache_control": {"type": "pin", "ttl_s": 300}})
    assert explicit_default_value.cache_control is not None
    assert explicit_default_value.cache_control.ttl_explicit is True


def test_body_cache_control_ttl_explicit_tracks_client_intent():
    """The legacy body channel derives the same flag from its own ``ttl`` key."""
    absent = parse_agent_hint({"agent_hint": {"cache_control": {"type": "ephemeral"}}})
    assert absent.cache_control is not None
    assert absent.cache_control.ttl_explicit is False

    present = parse_agent_hint({"agent_hint": {"cache_control": {"type": "ephemeral", "ttl": 120}}})
    assert present.cache_control is not None
    assert present.cache_control.ttl == 120
    assert present.cache_control.ttl_explicit is True


def test_ttl_explicit_is_derived_only_and_never_serialized():
    """The flag is server-side bookkeeping; it must not leak into the engine payload."""
    hint = parse_agent_hint({}, header_hint={"cache_control": {"type": "pin", "ttl_s": 60}})

    assert hint.cache_control is not None
    assert hint.cache_control.ttl_explicit is True
    assert "ttl_explicit" not in hint.cache_control.model_dump()


def test_header_hint_spec_cache_control_range_with_start_dropped():
    """unit=message with start>0 is unsupported: the range is dropped, not the hint."""
    hint = parse_agent_hint(
        {},
        header_hint={"cache_control": {"type": "pin", "range": {"start": 2, "end": 5}}},
    )

    assert hint.cache_control is not None
    assert hint.cache_control.type == "pin"
    assert hint.cache_control.range_unit is None
    assert hint.cache_control.msg_offset is None


def test_header_hint_spec_cache_control_without_range_still_defaults_msg_offset():
    """No behaviour change on the message path: an unset msg_offset still defaults to len(messages)."""
    messages = [{"role": "user", "content": "hi"} for _ in range(3)]
    hint = parse_agent_hint({"messages": messages}, header_hint={"cache_control": {"type": "pin"}})

    assert hint.cache_control is not None
    assert hint.cache_control.range_unit is None
    assert hint.cache_control.msg_offset == 3


def test_body_cache_control_stays_legacy_strict():
    """Body cache_control rejects pin and ignores spec keys (header-only protocol)."""
    pin_body = parse_agent_hint({"agent_hint": {"cache_control": {"type": "pin", "range": {"end": 1}}}})
    assert pin_body.cache_control is None

    spec_keys = parse_agent_hint({"agent_hint": {"cache_control": {"ttl": 120, "ttl_s": 600, "range": {"end": 1}}}})
    assert spec_keys.cache_control is not None
    assert spec_keys.cache_control.ttl == 120
    assert spec_keys.cache_control.msg_offset is None
    assert spec_keys.cache_control.range_unit is None


def test_body_cache_control_wins_over_header_spec_cache_control():
    """A legacy body cache_control is authoritative; the header spec one is dropped."""
    hint = parse_agent_hint(
        {
            "messages": [{"role": "user"} for _ in range(4)],
            "agent_hint": {"cache_control": {"ttl": 120}},
        },
        header_hint={"cache_control": {"type": "pin", "range": {"end": 1}}},
    )

    assert hint.cache_control is not None
    assert hint.cache_control.type == "ephemeral"
    assert hint.cache_control.ttl == 120
    assert hint.cache_control.msg_offset == 4


def test_header_spec_cache_control_used_when_body_absent():
    """A header-only client still gets cache_control through the spec path (ttl_s clamps like ttl)."""
    hint = parse_agent_hint(
        {},
        header_hint={"cache_control": {"type": "ephemeral", "ttl_s": 12, "range": {"end": 2}}},
    )

    assert hint.cache_control is not None
    assert hint.cache_control.ttl == 60
    assert hint.cache_control.msg_offset == 2


def test_server_only_fields_stripped_on_both_cache_control_paths():
    """The anti-forgery strip covers the header spec path too."""
    hint = parse_agent_hint(
        {},
        header_hint={"cache_control": {"type": "pin", "block_offset": 99, "range": {"end": 1}}},
    )

    assert hint.cache_control is not None
    assert hint.cache_control.block_offset is None
    assert hint.cache_control.msg_offset == 1


def test_apply_strict_priority_trust_strips_untrusted():
    """WP-E2: without a valid token the strict claim is removed entirely."""
    from motor.coordinator.domain.agent_hint import AgentHintInfo, PriorityHint, apply_strict_priority_trust

    info = AgentHintInfo(strict_priority=PriorityHint(level=3, reason="critical_path"))
    applied = apply_strict_priority_trust(info, {}, None)
    assert applied is False
    assert info.strict_priority is None
    assert info.strict_priority_trusted is False


def test_apply_strict_priority_trust_keeps_trusted():
    """WP-E2: enabled + matching token keeps and marks the strict claim."""
    from motor.config.coordinator import StrictPriorityConfig
    from motor.coordinator.domain.agent_hint import AgentHintInfo, PriorityHint, apply_strict_priority_trust

    info = AgentHintInfo(strict_priority=PriorityHint(level=3, reason="critical_path"))
    cfg = StrictPriorityConfig(enabled=True, tokens=["tok-1"])
    applied = apply_strict_priority_trust(info, {"x-strict-priority-token": "tok-1"}, cfg)
    assert applied is True
    assert info.strict_priority is not None
    assert info.strict_priority_trusted is True


def test_malformed_prediction_tool_entry_never_raises():
    """Review 194526933: a non-string tool name inside an optional hint must
    degrade to WARN + drop, never escape parse_agent_hint (a 500 on the
    inference path would violate the malformed-hint contract).
    """
    hint = parse_agent_hint(
        {},
        header_hint={"prediction": {"next_action": {"state": "tool", "tools": [{"name": 1}, {"name": "ok"}]}}},
    )
    assert hint.prediction is not None
    names = [tool.name for tool in hint.prediction.next_action.tools]
    assert names == ["ok"]


@pytest.mark.parametrize(
    "header_hint",
    [
        {"priority": {"level": 1e999}},
        {"strict_priority": {"level": 1e999}},
        {"cache_control": {"type": "pin", "range": {"unit": "message", "end": 1e999}}},
        {"prediction": {"remaining_calls": 1e999}},
        {"prediction": {"next_action": {"state": "tool", "tools": [{"name": "t", "expected_duration_s": 1e999}]}}},
        {"agent_status": {"state": "tool", "state_since_ms": 1e999}},
        {"session": {"session_id": "s", "call_index": 1e999}},
        {"session": {"session_id": "s", "epoch": 1e999}},
        {"session": {"session_id": "s", "derivation": {"fork_point": 1e999}}},
        {"context_management": {"edits": [{"type": "offload", "start": 1e999}]}},
    ],
)
def test_non_finite_numbers_never_raise(header_hint):
    """Review 194527501: json ``1e999`` decodes to ``inf``; ``int(inf)`` raises
    OverflowError which is not in the tolerated exception set. Every numeric
    entry point must reject non-finite values via WARN + unset.
    """
    hint = parse_agent_hint({}, header_hint=header_hint)
    assert hint is not None
