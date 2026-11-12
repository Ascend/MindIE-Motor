# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Cross-process export of Agent Hint KVC counters.

The lifecycle controller runs in the inference process while ``/metrics`` is
served by the Obs process. A tiny fixed-size shared-memory block carries the
counters between them, mirroring the sched/kv-usage shm patterns.
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

DEFAULT_KVC_METRICS_SHM_NAME = "motor_kvc_metrics"

_MAGIC = 0x4B56434D  # "KVCM"
_SCHEMA_VERSION = 1
_HEADER_FMT = "<I H H q I 12x"
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
COUNTER_KEYS = (
    "evict_total",
    "offload_total",
    "prefetch_total",
    "edit_success",
    "edit_fail",
    "send_fail",
    "gate_skip",
    "queue_full",
    "epoch_revive",
)
_ENTRY_FMT = "<" + "q" * len(COUNTER_KEYS)
_ENTRY_SIZE = struct.calcsize(_ENTRY_FMT)
TOTAL_SIZE = _HEADER_SIZE + _ENTRY_SIZE

_METRIC_NAMES = {
    "evict_total": ("motor:kvc_evict_total", "KVC evict actions dispatched"),
    "offload_total": ("motor:kvc_offload_total", "KVC offload actions dispatched"),
    "prefetch_total": ("motor:kvc_prefetch_total", "KVC prefetch actions dispatched"),
    "edit_success": ("motor:kvc_edit_success_total", "KVC engine edits that returned op_status=true"),
    "edit_fail": ("motor:kvc_edit_fail_total", "KVC engine edits that returned op_status=false"),
    "send_fail": ("motor:kvc_send_fail_total", "KVC management requests that failed to send"),
    "gate_skip": ("motor:kvc_gate_skip_total", "KVC actions skipped by the capacity gate"),
    "queue_full": ("motor:kvc_queue_full_total", "KVC actions dropped because the queue was full"),
    "epoch_revive": ("motor:kvc_epoch_revive_total", "Sessions revived by a newer epoch"),
}


def _untrack_shared_memory(shm) -> None:
    """Keep an attach-only reader from unlinking the owner's segment at exit."""
    if sys.platform == "win32":
        return
    from multiprocessing import resource_tracker

    with contextlib.suppress(Exception):
        registered_name = getattr(shm, "_name", None) or shm.name
        resource_tracker.unregister(registered_name, "shared_memory")


class KvcMetricsShmWriter:
    """Inference-process writer: publish the lifecycle counter snapshot."""

    def __init__(self, shm_name: str = DEFAULT_KVC_METRICS_SHM_NAME) -> None:
        from multiprocessing import shared_memory

        self._shm_name = shm_name
        self._lock = threading.Lock()
        self._sequence = 0
        try:
            self._shm = shared_memory.SharedMemory(name=shm_name, create=True, size=TOTAL_SIZE)
        except FileExistsError:
            stale = shared_memory.SharedMemory(name=shm_name, create=False)
            with contextlib.suppress(FileNotFoundError):
                stale.unlink()
            _untrack_shared_memory(stale)
            stale.close()
            self._shm = shared_memory.SharedMemory(name=shm_name, create=True, size=TOTAL_SIZE)
        self._buf = memoryview(self._shm.buf)

    @property
    def shm_name(self) -> str:
        return self._shm_name

    def update(self, stats: dict[str, int]) -> None:
        """Overwrite the whole counter block with a seqlock-consistent snapshot."""
        values = tuple(int(stats.get(key, 0)) for key in COUNTER_KEYS)
        with self._lock:
            self._sequence += 1
            struct.pack_into(
                _HEADER_FMT,
                self._buf,
                0,
                _MAGIC,
                _SCHEMA_VERSION,
                0,
                self._sequence,
                len(COUNTER_KEYS),
            )
            struct.pack_into(_ENTRY_FMT, self._buf, _HEADER_SIZE, *values)
            self._sequence += 1
            struct.pack_into(
                _HEADER_FMT,
                self._buf,
                0,
                _MAGIC,
                _SCHEMA_VERSION,
                0,
                self._sequence,
                len(COUNTER_KEYS),
            )

    def close(self) -> None:
        with self._lock:
            self._buf.release()
            with contextlib.suppress(FileNotFoundError):
                self._shm.unlink()
            _untrack_shared_memory(self._shm)
            self._shm.close()


class KvcMetricsShmReader:
    """Obs-process reader: attach lazily and decode the latest snapshot."""

    def __init__(self, shm_name: str = DEFAULT_KVC_METRICS_SHM_NAME) -> None:
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

    def read(self) -> dict[str, int] | None:
        if self._buf is None:
            return None
        try:
            for _ in range(4):
                magic, version, _pad, sequence, count = struct.unpack_from(_HEADER_FMT, self._buf, 0)
                if magic != _MAGIC or version != _SCHEMA_VERSION or sequence % 2 == 1:
                    continue
                if count != len(COUNTER_KEYS):
                    return None
                values = struct.unpack_from(_ENTRY_FMT, self._buf, _HEADER_SIZE)
                magic2, version2, _pad2, sequence2, count2 = struct.unpack_from(_HEADER_FMT, self._buf, 0)
                if (magic2, version2, sequence2, count2) == (magic, version, sequence, count):
                    return dict(zip(COUNTER_KEYS, (int(value) for value in values)))
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


def kvc_metrics_to_motor_metrics(
    stats: dict[str, int] | None,
    *,
    worker_index: int | None = None,
    scope: str | None = None,
) -> list[Any]:
    """Convert one KVC snapshot into Prometheus counter ``Metric`` objects.

    ``worker_index`` labels the historical per-worker shm snapshots; ``scope``
    labels the single central-owner snapshot. Both are rendered as complete
    sample lines because MetricsCollector treats ``Metric.label`` as the line.
    """
    if not stats:
        return []
    metrics: list[Metric] = []
    for key in COUNTER_KEYS:
        name, help_text = _METRIC_NAMES[key]
        if worker_index is not None:
            label = [f'{name}{{worker="{int(worker_index)}"}}']
        elif scope:
            label = [f'{name}{{scope="{scope}"}}']
        else:
            label = []
        metrics.append(
            Metric(
                name=name,
                help=help_text,
                type=MetricType.COUNTER,
                label=list(label),
                value=[float(stats.get(key, 0))],
            )
        )
    return metrics
