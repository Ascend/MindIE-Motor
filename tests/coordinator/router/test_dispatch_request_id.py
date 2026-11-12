# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
#
# MindIE is licensed under both the Mulan PSL v2 and the Apache License, Version 2.0.
# You may choose to use this software under the terms of either license.
#
# ---------------------------------------------------------------------------
# Mulan PSL v2:
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
#
# Apache License, Version 2.0:
# You may obtain a copy of the License at:
#         http://www.apache.org/licenses/LICENSE-2.0
# ---------------------------------------------------------------------------
#
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the respective licenses for more details.

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

# Keep the same package initialization order as existing router tests.
from motor.coordinator.domain import InstanceReadiness  # noqa: F401
from motor.common.logger.logger import _resolve_logger_name
from motor.coordinator.models.request import ReqState
from motor.coordinator.router.dispatch import _resolve_request_id, __create_request_info

_DISPATCH_LOGGER = _resolve_logger_name("motor.coordinator.router.dispatch")


class _Headers:
    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = {key.lower(): value for key, value in headers.items()}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._headers.get(key.lower(), default)

    def items(self):
        return self._headers.items()


class _FakeRequest:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = _Headers(headers)


def test_resolve_request_id_appends_unique_suffix_to_traceparent():
    trace_id = "0af7651916cd43dd8448ebd08f9ca98e"
    request = _FakeRequest({"traceparent": f"00-{trace_id}-0100000000000000-01"})
    request_manager = AsyncMock()

    with patch("motor.coordinator.router.dispatch.uuid.uuid4") as mock_uuid:
        mock_uuid.return_value.hex = "1234567890abcdef"
        request_id = asyncio.run(_resolve_request_id(request, request_manager))

    assert request_id == f"{trace_id}-12345678"
    request_manager.generate_request_id.assert_not_called()


def test_resolve_request_id_appends_unique_suffix_to_x_request_id():
    request = _FakeRequest({"x-request-id": "upstream-req"})
    request_manager = AsyncMock()

    with patch("motor.coordinator.router.dispatch.uuid.uuid4") as mock_uuid:
        mock_uuid.return_value.hex = "abcdef1234567890"
        request_id = asyncio.run(_resolve_request_id(request, request_manager))

    assert request_id == "upstream-req-abcdef12"
    request_manager.generate_request_id.assert_not_called()


def test_resolve_request_id_generates_local_id_without_upstream_id():
    request = _FakeRequest({})
    request_manager = AsyncMock()
    request_manager.generate_request_id.return_value = "local789"

    request_id = asyncio.run(_resolve_request_id(request, request_manager))

    assert request_id == "local789"
    request_manager.generate_request_id.assert_awaited_once()


class _Url:
    def __init__(self, path: str) -> None:
        self.path = path


