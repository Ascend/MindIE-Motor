# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for the O(P+D) dispatch-compatibility helpers in motor.common.resources.dispatch."""

from dataclasses import dataclass, field

from motor.common.resources.dispatch import (
    DispatchPlan,
    dispatch_plan_union,
    has_compatible_dispatch_pair,
    shared_dispatch_plans,
)

CONCURRENT = DispatchPlan.CONCURRENT_ENGINE_SYNC.value
HANDOFF = DispatchPlan.PREFILL_HANDOFF_DECODE.value


@dataclass
class _Inst:
    """Minimal stand-in carrying only the dispatch_capabilities attribute the helpers read."""

    id: int = 0
    dispatch_capabilities: list = field(default_factory=list)


def _pairwise_compatible(prefill_instances, decode_instances):
    """Reference O(P*D) definition the optimized helpers must stay equivalent to."""
    decode_list = list(decode_instances)
    return any(shared_dispatch_plans(p, d) for p in prefill_instances for d in decode_list)


def test_dispatch_plan_union_aggregates_and_ignores_unknown_values():
    instances = [_Inst(dispatch_capabilities=[CONCURRENT]), _Inst(dispatch_capabilities=[HANDOFF, "bogus"])]
    assert dispatch_plan_union(instances) == {
        DispatchPlan.CONCURRENT_ENGINE_SYNC,
        DispatchPlan.PREFILL_HANDOFF_DECODE,
    }
    assert dispatch_plan_union([]) == set()
    assert dispatch_plan_union([_Inst(dispatch_capabilities=[])]) == set()


def test_has_compatible_dispatch_pair_matches_pairwise_definition():
    cases = [
        ([_Inst(dispatch_capabilities=[CONCURRENT])], [_Inst(dispatch_capabilities=[CONCURRENT])]),
        ([_Inst(dispatch_capabilities=[CONCURRENT])], [_Inst(dispatch_capabilities=[HANDOFF])]),
        (
            [_Inst(dispatch_capabilities=[CONCURRENT]), _Inst(dispatch_capabilities=[HANDOFF])],
            [_Inst(dispatch_capabilities=[HANDOFF])],
        ),
        ([_Inst(dispatch_capabilities=[])], [_Inst(dispatch_capabilities=[CONCURRENT])]),
        ([], [_Inst(dispatch_capabilities=[CONCURRENT])]),
    ]
    for prefill, decode in cases:
        assert has_compatible_dispatch_pair(prefill, decode) == _pairwise_compatible(prefill, decode)
