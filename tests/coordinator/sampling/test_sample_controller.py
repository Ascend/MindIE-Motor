# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
from unittest.mock import MagicMock

import pytest

from motor.config.coordinator import PrecisionDetectionConfig
from motor.coordinator.fault_tolerance.precision.sample_controller import DecodeSample, SampleController


@pytest.mark.asyncio
async def test_local_claim_sample_is_atomic_per_decode_instance() -> None:
    controller = SampleController(PrecisionDetectionConfig(interval_seconds=30.0), MagicMock())

    results = await asyncio.gather(*(controller.claim_sample(7, 100.0) for _ in range(8)))

    assert results.count(True) == 1


@pytest.mark.asyncio
async def test_enqueue_sample_does_not_wait_for_precision_check() -> None:
    release_check = asyncio.Event()
    precision = MagicMock()

    async def handle(_sample) -> None:
        await release_check.wait()

    precision.handle = handle
    controller = SampleController(PrecisionDetectionConfig(), precision)
    sample = DecodeSample(
        p_instance_id=1,
        d_instance_id=2,
        prompt_token_ids=[1],
        output_token_ids=[2],
        logprobs=[-0.1],
        req_id="req-1",
    )

    controller.enqueue_sample(sample)

    assert len(controller._background_tasks) == 1
    release_check.set()
    await asyncio.gather(*tuple(controller._background_tasks))
    assert not controller._background_tasks


@pytest.mark.asyncio
async def test_shutdown_cancels_and_drains_background_checks() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()
    precision = MagicMock()

    async def handle(_sample) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    precision.handle = handle
    controller = SampleController(PrecisionDetectionConfig(), precision)
    sample = DecodeSample(
        p_instance_id=1,
        d_instance_id=2,
        prompt_token_ids=[1],
        output_token_ids=[2],
        logprobs=[-0.1],
        req_id="req-shutdown",
    )
    controller.enqueue_sample(sample)
    await started.wait()

    await controller.shutdown()

    assert cancelled.is_set()
    assert not controller._background_tasks
