# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Cross-process observability metrics for Coordinator scheduling and KV cache attribution.

Inference workers record selection, resource allocation, scheduling degradation,
and KV cache miss attribution counters/histograms into per-worker fixed-layout
shared memory (seqlock protected). The Obs process reads and aggregates every
worker's snapshot into ``motor:*`` Prometheus metrics exposed on ``/metrics``.
"""

from __future__ import annotations

import contextlib
import struct
import sys
import threading
from typing import Any

from motor.common.logger import get_logger
from motor.coordinator.metrics.metric_types import Metric, MetricType

logger = get_logger(__name__)

DEFAULT_SCHED_OBS_SHM_NAME = "motor_sched_obs_metrics"

_MAGIC = 0x534F4253  # "SOBS"
_SCHEMA_VERSION = 1
_HEADER_FMT = "<I H H q I 12x"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)

ROLES = ("prefill", "decode", "hybrid", "encode")
SELECT_POLICIES = ("kv_cache_affinity", "load_balance", "round_robin", "dynamic_bucket", "plugin", "pinned", "other")
SELECT_RESULTS = ("ok", "empty", "error")
ALLOCATE_RESULTS = ("ok", "retry", "failed")
DEGRADATION_REASONS = (
    "kv_conductor_unavailable",
    "kv_hit_rate_below_threshold",
    "load_balance_to_round_robin",
    "plugin_fallback",
    "session_affinity_fallback",
    "pd_to_hybrid",
    "pd_to_decode_colocation",
)
KV_ATTRIBUTION_REASONS = (
    "lb_abandoned",
    "index_stale",
    "prediction_mismatch",
    "cache_load_failed",
    "actual_recompute",
    "unknown",
    "hit",
)
LATENCY_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
_HIST_SLOTS = len(LATENCY_BUCKETS) + 3  # buckets + +Inf + count + sum_micros

_SELECT_COUNT = len(ROLES) * len(SELECT_POLICIES) * len(SELECT_RESULTS)
_ALLOC_COUNT = len(ROLES) * len(ALLOCATE_RESULTS)
_DEGRADE_COUNT = len(ROLES) * len(DEGRADATION_REASONS)
_KV_ATTR_COUNT = len(KV_ATTRIBUTION_REASONS)
_KV_TOKEN_COUNT = 3  # predicted_cached, actual_cached, actual_recompute
_HIST_TOTAL = 3 * len(ROLES) * _HIST_SLOTS  # select, allocate, degradation

_OFF_SELECT = 0
_OFF_ALLOC = _OFF_SELECT + _SELECT_COUNT
_OFF_DEGRADE = _OFF_ALLOC + _ALLOC_COUNT
_OFF_KV_ATTR = _OFF_DEGRADE + _DEGRADE_COUNT
_OFF_KV_TOKENS = _OFF_KV_ATTR + _KV_ATTR_COUNT
_OFF_CONDUCTOR_TIMEOUT = _OFF_KV_TOKENS + _KV_TOKEN_COUNT
_OFF_SELECT_HIST = _OFF_CONDUCTOR_TIMEOUT + 1
_OFF_ALLOC_HIST = _OFF_SELECT_HIST + len(ROLES) * _HIST_SLOTS
_OFF_DEGRADE_HIST = _OFF_ALLOC_HIST + len(ROLES) * _HIST_SLOTS
_TOTAL_SLOTS = _OFF_DEGRADE_HIST + len(ROLES) * _HIST_SLOTS

_PAYLOAD_FMT = f"<{_TOTAL_SLOTS}q"
_PAYLOAD_SIZE = struct.calcsize(_PAYLOAD_FMT)
TOTAL_SHM_SIZE = _HEADER_SIZE + _PAYLOAD_SIZE

_ROLE_INDEX = {name: idx for idx, name in enumerate(ROLES)}
_POLICY_INDEX = {name: idx for idx, name in enumerate(SELECT_POLICIES)}
_SELECT_RESULT_INDEX = {name: idx for idx, name in enumerate(SELECT_RESULTS)}
_ALLOC_RESULT_INDEX = {name: idx for idx, name in enumerate(ALLOCATE_RESULTS)}
_DEGRADE_REASON_INDEX = {name: idx for idx, name in enumerate(DEGRADATION_REASONS)}
_KV_ATTR_INDEX = {name: idx for idx, name in enumerate(KV_ATTRIBUTION_REASONS)}

_CACHE_LOAD_FAIL_KEYWORDS = (
    "kv_load_failed",
    "cache_load_failed",
    "kv cache load",
    "kv_transfer",
    "kv transfer",
    "failed to load kv",
)


def normalize_role(role: Any) -> str:
    raw = str(getattr(role, "value", role) or "").strip().lower()
    if raw == "prefill":
        return "prefill"
    if raw == "decode":
        return "decode"
    if raw == "encode":
        return "encode"
    return "hybrid"


def normalize_policy(policy: str | None) -> str:
    raw = str(policy or "").strip().lower()
    if raw.startswith("plugin"):
        return "plugin"
    if raw in _POLICY_INDEX:
        return raw
    return "other"


def _untrack_shared_memory(shm) -> None:
    if sys.platform == "win32":
        return
    from multiprocessing import resource_tracker

    with contextlib.suppress(Exception):
        registered_name = getattr(shm, "_name", None) or shm.name
        resource_tracker.unregister(registered_name, "shared_memory")


class SchedulingObsMetricsShmWriter:
    """Per-worker writer publishing scheduling & KV attribution slots via seqlock SHM."""

    def __init__(self, shm_name: str) -> None:
        from multiprocessing import shared_memory

        self._shm_name = shm_name
        self._lock = threading.Lock()
        self._sequence = 0
        try:
            self._shm = shared_memory.SharedMemory(name=shm_name, create=True, size=TOTAL_SHM_SIZE)
        except FileExistsError:
            stale = shared_memory.SharedMemory(name=shm_name, create=False)
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()
            _untrack_shared_memory(stale)
            stale.close()
            self._shm = shared_memory.SharedMemory(name=shm_name, create=True, size=TOTAL_SHM_SIZE)
        self._buf = memoryview(self._shm.buf)

    @property
    def shm_name(self) -> str:
        return self._shm_name

    def write(self, slots: list[int]) -> None:
        with self._lock:
            self._sequence += 1
            struct.pack_into(_HEADER_FMT, self._buf, 0, _MAGIC, _SCHEMA_VERSION, 0, self._sequence, _TOTAL_SLOTS)
            struct.pack_into(_PAYLOAD_FMT, self._buf, _HEADER_SIZE, *slots)
            self._sequence += 1
            struct.pack_into(_HEADER_FMT, self._buf, 0, _MAGIC, _SCHEMA_VERSION, 0, self._sequence, _TOTAL_SLOTS)

    def close(self) -> None:
        with self._lock:
            self._buf.release()
            with contextlib.suppress(FileNotFoundError):
                self._shm.unlink()
            _untrack_shared_memory(self._shm)
            self._shm.close()


class SchedulingObsMetricsShmReader:
    """Obs-process reader attaching lazily to one worker's scheduling obs SHM."""

    def __init__(self, shm_name: str) -> None:
        self._shm_name = shm_name
        self._shm = None
        self._buf: memoryview | None = None

    @property
    def shm_name(self) -> str:
        return self._shm_name

    def attach(self) -> bool:
        from multiprocessing import shared_memory

        try:
            self._shm = shared_memory.SharedMemory(name=self._shm_name, create=False)
        except FileNotFoundError:
            return False
        _untrack_shared_memory(self._shm)
        self._buf = memoryview(self._shm.buf)
        return True

    def read(self) -> tuple[int, ...] | None:
        if self._buf is None:
            return None
        try:
            for _ in range(4):
                magic, version, _pad, seq1, count = struct.unpack_from(_HEADER_FMT, self._buf, 0)
                if magic != _MAGIC or version != _SCHEMA_VERSION or seq1 % 2 == 1:
                    continue
                if count != _TOTAL_SLOTS:
                    return None
                slots = struct.unpack_from(_PAYLOAD_FMT, self._buf, _HEADER_SIZE)
                magic2, version2, _pad2, seq2, count2 = struct.unpack_from(_HEADER_FMT, self._buf, 0)
                if (magic2, version2, seq2, count2) == (magic, version, seq1, count):
                    return slots
        except (struct.error, ValueError):
            return None
        return None

    def detach(self) -> None:
        if self._buf is not None:
            self._buf.release()
            self._buf = None
        if self._shm is not None:
            self._shm.close()
            self._shm = None


