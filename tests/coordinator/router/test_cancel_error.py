# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
from __future__ import annotations

import asyncio

import pytest

import motor.common.utils.error as cancel_error
from motor.coordinator.router.strategies.base import check_cancel_error


def _cancelled(*args: str) -> asyncio.CancelledError:
    return asyncio.CancelledError(*args)


@pytest.mark.parametrize(
    ("error", "expected_reason", "expected_retry"),
    [
        (_cancelled(), "Exception", True),
        (_cancelled(cancel_error.CLIENT_DISCONNECT), cancel_error.CLIENT_DISCONNECT, False),
        (_cancelled(cancel_error.DISPATCH_ABORT), cancel_error.DISPATCH_ABORT, False),
        (_cancelled(cancel_error.SCOPE_ABORT), cancel_error.SCOPE_ABORT, False),
        (
            _cancelled(
                "Cancelled via cancel scope ffff980d3d10 by "
                "<Task pending name='Task-31' coro=<RequestResponseCycle.run_asgi()>>"
            ),
            cancel_error.SCOPE_ABORT,
            False,
        ),
        (
            _cancelled(f"{cancel_error.NODE_FAULT}: connection reset"),
            f"{cancel_error.NODE_FAULT}: connection reset",
            True,
        ),
        (_cancelled("some unknown transport error"), "some unknown transport error", True),
    ],
)
def test_check_cancel_error(
    error: asyncio.CancelledError,
    expected_reason: str,
    expected_retry: bool,
) -> None:
    reason, retry = check_cancel_error(error)
    assert reason == expected_reason
    assert retry is expected_retry
