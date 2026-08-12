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
    ensure_minimum_messages_for_session_edits,
    parse_agent_hint,
    parse_manage_request,
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
