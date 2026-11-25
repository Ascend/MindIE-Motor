# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Coordinator-native external request lifecycle observability metrics.

Tracks external inference requests independently of inference engine metrics:
- Traffic flow (received / admitted / rejected / completed + derived rates)
- In-flight / queued / executing gauges with strict reconciliation invariant:
    received == completed + in_flight
    completed == sum(terminal_outcomes)
- Terminal outcomes (success / failed / timeout / cancelled / client_disconnect)
- Internal attempts, retries, retry recoveries, and retry duration
- Request resource release count, failures, timeouts, retries, and duration

Per-worker counts are published through a tiny seqlock shared-memory segment
so the Obs process can aggregate across all inference workers on ``/metrics``.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import struct
import sys
import threading
import time
from typing import Any

from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

import motor.common.utils.error as cancel_error
from motor.common.logger import get_logger
from motor.coordinator.metrics.metric_types import Metric, MetricType

logger = get_logger(__name__)

DEFAULT_REQ_LIFECYCLE_SHM_NAME = "motor_req_lifecycle_metrics"

INFERENCE_TRACKED_PATHS = frozenset(
    {
        "/v1/completions",
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/messages",
    }
)

_MAGIC = 0x524C434D  # "RLCM"
_SCHEMA_VERSION = 1
_HEADER_FMT = "<I H H q I 12x"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
_SEQLOCK_READ_ATTEMPTS = 4

REJECT_REASONS = (
    "rate_limited",
    "body_too_large",
    "unauthorized",
    "invalid_request",
    "unavailable",
    "other",
)
TERMINAL_OUTCOMES = (
    "success",
    "failed",
    "timeout",
    "cancelled",
    "client_disconnect",
)
ROUTERS = ("unified_pd", "pd_hybrid")
RETRY_REASONS = ("timeout", "cancelled", "upstream_error", "hybrid_fallback", "other")
RELEASE_ROLES = ("prefill", "decode", "hybrid", "encode", "other")
RELEASE_RESULTS = ("success", "failed", "timeout")

LATENCY_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
_HIST_SLOTS = len(LATENCY_BUCKETS) + 3  # finite buckets + inf + count + sum_micros

# Scalar traffic & gauge slots
_IDX_RECEIVED = 0
_IDX_ADMITTED = 1
_IDX_COMPLETED = 2
_IDX_IN_FLIGHT = 3
_IDX_QUEUED = 4
_IDX_EXECUTING = 5
_NUM_SCALARS = 6

_OFF_REJECTED = _NUM_SCALARS
_OFF_TERMINAL = _OFF_REJECTED + len(REJECT_REASONS)
_OFF_ATTEMPTS = _OFF_TERMINAL + len(TERMINAL_OUTCOMES)
_OFF_RETRIES = _OFF_ATTEMPTS + len(ROUTERS)
_OFF_RETRY_RECOVERED = _OFF_RETRIES + len(ROUTERS) * len(RETRY_REASONS)
_OFF_RELEASE_TOTAL = _OFF_RETRY_RECOVERED + len(ROUTERS)
_OFF_RELEASE_RETRIES = _OFF_RELEASE_TOTAL + len(RELEASE_ROLES) * len(RELEASE_RESULTS)

# Histograms:
#   0: queue_duration
#   1: request_duration
#   2..2+len(ROUTERS)-1: retry_duration per router
#   next len(RELEASE_ROLES): release_duration per role
_OFF_HISTS = _OFF_RELEASE_RETRIES + len(RELEASE_ROLES)
_HIST_IDX_QUEUE = 0
_HIST_IDX_REQUEST = 1
_HIST_IDX_RETRY_BASE = 2
_HIST_IDX_RELEASE_BASE = _HIST_IDX_RETRY_BASE + len(ROUTERS)
_NUM_HISTS = _HIST_IDX_RELEASE_BASE + len(RELEASE_ROLES)

_TOTAL_SLOTS = _OFF_HISTS + _NUM_HISTS * _HIST_SLOTS
_PAYLOAD_FMT = f"<{_TOTAL_SLOTS}q"
_PAYLOAD_SIZE = struct.calcsize(_PAYLOAD_FMT)
TOTAL_SHM_SIZE = _HEADER_SIZE + _PAYLOAD_SIZE

