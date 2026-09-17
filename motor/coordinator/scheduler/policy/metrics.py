# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""In-process policy execution metrics (per Inference Worker)."""

from __future__ import annotations

import math
import threading
from collections import defaultdict

_POLICY_SELECTION_WARN_SECONDS = 0.05


class PolicyMetrics:
    """Thread-safe counters and histogram buckets for policy execution."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._selection_count: dict[str, int] = defaultdict(int)
        self._selection_sum_seconds: dict[str, float] = defaultdict(float)
        self._errors_total: dict[str, int] = defaultdict(int)
        self._fallback_total: dict[str, int] = defaultdict(int)
        self._cas_retries_total: dict[str, int] = defaultdict(int)

    def observe_selection(self, policy: str, duration_seconds: float) -> None:
        if duration_seconds < 0 or not math.isfinite(duration_seconds):
            return
        with self._lock:
            self._selection_count[policy] += 1
            self._selection_sum_seconds[policy] += duration_seconds

    def inc_errors(self, policy: str, count: int = 1) -> None:
        with self._lock:
            self._errors_total[policy] += count

    def inc_fallback(self, policy: str, count: int = 1) -> None:
        with self._lock:
            self._fallback_total[policy] += count

    def inc_cas_retries(self, policy: str, count: int = 1) -> None:
        with self._lock:
            self._cas_retries_total[policy] += count

    def reset(self) -> None:
        with self._lock:
            self._selection_count.clear()
            self._selection_sum_seconds.clear()
            self._errors_total.clear()
            self._fallback_total.clear()
            self._cas_retries_total.clear()

    def render_prometheus(self) -> str:
        with self._lock:
            selection_count = dict(self._selection_count)
            selection_sum = dict(self._selection_sum_seconds)
            errors = dict(self._errors_total)
            fallback = dict(self._fallback_total)
            cas_retries = dict(self._cas_retries_total)
        lines: list[str] = []
        all_policies = sorted(set(selection_count) | set(errors) | set(fallback) | set(cas_retries))
        if not all_policies:
            return ""
        lines.append("# HELP motor_policy_selection_seconds Policy rank() execution duration")
        lines.append("# TYPE motor_policy_selection_seconds summary")
        for policy in all_policies:
            count = selection_count.get(policy, 0)
            total = selection_sum.get(policy, 0.0)
            if count:
                avg = total / count
                lines.append('motor_policy_selection_seconds{policy="%s",quantile="avg"} %s' % (policy, avg))
        lines.append("# HELP motor_policy_errors_total Policy exceptions or invalid outputs")
        lines.append("# TYPE motor_policy_errors_total counter")
        for policy, value in sorted(errors.items()):
            lines.append('motor_policy_errors_total{policy="%s"} %s' % (policy, value))
        lines.append("# HELP motor_policy_fallback_total Policy fallback invocations")
        lines.append("# TYPE motor_policy_fallback_total counter")
        for policy, value in sorted(fallback.items()):
            lines.append('motor_policy_fallback_total{policy="%s"} %s' % (policy, value))
        lines.append("# HELP motor_policy_cas_retries_total CAS conflict re-selection attempts")
        lines.append("# TYPE motor_policy_cas_retries_total counter")
        for policy, value in sorted(cas_retries.items()):
            lines.append('motor_policy_cas_retries_total{policy="%s"} %s' % (policy, value))
        return "\n".join(lines) + "\n"

    @staticmethod
    def selection_warn_threshold() -> float:
        return _POLICY_SELECTION_WARN_SECONDS


_metrics = PolicyMetrics()


def get_policy_metrics() -> PolicyMetrics:
    return _metrics


def append_policy_metrics(prometheus_text: str) -> str:
    """Append process-local motor_policy_* samples to an existing exposition body."""
    extra = get_policy_metrics().render_prometheus()
    if not extra:
        return prometheus_text
    if prometheus_text and not prometheus_text.endswith("\n"):
        prometheus_text += "\n"
    return (prometheus_text or "") + extra
