# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Orchestrate check -> scheduler-global streak -> probe+alarm (async, non-blocking)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from motor.common.logger import get_logger
from motor.common.utils.consecutive_counter import ConsecutiveCounter
from motor.coordinator.fault_tolerance.alarm.base import AlarmAction, AlarmContext
from motor.coordinator.fault_tolerance.precision.checker import PrecisionChecker
from motor.coordinator.fault_tolerance.precision.sample_controller import (
    DecodeSample,
    PDGroupKey,
)

if TYPE_CHECKING:
    from typing import Any

_LOCAL_ACTION_LABEL = "local-test-token"

logger = get_logger(__name__)


class PrecisionReporter:
    def __init__(
        self,
        checker: PrecisionChecker,
        action: AlarmAction,
        *,
        threshold: int,
        scheduler_client: Any | None = None,
    ) -> None:
        self._checker = checker
        self._action = action
        self._threshold = threshold
        self._scheduler_client = scheduler_client
        # Local fallback when scheduler_client is None (unit tests only).
        self._local_counter = ConsecutiveCounter(threshold)
        self._local_probing: dict[PDGroupKey, bool] = {}
        self._probe_locks: dict[PDGroupKey, asyncio.Lock] = {}

    @property
    def _counter(self) -> ConsecutiveCounter:
        """Backward compat for tests using local fallback."""
        return self._local_counter

    def _lock(self, key: PDGroupKey) -> asyncio.Lock:
        if key not in self._probe_locks:
            self._probe_locks[key] = asyncio.Lock()
        return self._probe_locks[key]

    async def handle(self, sample: "DecodeSample") -> None:
        key: PDGroupKey = (sample.p_instance_id, sample.d_instance_id)
        lock = self._lock(key)
        async with lock:
            model = (sample.extra or {}).get("model") or None
            result = await self._checker.check(
                sample.prompt_token_ids,
                sample.output_token_ids,
                sample.logprobs,
                topk_logprobs=sample.topk_logprobs or None,
                model=model,
            )

            streak = await self._record_streak(key, result.has_issue)
            if streak is None:
                logger.warning(
                    "PrecisionReporter: streak record failed pd_group=(%s,%s), skip",
                    key[0],
                    key[1],
                )
                return
            if streak.skip:
                logger.debug(
                    "PrecisionReporter: probing active pd_group=(%s,%s), skip",
                    key[0],
                    key[1],
                )
                return

            if result.has_issue:
                logger.debug(
                    "PrecisionReporter: issue detected pd_group=(%s,%s) consecutive=%s",
                    key[0],
                    key[1],
                    streak.consecutive,
                )
            else:
                logger.debug(
                    "PrecisionReporter: check ok pd_group=(%s,%s), streak reset",
                    key[0],
                    key[1],
                )

            if not streak.threshold_hit:
                return

            issue_count = streak.consecutive
            action_token = streak.action_token
            extra = dict(sample.extra or {})
            extra.setdefault("d_infer_base_url", extra.get("d_infer_base_url") or "")
            extra.setdefault("model", extra.get("model") or "")

        logger.warning(
            "PrecisionReporter: threshold reached pd_group=(%s,%s) count=%s, launch action",
            key[0],
            key[1],
            issue_count,
        )
        asyncio.create_task(
            self._run_action(
                key=key,
                issue_count=issue_count,
                extra=extra,
                action_token=action_token,
            ),
            name=f"precision-action-{key}",
        )

    async def _record_streak(self, key: PDGroupKey, has_issue: bool):
        from motor.coordinator.fault_tolerance.precision.streak_result import (
            PrecisionStreakResult,
        )

        if self._scheduler_client is not None:
            return await self._scheduler_client.record_precision_result(key, has_issue, self._threshold)

        if self._local_probing.get(key):
            return PrecisionStreakResult(
                skip=True,
                consecutive=self._local_counter.get_count(key),
            )
        threshold_hit = await self._local_counter.record(key, has_issue)
        if threshold_hit:
            self._local_probing[key] = True
            return PrecisionStreakResult(
                threshold_hit=True,
                consecutive=self._local_counter.get_count(key),
                action_token=_LOCAL_ACTION_LABEL,
            )
        return PrecisionStreakResult(
            consecutive=self._local_counter.get_count(key),
        )

    async def _run_action(
        self,
        *,
        key: PDGroupKey,
        issue_count: int,
        extra: dict,
        action_token: str | None,
    ) -> None:
        try:
            ctx = AlarmContext(
                p_instance_id=key[0],
                d_instance_id=key[1],
                issue_count=issue_count,
                extra=extra,
            )
            await self._action.execute(ctx)
        except Exception as e:
            logger.warning("PrecisionReporter: action failed pd_group=%s: %s", key, e)
        finally:
            if self._scheduler_client is not None and action_token:
                finished = await self._scheduler_client.finish_precision_action(key, action_token)
                if not finished:
                    logger.warning(
                        "PrecisionReporter: finish_precision_action failed pd_group=%s",
                        key,
                    )
            else:
                lock = self._lock(key)
                async with lock:
                    self._local_probing[key] = False
                    await self._local_counter.reset(key)
            logger.info(
                "PrecisionReporter: action finished pd_group=(%s,%s)",
                key[0],
                key[1],
            )
