# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from motor.coordinator.scheduler.scheduler import Scheduler


def _record(scheduler: Scheduler, **kwargs):
    defaults = {
        "p_instance_id": 1,
        "d_instance_id": 2,
        "has_issue": True,
        "threshold": 3,
        "clear_threshold": 10,
        "check_valid": True,
    }
    defaults.update(kwargs)
    return scheduler.record_precision_result(**defaults)


@pytest.mark.asyncio
async def test_record_precision_streak_threshold_and_probing() -> None:
    scheduler = Scheduler(MagicMock())
    threshold = 3
    for _ in range(2):
        r = await _record(scheduler, has_issue=True, threshold=threshold)
        assert not r["threshold_hit"]
        assert r["consecutive"] in (1, 2)
    r = await _record(scheduler, has_issue=True, threshold=threshold)
    assert r["threshold_hit"]
    assert r["consecutive"] == 3
    token = r["action_token"]
    assert token

    skip = await _record(scheduler, has_issue=True, threshold=threshold)
    assert skip["skip"]

    ok = await scheduler.finish_precision_action(
        p_instance_id=1,
        d_instance_id=2,
        action_token=token,
        action_type="raise",
        success=True,
        alarm_moi="service name=Coordinator, service ip=1.2.3.4, pId=1, instanceId=2",
    )
    assert ok
    assert scheduler._precision_alarm_active[(1, 2)]
    assert scheduler._precision_alarm_moi[(1, 2)].endswith("instanceId=2")

    r2 = await _record(scheduler, has_issue=True, threshold=threshold)
    assert not r2["skip"]
    assert r2["consecutive"] == 0


@pytest.mark.asyncio
async def test_dismiss_precision_alarm_state_clears_group() -> None:
    scheduler = Scheduler(MagicMock())
    scheduler._precision_alarm_active[(1, 2)] = True
    scheduler._precision_alarm_moi[(1, 2)] = "moi-1"
    scheduler._precision_normal_streak_counts[(1, 2)] = 3

    ok = await scheduler.dismiss_precision_alarm_state(p_instance_id=1, d_instance_id=2)
    assert ok
    assert (1, 2) not in scheduler._precision_alarm_active
    assert (1, 2) not in scheduler._precision_alarm_moi
    assert (1, 2) not in scheduler._precision_normal_streak_counts


@pytest.mark.asyncio
async def test_false_resets_streak() -> None:
    scheduler = Scheduler(MagicMock())
    await _record(scheduler, has_issue=True, threshold=10)
    await _record(scheduler, has_issue=True, threshold=10)
    r = await _record(scheduler, has_issue=False, threshold=10)
    assert r["consecutive"] == 0


@pytest.mark.asyncio
async def test_finish_rejects_stale_token() -> None:
    scheduler = Scheduler(MagicMock())
    await _record(scheduler, p_instance_id=None, d_instance_id=9, has_issue=True, threshold=2)
    r = await _record(scheduler, p_instance_id=None, d_instance_id=9, has_issue=True, threshold=2)
    assert r["threshold_hit"]
    assert not await scheduler.finish_precision_action(
        p_instance_id=None,
        d_instance_id=9,
        action_token="wrong-token",
        action_type="raise",
        success=True,
    )


@pytest.mark.asyncio
async def test_alarm_active_clear_after_normal_streak() -> None:
    scheduler = Scheduler(MagicMock())
    moi = "service name=Coordinator, service ip=10.0.0.1, instanceId=9"
    r = await _record(scheduler, p_instance_id=None, d_instance_id=9, has_issue=True, threshold=1)
    assert r["threshold_hit"]
    await scheduler.finish_precision_action(
        p_instance_id=None,
        d_instance_id=9,
        action_token=r["action_token"],
        action_type="raise",
        success=True,
        alarm_moi=moi,
    )
    for i in range(9):
        nr = await _record(
            scheduler,
            p_instance_id=None,
            d_instance_id=9,
            has_issue=False,
            clear_threshold=10,
        )
        assert not nr["clear_threshold_hit"]
        assert nr["consecutive"] == i + 1
    cr = await _record(
        scheduler,
        p_instance_id=None,
        d_instance_id=9,
        has_issue=False,
        clear_threshold=10,
    )
    assert cr["clear_threshold_hit"]
    assert cr["alarm_moi"] == moi
    await scheduler.finish_precision_action(
        p_instance_id=None,
        d_instance_id=9,
        action_token=cr["action_token"],
        action_type="clear",
        success=True,
    )
    assert (None, 9) not in scheduler._precision_alarm_active