class SchedulingObsMetrics:
    """Thread-safe collector for scheduling & KV miss attribution metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: list[int] = [0] * _TOTAL_SLOTS
        self._writer: SchedulingObsMetricsShmWriter | None = None

    def attach_shm_writer(self, shm_name: str) -> None:
        try:
            writer = SchedulingObsMetricsShmWriter(shm_name)
        except Exception as e:  # noqa: BLE001 - metrics must never block worker startup
            logger.warning("scheduling obs metrics shm unavailable (%s): %s", shm_name, e)
            return
        with self._lock:
            old = self._writer
            self._writer = writer
            writer.write(self._slots)
        if old is not None:
            with contextlib.suppress(Exception):
                old.close()

    def detach_shm_writer(self) -> None:
        with self._lock:
            writer = self._writer
            self._writer = None
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()

    def _flush_locked(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.write(self._slots)

    def _observe_hist_locked(self, base_offset: int, role_idx: int, duration_s: float) -> None:
        val = duration_s if duration_s >= 0.0 else 0.0
        start = base_offset + role_idx * _HIST_SLOTS
        for i, bound in enumerate(LATENCY_BUCKETS):
            if val <= bound:
                self._slots[start + i] += 1
        self._slots[start + len(LATENCY_BUCKETS)] += 1  # +Inf
        self._slots[start + len(LATENCY_BUCKETS) + 1] += 1  # count
        self._slots[start + len(LATENCY_BUCKETS) + 2] += int(round(val * 1_000_000.0))

    def record_select(self, role: Any, policy: str | None, result: str, duration_s: float) -> None:
        r_idx = _ROLE_INDEX[normalize_role(role)]
        p_idx = _POLICY_INDEX[normalize_policy(policy)]
        res_idx = _SELECT_RESULT_INDEX.get(result, _SELECT_RESULT_INDEX["error"])
        slot = _OFF_SELECT + (r_idx * len(SELECT_POLICIES) + p_idx) * len(SELECT_RESULTS) + res_idx
        with self._lock:
            self._slots[slot] += 1
            self._observe_hist_locked(_OFF_SELECT_HIST, r_idx, duration_s)
            self._flush_locked()

    def record_allocate(self, role: Any, result: str, duration_s: float | None = None) -> None:
        r_idx = _ROLE_INDEX[normalize_role(role)]
        res_idx = _ALLOC_RESULT_INDEX.get(result, _ALLOC_RESULT_INDEX["failed"])
        slot = _OFF_ALLOC + r_idx * len(ALLOCATE_RESULTS) + res_idx
        with self._lock:
            self._slots[slot] += 1
            if duration_s is not None:
                self._observe_hist_locked(_OFF_ALLOC_HIST, r_idx, duration_s)
            self._flush_locked()

    def record_degradation(self, role: Any, reason: str, duration_s: float | None = None) -> None:
        reason_idx = _DEGRADE_REASON_INDEX.get(reason)
        if reason_idx is None:
            return
        r_idx = _ROLE_INDEX[normalize_role(role)]
        slot = _OFF_DEGRADE + r_idx * len(DEGRADATION_REASONS) + reason_idx
        with self._lock:
            self._slots[slot] += 1
            if duration_s is not None:
                self._observe_hist_locked(_OFF_DEGRADE_HIST, r_idx, duration_s)
            self._flush_locked()

    def record_kv_attribution(
        self,
        reason: str,
        *,
        predicted_cached_tokens: int | None = None,
        actual_cached_tokens: int | None = None,
        actual_recompute_tokens: int | None = None,
    ) -> None:
        idx = _KV_ATTR_INDEX.get(reason, _KV_ATTR_INDEX["unknown"])
        with self._lock:
            self._slots[_OFF_KV_ATTR + idx] += 1
            if predicted_cached_tokens is not None and predicted_cached_tokens > 0:
                self._slots[_OFF_KV_TOKENS] += int(predicted_cached_tokens)
            if actual_cached_tokens is not None and actual_cached_tokens > 0:
                self._slots[_OFF_KV_TOKENS + 1] += int(actual_cached_tokens)
            if actual_recompute_tokens is not None and actual_recompute_tokens > 0:
                self._slots[_OFF_KV_TOKENS + 2] += int(actual_recompute_tokens)
            self._flush_locked()

    def record_conductor_query_timeout(self) -> None:
        with self._lock:
            self._slots[_OFF_CONDUCTOR_TIMEOUT] += 1
            self._flush_locked()

    def snapshot(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._slots)

    def reset(self) -> None:
        with self._lock:
            self._slots = [0] * _TOTAL_SLOTS
            self._flush_locked()


_GLOBAL_SCHED_OBS_METRICS = SchedulingObsMetrics()


def get_scheduling_obs_metrics() -> SchedulingObsMetrics:
    return _GLOBAL_SCHED_OBS_METRICS


def mark_kv_cache_load_failed(req_info: Any, body_or_error: Any) -> bool:
    """Mark the request's KV obs context when an engine error/body signals cache load failure."""
    if req_info is None or not is_cache_load_failure_signal(body_or_error):
        return False
    ctx = getattr(req_info, "_kv_obs_context", None)
    if isinstance(ctx, dict):
        ctx["cache_load_failed"] = True
        return True
    return False


