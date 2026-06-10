# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from motor.coordinator.scheduler.scheduler import Scheduler


@pytest.mark.asyncio
async def test_scheduler_confirm_sample_exit_respects_interval() -> None:
    scheduler = Scheduler(MagicMock())
    interval = 30.0
    assert await scheduler.confirm_sample_exit(p_instance_id=1, d_instance_id=2, now=100.0, interval_seconds=interval)
    assert not await scheduler.confirm_sample_exit(
        p_instance_id=1, d_instance_id=2, now=120.0, interval_seconds=interval
    )
    assert await scheduler.confirm_sample_exit(p_instance_id=1, d_instance_id=2, now=130.0, interval_seconds=interval)


@pytest.mark.asyncio
async def test_scheduler_confirm_sample_exit_pd_groups_independent() -> None:
    scheduler = Scheduler(MagicMock())
    t0 = 1000.0
    assert await scheduler.confirm_sample_exit(p_instance_id=None, d_instance_id=10, now=t0, interval_seconds=10.0)
    assert await scheduler.confirm_sample_exit(p_instance_id=1, d_instance_id=10, now=t0, interval_seconds=10.0)
