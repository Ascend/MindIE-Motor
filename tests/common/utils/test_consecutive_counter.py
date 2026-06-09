# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import pytest

from motor.common.utils.consecutive_counter import ConsecutiveCounter


@pytest.mark.asyncio
async def test_record_hits_threshold() -> None:
    c = ConsecutiveCounter(threshold=3)
    key = (1, 2)
    assert await c.record(key, True) is False
    assert await c.record(key, True) is False
    assert await c.record(key, True) is True
    assert c.get_count(key) == 3


@pytest.mark.asyncio
async def test_record_miss_resets() -> None:
    c = ConsecutiveCounter(threshold=3)
    key = (None, 5)
    await c.record(key, True)
    await c.record(key, True)
    assert await c.record(key, False) is False
    assert c.get_count(key) == 0
    assert await c.record(key, True) is False


@pytest.mark.asyncio
async def test_reset_clears_count() -> None:
    c = ConsecutiveCounter(threshold=2)
    key = (1, 1)
    await c.record(key, True)
    await c.reset(key)
    assert c.get_count(key) == 0
