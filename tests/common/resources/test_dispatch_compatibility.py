# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for dispatch helpers in motor.common.resources.dispatch."""

from dataclasses import dataclass, field
from types import SimpleNamespace

from motor.common.resources.dispatch import (
    DispatchPlan,
    DispatchProfile,
    dispatch_plan_union,
    has_compatible_dispatch_pair,
    infer_vllm_dispatch_profile_from_config,
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


class _EngineConfig:
    def __init__(self, configs):
        self.configs = configs

    def get(self, key, default=None):
        return self.configs.get(key, default)


class _Config:
    def __init__(self, engine_type="vllm", engine_config=None, dispatch_profile=None):
        self._endpoint_config = SimpleNamespace(
            engine_type=engine_type,
            deploy_config=SimpleNamespace(
                engine_config=_EngineConfig(engine_config or {}),
                dispatch_profile=dispatch_profile,
            ),
        )

    def get_endpoint_config(self):
        return self._endpoint_config


def test_infer_vllm_dispatch_profile_from_config_layerwise():
    config = _Config(
        engine_config={
            "kv_transfer_config": {
                "kv_connector": "MooncakeLayerwiseConnector",
            }
        }
    )
    assert infer_vllm_dispatch_profile_from_config(config) == DispatchProfile.TRIGGER


def test_infer_vllm_dispatch_profile_from_config_handoff():
    config = _Config(
        engine_config={
            "kv_transfer_config": {
                "kv_connector": "MooncakeHybridConnector",
            }
        }
    )
    assert infer_vllm_dispatch_profile_from_config(config) == DispatchProfile.HANDOFF


def test_infer_vllm_dispatch_profile_from_config_non_vllm_engine():
    config = _Config(engine_type="sglang")
    assert infer_vllm_dispatch_profile_from_config(config) == DispatchProfile.UNKNOWN