_REJECT_INDEX = {name: i for i, name in enumerate(REJECT_REASONS)}
_OUTCOME_INDEX = {name: i for i, name in enumerate(TERMINAL_OUTCOMES)}
_ROUTER_INDEX = {name: i for i, name in enumerate(ROUTERS)}
_RETRY_REASON_INDEX = {name: i for i, name in enumerate(RETRY_REASONS)}
_RELEASE_ROLE_INDEX = {name: i for i, name in enumerate(RELEASE_ROLES)}
_RELEASE_RESULT_INDEX = {name: i for i, name in enumerate(RELEASE_RESULTS)}

_ROLE_NORMALIZE = {
    "prefill": "prefill",
    "p": "prefill",
    "decode": "decode",
    "d": "decode",
    "hybrid": "hybrid",
    "union": "hybrid",
    "both": "hybrid",
    "u": "hybrid",
    "encode": "encode",
    "e": "encode",
}


def _norm_release_role(role: Any) -> str:
    raw = str(getattr(role, "value", role) or "").strip().lower()
    return _ROLE_NORMALIZE.get(raw, "other")


def _norm_router(router: str | None) -> str:
    raw = str(router or "").strip().lower()
    return raw if raw in _ROUTER_INDEX else "unified_pd"


def _untrack_shared_memory(shm) -> None:
    if sys.platform == "win32":
        return
    from multiprocessing import resource_tracker

    with contextlib.suppress(Exception):
        registered_name = getattr(shm, "_name", None) or shm.name
        resource_tracker.unregister(registered_name, "shared_memory")


class RequestLifecycleMetricsShmWriter:
    """Inference-worker seqlock SHM writer for request lifecycle metrics."""

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
        self._buf: memoryview | None = memoryview(self._shm.buf)
        self.write([0] * _TOTAL_SLOTS)

    @property
    def shm_name(self) -> str:
        return self._shm_name

    def write(self, slots: list[int]) -> None:
        with self._lock:
            if self._buf is None:
                return
            self._sequence += 1
            struct.pack_into(_HEADER_FMT, self._buf, 0, _MAGIC, _SCHEMA_VERSION, 0, self._sequence, _TOTAL_SLOTS)
            struct.pack_into(_PAYLOAD_FMT, self._buf, _HEADER_SIZE, *slots)
            self._sequence += 1
            struct.pack_into(_HEADER_FMT, self._buf, 0, _MAGIC, _SCHEMA_VERSION, 0, self._sequence, _TOTAL_SLOTS)

    def close(self) -> None:
        with self._lock:
            if self._buf is not None:
                self._buf.release()
                self._buf = None
            if self._shm is not None:
                with contextlib.suppress(FileNotFoundError):
                    self._shm.unlink()
                _untrack_shared_memory(self._shm)
                with contextlib.suppress(Exception):
                    self._shm.close()
                self._shm = None


class RequestLifecycleMetricsShmReader:
    """Obs-process seqlock SHM reader for one worker's request lifecycle metrics."""

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
            shm = shared_memory.SharedMemory(name=self._shm_name, create=False)
        except FileNotFoundError:
            return False
        _untrack_shared_memory(shm)
        self._shm = shm
        self._buf = memoryview(shm.buf)
        return True

    def read(self) -> list[int] | None:
        if self._buf is None:
            return None
        try:
            for _ in range(_SEQLOCK_READ_ATTEMPTS):
                magic, version, _pad, seq1, count = struct.unpack_from(_HEADER_FMT, self._buf, 0)
                if magic != _MAGIC or version != _SCHEMA_VERSION or count != _TOTAL_SLOTS:
                    return None
                if seq1 & 1:
                    continue
                values = struct.unpack_from(_PAYLOAD_FMT, self._buf, _HEADER_SIZE)
                _, _, _, seq2, _ = struct.unpack_from(_HEADER_FMT, self._buf, 0)
                if seq1 == seq2:
                    return list(values)
        except (struct.error, ValueError):
            return None
        return None

    def detach(self) -> None:
        if self._buf is not None:
            self._buf.release()
            self._buf = None
        if self._shm is not None:
            with contextlib.suppress(Exception):
                self._shm.close()
            self._shm = None


