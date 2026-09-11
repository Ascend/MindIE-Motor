# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
HPA autoscaling metric contract.

K8s External Metrics names conventionally avoid ``:``, so the metrics that
HPA / External Metrics Adaptors consume are exposed twice on ``/metrics``:
once under their canonical ``motor:`` / ``vllm:`` name and once under a
colon-free alias (``:`` → ``_``).  Only the contract metrics below get an
alias; everything else keeps its single canonical series.
"""

# Canonical names that form the HPA contract. Histogram-derived gauges
# (``_mean`` / ``_p50`` / ``_p95`` / ``_p99``) of a contract histogram are
# covered via its base name.
_HPA_CONTRACT_BASE_NAMES: frozenset[str] = frozenset(
    {
        "motor:prefill_utilization",
        "motor:decode_utilization",
        "motor:prefill_replicas_required",
        "motor:decode_replicas_required",
        "motor:request_rate",
        "motor:pd_ratio_current",
        "motor:pd_ratio_suggested",
        "vllm:request_prefill_time_seconds",
    }
)

# Suffixes appended by SemanticAggregationEngine quantile/mean post-processing.
_DERIVED_SUFFIXES: tuple[str, ...] = ("_mean", "_p50", "_p95", "_p99")


def get_hpa_alias(metric_name: str) -> str | None:
    """Return the colon-free HPA alias for *metric_name*, or None.

    A metric gets an alias when it is a contract metric itself or a derived
    quantile/mean gauge of a contract histogram. The alias is the metric name
    with every ``:`` replaced by ``_``.
    """
    base = metric_name
    for suffix in _DERIVED_SUFFIXES:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    if base in _HPA_CONTRACT_BASE_NAMES:
        return metric_name.replace(":", "_")
    return None
