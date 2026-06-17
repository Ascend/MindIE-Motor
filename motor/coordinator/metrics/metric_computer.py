# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Motor-specific computed metrics framework.

All Motor-originated metrics that are derived from raw engine counters
or instance metadata are registered and computed here.  The
MetricsCollector delegates to MotorMetricComputer at two injection
points:

* pre_aggregation  – DP-level metrics injected into each endpoint's
  metrics list; they then flow through the normal aggregation pipeline
  (summed at instance / node / role / service scope).
* post_aggregation – service-level metrics appended directly to the
  final aggregate list after the engine-metric aggregation is complete.
"""

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from motor.common.resources import PDRole
from motor.coordinator.metrics.metric_types import Metric, MetricType


# ---------------------------------------------------------------------------
# Computed-metric definition
# ---------------------------------------------------------------------------


@dataclass
class ComputedMetricDef:
    """Declarative definition of a single Motor-computed metric."""

    name: str  # Prometheus metric name
    help: str  # HELP text
    phase: str  # "pre_aggregation" | "post_aggregation"
    compute_type: str  # "counter_rate" | "worker_count"
    source_counters: list[str] = field(default_factory=list)
    role_filter: list[str] | None = None  # e.g. ["decode"] or None (all roles)


# ---------------------------------------------------------------------------
# Built-in registry of Motor computed metrics
# ---------------------------------------------------------------------------

_MOTOR_COMPUTED_METRICS: list[ComputedMetricDef] = [
    # -- DP-level: counter rate → tokens-per-second --------------------------
    ComputedMetricDef(
        name="motor:prompt_tokens_per_second",
        help=("Prompt tokens per second computed from vllm:prompt_tokens_total counter deltas"),
        phase="pre_aggregation",
        compute_type="counter_rate",
        source_counters=["vllm:prompt_tokens_total"],
        role_filter=None,
    ),
    ComputedMetricDef(
        name="motor:generation_tokens_per_second",
        help=("Generation tokens per second computed from vllm:generation_tokens_total counter deltas"),
        phase="pre_aggregation",
        compute_type="counter_rate",
        source_counters=["vllm:generation_tokens_total"],
        role_filter=None,
    ),
    # -- Service-level: worker counts ----------------------------------------
    ComputedMetricDef(
        name="motor:active_prefill_workers",
        help="Number of active prefill instances",
        phase="post_aggregation",
        compute_type="worker_count",
        role_filter=["prefill"],
    ),
    ComputedMetricDef(
        name="motor:active_decode_workers",
        help="Number of active decode instances",
        phase="post_aggregation",
        compute_type="worker_count",
        role_filter=["decode"],
    ),
    ComputedMetricDef(
        name="motor:inactive_prefill_workers",
        help="Number of inactive prefill instances",
        phase="post_aggregation",
        compute_type="worker_count",
        role_filter=["prefill"],
    ),
    ComputedMetricDef(
        name="motor:inactive_decode_workers",
        help="Number of inactive decode instances",
        phase="post_aggregation",
        compute_type="worker_count",
        role_filter=["decode"],
    ),
]


# ---------------------------------------------------------------------------
# MotorMetricComputer
# ---------------------------------------------------------------------------


class MotorMetricComputer:
    """Centralised computation of Motor-specific metrics.

    Two-phase design:

    1. ``compute_pre_aggregation`` – called inside ``_collect_metrics``
       after parsing.  Injects DP-level derived metrics (e.g. TPS) into
       each endpoint's ``metrics`` list so they participate in the
       normal aggregation pipeline.

    2. ``compute_post_aggregation`` – called inside
       ``_generate_full_metrics`` after the engine-metric aggregation +
       post-processing.  Appends service-level metrics (e.g. worker
       counts) directly to the aggregate list.
    """

    def __init__(self) -> None:
        # DP-level counter-rate tracking state.
        # Key:  (job_name, dp_rank, source_counter_name)
        # Value: dict with baseline, last_effective, last_raw, last_ts, last_ins_id
        self._dp_state: dict[tuple[str, int, str], dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_pre_aggregation(
        self,
        collects: dict[int, dict[str, Any]],
    ) -> None:
        """DP-level metrics: inject into each endpoint's metrics list."""
        for defn in _get_defs_by_phase("pre_aggregation"):
            if defn.compute_type == "counter_rate":
                self._compute_counter_rates(collects, defn)

    def compute_post_aggregation(
        self,
        aggregate: list[Metric],
        collects: dict[int, dict[str, Any]],
        deploy_config: Any,
    ) -> None:
        """Service-level metrics: append to the aggregate list."""
        for defn in _get_defs_by_phase("post_aggregation"):
            if defn.compute_type == "worker_count":
                self._compute_worker_counts(aggregate, collects, deploy_config, defn)

    # ------------------------------------------------------------------
    # Counter rate (DP-level)
    # ------------------------------------------------------------------

    def _compute_counter_rates(
        self,
        collects: dict[int, dict[str, Any]],
        defn: ComputedMetricDef,
    ) -> None:
        """Correct raw counters in-place with baseline and inject TPS gauges.

        For each endpoint that carries one of *defn.source_counters* the
        raw vLLM counter value is adjusted so that it never drops across
        engine restarts (tracked per ``(job_name, dp_rank)``).  A TPS
        rate gauge is also injected.
        """
        now = time.monotonic()
        for ins_id, ins_data in collects.items():
            job_name: str = ins_data.get("job_name", "")
            if not job_name:
                continue

            for ep_id, pod_info in ins_data.get("endpoints", {}).items():
                metrics: list[Metric] = pod_info.get("metrics", [])
                if not metrics:
                    continue

                for src_name in defn.source_counters:
                    src_metric = _find_metric(metrics, src_name)
                    if src_metric is None:
                        continue

                    raw_total = float(sum(src_metric.value))
                    effective, tps = self._compute_effective_and_rate(
                        job_name=job_name,
                        dp_rank=ep_id,
                        src_name=src_name,
                        raw_counter=raw_total,
                        ins_id=ins_id,
                        now=now,
                    )

                    # Correct raw counter values in-place so that every
                    # downstream view (dp / instance / role / full) sees a
                    # continuous total across engine restarts.
                    offset = effective - raw_total
                    if offset != 0.0:
                        _per_label = offset / float(len(src_metric.value))
                        for i in range(len(src_metric.value)):
                            src_metric.value[i] += _per_label

                    # Inject TPS rate (GAUGE)
                    metrics.append(
                        Metric(
                            name=defn.name,
                            help=defn.help,
                            type=MetricType.GAUGE,
                            label=[defn.name],
                            value=[tps],
                        )
                    )

    # ------------------------------------------------------------------
    # Effective counter + TPS (shared state machine)
    # ------------------------------------------------------------------

    def _compute_effective_and_rate(
        self,
        job_name: str,
        dp_rank: int,
        src_name: str,
        raw_counter: float,
        ins_id: int,
        now: float,
    ) -> tuple[float, float]:
        """Return ``(effective_counter, tps_rate)`` with restart-resilient baseline."""
        key = (job_name, dp_rank, src_name)
        state = self._dp_state.get(key)

        if state is None:
            self._dp_state[key] = {
                "baseline": 0.0,
                "last_effective": raw_counter,
                "last_raw": raw_counter,
                "last_ts": now,
                "last_ins_id": ins_id,
            }
            return raw_counter, 0.0

        # Detect engine restart: new instance_id or counter dropped >10 %
        restart = ins_id != state["last_ins_id"] or raw_counter < state["last_raw"] * 0.9
        if restart:
            state["baseline"] = state["last_effective"]
            state["last_ins_id"] = ins_id

        effective = raw_counter + state["baseline"]
        dt = now - state["last_ts"]
        tps = (effective - state["last_effective"]) / dt if dt > 0 else 0.0

        state["last_effective"] = effective
        state["last_raw"] = raw_counter
        state["last_ts"] = now

        return effective, max(tps, 0.0)

    # ------------------------------------------------------------------
    # Worker counts (service-level)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_worker_counts(
        aggregate: list[Metric],
        collects: dict[int, dict[str, Any]],
        deploy_config: Any,
        defn: ComputedMetricDef,
    ) -> None:
        """Generate a single worker-count metric determined by *defn*."""
        role_counts = Counter(ins_data.get("role", "") for ins_data in collects.values())
        available_p = role_counts.get(PDRole.ROLE_P, 0)
        available_d = role_counts.get(PDRole.ROLE_D, 0)
        p_num = deploy_config.p_instances_num
        d_num = deploy_config.d_instances_num

        name = defn.name
        if name == "motor:active_prefill_workers":
            value = available_p
        elif name == "motor:active_decode_workers":
            value = available_d
        elif name == "motor:inactive_prefill_workers":
            value = p_num - available_p
        elif name == "motor:inactive_decode_workers":
            value = d_num - available_d
        else:
            return

        aggregate.append(Metric(
            name=name,
            help=defn.help,
            type=MetricType.GAUGE,
            label=[name],
            value=[value],
        ))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_defs_by_phase(phase: str) -> list[ComputedMetricDef]:
    """Return registered computed-metric definitions for *phase*."""
    return [d for d in _MOTOR_COMPUTED_METRICS if d.phase == phase]


def _get_counter_sum(metrics: list[Metric], name: str) -> float | None:
    """Return the sum of all label values for counter *name* in *metrics*,
    or None if the counter is not present.
    """
    for m in metrics:
        if m.name == name:
            return float(sum(m.value))
    return None


def _find_metric(metrics: list[Metric], name: str) -> Metric | None:
    """Return the Metric with *name* in *metrics*, or None."""
    for m in metrics:
        if m.name == name:
            return m
    return None


# ---------------------------------------------------------------------------
# Inherited counter names (for inactive-aggregate coordination)
# ---------------------------------------------------------------------------


def get_inherited_metric_names() -> set[str]:
    """Return source counter names whose values are inherited across restarts.

    These raw vLLM counters are corrected in-place by
    ``MotorMetricComputer._compute_counter_rates``, so the inactive
    aggregate must NOT preserve their old values (the new instance
    already carries forward the inherited total via baseline offset).
    """
    inherited: set[str] = set()
    for defn in _MOTOR_COMPUTED_METRICS:
        if defn.compute_type == "counter_rate":
            inherited.update(defn.source_counters)
    return inherited
