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

"""Per-PD-group sampling interval control and sample submission to precision pipeline."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from motor.common.logger import get_logger

if TYPE_CHECKING:
    from motor.config.coordinator import TokenSamplingConfig
    from motor.coordinator.fault_tolerance.precision.reporter import PrecisionReporter

logger = get_logger(__name__)

# (p_instance_id or None, d_instance_id)
# p_instance_id 为 None 表示 CDP/PD_SEPARATE 模式下 coordinator 侧不感知 P 实例
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
    """Per-PD-group sampling interval and submission to precision pipeline.

    Design:
    - **Exit-side gate**: all decode requests inject logprobs; only one sample per PD
      group per ``interval_seconds`` is submitted.
    - ``confirm_sample`` delegates to the **scheduler process** via ZMQ so all
      inference workers share one ``_last_exit_time`` table.
    - Local per-worker state is only used when ``scheduler_client`` is None (tests).
    """

    def __init__(
        self,
        config: "TokenSamplingConfig",
        precision: "PrecisionReporter",
        *,
        scheduler_client: Any | None = None,
    ) -> None:
        self._interval: float = config.interval_seconds
        self._precision = precision
        self._scheduler_client = scheduler_client
        self._local_last_exit_time: dict[PDGroupKey, float] = {}
        self._local_locks: dict[PDGroupKey, asyncio.Lock] = {}

    def _local_lock(self, key: PDGroupKey) -> asyncio.Lock:
        if key not in self._local_locks:
            self._local_locks[key] = asyncio.Lock()
        return self._local_locks[key]

    async def _confirm_sample_local(self, key: PDGroupKey, now: float) -> bool:
        lock = self._local_lock(key)
        async with lock:
            last_exit = self._local_last_exit_time.get(key, 0.0)
            if now - last_exit >= self._interval:
                self._local_last_exit_time[key] = now
                return True
        return False

    async def confirm_sample(self, key: PDGroupKey, now: float) -> bool:
        """Exit gate: True if this PD group may submit a sample (interval elapsed)."""
        if self._scheduler_client is not None:
            try:
                confirmed = await self._scheduler_client.confirm_sample(key, now, self._interval)
                if confirmed:
                    logger.debug(
                        "SampleController: confirmed (scheduler) pd_group=(%s,%s) interval=%.1fs",
                        key[0],
                        key[1],
                        self._interval,
                    )
                return confirmed
            except Exception as e:
                logger.warning(
                    "SampleController: scheduler confirm_sample failed pd_group=%s: %s",
                    key,
                    e,
                )
                return False

        if await self._confirm_sample_local(key, now):
            logger.debug(
                "SampleController: confirmed (local) pd_group=(%s,%s) interval=%.1fs",
                key[0],
                key[1],
                self._interval,
            )
            return True
        return False

    async def submit_sample(self, sample: DecodeSample) -> None:
        """Submit a confirmed sample to the precision pipeline."""
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
