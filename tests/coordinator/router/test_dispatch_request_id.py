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
    def __init__(self, body: bytes, payload: dict) -> None:
        self.headers = {}
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
