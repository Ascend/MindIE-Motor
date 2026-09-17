# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Built-in scheduling policies adapted to the LoadBalancingPolicy rank() interface."""

from __future__ import annotations

import math
from collections.abc import Sequence

from motor.config.coordinator import (
    KV_AFFINITY_MODE_LOAD_GATED,
    KV_AFFINITY_MODE_UNIFIED,
)
from motor.coordinator.scheduler.policy.api import (
    CandidateId,
    LoadBalancingPolicy,
    RankedCandidate,
    SelectionInput,
)


class BuiltinLoadBalancePolicy(LoadBalancingPolicy):
    """Built-in load_balance strategy exposed through rank()."""

    api_version = 1
    requires_kv_match = False

    def rank(self, selection: SelectionInput) -> Sequence[RankedCandidate]:
        weight = float(self.options.get("instance_score_weight", 0.05))
        tie_order = 0
        scored: list[tuple[float, int, RankedCandidate]] = []
        for candidate in selection.candidates:
            if candidate.id in selection.excluded or candidate.blocked:
                continue
            endpoint_count = 1
            if candidate.attributes and "endpoint_count" in candidate.attributes:
                try:
                    endpoint_count = max(1, int(candidate.attributes["endpoint_count"]))
                except ValueError:
                    endpoint_count = 1
            score = candidate.active_tokens + weight * (candidate.instance_active_tokens / endpoint_count)
            item = RankedCandidate(id=candidate.id, score=score)
            scored.append((score, tie_order, item))
            tie_order += 1
        scored.sort(key=lambda item: (item[0], item[1]))
        return [item for _, _, item in scored]


class BuiltinRoundRobinPolicy(LoadBalancingPolicy):
    """Built-in round_robin strategy exposed through rank()."""

    api_version = 1
    requires_kv_match = False

    def __init__(self, options=None) -> None:
        super().__init__(options)
        self._counter = int(self.options.get("start_counter", 0))

    def set_start_counter(self, start_counter: int) -> None:
        self._counter = int(start_counter)

    def rank(self, selection: SelectionInput) -> Sequence[RankedCandidate]:
        eligible = [
            candidate
            for candidate in selection.candidates
            if candidate.id not in selection.excluded and not candidate.blocked
        ]
        if not eligible:
            return []
        n = len(eligible)
        start = self._counter % n
        self._counter = (self._counter + 1) % n if n else self._counter
        ordered = [eligible[(start + i) % n] for i in range(n)]
        return [RankedCandidate(id=candidate.id, score=float(index)) for index, candidate in enumerate(ordered)]


class BuiltinKvCacheAffinityPolicy(LoadBalancingPolicy):
    """Built-in kv_cache_affinity strategy exposed through rank()."""

    api_version = 1
    requires_kv_match = True

    def rank(self, selection: SelectionInput) -> Sequence[RankedCandidate]:
        mode = str(self.options.get("mode", KV_AFFINITY_MODE_UNIFIED)).lower()
        prefill_load_scale = float(self.options.get("prefill_load_scale", 1.0))
        load_weight = float(self.options.get("load_weight", 1.0))
        load_gate_topn = int(self.options.get("load_gate_topn", 0))
        eligible = [
            candidate
            for candidate in selection.candidates
            if candidate.id not in selection.excluded and not candidate.blocked
        ]
        if not eligible:
            return []
        # Keep KVA semantics fail-closed to load_balance when conductor data is unavailable.
        if not selection.kv_available:
            return BuiltinLoadBalancePolicy(options=self.options).rank(selection)
        kv_candidates = [candidate for candidate in eligible if candidate.kv_match is not None]
        if not kv_candidates:
            return BuiltinLoadBalancePolicy(options=self.options).rank(selection)
        indexed = list(enumerate(kv_candidates))
        if mode == KV_AFFINITY_MODE_LOAD_GATED:
            topn = load_gate_topn if load_gate_topn > 0 else 2
            gated = sorted(indexed, key=lambda item: (item[1].active_tokens, item[0]))[: max(1, topn)]
            ranked = sorted(
                gated,
                key=lambda item: (
                    -int(item[1].kv_match.matched_tokens if item[1].kv_match is not None else 0),
                    item[1].active_tokens,
                    item[0],
                ),
            )
            return [RankedCandidate(id=item[1].id, score=float(item[1].active_tokens)) for item in ranked]

        scored: list[tuple[float, int, CandidateId]] = []
        for order, candidate in indexed:
            prefill_cost = (
                float(candidate.kv_match.prefill_cost)
                if candidate.kv_match is not None
                else float(selection.request.prompt_tokens)
            )
            score = prefill_load_scale * prefill_cost + load_weight * float(candidate.active_tokens)
            if not math.isfinite(score):
                continue
            scored.append((score, order, candidate.id))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [RankedCandidate(id=candidate_id, score=score) for score, _order, candidate_id in scored]