def record_selection_kv_context(
    req_info: Any, *, selected_instance_id: int, selected_endpoint_id: int, candidate_policy: str
) -> None:
    """Capture KV affinity prediction vs chosen endpoint to detect LB-induced abandonment."""
    if req_info is None:
        return
    affinity_debug = getattr(req_info, "kv_affinity_debug", None)
    token_ids = getattr(req_info, "token_ids", None)
    prompt_tokens = len(token_ids) if isinstance(token_ids, list) else 0
    if not isinstance(affinity_debug, dict) or not affinity_debug:
        # Only track KV attribution when kv_cache_affinity participated or prompt was queried.
        return

    best_matched = 0
    for rec in affinity_debug.values():
        if isinstance(rec, (tuple, list)) and rec:
            matched = int(rec[0] or 0)
            best_matched = max(best_matched, matched)

    selected_rec = affinity_debug.get((selected_instance_id, selected_endpoint_id))
    selected_known = isinstance(selected_rec, (tuple, list)) and bool(selected_rec)
    selected_matched = int(selected_rec[0] or 0) if selected_known else 0
    selected_tiers = selected_rec[3] if selected_known and len(selected_rec) > 3 else None
    # Only an endpoint that was in the affinity candidate set can be an LB abandonment.
    # Pinned or plugin endpoints absent from kv_affinity_debug are not treated as a 0-token hit.
    lb_abandoned = selected_known and best_matched > 0 and selected_matched < best_matched

    try:
        req_info._kv_obs_context = {
            "candidate_policy": candidate_policy,
            "prompt_tokens": prompt_tokens,
            "best_matched": best_matched,
            "selected_matched": selected_matched,
            "selected_tiers": selected_tiers,
            "lb_abandoned": lb_abandoned,
            "cache_load_failed": False,
        }
        req_info._kv_obs_recorded = False
    except Exception:  # pragma: no cover - defensive for stub req_info
        return