def reject_reason_from_status(status_code: int | None) -> str:
    if status_code == 429:
        return "rate_limited"
    if status_code == 413:
        return "body_too_large"
    if status_code in (401, 403):
        return "unauthorized"
    if status_code in (400, 422):
        return "invalid_request"
    if status_code == 503:
        return "unavailable"
    return "other"


def outcome_from_cancel_reason(reason: Any) -> str:
    text = str(reason or "")
    if text == cancel_error.CLIENT_DISCONNECT:
        return "client_disconnect"
    if text == cancel_error.INFER_TIMEOUT:
        return "timeout"
    return "cancelled"


class RequestLifecycleTracker:
    """Single-request idempotent guard ensuring in-flight counts always reconcile.

    Every call to :meth:`RequestLifecycleCollector.start_request` increments
    ``received`` and ``in_flight``. Calling :meth:`finish` (always invoked from
    middleware ``finally``) atomically decrements ``in_flight`` (plus any active
    ``queued``/``executing`` state) and increments ``completed`` and exactly one
    terminal outcome.
    """

    __slots__ = (
        "_collector",
        "_start_ts",
        "_admit_ts",
        "_admitted",
        "_rejected",
        "_in_queue",
        "_in_exec",
        "_finished",
        "_outcome",
    )

    def __init__(self, collector: "RequestLifecycleCollector") -> None:
        self._collector = collector
        self._start_ts = time.perf_counter()
        self._admit_ts: float | None = None
        self._admitted = False
        self._rejected = False
        self._in_queue = False
        self._in_exec = False
        self._finished = False
        self._outcome: str | None = None

    def mark_admitted(self) -> None:
        self._collector._tracker_admit(self)

    def enter_queue(self) -> None:
        self._collector._tracker_enter_queue(self)

    def mark_rejected(self, reason: str) -> None:
        self._collector._tracker_reject(self, reason)

    def mark_executing(self) -> None:
        self._collector._tracker_execute(self)

    def enter_executing(self) -> None:
        self._collector._tracker_execute(self)

    def set_outcome(self, outcome: str, *, overwrite: bool = False) -> None:
        if outcome not in _OUTCOME_INDEX:
            return
        if self._outcome is None or overwrite:
            self._outcome = outcome

    def set_terminal_hint(self, outcome: str, *, overwrite: bool = False) -> None:
        self.set_outcome(outcome, overwrite=overwrite)

    def finish(self, *, status_code: int | None = None) -> str | None:
        return self._collector._tracker_finish(self, status_code=status_code)


