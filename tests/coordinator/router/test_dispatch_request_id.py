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
from unittest.mock import AsyncMock, patch

# Keep the same package initialization order as existing router tests.
from motor.coordinator.domain import InstanceReadiness  # noqa: F401
from motor.coordinator.router.dispatch import _resolve_request_id


class _Headers:
    def __init__(self, headers: dict[str, str]) -> None:
        self._headers = {key.lower(): value for key, value in headers.items()}

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._headers.get(key.lower(), default)


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