def is_cache_load_failure_signal(body_or_error: Any) -> bool:
    """Check whether an engine response or error explicitly signals a KV cache load failure."""
    if isinstance(body_or_error, dict):
        if body_or_error.get("kv_load_failed") or body_or_error.get("cache_load_failed"):
            return True
        usage = body_or_error.get("usage")
        if isinstance(usage, dict):
            details = usage.get("prompt_tokens_details")
            if isinstance(details, dict) and (details.get("kv_load_failed") or details.get("cache_load_failed")):
                return True
        return False
    text = str(body_or_error or "").lower()
    return any(keyword in text for keyword in _CACHE_LOAD_FAIL_KEYWORDS)


def classify_and_record_kv_attribution(
    req_info: Any,
    *,
    response_body: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    cache_load_failed: bool = False,
) -> str | None:
    """Classify KV cache miss attribution once per request and record to metrics.

    Categories:
      - ``lb_abandoned``: load balancing abandoned a candidate with higher predicted hit
      - ``cache_load_failed``: engine reported KV cache load/transfer failure
      - ``unknown``: actual cached token count unavailable from engine (never estimated)
      - ``index_stale``: predicted hit > 0, actual cached == 0
      - ``prediction_mismatch``: predicted hit > 0, 0 < actual cached < predicted
      - ``actual_recompute``: predicted hit == 0 and prompt tokens were recomputed
      - ``hit``: predicted hit > 0 and actual cached >= predicted
    """
    if req_info is None:
        return None
    ctx = getattr(req_info, "_kv_obs_context", None)
    if not isinstance(ctx, dict):
        return None
    if getattr(req_info, "_kv_obs_recorded", False):
        return None

    predicted = int(ctx.get("selected_matched", 0) or 0)
    prompt_tokens = int(ctx.get("prompt_tokens", 0) or 0)
    lb_abandoned = bool(ctx.get("lb_abandoned", False))

    actual_cached: int | None = None
    actual_prompt: int | None = None
    load_failed = bool(cache_load_failed or ctx.get("cache_load_failed", False))

    effective_usage: dict[str, Any] | None = usage if isinstance(usage, dict) else None
    if isinstance(response_body, dict):
        if is_cache_load_failure_signal(response_body):
            load_failed = True
        if effective_usage is None:
            body_usage = response_body.get("usage")
            if isinstance(body_usage, dict):
                effective_usage = body_usage
            elif isinstance(response_body.get("payload"), dict):
                payload_usage = response_body["payload"].get("usage")
                if isinstance(payload_usage, dict):
                    effective_usage = payload_usage

    if isinstance(effective_usage, dict):
        raw_prompt = effective_usage.get("prompt_tokens")
        if isinstance(raw_prompt, int) and not isinstance(raw_prompt, bool) and raw_prompt >= 0:
            actual_prompt = raw_prompt
        details = effective_usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            if details.get("kv_load_failed") or details.get("cache_load_failed"):
                load_failed = True
            if "cached_tokens" in details:
                raw_cached = details.get("cached_tokens")
                if isinstance(raw_cached, int) and not isinstance(raw_cached, bool) and raw_cached >= 0:
                    actual_cached = raw_cached

    if actual_cached is None:
        details = getattr(req_info, "prompt_tokens_details", None)
        if isinstance(details, dict) and "cached_tokens" in details:
            raw_cached = details.get("cached_tokens")
            if isinstance(raw_cached, int) and not isinstance(raw_cached, bool) and raw_cached >= 0:
                actual_cached = raw_cached

    effective_prompt = actual_prompt if actual_prompt is not None else prompt_tokens
    recompute_tokens: int | None = None
    if actual_cached is not None and effective_prompt > 0:
        recompute_tokens = max(0, effective_prompt - actual_cached)

    if lb_abandoned:
        reason = "lb_abandoned"
    elif load_failed:
        reason = "cache_load_failed"
    elif actual_cached is None:
        reason = "unknown"
    elif predicted > 0 and actual_cached == 0:
        reason = "index_stale"
    elif predicted > 0 and actual_cached < predicted:
        reason = "prediction_mismatch"
    elif predicted == 0 and (recompute_tokens is None or recompute_tokens > 0):
        reason = "actual_recompute"
    else:
        reason = "hit"

    with contextlib.suppress(Exception):
        req_info._kv_obs_recorded = True

    get_scheduling_obs_metrics().record_kv_attribution(
        reason,
        predicted_cached_tokens=predicted if predicted > 0 else None,
        actual_cached_tokens=actual_cached,
        actual_recompute_tokens=recompute_tokens,
    )
    return reason