class RequestLifecycleCollector:
    """Thread-safe collector for Coordinator-native request lifecycle metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: list[int] = [0] * _TOTAL_SLOTS
        self._writer: RequestLifecycleMetricsShmWriter | None = None

    def attach_shm_writer(self, shm_name: str) -> bool:
        with self._lock:
            self._close_writer_locked()
            try:
                self._writer = RequestLifecycleMetricsShmWriter(shm_name)
                self._writer.write(self._slots)
                return True
            except Exception as exc:  # noqa: BLE001 — metrics export must never block startup
                logger.warning("request lifecycle metrics shm unavailable (%s): %s", shm_name, exc)
                self._writer = None
                return False

    def detach_shm_writer(self) -> None:
        with self._lock:
            self._close_writer_locked()

    def _close_writer_locked(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
            self._writer = None

    def _flush_locked(self) -> None:
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.write(self._slots)

    def _observe_hist_locked(self, hist_idx: int, value_s: float) -> None:
        base = _OFF_HISTS + hist_idx * _HIST_SLOTS
        v = float(value_s) if (math.isfinite(value_s) and value_s >= 0.0) else 0.0
        for i, bound in enumerate(LATENCY_BUCKETS):
            if v <= bound:
                self._slots[base + i] += 1
        self._slots[base + len(LATENCY_BUCKETS)] += 1  # +Inf
        self._slots[base + len(LATENCY_BUCKETS) + 1] += 1  # count
        self._slots[base + len(LATENCY_BUCKETS) + 2] += int(round(v * 1_000_000))

    def start_request(self) -> RequestLifecycleTracker:
        tracker = RequestLifecycleTracker(self)
        with self._lock:
            self._slots[_IDX_RECEIVED] += 1
            self._slots[_IDX_IN_FLIGHT] += 1
            self._flush_locked()
        return tracker

    def _tracker_admit(self, tracker: RequestLifecycleTracker) -> None:
        with self._lock:
            if tracker._finished or tracker._admitted or tracker._rejected:
                return
            tracker._admitted = True
            tracker._admit_ts = time.perf_counter()
            self._slots[_IDX_ADMITTED] += 1
            self._flush_locked()

    def _tracker_enter_queue(self, tracker: RequestLifecycleTracker) -> None:
        with self._lock:
            if tracker._finished or tracker._in_queue or tracker._in_exec:
                return
            if tracker._admit_ts is None:
                tracker._admit_ts = time.perf_counter()
            tracker._in_queue = True
            self._slots[_IDX_QUEUED] += 1
            self._flush_locked()

    def _tracker_reject(self, tracker: RequestLifecycleTracker, reason: str) -> None:
        idx = _REJECT_INDEX.get(reason, _REJECT_INDEX["other"])
        with self._lock:
            if tracker._finished or tracker._admitted or tracker._rejected:
                return
            tracker._rejected = True
            self._slots[_OFF_REJECTED + idx] += 1
            self._flush_locked()

    def _tracker_execute(self, tracker: RequestLifecycleTracker) -> None:
        with self._lock:
            if tracker._finished or tracker._in_exec:
                return
            if tracker._in_queue:
                tracker._in_queue = False
                self._slots[_IDX_QUEUED] = max(0, self._slots[_IDX_QUEUED] - 1)
                start = tracker._admit_ts if tracker._admit_ts is not None else tracker._start_ts
                self._observe_hist_locked(_HIST_IDX_QUEUE, time.perf_counter() - start)
            tracker._in_exec = True
            self._slots[_IDX_EXECUTING] += 1
            self._flush_locked()

    def _tracker_finish(self, tracker: RequestLifecycleTracker, *, status_code: int | None = None) -> str | None:
        with self._lock:
            if tracker._finished:
                return None
            tracker._finished = True

            # Determine final terminal outcome
            outcome = tracker._outcome
            if outcome is None:
                if status_code == 499:
                    outcome = "client_disconnect"
                elif status_code == 504:
                    outcome = "timeout"
                elif status_code is not None and 200 <= status_code < 400:
                    outcome = "success"
                else:
                    outcome = "failed"

            # Unadmitted failed requests (e.g. 429/413/401/403/400/503 before handle_request)
            # are counted as rejected with reason inferred from status_code.
            if not tracker._admitted and not tracker._rejected and outcome == "failed":
                tracker._rejected = True
                reason = reject_reason_from_status(status_code)
                self._slots[_OFF_REJECTED + _REJECT_INDEX[reason]] += 1

            # Reclaim sub-phase gauges on every exit path
            if tracker._in_queue:
                tracker._in_queue = False
                self._slots[_IDX_QUEUED] = max(0, self._slots[_IDX_QUEUED] - 1)
            if tracker._in_exec:
                tracker._in_exec = False
                self._slots[_IDX_EXECUTING] = max(0, self._slots[_IDX_EXECUTING] - 1)

            # Reconciliation invariant: received == completed + in_flight
            self._slots[_IDX_IN_FLIGHT] = max(0, self._slots[_IDX_IN_FLIGHT] - 1)
            self._slots[_IDX_COMPLETED] += 1
            self._slots[_OFF_TERMINAL + _OUTCOME_INDEX[outcome]] += 1
            self._observe_hist_locked(_HIST_IDX_REQUEST, time.perf_counter() - tracker._start_ts)
            self._flush_locked()
            return outcome

    def record_attempt(self, router: str) -> None:
        r_idx = _ROUTER_INDEX[_norm_router(router)]
        with self._lock:
            self._slots[_OFF_ATTEMPTS + r_idx] += 1
            self._flush_locked()

    def record_retry(self, router: str, reason: str) -> None:
        r_idx = _ROUTER_INDEX[_norm_router(router)]
        reason_idx = _RETRY_REASON_INDEX.get(reason, _RETRY_REASON_INDEX["other"])
        slot = _OFF_RETRIES + r_idx * len(RETRY_REASONS) + reason_idx
        with self._lock:
            self._slots[slot] += 1
            self._flush_locked()

    def record_retry_outcome(self, router: str, *, recovered: bool, duration_s: float) -> None:
        r_idx = _ROUTER_INDEX[_norm_router(router)]
        with self._lock:
            if recovered:
                self._slots[_OFF_RETRY_RECOVERED + r_idx] += 1
            self._observe_hist_locked(_HIST_IDX_RETRY_BASE + r_idx, duration_s)
            self._flush_locked()

    def record_retry_recovered(self, router: str, duration_s: float) -> None:
        self.record_retry_outcome(router, recovered=True, duration_s=duration_s)

    def record_retry_duration(self, router: str, duration_s: float) -> None:
        self.record_retry_outcome(router, recovered=False, duration_s=duration_s)

    def record_resource_release(self, role: Any, result: str, duration_s: float, *, retries: int = 0) -> None:
        role_idx = _RELEASE_ROLE_INDEX[_norm_release_role(role)]
        res_idx = _RELEASE_RESULT_INDEX.get(result, _RELEASE_RESULT_INDEX["failed"])
        slot = _OFF_RELEASE_TOTAL + role_idx * len(RELEASE_RESULTS) + res_idx
        with self._lock:
            self._slots[slot] += 1
            if retries > 0:
                self._slots[_OFF_RELEASE_RETRIES + role_idx] += int(retries)
            self._observe_hist_locked(_HIST_IDX_RELEASE_BASE + role_idx, duration_s)
            self._flush_locked()

    def record_resource_release_retry(self, role: Any) -> None:
        role_idx = _RELEASE_ROLE_INDEX[_norm_release_role(role)]
        with self._lock:
            self._slots[_OFF_RELEASE_RETRIES + role_idx] += 1
            self._flush_locked()

    def snapshot(self) -> list[int]:
        with self._lock:
            return list(self._slots)

    def reset(self) -> None:
        with self._lock:
            self._slots = [0] * _TOTAL_SLOTS
            self._flush_locked()


_GLOBAL_REQ_LIFECYCLE_COLLECTOR = RequestLifecycleCollector()


def get_request_lifecycle_collector() -> RequestLifecycleCollector:
    return _GLOBAL_REQ_LIFECYCLE_COLLECTOR


def get_request_lifecycle_metrics() -> RequestLifecycleCollector:
    return _GLOBAL_REQ_LIFECYCLE_COLLECTOR


def get_tracker_from_scope(scope: Scope | None) -> RequestLifecycleTracker | None:
    if not isinstance(scope, dict):
        return None
    state = scope.get("state")
    if isinstance(state, dict):
        tracker = state.get("req_lifecycle")
        if isinstance(tracker, RequestLifecycleTracker):
            return tracker
    return None


def get_tracker_from_request(request: Any) -> RequestLifecycleTracker | None:
    if request is None:
        return None
    state = getattr(request, "state", None)
    tracker = getattr(state, "req_lifecycle", None)
    if isinstance(tracker, RequestLifecycleTracker):
        return tracker
    return get_tracker_from_scope(getattr(request, "scope", None))


class RequestLifecycleMiddleware:
    """Outermost ASGI middleware tracking every external inference request lifecycle."""

    def __init__(
        self,
        app: ASGIApp,
        collector: RequestLifecycleCollector | None = None,
    ) -> None:
        self.app = app
        self._collector = collector or get_request_lifecycle_collector()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = str(scope.get("method", "")).upper()
        if method != "POST" or path not in INFERENCE_TRACKED_PATHS:
            await self.app(scope, receive, send)
            return

        tracker = self._collector.start_request()
        state = scope.setdefault("state", {})
        state["req_lifecycle"] = tracker
        status_code: int | None = None

        async def wrapped_send(message: Message) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                raw_status = message.get("status")
                if isinstance(raw_status, int):
                    status_code = raw_status
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        except ClientDisconnect:
            tracker.set_outcome("client_disconnect")
            raise
        except TimeoutError:
            tracker.set_outcome("timeout")
            raise
        except asyncio.CancelledError as exc:
            reason = exc.args[0] if exc.args else None
            tracker.set_outcome(outcome_from_cancel_reason(reason))
            raise
        except BaseException:
            tracker.set_outcome("failed")
            raise
        finally:
            tracker.finish(status_code=status_code)


class RequestLifecycleRateTracker:
    """Compute per-second request flow rates across consecutive Obs scrapes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prev_ts: float | None = None
        self._prev_counts: tuple[int, int, int, int] | None = None
        self._rates: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def update(self, received: int, admitted: int, rejected: int, completed: int) -> tuple[float, float, float, float]:
        now = time.monotonic()
        current = (received, admitted, rejected, completed)
        with self._lock:
            if self._prev_ts is not None and self._prev_counts is not None:
                dt = now - self._prev_ts
                if dt >= 0.05:
                    self._rates = tuple(max(0.0, (cur - prev) / dt) for cur, prev in zip(current, self._prev_counts))
                    self._prev_ts = now
                    self._prev_counts = current
            else:
                self._prev_ts = now
                self._prev_counts = current
            return self._rates


