# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Per-decode-instance sampling admission and background precision checks."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from motor.common.logger import get_logger

if TYPE_CHECKING:
    from motor.config.coordinator import PrecisionDetectionConfig
    from motor.coordinator.fault_tolerance.precision.reporter import PrecisionReporter

logger = get_logger(__name__)

# (p_instance_id or None, d_instance_id)
# p_instance_id 为 None 表示请求由单个 Hybrid/Union 实例完成。
PDGroupKey = tuple[int | None, int]


@dataclass
class DecodeSample:
    """One decode sample: token ids for checking plus safe structures for tracing."""

    p_instance_id: int | None
    d_instance_id: int
    prompt_token_ids: list[int]
    output_token_ids: list[int]
    logprobs: list[float]
    req_id: str
    timestamp: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)
    # Per-position top-k logprobs for msprobe (Chat only). Aligned with
    # ``output_token_ids``; ``logprobs_count == 1`` yields single-key dicts.
    topk_logprobs: list[dict[int, float]] = field(default_factory=list)
    # W3C traceparent/tracestate headers from the request that generated this
    # sample, so PrecisionReporter can create a child span under the original
    # trace when reporting anomalies.
    trace_headers: dict[str, str] = field(default_factory=dict)
    # Content-free request/output summaries for trace attributes.
    request_structure: str = ""
    output_structure: str = ""


class SampleController:
    """Per-D-instance sampling admission and background precision checks.

    Design:
    - **Entry-side admission**: only one request per D instance and
      ``interval_seconds`` injects precision-sampling fields.
    - ``claim_sample`` delegates to the **scheduler process** via ZMQ so all
      inference workers share one admission table.
    - Local per-worker state is only used when ``scheduler_client`` is None (tests).
    - Precision checks run in tracked background tasks and never block the user response.
    """

    def __init__(
        self,
        config: "PrecisionDetectionConfig",
        precision: "PrecisionReporter",
        *,
        scheduler_client: Any | None = None,
    ) -> None:
        self._interval: float = config.interval_seconds
        self._precision = precision
        self._scheduler_client = scheduler_client
        self._local_last_claim_time: dict[int, float] = {}
        self._local_locks: dict[int, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task] = set()

    def _local_lock(self, d_instance_id: int) -> asyncio.Lock:
        if d_instance_id not in self._local_locks:
            self._local_locks[d_instance_id] = asyncio.Lock()
        return self._local_locks[d_instance_id]

    async def _claim_sample_local(self, d_instance_id: int, now: float) -> bool:
        lock = self._local_lock(d_instance_id)
        async with lock:
            last_claim = self._local_last_claim_time.get(d_instance_id, 0.0)
            if now - last_claim >= self._interval:
                self._local_last_claim_time[d_instance_id] = now
                return True
        return False

    async def claim_sample(self, d_instance_id: int, now: float) -> bool:
        """Claim the next sampling window for a D instance before engine dispatch."""
        if self._scheduler_client is not None:
            try:
                claimed = await self._scheduler_client.claim_sample(d_instance_id, now, self._interval)
                if claimed:
                    logger.debug(
                        "SampleController: claimed (scheduler) d_instance_id=%s interval=%.1fs",
                        d_instance_id,
                        self._interval,
                    )
                return claimed
            except Exception as e:
                logger.warning(
                    "SampleController: scheduler claim_sample failed d_instance_id=%s: %s",
                    d_instance_id,
                    e,
                )
                return False

        if await self._claim_sample_local(d_instance_id, now):
            logger.debug(
                "SampleController: claimed (local) d_instance_id=%s interval=%.1fs",
                d_instance_id,
                self._interval,
            )
            return True
        return False

    def enqueue_sample(self, sample: DecodeSample) -> None:
        """Run a completed sample through the precision pipeline in the background."""
        task = asyncio.create_task(
            self._handle_sample(sample),
            name=f"precision-check-{sample.d_instance_id}-{sample.req_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def shutdown(self) -> None:
        """Cancel and drain background checks before the scheduler connection closes."""
        tasks = tuple(self._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_sample(self, sample: DecodeSample) -> None:
        try:
            await self._precision.handle(sample)
        except Exception as e:
            logger.warning(
                "SampleController: precision.handle failed pd_group=(%s,%s) req_id=%s: %s",
                sample.p_instance_id,
                sample.d_instance_id,
                sample.req_id,
                e,
            )