@pytest.mark.asyncio
async def test_auto_recovery_clears_group_state() -> None:
    scheduler = Scheduler(MagicMock())
    r = await _record(scheduler, has_issue=True, threshold=1)
    await scheduler.finish_precision_action(
        p_instance_id=1,
        d_instance_id=2,
        action_token=r["action_token"],
        action_type="raise",
        success=True,
        alarm_moi="moi-a",
        auto_recovery_cleared=True,
    )
    assert (1, 2) not in scheduler._precision_alarm_active


@pytest.mark.asyncio
async def test_auto_recovery_finish_ok_after_external_dismiss() -> None:
    """Controller dismiss clears raise token before Reporter finishes."""
    scheduler = Scheduler(MagicMock())
    r = await _record(scheduler, has_issue=True, threshold=1)
    await scheduler.dismiss_precision_alarm_state(p_instance_id=1, d_instance_id=2)
    ok = await scheduler.finish_precision_action(
        p_instance_id=1,
        d_instance_id=2,
        action_token=r["action_token"],
        action_type="raise",
        success=True,
        alarm_moi="moi-a",
        auto_recovery_cleared=True,
    )
    assert ok
    assert (1, 2) not in scheduler._precision_alarm_active


@pytest.mark.asyncio
async def test_invalid_check_does_not_count_toward_clear() -> None:
    scheduler = Scheduler(MagicMock())
    moi = "moi-b"
    r = await _record(scheduler, has_issue=True, threshold=1)
    await scheduler.finish_precision_action(
        p_instance_id=1,
        d_instance_id=2,
        action_token=r["action_token"],
        action_type="raise",
        success=True,
        alarm_moi=moi,
    )
    nr = await _record(scheduler, has_issue=False, check_valid=False)
    assert nr["consecutive"] == 0
    assert not nr["clear_threshold_hit"]


@pytest.mark.asyncio
async def test_raise_failure_resets_streak_counters() -> None:
    scheduler = Scheduler(MagicMock())
    threshold = 2
    await _record(scheduler, has_issue=True, threshold=threshold)
    r = await _record(scheduler, has_issue=True, threshold=threshold)
    assert r["threshold_hit"]
    scheduler._precision_normal_streak_counts[(1, 2)] = 5

    ok = await scheduler.finish_precision_action(
        p_instance_id=1,
        d_instance_id=2,
        action_token=r["action_token"],
        action_type="raise",
        success=False,
    )
    assert ok
    assert scheduler._precision_streak_counts[(1, 2)] == 0
    assert scheduler._precision_normal_streak_counts[(1, 2)] == 0


@pytest.mark.asyncio
async def test_clear_failure_resets_normal_streak_counter() -> None:
    scheduler = Scheduler(MagicMock())
    moi = "moi-clear-fail"
    r = await _record(scheduler, has_issue=True, threshold=1)
    await scheduler.finish_precision_action(
        p_instance_id=1,
        d_instance_id=2,
        action_token=r["action_token"],
        action_type="raise",
        success=True,
        alarm_moi=moi,
    )
    for _ in range(9):
        await _record(scheduler, has_issue=False, clear_threshold=10)
    cr = await _record(scheduler, has_issue=False, clear_threshold=10)
    assert cr["clear_threshold_hit"]

    ok = await scheduler.finish_precision_action(
        p_instance_id=1,
        d_instance_id=2,
        action_token=cr["action_token"],
        action_type="clear",
        success=False,
    )
    assert ok
    assert scheduler._precision_normal_streak_counts[(1, 2)] == 0
    assert scheduler._precision_alarm_active[(1, 2)]
