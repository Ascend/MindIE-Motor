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
