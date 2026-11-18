# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Parse Runtime Guard anomaly counters from engine metrics."""

from __future__ import annotations

import re
from collections.abc import Callable

_COUNTER_NAMES = frozenset(
    {
        "vllm_profiling_runtime_guard:anomaly:count_total",
        "vllm_profiling_runtime_guard:anomaly:count",
        "runtime_guard:anomaly:count_total",
        "runtime_guard:anomaly:count",
    }
)
_PROCESS_START_NAMES = frozenset({"process_start_time_seconds"})
_INCIDENT_TYPE_RE = re.compile(r'incident_type="([^"]*)"')


def _read_anomaly_samples(
    metrics_text: str,
    parser: Callable[[str], list | None],
) -> tuple[dict[str, float], float | None, bool]:
    """Return incident totals, process start time, and whether the text was readable."""
    parsed = parser(metrics_text)
    if parsed is None:
        return {}, None, False

    totals: dict[str, float] = {}
    generation: float | None = None
    for metric in parsed:
        if metric.name in _PROCESS_START_NAMES and metric.value and generation is None:
            generation = metric.value[0]
        if metric.name not in _COUNTER_NAMES:
            continue
        for label, value in zip(metric.label, metric.value):
            match = _INCIDENT_TYPE_RE.search(label)
            if match is None:
                continue
            incident_type = match.group(1)
            totals[incident_type] = totals.get(incident_type, 0.0) + value
    return totals, generation, True
