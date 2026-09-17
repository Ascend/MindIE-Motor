# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Policy execution, output validation, fallback, and metrics."""

from __future__ import annotations

import math
import time
from collections.abc import Sequence

from motor.common.logger import get_logger
from motor.coordinator.scheduler.policy.api import (
    CandidateId,
    LoadBalancingPolicy,
    RankedCandidate,
    SelectionInput,
)
from motor.coordinator.scheduler.policy.metrics import get_policy_metrics

logger = get_logger(__name__)


class PolicyExecutor:
    """Invoke a policy, validate output, record metrics, and apply fallback."""

    def __init__(
        self,
        policy: LoadBalancingPolicy,
        *,
        policy_name: str,
        fallback_policy: LoadBalancingPolicy | None = None,
        fallback_name: str | None = None,
    ) -> None:
        self._policy = policy
        self._policy_name = policy_name
        self._fallback_policy = fallback_policy
        self._fallback_name = fallback_name or "load_balance"
        self._metrics = get_policy_metrics()
        self._last_used_fallback = False

    @property
    def policy_name(self) -> str:
        return self._policy_name

    @property
    def last_used_fallback(self) -> bool:
        return self._last_used_fallback

    @property
    def requires_kv_match(self) -> bool:
        return bool(getattr(self._policy, "requires_kv_match", False))

    @property
    def policy(self) -> LoadBalancingPolicy:
        return self._policy

    @property
    def fallback_policy(self) -> LoadBalancingPolicy | None:
        return self._fallback_policy

    def rank(self, selection: SelectionInput) -> tuple[RankedCandidate, ...]:
        started = time.perf_counter()
        used_fallback = False
        try:
            ranked = self._invoke_policy(self._policy, selection)
            if not ranked:
                ranked = self._try_fallback(selection)
                used_fallback = bool(ranked)
        except Exception as exc:
            logger.warning(
                "Policy %s rank() failed attempt=%s: %s",
                self._policy_name,
                selection.attempt,
                exc,
                exc_info=True,
            )
            self._metrics.inc_errors(self._policy_name)
            ranked = self._try_fallback(selection)
            used_fallback = bool(ranked)
        duration = time.perf_counter() - started
        self._metrics.observe_selection(self._policy_name, duration)
        if duration > self._metrics.selection_warn_threshold():
            logger.warning(
                "Policy %s rank() slow duration_seconds=%.4f attempt=%s",
                self._policy_name,
                duration,
                selection.attempt,
            )
        if used_fallback:
            self._metrics.inc_fallback(self._policy_name)
        self._last_used_fallback = used_fallback
        return ranked

    def _try_fallback(self, selection: SelectionInput) -> tuple[RankedCandidate, ...]:
        if self._fallback_policy is None:
            return ()
        try:
            return self._invoke_policy(self._fallback_policy, selection)
        except Exception as exc:
            logger.warning(
                "Fallback policy %s rank() failed attempt=%s: %s",
                self._fallback_name,
                selection.attempt,
                exc,
                exc_info=True,
            )
            self._metrics.inc_errors(self._fallback_name)
            return ()

    def _invoke_policy(
        self,
        policy: LoadBalancingPolicy,
        selection: SelectionInput,
    ) -> tuple[RankedCandidate, ...]:
        raw = policy.rank(selection)
        if raw is None:
            raise ValueError("rank() returned None")
        if not isinstance(raw, Sequence):
            raise ValueError("rank() must return a Sequence")
        allowed = {
            candidate.id
            for candidate in selection.candidates
            if candidate.id not in selection.excluded and not candidate.blocked
        }
        seen: set[CandidateId] = set()
        validated: list[RankedCandidate] = []
        for item in raw:
            if not isinstance(item, RankedCandidate):
                raise ValueError("rank() items must be RankedCandidate")
            if item.id not in allowed:
                raise ValueError("rank() returned unknown or excluded candidate id")
            if item.id in seen:
                raise ValueError("rank() returned duplicate candidate id")
            if not math.isfinite(item.score):
                raise ValueError("rank() returned non-finite score")
            seen.add(item.id)
            validated.append(item)
        return tuple(validated)