def merge_request_lifecycle_snapshots(snapshots: list[list[int]]) -> list[int]:
    merged = [0] * _TOTAL_SLOTS
    for snap in snapshots:
        if len(snap) != _TOTAL_SLOTS:
            continue
        for i, val in enumerate(snap):
            merged[i] += val
    return merged


def _build_unlabelled_histogram(name: str, help_text: str, slots: list[int], hist_idx: int) -> Metric:
    base = _OFF_HISTS + hist_idx * _HIST_SLOTS
    labels: list[str] = []
    values: list[float] = []
    for i, bound in enumerate(LATENCY_BUCKETS):
        labels.append(f'{name}_bucket{{le="{bound}"}}')
        values.append(float(slots[base + i]))
    labels.append(f'{name}_bucket{{le="+Inf"}}')
    values.append(float(slots[base + len(LATENCY_BUCKETS)]))
    labels.append(f"{name}_count")
    values.append(float(slots[base + len(LATENCY_BUCKETS) + 1]))
    labels.append(f"{name}_sum")
    values.append(slots[base + len(LATENCY_BUCKETS) + 2] / 1_000_000.0)
    return Metric(name=name, help=help_text, type=MetricType.HISTOGRAM, label=labels, value=values)


def _build_labelled_histogram(
    name: str,
    help_text: str,
    slots: list[int],
    label_key: str,
    label_values: tuple[str, ...],
    base_hist_idx: int,
) -> Metric:
    labels: list[str] = []
    values: list[float] = []
    for offset, lv in enumerate(label_values):
        base = _OFF_HISTS + (base_hist_idx + offset) * _HIST_SLOTS
        for i, bound in enumerate(LATENCY_BUCKETS):
            labels.append(f'{name}_bucket{{{label_key}="{lv}",le="{bound}"}}')
            values.append(float(slots[base + i]))
        labels.append(f'{name}_bucket{{{label_key}="{lv}",le="+Inf"}}')
        values.append(float(slots[base + len(LATENCY_BUCKETS)]))
        labels.append(f'{name}_count{{{label_key}="{lv}"}}')
        values.append(float(slots[base + len(LATENCY_BUCKETS) + 1]))
        labels.append(f'{name}_sum{{{label_key}="{lv}"}}')
        values.append(slots[base + len(LATENCY_BUCKETS) + 2] / 1_000_000.0)
    return Metric(name=name, help=help_text, type=MetricType.HISTOGRAM, label=labels, value=values)


