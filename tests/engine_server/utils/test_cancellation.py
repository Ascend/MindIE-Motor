#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio

import pytest
from fastapi import Request

from motor.engine_server.utils.cancellation import with_cancellation


def _make_request(receive):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
        },
        receive=receive,
    )


@pytest.mark.asyncio
async def test_with_cancellation_returns_handler_result():
    async def receive():
        await asyncio.Future()

    @with_cancellation
    async def handler(raw_request: Request):
        return "ok"

    request = _make_request(receive)

    assert await handler(raw_request=request) == "ok"


@pytest.mark.asyncio
async def test_with_cancellation_cancels_handler_on_disconnect():
    cancelled = False

    async def receive():
        return {"type": "http.disconnect"}

    @with_cancellation
    async def handler(raw_request: Request):
        nonlocal cancelled
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled = True
            raise

    request = _make_request(receive)

    assert await handler(raw_request=request) is None
    assert cancelled is True
