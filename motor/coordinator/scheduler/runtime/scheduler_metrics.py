# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Scheduler-side per-endpoint scheduling metrics, published via shared memory.

The SchedulerServer is the only process that knows the authoritative scheduling state:
per-endpoint request counts (cnt), the load-balance score (fresh_load), the active
tokens (a_tokens) and the cumulative request count (total_cnt). A background thread
periodically snapshots these values and writes them to a small shared-memory region;
the Obs process's MetricsCollector reads the region and exposes them as ``motor:*``
metrics (e.g. ``motor:endpoint_state`` with ``stat="request_count"``,
``stat="fresh_load"``, ``stat="active_tokens"``, ``stat="total_cnt"``).

Layout mirrors kv_usage's shared memory: a 64-byte header (seqlock sequence) followed
by fixed-size entries.
"""

import math
import struct
import sys
import threading
from typing import Callable

from motor.common.logger import get_logger
from motor.coordinator.metrics.metric_types import MOTOR_ENDPOINT_STATE_HELP, MOTOR_ENDPOINT_STATE_METRIC

logger = get_logger(__name__)

# Magic: "SHMT" as ASCII -> 0x53 0x48 0x4D 0x54 = 0x53484D54 (little-endian).
_SCHED_METRICS_MAGIC = 0x53484D54
# v2: entry extended with cumulative request count (total_cnt); readers reject other schemas.
_SCHED_METRICS_SCHEMA_VERSION = 2

# Role bytes (mirror kv_usage/workload shm conventions).
ROLE_PREFILL = 0
ROLE_DECODE = 1
ROLE_HYBRID = 2
ROLE_ENCODE = 3

# Header 64B: magic 4B, schema 2B, padding 2B, sequence 8B (seqlock), entry_count 4B,
# max_entries 4B, instance_version 8B, padding 32B.
_HEADER_SIZE = 64
_HEADER_FMT = "<I H H q I I Q 32x"
# Entry 48B: instance_id 4B, endpoint_id 4B, role 1B + 3B pad, fresh_load 8B,
# active_tokens 8B, cnt 8B (q), total_cnt 8B (q), 4B pad.
_ENTRY_SIZE = 48
_ENTRY_FMT = "<i i B 3x d d q q 4x"

# Seqlock 读取在撕裂（写者并发写）时的最大重试次数（与 kv_usage 一致）。
_SEQLOCK_READ_ATTEMPTS = 4

DEFAULT_SCHED_METRICS_SHM_MAX_ENTRIES = 10240


def _untrack_shared_memory(shm) -> None:
    """Unregister an attach-only segment from this process's resource_tracker.

    CPython registers every ``SharedMemory`` (even ``create=False`` attaches) with the per-process resource_tracker, which unlinks registered segments at process exit.
    The sched metrics segment is created/owned by SchedulerServer: a reader (Obs)
    process exiting must never unlink it (workload_shm attaches via the Rust .so for
    the same reason). Note the tracker key is the private ``_name`` (leading slash on
    POSIX); the public ``name`` property strips that slash and would not match.
    """
    if sys.platform == "win32":
        return
    from multiprocessing import resource_tracker

    try:
        registered_name = getattr(shm, "_name", None) or shm.name
        resource_tracker.unregister(registered_name, "shared_memory")
    except Exception as e:
        logger.debug("sched metrics shm resource_tracker unregister failed: %s", e)


def sched_metrics_shm_total_size(max_entries: int) -> int:
    """Total shared memory size in bytes."""
    return _HEADER_SIZE + max_entries * _ENTRY_SIZE


def _pack_role(role) -> int:
    """Map a PDRole to the layout role byte."""

    value = getattr(role, "value", str(role))
    if value == "prefill":
        return ROLE_PREFILL
    if value == "decode":
        return ROLE_DECODE
    if value in ("union", "both"):
        return ROLE_HYBRID
    if value == "encode":
        return ROLE_ENCODE
    return ROLE_HYBRID


def _unpack_role(role_byte: int) -> str:
    """Map a layout role byte back to a role string."""
    if role_byte == ROLE_PREFILL:
        return "prefill"
    if role_byte == ROLE_DECODE:
        return "decode"
    if role_byte == ROLE_ENCODE:
        return "encode"
    return "union"


def sched_metrics_snapshot_to_motor_metrics(
    snapshot: dict[tuple[int, int], tuple[int, float, float, int, int]],
) -> list:
    """Convert a scheduler-metrics snapshot into a single ``motor:endpoint_state`` family.

    Each snapshot entry ``(instance_id, endpoint_id) -> (role_byte, fresh_load,
    active_tokens, cnt, total_cnt)`` is emitted as four labelled samples of one gauge
    family, distinguished by the ``stat`` label:

      - ``stat="request_count"``   (cnt)
      - ``stat="fresh_load"``      (authoritative load-balance score)
      - ``stat="active_tokens"``   (a_tokens)
      - ``stat="total_cnt"``       (cumulative requests allocated, never decremented)

    All samples are merged into a single Metric object with parallel label/value
    arrays, so MetricsCollector emits one HELP/TYPE per family instead of repeating
    them for every endpoint. Returns a list of Metric objects suitable for
    MetricsCollector formatting.
    """
    from motor.coordinator.metrics.metric_types import Metric, MetricType

    labels: list[str] = []
    values: list[float] = []
    for (iid, eid), (role_byte, fresh_load, active_tokens, cnt, total_cnt) in snapshot.items():
        role = _unpack_role(role_byte)
        base = f'instance_id="{iid}",endpoint_id="{eid}",role="{role}"'
        labels.append(f'{MOTOR_ENDPOINT_STATE_METRIC}{{{base},stat="request_count"}}')
        values.append(float(cnt))
        labels.append(f'{MOTOR_ENDPOINT_STATE_METRIC}{{{base},stat="fresh_load"}}')
        values.append(fresh_load)
        labels.append(f'{MOTOR_ENDPOINT_STATE_METRIC}{{{base},stat="active_tokens"}}')
        values.append(active_tokens)
        labels.append(f'{MOTOR_ENDPOINT_STATE_METRIC}{{{base},stat="total_cnt"}}')
        values.append(float(total_cnt))
    if not labels:
        return []
    return [
        Metric(
            name=MOTOR_ENDPOINT_STATE_METRIC,
            help=MOTOR_ENDPOINT_STATE_HELP,
            type=MetricType.GAUGE,
            label=labels,
            value=values,
        )
    ]


class SchedulerMetricsShmWriter:
    """Scheduler-side writer: publishes per-endpoint scheduling metrics to shared memory."""

    def __init__(self, shm, max_entries: int = DEFAULT_SCHED_METRICS_SHM_MAX_ENTRIES):
        self._shm = shm
        self._buf = memoryview(shm.buf)
        self._max_entries = max_entries
        self._sequence = 0
        self._instance_version = 0

    @property
    def shm_name(self) -> str:
        """Public name of the shared memory block for readers."""
        return self._shm.name if self._shm else ""

    def write_snapshot(
        self,
        snapshot: dict[tuple[int, int], tuple[int, float, float, int, int]],
    ) -> None:
        """Write the full scheduling-metrics snapshot. Seqlock: odd sequence while writing.

        ``snapshot`` maps ``(instance_id, endpoint_id)`` to
        ``(role_byte, fresh_load, active_tokens, cnt, total_cnt)``.
        """
        entries = [
            (iid, eid, role, fresh_load, active_tokens, cnt, total_cnt)
            for (iid, eid), (role, fresh_load, active_tokens, cnt, total_cnt) in snapshot.items()
        ]
        if len(entries) > self._max_entries:
            logger.warning(
                "Sched metrics shm max_entries=%d exceeded (got %d), truncating",
                self._max_entries,
                len(entries),
            )
            entries = entries[: self._max_entries]
        self._sequence += 1  # odd: writer in progress
        struct.pack_into(
            _HEADER_FMT,
            self._buf,
            0,
            _SCHED_METRICS_MAGIC,
            _SCHED_METRICS_SCHEMA_VERSION,
            0,
            self._sequence,
            len(entries),
            self._max_entries,
            self._instance_version,
        )
        for slot, (iid, eid, role, fresh_load, active_tokens, cnt, total_cnt) in enumerate(entries):
            offset = _HEADER_SIZE + slot * _ENTRY_SIZE
            struct.pack_into(_ENTRY_FMT, self._buf, offset, iid, eid, role, fresh_load, active_tokens, cnt, total_cnt)
        self._instance_version += 1
        self._sequence += 1  # even: stable snapshot
        struct.pack_into(
            _HEADER_FMT,
            self._buf,
            0,
            _SCHED_METRICS_MAGIC,
            _SCHED_METRICS_SCHEMA_VERSION,
            0,
            self._sequence,
            len(entries),
            self._max_entries,
            self._instance_version,
        )

    def release(self) -> None:
        """Release the buffer reference before the owner closes the SharedMemory."""
        self._buf = None
        self._shm = None


class SchedulerMetricsShmReader:
    """Obs-side reader: reads the scheduling-metrics snapshot published by the scheduler."""

    def __init__(self, shm_name: str):
        self._shm_name = shm_name
        self._shm = None
        self._buf: memoryview | None = None
        self._last_sequence: int | None = None

    @property
    def last_sequence(self) -> int | None:
        """Last stable snapshot sequence read from shared memory."""
        return self._last_sequence

    @property
    def shm_name(self) -> str:
        """Public name of the scheduling-metrics shared memory region this reader attaches to."""
        return self._shm_name

    def attach(self) -> None:
        """Attach to the existing shared memory region."""
        from multiprocessing import shared_memory

        self._shm = shared_memory.SharedMemory(name=self._shm_name, create=False)
        _untrack_shared_memory(self._shm)
        self._buf = memoryview(self._shm.buf)

    def detach(self) -> None:
        """Detach from shared memory. Release the buffer before closing."""
        if self._shm:
            self._buf = None
            try:
                self._shm.close()
            except Exception as e:
                logger.warning("SchedulerMetricsShmReader detach error: %s", e)
            self._shm = None

    def read_snapshot(
        self,
    ) -> dict[tuple[int, int], tuple[int, float, float, int, int]]:
        """Read the current stable snapshot; returns {} when unavailable or unchanged.

        Seqlock protocol: the header sequence is validated both BEFORE and AFTER
        reading the entries, so a torn read (the writer racing mid-cycle in another
        process) is retried instead of being accepted; after exhausting retries the
        read reports unchanged without consuming any sequence.
        """
        if not self._buf:
            return {}
        if len(self._buf) < _HEADER_SIZE:
            return {}
        try:
            for _attempt in range(_SEQLOCK_READ_ATTEMPTS):
                t = struct.unpack_from(_HEADER_FMT, self._buf, 0)
                magic, schema, _pad, sequence, entry_count, max_entries, _instance_version = t[:7]
                if magic != _SCHED_METRICS_MAGIC or schema != _SCHED_METRICS_SCHEMA_VERSION or sequence % 2 == 1:
                    return {}
                if self._last_sequence is not None and sequence == self._last_sequence:
                    return {}
                snapshot: dict[tuple[int, int], tuple[int, float, float, int, int]] = {}
                torn = not (0 <= entry_count <= max_entries)
                if not torn:
                    for slot in range(entry_count):
                        offset = _HEADER_SIZE + slot * _ENTRY_SIZE
                        if offset + _ENTRY_SIZE > len(self._buf):
                            torn = True
                            break
                        e = struct.unpack_from(_ENTRY_FMT, self._buf, offset)
                        fresh_load, active_tokens = e[3], e[4]
                        if not (math.isfinite(fresh_load) and math.isfinite(active_tokens)):
                            # 撕裂/损坏的 double：丢弃该条而不是输出 NaN 指标。
                            continue
                        snapshot[(e[0], e[1])] = (e[2], fresh_load, active_tokens, e[5], e[6])
                # 读完 entries 后复查 sequence：写者在读取期间开始新一轮写入则视为撕裂，
                # 丢弃本次数据并重试（不提交 _last_sequence，下轮仍能读到新快照）。
                if struct.unpack_from(_HEADER_FMT, self._buf, 0)[3] != sequence:
                    continue
                if torn:
                    continue
                self._last_sequence = sequence
                return snapshot
        except Exception as e:
            logger.warning("Failed to read sched metrics shared memory: %s", e)
            return {}
        logger.warning("Sched metrics shm read stayed torn after %d attempts", _SEQLOCK_READ_ATTEMPTS)
        return {}


class SchedulerMetricsCollector:
    """Background daemon thread that snapshots and publishes scheduling metrics.

    ``snapshot_fn`` returns the per-endpoint snapshot dict to publish (called each cycle,
    off the scheduling hot path); ``on_publish`` receives each published snapshot, e.g.
    to write it to shared memory.
    """

    def __init__(
        self,
        snapshot_fn: Callable[[], dict[tuple[int, int], tuple[int, float, float, int, int]]],
        on_publish: Callable[[dict[tuple[int, int], tuple[int, float, float, int, int]]], None],
        interval: float = 2.0,
    ):
        self._snapshot_fn = snapshot_fn
        self._on_publish = on_publish
        self._interval = max(0.5, float(interval))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the collector thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="SchedulerMetricsCollector", daemon=True)
        self._thread.start()
        logger.info("SchedulerMetricsCollector started (interval=%.1fs)", self._interval)

    def stop(self) -> None:
        """Stop the collector thread and wait for it to finish."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=max(0.5, self._interval + 1.0))
            self._thread = None
        logger.info("SchedulerMetricsCollector stopped")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                snapshot = self._snapshot_fn()
                # 空快照也发布：清掉 shm 里的陈旧条目，否则实例全部下线后
                # Obs 侧会一直读到旧数据。
                self._on_publish(snapshot)
            except Exception as e:
                logger.warning("SchedulerMetricsCollector cycle failed: %s", e)
            self._stop_event.wait(self._interval)