def request_lifecycle_slots_to_motor_metrics(
    slots: list[int] | None,
    *,
    rate_tracker: RequestLifecycleRateTracker | None = None,
) -> list[Metric]:
    """Convert merged worker request-lifecycle slots to ``motor:*`` Prometheus Metric objects."""
    if not slots or len(slots) != _TOTAL_SLOTS:
        return []

    received = slots[_IDX_RECEIVED]
    admitted = slots[_IDX_ADMITTED]
    completed = slots[_IDX_COMPLETED]
    in_flight = slots[_IDX_IN_FLIGHT]
    queued = slots[_IDX_QUEUED]
    executing = slots[_IDX_EXECUTING]
    rejected_total = sum(slots[_OFF_REJECTED + i] for i in range(len(REJECT_REASONS)))

    recv_rate, admit_rate, reject_rate, complete_rate = (
        rate_tracker.update(received, admitted, rejected_total, completed)
        if rate_tracker is not None
        else (0.0, 0.0, 0.0, 0.0)
    )

    metrics: list[Metric] = [
        Metric(
            name="motor:coordinator_requests_received_total",
            help="Total external inference requests received by Coordinator.",
            type=MetricType.COUNTER,
            label=["motor:coordinator_requests_received_total"],
            value=[float(received)],
        ),
        Metric(
            name="motor:coordinator_requests_admitted_total",
            help="Total external inference requests admitted into Coordinator routing.",
            type=MetricType.COUNTER,
            label=["motor:coordinator_requests_admitted_total"],
            value=[float(admitted)],
        ),
        Metric(
            name="motor:coordinator_requests_rejected_total",
            help="Total external inference requests rejected by Coordinator before routing, by reason.",
            type=MetricType.COUNTER,
            label=[f'motor:coordinator_requests_rejected_total{{reason="{r}"}}' for r in REJECT_REASONS],
            value=[float(slots[_OFF_REJECTED + i]) for i in range(len(REJECT_REASONS))],
        ),
        Metric(
            name="motor:coordinator_requests_completed_total",
            help="Total external inference requests that reached a terminal state in Coordinator.",
            type=MetricType.COUNTER,
            label=["motor:coordinator_requests_completed_total"],
            value=[float(completed)],
        ),
        Metric(
            name="motor:coordinator_requests_in_flight",
            help="Current in-flight external inference requests in Coordinator (received - completed).",
            type=MetricType.GAUGE,
            label=["motor:coordinator_requests_in_flight"],
            value=[float(in_flight)],
        ),
        Metric(
            name="motor:coordinator_requests_queued",
            help="Current admitted inference requests waiting for scheduling/dispatch in Coordinator.",
            type=MetricType.GAUGE,
            label=["motor:coordinator_requests_queued"],
            value=[float(queued)],
        ),
        Metric(
            name="motor:coordinator_requests_executing",
            help="Current inference requests actively executing upstream in Coordinator.",
            type=MetricType.GAUGE,
            label=["motor:coordinator_requests_executing"],
            value=[float(executing)],
        ),
        Metric(
            name="motor:coordinator_request_receive_rate",
            help="Coordinator external inference request receive rate (requests/sec).",
            type=MetricType.GAUGE,
            label=["motor:coordinator_request_receive_rate"],
            value=[recv_rate],
        ),
        Metric(
            name="motor:coordinator_request_admit_rate",
            help="Coordinator external inference request admit rate (requests/sec).",
            type=MetricType.GAUGE,
            label=["motor:coordinator_request_admit_rate"],
            value=[admit_rate],
        ),
        Metric(
            name="motor:coordinator_request_reject_rate",
            help="Coordinator external inference request reject rate (requests/sec).",
            type=MetricType.GAUGE,
            label=["motor:coordinator_request_reject_rate"],
            value=[reject_rate],
        ),
        Metric(
            name="motor:coordinator_request_complete_rate",
            help="Coordinator external inference request completion rate (requests/sec).",
            type=MetricType.GAUGE,
            label=["motor:coordinator_request_complete_rate"],
            value=[complete_rate],
        ),
        Metric(
            name="motor:coordinator_requests_terminal_total",
            help="Total external inference requests by terminal outcome (success, failed, timeout, cancelled, client_disconnect).",
            type=MetricType.COUNTER,
            label=[f'motor:coordinator_requests_terminal_total{{outcome="{o}"}}' for o in TERMINAL_OUTCOMES],
            value=[float(slots[_OFF_TERMINAL + i]) for i in range(len(TERMINAL_OUTCOMES))],
        ),
        _build_unlabelled_histogram(
            "motor:coordinator_request_queue_duration_seconds",
            "Coordinator request queue duration from admission to first engine dispatch in seconds.",
            slots,
            _HIST_IDX_QUEUE,
        ),
        _build_unlabelled_histogram(
            "motor:coordinator_request_duration_seconds",
            "Coordinator end-to-end request lifecycle duration from ingress to terminal state in seconds.",
            slots,
            _HIST_IDX_REQUEST,
        ),
        Metric(
            name="motor:coordinator_request_attempts_total",
            help="Total internal routing attempts initiated by Coordinator, by router.",
            type=MetricType.COUNTER,
            label=[f'motor:coordinator_request_attempts_total{{router="{r}"}}' for r in ROUTERS],
            value=[float(slots[_OFF_ATTEMPTS + i]) for i in range(len(ROUTERS))],
        ),
        Metric(
            name="motor:coordinator_request_retries_total",
            help="Total internal routing retries initiated by Coordinator, by router and reason.",
            type=MetricType.COUNTER,
            label=[
                f'motor:coordinator_request_retries_total{{router="{router}",reason="{reason}"}}'
                for router in ROUTERS
                for reason in RETRY_REASONS
            ],
            value=[
                float(slots[_OFF_RETRIES + r_idx * len(RETRY_REASONS) + re_idx])
                for r_idx in range(len(ROUTERS))
                for re_idx in range(len(RETRY_REASONS))
            ],
        ),
        Metric(
            name="motor:coordinator_request_retry_recovered_total",
            help="Total requests that succeeded after at least one internal retry in Coordinator.",
            type=MetricType.COUNTER,
            label=[f'motor:coordinator_request_retry_recovered_total{{router="{r}"}}' for r in ROUTERS],
            value=[float(slots[_OFF_RETRY_RECOVERED + i]) for i in range(len(ROUTERS))],
        ),
        _build_labelled_histogram(
            "motor:coordinator_request_retry_duration_seconds",
            "Coordinator retry duration from first retry trigger to retry resolution in seconds.",
            slots,
            "router",
            ROUTERS,
            _HIST_IDX_RETRY_BASE,
        ),
        Metric(
            name="motor:coordinator_resource_release_total",
            help="Total Coordinator request resource release operations by role and result (success, failed, timeout).",
            type=MetricType.COUNTER,
            label=[
                f'motor:coordinator_resource_release_total{{role="{role}",result="{res}"}}'
                for role in RELEASE_ROLES
                for res in RELEASE_RESULTS
            ],
            value=[
                float(slots[_OFF_RELEASE_TOTAL + r_idx * len(RELEASE_RESULTS) + res_idx])
                for r_idx in range(len(RELEASE_ROLES))
                for res_idx in range(len(RELEASE_RESULTS))
            ],
        ),
        Metric(
            name="motor:coordinator_resource_release_retries_total",
            help="Total Coordinator request resource release retries (including residual reclaim), by role.",
            type=MetricType.COUNTER,
            label=[f'motor:coordinator_resource_release_retries_total{{role="{role}"}}' for role in RELEASE_ROLES],
            value=[float(slots[_OFF_RELEASE_RETRIES + i]) for i in range(len(RELEASE_ROLES))],
        ),
        _build_labelled_histogram(
            "motor:coordinator_resource_release_duration_seconds",
            "Coordinator request resource release duration in seconds, by role.",
            slots,
            "role",
            RELEASE_ROLES,
            _HIST_IDX_RELEASE_BASE,
        ),
    ]
    return metrics
