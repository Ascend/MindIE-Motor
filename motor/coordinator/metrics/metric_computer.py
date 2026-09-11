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

The HPA CapacityPlanner is fed on the collection path
(``update_planner``, called from ``MetricsCollector._collect_metrics``
right after ``compute_pre_aggregation``), so its snapshot interval
follows the collection cycle rather than any particular view's scrape
cadence.  The full view (``compute_post_aggregation``) and the role
view (``compute_role_utilization``) only render the planner's cached
output.
"""

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from motor.common.logger import get_logger
from motor.common.resources import PDRole
from motor.coordinator.metrics.capacity_planner import (
    CapacityPlanner,
    PlannerConfig,
    PlannerSnapshot,
    RoleSnapshot,
)
from motor.coordinator.metrics.metric_types import Metric, MetricType

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Computed-metric definition
# ---------------------------------------------------------------------------


@dataclass
class ComputedMetricDef:
    """Declarative definition of a single Motor-computed metric."""

    name: str  # Prometheus metric name
    help: str  # HELP text
    phase: str  # "pre_aggregation" | "post_aggregation"
    compute_type: str  # "counter_rate" | "worker_count" | "pd_ratio"
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
    ComputedMetricDef(
        name="motor:request_rate",
        help=("Request completion rate (requests/s) computed from vllm:request_success_total counter deltas"),
        phase="pre_aggregation",
        compute_type="counter_rate",
        source_counters=["vllm:request_success_total"],
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
    # -- Service-level: PD ratio signals --------------------------------------
    ComputedMetricDef(
        name="motor:pd_ratio_current",
        help="Current PD ratio: active_prefill_workers / max(active_decode_workers, 1)",
        phase="post_aggregation",
        compute_type="pd_ratio",
    ),
    ComputedMetricDef(
        name="motor:pd_ratio_suggested",
        help=(
            "Suggested PD ratio derived from the CapacityPlanner's "
            "pd_ratio_required_raw, EMA-smoothed and clamped; equals the "
            "current ratio while capacity is uncalibrated or demand-free; "
            "advisory signal for an external controller"
        ),
        phase="post_aggregation",
        compute_type="pd_ratio",
    ),
]

# Planner compute() key → (metric name, HELP text).  Keys absent from the
# planner output (role never observed, no prior, K unknown) are skipped.
_PLANNER_GAUGE_METRICS: tuple[tuple[str, str, str], ...] = (
    (
        "prefill_utilization",
        "motor:prefill_utilization",
        "Prefill utilization: demand_tps / (active_instances * capacity_tps)",
    ),
    (
        "decode_utilization",
        "motor:decode_utilization",
        "Decode utilization: demand_tps / (active_instances * capacity_tps)",
    ),
    (
        "prefill_replicas_required",
        "motor:prefill_replicas_required",
        "Prefill replicas required by the demand/capacity model (0 when uncalibrated)",
    ),
    (
        "decode_replicas_required",
        "motor:decode_replicas_required",
        "Decode replicas required: max of throughput and KV constraints",
    ),
    (
        "prefill_capacity_tps",
        "motor:prefill_capacity_tps",
        "Estimated per-instance prefill capacity in prompt tokens/s",
    ),
    (
        "decode_capacity_tps",
        "motor:decode_capacity_tps",
        "Estimated per-instance decode capacity in generation tokens/s",
    ),
    (
        "prefill_demand_tps",
        "motor:prefill_demand_tps",
        "Prefill demand in prompt tokens/s: EMA(arrival rate) x EMA(mean length)",
    ),
    (
        "decode_demand_tps",
        "motor:decode_demand_tps",
        "Decode demand in generation tokens/s: EMA(arrival rate) x EMA(mean length)",
    ),
    (
        "kv_demand_tokens",
        "motor:kv_demand_tokens",
        "KV cache demand in tokens: arrival_rate x W_kv x (mean input + output length)",
    ),
)

_CAPACITY_CALIBRATED_METRIC = (
    "motor:capacity_calibrated",
    "Whether the role capacity estimate is calibrated (1) or not (0)",
)


# Cumulative counters that must stay monotonic across engine restarts.
_CUMULATIVE_COUNTERS: frozenset[str] = frozenset(
    {
        "vllm:prompt_tokens_total",
        "vllm:generation_tokens_total",
        "vllm:new_tokens_total",
        "vllm:request_success_total",
        "vllm:num_preemptions_total",
        "vllm:prefix_cache_queries_total",
        "vllm:prefix_cache_hits_total",
        "vllm:gpu_prefix_cache_queries_total",
        "vllm:gpu_prefix_cache_hits_total",
    }
)


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
       counts and the cached CapacityPlanner signals) directly to the
       aggregate list.
    """

    def __init__(self) -> None:
        # Per-series counter tracking state.  A series is one label of one
        # counter, so a multi-label counter never shares an offset between
        # labels.
        # Key:  (job_name, dp_rank, series_label)
        # Value: dict with baseline, last_effective, last_raw, last_ts, last_ins_id
        self._dp_state: dict[tuple[str, int, str], dict[str, Any]] = {}
        # EMA state for motor:pd_ratio_suggested (None = not initialized).
        self._pd_ratio_smoothed: float | None = None
        # CapacityPlanner lifecycle: rebuilt whenever the capacity_planning
        # sub-config changes (PlannerConfig dataclass equality).
        self._planner: CapacityPlanner | None = None
        self._planner_config: PlannerConfig | None = None
        self._last_planner_ts: float | None = None
        self._planner_output: dict[str, float] = {}
        # One-shot WARNING flag: decode instances without usable
        # vllm:cache_config_info (KV constraint degraded).  Reset when K
        # becomes available again.
        self._kv_info_missing_warned = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_pre_aggregation(
        self,
        collects: dict[int, dict[str, Any]],
    ) -> None:
        """DP-level metrics: inject into each endpoint's metrics list."""
        self._correct_cumulative_counters(collects)

    def compute_post_aggregation(
        self,
        aggregate: list[Metric],
        collects: dict[int, dict[str, Any]],
        deploy_config: Any,
        metrics_config: Any = None,
    ) -> None:
        """Service-level metrics: append to the aggregate list.

        Planner signals are rendered from the output cached by
        ``update_planner`` (fed on the collection path); this method never
        feeds the planner itself.
        """
        self._emit_planner_metrics(aggregate)
        for defn in _get_defs_by_phase("post_aggregation"):
            if defn.compute_type == "worker_count":
                self._compute_worker_counts(aggregate, collects, deploy_config, defn)
            elif defn.compute_type == "pd_ratio":
                self._compute_pd_ratio(aggregate, collects, defn, metrics_config)

    def update_planner(
        self,
        collects: dict[int, dict[str, Any]],
        metrics_config: Any,
    ) -> None:
        """Feed one collection-cycle snapshot into the CapacityPlanner.

        Called from ``MetricsCollector._collect_metrics`` right after
        ``compute_pre_aggregation``, so the snapshot interval ``dt``
        (``time.monotonic()`` delta) follows the collection cycle; the
        first snapshot only establishes counter baselines inside the
        planner (``dt = 0`` produces no samples).  The planner is (re)built
        whenever the ``capacity_planning`` sub-config changes, which also
        resets the pd_ratio suggestion EMA.
        """
        planning_config = getattr(metrics_config, "capacity_planning", None)
        if planning_config is None:
            return

        planner_config = PlannerConfig(
            target_utilization=getattr(planning_config, "target_utilization", 0.8),
            kv_target_utilization=getattr(planning_config, "kv_target_utilization", 0.9),
            ema_alpha=getattr(planning_config, "ema_alpha", 0.85),
            capacity_decay=getattr(planning_config, "capacity_decay", 0.005),
            prefill_tps_capacity_prior=getattr(planning_config, "prefill_tps_capacity_prior", 0.0),
            decode_tps_capacity_prior=getattr(planning_config, "decode_tps_capacity_prior", 0.0),
            capacity_window_cycles=getattr(planning_config, "capacity_window_cycles", 200),
        )
        if self._planner is None or planner_config != self._planner_config:
            self._planner = CapacityPlanner(planner_config)
            self._planner_config = planner_config
            self._last_planner_ts = None
            self._planner_output = {}
            self._pd_ratio_smoothed = None

        now = time.monotonic()
        dt = 0.0 if self._last_planner_ts is None else max(now - self._last_planner_ts, 0.0)
        self._last_planner_ts = now

        snapshot = _build_snapshot(collects, dt)
        self._planner.update(snapshot)
        self._planner_output = self._planner.compute()
        self._warn_if_kv_info_missing(snapshot)

    def _warn_if_kv_info_missing(self, snapshot: PlannerSnapshot) -> None:
        """One-shot WARNING when decode runs without KV block info.

        With ``kv_tokens_per_instance_decode`` unknown the planner skips the KV
        constraint and derives ``decode_replicas_required`` from the
        throughput constraint only; per the HPA metrics contract this
        degradation MUST be logged once.  The flag resets when K becomes
        available again (e.g. after an engine upgrade).
        """
        decode = snapshot.decode
        if decode is None:
            return
        if decode.kv_tokens_per_instance_decode > 0.0:
            self._kv_info_missing_warned = False
            return
        if self._kv_info_missing_warned:
            return
        self._kv_info_missing_warned = True
        logger.warning(
            "[Metrics] vllm:cache_config_info unavailable on all %d decode "
            "instance(s): KV constraint degraded, motor:decode_replicas_required "
            "is derived from the throughput constraint only. Likely cause: the "
            "engine version does not expose cache_config_info.",
            decode.active_instances,
        )

    def compute_role_utilization(
        self,
        collects: dict[int, dict[str, Any]],
        role: str,
    ) -> list[Metric]:
        """Return the role's utilization metric from the latest planner output.

        Used by the role view.  Returns an empty list for unknown roles or
        when the planner has not produced a utilization for the role yet
        (never observed, no capacity, or no active instances).
        """
        del collects  # utilization comes from the planner's cached output
        # Snapshot the shared dict once: the collection thread may rebind
        # self._planner_output between two attribute loads (config-change
        # rebuild or a fresh compute() without this key).
        output = self._planner_output
        for key, name, help_text in _PLANNER_GAUGE_METRICS:
            if key == f"{role}_utilization" and key in output:
                return [
                    Metric(
                        name=name,
                        help=help_text,
                        type=MetricType.GAUGE,
                        label=[name],
                        value=[output[key]],
                    )
                ]
        return []

    # ------------------------------------------------------------------
    # Cumulative counters (DP-level): restart compensation + TPS
    # ------------------------------------------------------------------

    def _correct_cumulative_counters(
        self,
        collects: dict[int, dict[str, Any]],
    ) -> None:
        """Correct cumulative counters in-place and inject TPS gauges.

        An engine restart resets these counters to 0, and the Obs process
        cannot see the unavailable-instance pool, so nothing else carries the
        old totals forward.  Every label is tracked as its own series: sharing
        one offset across labels would move a label's history onto labels that
        never fired.  Counters declared by a ``counter_rate`` definition also
        get their TPS gauge injected here.
        """
        now = time.monotonic()
        rate_defs = _get_rate_defs_by_source()
        for ins_id, ins_data in collects.items():
            job_name: str = ins_data.get("job_name", "")
            if not job_name:
                continue

            for ep_id, pod_info in ins_data.get("endpoints", {}).items():
                metrics: list[Metric] = pod_info.get("metrics", [])
                injected: list[Metric] = []

                for metric in metrics:
                    if metric.name not in _CUMULATIVE_COUNTERS:
                        continue

                    tps_total = 0.0
                    for i, label in enumerate(metric.label):
                        effective, tps = self._compute_effective_and_rate(
                            job_name=job_name,
                            dp_rank=ep_id,
                            series=label,
                            raw_counter=float(metric.value[i]),
                            ins_id=ins_id,
                            now=now,
                        )
                        metric.value[i] = effective
                        tps_total += tps

                    defn = rate_defs.get(metric.name)
                    if defn is not None:
                        injected.append(
                            Metric(
                                name=defn.name,
                                help=defn.help,
                                type=MetricType.GAUGE,
                                label=[defn.name],
                                value=[tps_total],
                            )
                        )

                metrics.extend(injected)

    # ------------------------------------------------------------------
    # Effective counter + TPS (shared state machine)
    # ------------------------------------------------------------------

    def _compute_effective_and_rate(
        self,
        job_name: str,
        dp_rank: int,
        series: str,
        raw_counter: float,
        ins_id: int,
        now: float,
    ) -> tuple[float, float]:
        """Return ``(effective_counter, tps_rate)`` with restart-resilient baseline.

        *series* identifies one label of one counter, so each label keeps its
        own baseline.
        """
        key = (job_name, dp_rank, series)
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

        aggregate.append(
            Metric(
                name=name,
                help=defn.help,
                type=MetricType.GAUGE,
                label=[name],
                value=[value],
            )
        )

    # ------------------------------------------------------------------
    # Capacity planning output (rendered from the update_planner cache)
    # ------------------------------------------------------------------

    def _emit_planner_metrics(self, aggregate: list[Metric]) -> None:
        """Append the cached CapacityPlanner outputs to the aggregate list."""
        # Read the cache once: update_planner swaps it on the collection
        # thread, so repeated attribute loads could observe mixed states.
        output = self._planner_output
        for key, name, help_text in _PLANNER_GAUGE_METRICS:
            if key in output:
                aggregate.append(
                    Metric(
                        name=name,
                        help=help_text,
                        type=MetricType.GAUGE,
                        label=[name],
                        value=[output[key]],
                    )
                )
        calibrated_labels = []
        calibrated_values = []
        for role in ("prefill", "decode"):
            key = f"capacity_calibrated_{role}"
            if key in output:
                calibrated_labels.append(f'{_CAPACITY_CALIBRATED_METRIC[0]}{{role="{role}"}}')
                calibrated_values.append(output[key])
        if calibrated_labels:
            aggregate.append(
                Metric(
                    name=_CAPACITY_CALIBRATED_METRIC[0],
                    help=_CAPACITY_CALIBRATED_METRIC[1],
                    type=MetricType.GAUGE,
                    label=calibrated_labels,
                    value=calibrated_values,
                )
            )

    # ------------------------------------------------------------------
    # PD ratio signals (service-level)
    # ------------------------------------------------------------------

    def _compute_pd_ratio(
        self,
        aggregate: list[Metric],
        collects: dict[int, dict[str, Any]],
        defn: ComputedMetricDef,
        metrics_config: Any,
    ) -> None:
        """Append motor:pd_ratio_current / motor:pd_ratio_suggested."""
        role_counts = Counter(ins_data.get("role", "") for ins_data in collects.values())
        active_p = role_counts.get(PDRole.ROLE_P, 0)
        active_d = role_counts.get(PDRole.ROLE_D, 0)
        current = active_p / max(active_d, 1)

        if defn.name == "motor:pd_ratio_current":
            value = current
        else:
            # Suggested ratio: the planner's required ratio, EMA-smoothed and
            # clamped.  Falls back to the current ratio when the planner has
            # not run yet (no capacity_planning config).  Planner output and
            # EMA state are read into locals once: update_planner may swap
            # them concurrently on the collection thread.
            output = self._planner_output
            target = output.get("pd_ratio_required_raw", current)
            planning_config = getattr(metrics_config, "capacity_planning", None)
            ratio_min = getattr(planning_config, "pd_ratio_min", None)
            if ratio_min is None:
                ratio_min = 0.05
            ratio_max = getattr(planning_config, "pd_ratio_max", None)
            if ratio_max is None:
                ratio_max = 20.0
            alpha = getattr(planning_config, "pd_ratio_smooth_alpha", None)
            if alpha is None:
                alpha = 0.3
            target = min(max(target, ratio_min), ratio_max)
            smoothed = self._pd_ratio_smoothed
            if smoothed is None:
                smoothed = target
            else:
                smoothed += alpha * (target - smoothed)
            self._pd_ratio_smoothed = smoothed
            value = smoothed

        aggregate.append(
            Metric(
                name=defn.name,
                help=defn.help,
                type=MetricType.GAUGE,
                label=[defn.name],
                value=[value],
            )
        )