class _IngressRequest:
    def __init__(self, body: bytes, payload: dict, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {}
        self.url = _Url("/v1/chat/completions")
        self._body = body
        self._payload = payload

    async def body(self):
        return self._body

    async def json(self):
        return self._payload


@pytest.mark.asyncio
async def test_create_request_info_logs_request_arrive(caplog):
    caplog.set_level(logging.INFO, logger=_DISPATCH_LOGGER)
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    raw = _IngressRequest(b'{"messages":[]}', payload)
    request_manager = AsyncMock()
    request_manager.generate_request_id.return_value = "req-arrive-1"

    req_info = await __create_request_info(raw, request_manager)

    assert req_info.req_id == "req-arrive-1"
    arrive_ts = req_info.status[ReqState.ARRIVE]
    records = [rec.getMessage() for rec in caplog.records if "stage=request_arrive" in rec.getMessage()]
    assert len(records) == 1
    assert "req_id=req-arrive-1" in records[0]
    assert f"{arrive_ts:.6f}" in records[0]


def _b64_hint(payload: dict) -> str:
    import base64
    import json

    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


@pytest.mark.asyncio
async def test_create_request_info_header_only_session_reaches_engine_body():
    """Header-only hints (no body agent_hint) must reach req_data, not only request_json.

    Regression for the E2E finding: dispatch copied request_json before merging the
    X-Agent-Hint header, so a header-only session never entered the engine request.
    """
    header = _b64_hint(
        {
            "session": {"session_id": "s-header-only"},
            "cache_control": {"type": "ephemeral", "ttl_s": 120, "range": {"end": 2}},
        }
    )
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    raw = _IngressRequest(b'{"messages":[]}', payload, headers={"x-agent-hint": header})
    request_manager = AsyncMock()
    request_manager.generate_request_id.return_value = "req-header-only"

    req_info = await __create_request_info(raw, request_manager)

    assert req_info.req_data["agent_hint"]["session_id"] == "s-header-only"
    # Spec cache_control is translated into the engine view only; the parse view
    # stays header-only for parse_agent_hint.
    assert req_info.req_data["agent_hint"]["cache_control"] == {
        "type": "ephemeral",
        "ttl": 120,
        "msg_offset": 2,
    }
    assert "cache_control" not in payload["agent_hint"]


@pytest.mark.asyncio
async def test_create_request_info_attaches_offsets_for_plain_cache_control():
    """Ordinary requests with cache_control must translate msg -> block coordinates."""
    header = _b64_hint(
        {
            "session": {"session_id": "s-cc"},
            "cache_control": {"type": "ephemeral", "ttl_s": 60, "range": {"end": 1}},
        }
    )
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    raw = _IngressRequest(b'{"messages":[]}', payload, headers={"x-agent-hint": header})
    request_manager = AsyncMock()
    request_manager.generate_request_id.return_value = "req-cc"

    with patch("motor.coordinator.router.dispatch._attach_hint_block_offsets") as attach:
        await __create_request_info(raw, request_manager)

    attach.assert_called_once()


@pytest.mark.asyncio
async def test_create_request_info_skips_offsets_without_hint():
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    raw = _IngressRequest(b'{"messages":[]}', payload)
    request_manager = AsyncMock()
    request_manager.generate_request_id.return_value = "req-plain"

    with patch("motor.coordinator.router.dispatch._attach_hint_block_offsets") as attach:
        await __create_request_info(raw, request_manager)

    attach.assert_not_called()


@pytest.mark.asyncio
async def test_engine_hint_strip_keeps_scheduler_hint(monkeypatch):
    """Motor-only affinity: parse the hint, but leave the engine prefix cache untouched."""
    monkeypatch.setenv("MOTOR_ENGINE_AGENT_HINT", "0")
    header = _b64_hint(
        {
            "session": {"session_id": "s-motor-only"},
            "cache_control": {"type": "ephemeral", "ttl_s": 300},
        }
    )
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    raw = _IngressRequest(b'{"messages":[]}', payload, headers={"x-agent-hint": header})
    request_manager = AsyncMock()
    request_manager.generate_request_id.return_value = "req-motor-only"

    req_info = await __create_request_info(raw, request_manager)

    assert "agent_hint" not in req_info.req_data
    assert req_info.agent_hint_info.session_id == "s-motor-only"


@pytest.mark.asyncio
async def test_engine_hint_strip_keeps_manage_requests(monkeypatch):
    """Manage requests need the engine-side session hint to resolve the session."""
    monkeypatch.setenv("MOTOR_ENGINE_AGENT_HINT", "0")
    payload = {
        "messages": [{"role": "user", "content": ""}],
        "agent_hint": {
            "session_id": "s-manage",
            "context_management": {
                "manage_request": True,
                "edits": [{"type": "offload", "target": "session"}],
            },
        },
    }
    raw = _IngressRequest(b'{"messages":[]}', payload)
    request_manager = AsyncMock()
    request_manager.generate_request_id.return_value = "req-manage"

    req_info = await __create_request_info(raw, request_manager)

    assert req_info.req_data["agent_hint"]["session_id"] == "s-manage"
