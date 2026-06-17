# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from motor.coordinator.scheduler.scheduler import Scheduler


@pytest.mark.asyncio
async def test_record_precision_streak_threshold_and_probing() -> None:
    scheduler = Scheduler(MagicMock())
    threshold = 3
    for _ in range(2):
        r = await scheduler.record_precision_result(
            p_instance_id=1, d_instance_id=2, has_issue=True, threshold=threshold
        )
        assert not r["threshold_hit"]
        assert r["consecutive"] in (1, 2)
    r = await scheduler.record_precision_result(p_instance_id=1, d_instance_id=2, has_issue=True, threshold=threshold)
    assert r["threshold_hit"]
    assert r["consecutive"] == 3
    token = r["action_token"]
    assert token

    skip = await scheduler.record_precision_result(
        p_instance_id=1, d_instance_id=2, has_issue=True, threshold=threshold
    )
    assert skip["skip"]

    ok = await scheduler.finish_precision_action(p_instance_id=1, d_instance_id=2, action_token=token)
    assert ok

    r2 = await scheduler.record_precision_result(p_instance_id=1, d_instance_id=2, has_issue=True, threshold=threshold)
    assert not r2["skip"]
    assert r2["consecutive"] == 1


@pytest.mark.asyncio
async def test_false_resets_streak() -> None:
    scheduler = Scheduler(MagicMock())
    await scheduler.record_precision_result(p_instance_id=1, d_instance_id=2, has_issue=True, threshold=10)
    await scheduler.record_precision_result(p_instance_id=1, d_instance_id=2, has_issue=True, threshold=10)
    r = await scheduler.record_precision_result(p_instance_id=1, d_instance_id=2, has_issue=False, threshold=10)
    assert r["consecutive"] == 0


@pytest.mark.asyncio
async def test_finish_rejects_stale_token() -> None:
    scheduler = Scheduler(MagicMock())
    await scheduler.record_precision_result(p_instance_id=None, d_instance_id=9, has_issue=True, threshold=2)
    r = await scheduler.record_precision_result(p_instance_id=None, d_instance_id=9, has_issue=True, threshold=2)
    assert r["threshold_hit"]
    assert not await scheduler.finish_precision_action(p_instance_id=None, d_instance_id=9, action_token="wrong-token")
