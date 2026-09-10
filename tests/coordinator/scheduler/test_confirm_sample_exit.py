# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from motor.coordinator.scheduler.scheduler import Scheduler


@pytest.mark.asyncio
async def test_scheduler_precision_sample_admission_respects_interval() -> None:
    scheduler = Scheduler(MagicMock())
    interval = 30.0
    assert await scheduler.claim_precision_sample(d_instance_id=2, now=100.0, interval_seconds=interval)
    assert not await scheduler.claim_precision_sample(d_instance_id=2, now=120.0, interval_seconds=interval)
    assert await scheduler.claim_precision_sample(d_instance_id=2, now=130.0, interval_seconds=interval)


@pytest.mark.asyncio
async def test_scheduler_precision_sample_admission_is_independent_per_decode_instance() -> None:
    scheduler = Scheduler(MagicMock())
    t0 = 1000.0
    assert await scheduler.claim_precision_sample(d_instance_id=10, now=t0, interval_seconds=10.0)
    assert await scheduler.claim_precision_sample(d_instance_id=11, now=t0, interval_seconds=10.0)


@pytest.mark.asyncio
async def test_scheduler_precision_sample_admission_is_atomic() -> None:
    scheduler = Scheduler(MagicMock())

    results = await asyncio.gather(
        *(scheduler.claim_precision_sample(d_instance_id=10, now=1000.0, interval_seconds=30.0) for _ in range(8))
    )

    assert results.count(True) == 1
