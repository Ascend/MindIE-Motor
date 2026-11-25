# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Shared HPA spec construction for deployment backends."""

import lib.constant as C

_DEFAULT_SCALING_METRICS = {
    C.ROLE_PREFILL: C.DEFAULT_PREFILL_SCALING_METRIC,
    C.ROLE_DECODE: C.DEFAULT_DECODE_SCALING_METRIC,
}


def build_hpa_spec(role_name, policy_cfg, default_max_replicas, *, selector=None, target_ref=None):
    if not isinstance(policy_cfg, dict):
        raise ValueError(f"scaling_policy for role '{role_name}' must be a dict")
    metric_name = policy_cfg.get(C.SCALING_METRIC) or _DEFAULT_SCALING_METRICS.get(role_name)
    if not metric_name:
        raise ValueError(
            f"scaling_policy for role '{role_name}' requires '{C.SCALING_METRIC}' (no default metric for this role)"
        )
    min_replicas = int(policy_cfg.get(C.SCALING_MIN_REPLICAS, C.DEFAULT_SCALING_MIN_REPLICAS))
    max_replicas = int(policy_cfg.get(C.SCALING_MAX_REPLICAS, default_max_replicas))
    if min_replicas < 1 or max_replicas < min_replicas:
        raise ValueError(
            f"scaling_policy for role '{role_name}' requires 1 <= min_replicas <= max_replicas, "
            f"got min_replicas={min_replicas}, max_replicas={max_replicas}"
        )
    target = float(policy_cfg.get(C.SCALING_TARGET, C.DEFAULT_SCALING_TARGET))
    if target <= 0:
        raise ValueError(f"scaling_policy for role '{role_name}' requires target > 0, got {target}")
    target_type = policy_cfg.get(C.SCALING_TARGET_TYPE, C.DEFAULT_SCALING_TARGET_TYPE)
    if target_type not in C.SCALING_TARGET_TYPES:
        raise ValueError(
            f"scaling_policy for role '{role_name}' requires target_type in {C.SCALING_TARGET_TYPES}, "
            f"got {target_type!r}"
        )
    target_block = (
        {"type": "Value", "value": str(target)}
        if target_type == "Value"
        else {"type": "AverageValue", "averageValue": str(target)}
    )
    metric_selector = {C.SELECTOR: {C.MATCHLABELS: selector}} if selector else {}
    spec = {
        "minReplicas": min_replicas,
        "maxReplicas": max_replicas,
        "metrics": [
            {
                "type": "External",
                "external": {
                    "metric": {C.NAME: metric_name, **metric_selector},
                    "target": target_block,
                },
            }
        ],
    }
    if target_ref is not None:
        spec["scaleTargetRef"] = target_ref
    return spec
