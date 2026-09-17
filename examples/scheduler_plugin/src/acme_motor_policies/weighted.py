# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


from motor.coordinator.scheduler.policy.api import (
    LoadBalancingPolicy,
    RankedCandidate,
    SelectionInput,
)


class WeightedPolicy(LoadBalancingPolicy):
    api_version = 1
    requires_kv_match = True

    def rank(self, selection: SelectionInput) -> list[RankedCandidate]:
        load_weight = float(self.options.get("load_weight", 1.0))
        kv_weight = float(self.options.get("kv_weight", 0.8))
        ranked: list[RankedCandidate] = []

        for candidate in selection.candidates:
            if candidate.id in selection.excluded or candidate.blocked:
                continue
            matched = candidate.kv_match.matched_tokens if candidate.kv_match else 0
            score = load_weight * candidate.active_tokens - kv_weight * matched
            ranked.append(RankedCandidate(id=candidate.id, score=score))

        return sorted(ranked, key=lambda item: item.score)