# ---------------------------------------------------------------------------
# Planner snapshot construction
# ---------------------------------------------------------------------------

# Role → (tokens counter, per-instance TPS gauge)
_ROLE_COUNTERS: dict[str, tuple[str, str]] = {
    PDRole.ROLE_P: ("vllm:prompt_tokens_total", "motor:prompt_tokens_per_second"),
    PDRole.ROLE_D: (
        "vllm:generation_tokens_total",
        "motor:generation_tokens_per_second",
    ),
}

_NUM_GPU_BLOCKS_RE = re.compile(r'num_gpu_blocks="(\d+)"')
_BLOCK_SIZE_RE = re.compile(r'block_size="(\d+)"')


def _parse_kv_tokens_per_instance(label: str) -> float:
    """Parse ``num_gpu_blocks`` x ``block_size`` from a cache_config_info label.

    Returns 0.0 when either attribute is missing or malformed.
    """
    blocks = _NUM_GPU_BLOCKS_RE.search(label)
    block_size = _BLOCK_SIZE_RE.search(label)
    if blocks is None or block_size is None:
        return 0.0
    return float(int(blocks.group(1)) * int(block_size.group(1)))


def _build_snapshot(
    collects: dict[int, dict[str, Any]],
    dt: float,
) -> PlannerSnapshot:
    """Aggregate the collects counters into a PlannerSnapshot.

    All counter fields are role-level sums of the cumulative counters.  The
    plain counters (request_success_total / tokens_total) are already
    baseline-corrected by the counter_rate pass; the histogram ``_sum`` /
    ``_count`` label rows are consumed raw and negative deltas caused by
    engine restarts or scale-in are absorbed by the planner's hold-on-bad-
    delta semantics.  A role without instances yields a None snapshot.
    """
    accumulators: dict[str, dict[str, Any]] = {}
    for ins_data in collects.values():
        role = ins_data.get("role", "")
        counters = _ROLE_COUNTERS.get(role)
        if counters is None:
            continue
        tokens_name, tps_name = counters
        acc = accumulators.setdefault(
            role,
            {
                "instances": 0,
                "instance_tps": [],
                "request_success_total": 0.0,
                "tokens_total": 0.0,
                "prefill_time_sum": 0.0,
                "queue_time_sum": 0.0,
                "queue_time_count": 0.0,
                "decode_time_sum": 0.0,
                "decode_time_count": 0.0,
                "kv_tokens_per_instance_decode": 0.0,
            },
        )
        acc["instances"] += 1
        instance_tps = 0.0
        for pod_info in ins_data.get("endpoints", {}).values():
            metrics: list[Metric] = pod_info.get("metrics", [])
            acc["request_success_total"] += _get_counter_sum(metrics, "vllm:request_success_total") or 0.0
            acc["tokens_total"] += _get_counter_sum(metrics, tokens_name) or 0.0
            instance_tps += _get_counter_sum(metrics, tps_name) or 0.0
            if role == PDRole.ROLE_P:
                acc["prefill_time_sum"] += _histogram_row_sum(metrics, "vllm:request_prefill_time_seconds_sum")
            else:
                acc["queue_time_sum"] += _histogram_row_sum(metrics, "vllm:request_queue_time_seconds_sum")
                acc["queue_time_count"] += _histogram_row_sum(metrics, "vllm:request_queue_time_seconds_count")
                acc["decode_time_sum"] += _histogram_row_sum(metrics, "vllm:request_decode_time_seconds_sum")
                acc["decode_time_count"] += _histogram_row_sum(metrics, "vllm:request_decode_time_seconds_count")
                # Heterogeneous decode fleets: keep the minimum reported
                # capacity so the KV constraint stays conservative.
                kv_tokens = _parse_cache_config_info(metrics)
                if kv_tokens > 0.0:
                    current = acc["kv_tokens_per_instance_decode"]
                    acc["kv_tokens_per_instance_decode"] = kv_tokens if current <= 0.0 else min(current, kv_tokens)
        acc["instance_tps"].append(instance_tps)

    snapshots: dict[str, RoleSnapshot | None] = {"prefill": None, "decode": None}
    for role, acc in accumulators.items():
        snapshots[role] = RoleSnapshot(
            active_instances=acc["instances"],
            instance_tps=acc["instance_tps"],
            request_success_total=acc["request_success_total"],
            tokens_total=acc["tokens_total"],
            prefill_time_sum=acc["prefill_time_sum"],
            queue_time_sum=acc["queue_time_sum"],
            queue_time_count=acc["queue_time_count"],
            decode_time_sum=acc["decode_time_sum"],
            decode_time_count=acc["decode_time_count"],
            kv_tokens_per_instance_decode=acc["kv_tokens_per_instance_decode"],
        )
    return PlannerSnapshot(dt=dt, prefill=snapshots["prefill"], decode=snapshots["decode"])