def _build_histogram_metric(name: str, help_text: str, totals: list[int], base_offset: int) -> Metric:
    labels: list[str] = []
    values: list[float] = []
    for r_idx, role in enumerate(ROLES):
        start = base_offset + r_idx * _HIST_SLOTS
        for i, bound in enumerate(LATENCY_BUCKETS):
            labels.append(f'{name}_bucket{{role="{role}",le="{bound}"}}')
            values.append(float(totals[start + i]))
        labels.append(f'{name}_bucket{{role="{role}",le="+Inf"}}')
        values.append(float(totals[start + len(LATENCY_BUCKETS)]))
        labels.append(f'{name}_sum{{role="{role}"}}')
        values.append(float(totals[start + len(LATENCY_BUCKETS) + 2]) / 1_000_000.0)
        labels.append(f'{name}_count{{role="{role}"}}')
        values.append(float(totals[start + len(LATENCY_BUCKETS) + 1]))
    return Metric(name=name, help=help_text, type=MetricType.HISTOGRAM, label=labels, value=values)


def merge_scheduling_obs_snapshots(snapshots: list[tuple[int, ...] | list[int]]) -> list[int]:
    """Sum one or more per-worker slot arrays into a single aggregate slot array."""
    totals = [0] * _TOTAL_SLOTS
    for snap in snapshots:
        if len(snap) != _TOTAL_SLOTS:
            continue
        for i, val in enumerate(snap):
            totals[i] += int(val)
    return totals