def _parse_cache_config_info(metrics: list[Metric]) -> float:
    """First successful num_gpu_blocks x block_size parse, or 0.0."""
    cache_info = _find_metric(metrics, "vllm:cache_config_info")
    if cache_info is None:
        return 0.0
    for label in cache_info.label:
        kv_tokens = _parse_kv_tokens_per_instance(label)
        if kv_tokens > 0.0:
            return kv_tokens
    return 0.0


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


def _get_rate_defs_by_source() -> dict[str, ComputedMetricDef]:
    """Map source counter name → the counter_rate definition that reads it."""
    return {
        src: defn
        for defn in _MOTOR_COMPUTED_METRICS
        if defn.compute_type == "counter_rate"
        for src in defn.source_counters
    }


def _label_base(label: str) -> str:
    """Label row name without the ``{...}`` attribute part."""
    return label.split("{", 1)[0]


def _histogram_base_name(row_name: str) -> str | None:
    """Base histogram name for a ``_sum`` / ``_count`` row name, else None."""
    for suffix in ("_sum", "_count"):
        if row_name.endswith(suffix):
            return row_name[: -len(suffix)]
    return None


def _histogram_row_sum(metrics: list[Metric], row_name: str) -> float:
    """Sum of the label rows named *row_name* (e.g. ``..._sum``) across the
    base histogram metric, or 0.0 when the metric/rows are absent.
    """
    base_name = _histogram_base_name(row_name)
    if base_name is None:
        return 0.0
    histogram = _find_metric(metrics, base_name)
    if histogram is None:
        return 0.0
    return float(sum(value for label, value in zip(histogram.label, histogram.value) if _label_base(label) == row_name))


# ---------------------------------------------------------------------------
# Inherited counter names (for inactive-aggregate coordination)
# ---------------------------------------------------------------------------


def get_inherited_metric_names() -> set[str]:
    """Return counter names whose values are inherited across restarts.

    These raw vLLM counters are corrected in-place by
    ``MotorMetricComputer._correct_cumulative_counters``, so the inactive
    aggregate must NOT preserve their old values (the new instance already
    carries forward the inherited total via baseline offset).
    """
    return set(_CUMULATIVE_COUNTERS)