def scheduling_obs_slots_to_motor_metrics(totals: list[int] | tuple[int, ...]) -> list[Metric]:
    """Convert an aggregated slot array into ``motor:*`` Metric objects."""
    if len(totals) != _TOTAL_SLOTS:
        return []
    totals = list(totals)

    metrics: list[Metric] = []

    # 1. Selection counter
    sel_labels: list[str] = []
    sel_values: list[float] = []
    for r_idx, role in enumerate(ROLES):
        for p_idx, policy in enumerate(SELECT_POLICIES):
            for res_idx, res in enumerate(SELECT_RESULTS):
                slot = _OFF_SELECT + (r_idx * len(SELECT_POLICIES) + p_idx) * len(SELECT_RESULTS) + res_idx
                v = totals[slot]
                if v > 0 or (policy in ("kv_cache_affinity", "load_balance") and res == "ok"):
                    sel_labels.append(f'motor:scheduler_select_total{{role="{role}",policy="{policy}",result="{res}"}}')
                    sel_values.append(float(v))
    metrics.append(
        Metric(
            name="motor:scheduler_select_total",
            help="Total Coordinator endpoint selection calls by role, policy, and result.",
            type=MetricType.COUNTER,
            label=sel_labels,
            value=sel_values,
        )
    )
    metrics.append(
        _build_histogram_metric(
            "motor:scheduler_select_duration_seconds",
            "Coordinator endpoint selection latency in seconds.",
            totals,
            _OFF_SELECT_HIST,
        )
    )

    # 2. Allocation counter & histogram
    alloc_labels: list[str] = []
    alloc_values: list[float] = []
    for r_idx, role in enumerate(ROLES):
        for res_idx, res in enumerate(ALLOCATE_RESULTS):
            slot = _OFF_ALLOC + r_idx * len(ALLOCATE_RESULTS) + res_idx
            alloc_labels.append(f'motor:scheduler_allocate_total{{role="{role}",result="{res}"}}')
            alloc_values.append(float(totals[slot]))
    metrics.append(
        Metric(
            name="motor:scheduler_allocate_total",
            help="Total Coordinator workload resource allocation attempts by role and result.",
            type=MetricType.COUNTER,
            label=alloc_labels,
            value=alloc_values,
        )
    )
    metrics.append(
        _build_histogram_metric(
            "motor:scheduler_allocate_duration_seconds",
            "Coordinator workload resource allocation latency in seconds.",
            totals,
            _OFF_ALLOC_HIST,
        )
    )

    # 3. Degradation counter & histogram
    deg_labels: list[str] = []
    deg_values: list[float] = []
    for r_idx, role in enumerate(ROLES):
        for reason_idx, reason in enumerate(DEGRADATION_REASONS):
            slot = _OFF_DEGRADE + r_idx * len(DEGRADATION_REASONS) + reason_idx
            deg_labels.append(f'motor:scheduler_degradation_total{{role="{role}",reason="{reason}"}}')
            deg_values.append(float(totals[slot]))
    metrics.append(
        Metric(
            name="motor:scheduler_degradation_total",
            help="Total Coordinator scheduling degradations by role and reason.",
            type=MetricType.COUNTER,
            label=deg_labels,
            value=deg_values,
        )
    )
    metrics.append(
        _build_histogram_metric(
            "motor:scheduler_degradation_duration_seconds",
            "Latency of degraded Coordinator scheduling decisions in seconds.",
            totals,
            _OFF_DEGRADE_HIST,
        )
    )

    # 4. KV cache miss attribution & predicted/actual token totals
    attr_labels = [f'motor:kv_cache_miss_attribution_total{{reason="{reason}"}}' for reason in KV_ATTRIBUTION_REASONS]
    attr_values = [float(totals[_OFF_KV_ATTR + idx]) for idx in range(_KV_ATTR_COUNT)]
    metrics.append(
        Metric(
            name="motor:kv_cache_miss_attribution_total",
            help="KV cache hit/miss attribution by reason (lb_abandoned, index_stale, prediction_mismatch, cache_load_failed, actual_recompute, unknown, hit).",
            type=MetricType.COUNTER,
            label=attr_labels,
            value=attr_values,
        )
    )
    metrics.append(
        Metric(
            name="motor:kv_cache_predicted_cached_tokens_total",
            help="Cumulative prompt tokens predicted as cached on the selected endpoint.",
            type=MetricType.COUNTER,
            label=["motor:kv_cache_predicted_cached_tokens_total"],
            value=[float(totals[_OFF_KV_TOKENS])],
        )
    )
    metrics.append(
        Metric(
            name="motor:kv_cache_actual_cached_tokens_total",
            help="Cumulative actual cached prompt tokens reported by the engine.",
            type=MetricType.COUNTER,
            label=["motor:kv_cache_actual_cached_tokens_total"],
            value=[float(totals[_OFF_KV_TOKENS + 1])],
        )
    )
    metrics.append(
        Metric(
            name="motor:kv_cache_actual_recompute_tokens_total",
            help="Cumulative actual recomputed prompt tokens reported by the engine.",
            type=MetricType.COUNTER,
            label=["motor:kv_cache_actual_recompute_tokens_total"],
            value=[float(totals[_OFF_KV_TOKENS + 2])],
        )
    )
    metrics.append(
        Metric(
            name="motor:kv_conductor_query_timeouts_total",
            help="Coordinator queries to KV Conductor that exceeded the client timeout.",
            type=MetricType.COUNTER,
            label=["motor:kv_conductor_query_timeouts_total"],
            value=[float(totals[_OFF_CONDUCTOR_TIMEOUT])],
        )
    )

    return metrics
