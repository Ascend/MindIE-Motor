# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""KV cache usage cache fed by MetricsCollector (ObsServer) via shared memory.

**MetricsCollector** (Obs process) is the single engine-metrics querier: it already
scrapes each PD endpoint's ``/metrics`` periodically. After every collect cycle it
extracts the per-endpoint ``vllm:kv_cache_usage_perc`` value and publishes the snapshot
to a small shared-memory region (``KvUsageShmPublisher``). SchedulerServer attaches the
region (``KvUsageShmReader``) and reads it **on demand**: every ``get_endpoint_kv_cache_usage``
call pulls the latest snapshot from shared memory when MetricsCollector published a new
one (pure in-memory read, no I/O), so there is no periodic sync thread — freshness is
driven entirely by the collector's collect cycle.

Cache key is ``(instance_id, endpoint_id)`` so load-balance exclusion can act at
endpoint granularity (each DP rank has its own KV cache). A failed scrape keeps the
previous snapshot (the publisher is not invoked), while a successful scrape that
yields no kv usage data publishes an empty snapshot which clears stale entries;
entries also expire after ``KV_USAGE_CACHE_TTL_SECONDS`` when the publisher stops
updating entirely, so load-balance behavior degrades gracefully without lingering
stale usage values.
"""

import math
import struct
import sys
import threading
import time
from typing import Callable

from motor.common.logger import get_logger
from motor.common.resources.instance import Instance
from motor.common.resources.endpoint import Endpoint

logger = get_logger(__name__)

# MetricsCollector stores parsed per-endpoint Metric lists under this key in collects.
_METRICS_KEY = "metrics"
_KV_USAGE_METRIC_NAME = "vllm:kv_cache_usage_perc"

# 共享内存最大条数（KV usage 按 endpoint 粒度）
DEFAULT_KV_USAGE_SHM_MAX_ENTRIES = 10240

_cache_lock = threading.Lock()
_cache: dict[tuple[int, int], float] = {}  # (instance_id, endpoint_id) -> kv usage ratio (0~1)
_cache_ts: dict[tuple[int, int], float] = {}  # 每个条目最近一次刷新的 monotonic 时间
# 条目超过该时长未刷新则视为过期（返回 None）。需明显大于 MetricsCollector 的采集
# 周期（默认 reuse_time=3s，约 10 个周期），仅在发布端完全停止更新时兜底。
_kv_usage_cache_ttl = 30.0
# Registered by SchedulerServer startup; None disables the on-demand shm read.
_kv_usage_reader: "KvUsageShmReader | None" = None


def set_kv_usage_reader(reader: "KvUsageShmReader | None") -> None:
    """Register the kv usage shm reader (SchedulerServer startup); None clears it."""
    global _kv_usage_reader
    _kv_usage_reader = reader


def _refresh_if_new_snapshot() -> None:
    """Pull the latest shm snapshot into the local cache (no-op when unchanged).

    ``KvUsageShmReader.read_snapshot_ex`` is a pure in-memory read (seqlock + sequence
    dedup): it reports ``changed`` only when MetricsCollector published a newer
    snapshot, so the cache is refreshed exactly when the collector's collect cycle
    pushes new data. A changed-but-empty snapshot clears the cache (engine restart /
    metric removed) instead of leaving stale usage values behind.
    """
    reader = _kv_usage_reader
    if reader is None:
        return
    try:
        changed, snapshot = reader.read_snapshot_ex()
    except Exception as e:
        logger.warning("Kv usage snapshot read failed: %s", e)
        return
    if changed:
        update_kv_usage_cache(snapshot, allow_clear=True)


def get_endpoint_kv_cache_usage(instance: Instance, endpoint: Endpoint) -> float | None:
    """Return the cached KV cache usage ratio (0~1) of the endpoint, or None.

    On-demand read: refreshes the local cache from shared memory only when the
    collector published a new snapshot (pure in-memory read, no I/O). There is no
    periodic sync thread — data freshness is driven by MetricsCollector's collect
    cycle, and the scheduling hot path only reads the cache. Entries older than
    ``_kv_usage_cache_ttl`` are reported as None so a dead publisher cannot pin a
    stale usage value on a node forever.
    """
    _refresh_if_new_snapshot()
    key = (instance.id, endpoint.id)
    with _cache_lock:
        ts = _cache_ts.get(key)
        if ts is None:
            return None
        if time.monotonic() - ts > _kv_usage_cache_ttl:
            return None
        return _cache.get(key)


def update_kv_usage_cache(mapping: dict[tuple[int, int], float], *, allow_clear: bool = False) -> None:
    """Overwrite the cache from a mapping (shm sync).

    ``mapping`` keys are ``(instance_id, endpoint_id)`` tuples. By default an empty
    mapping is ignored (``read_snapshot`` returns ``{}`` both for "unchanged" and
    "unavailable"); the shm sync path passes ``allow_clear=True`` once it knows a
    new *empty* snapshot was published, which then clears stale entries. Every
    applied entry's refresh timestamp is reset for TTL-based expiry.
    """
    now = time.monotonic()
    with _cache_lock:
        if not mapping:
            if allow_clear:
                _cache.clear()
                _cache_ts.clear()
            return
        _cache.clear()
        _cache.update(mapping)
        _cache_ts.clear()
        for key in mapping:
            _cache_ts[key] = now


def snapshot_kv_usage_cache() -> dict[tuple[int, int], float]:
    """Return a copy of the cache (tests/debug)."""
    with _cache_lock:
        return dict(_cache)


def clear_kv_usage_cache() -> None:
    """Clear the cache (used by tests)."""
    with _cache_lock:
        _cache.clear()
        _cache_ts.clear()


def extract_kv_usage_from_collects(
    collects: dict | None,
) -> dict[tuple[int, int], float]:
    """Extract per-endpoint ``vllm:kv_cache_usage_perc`` from MetricsCollector collects.

    ``collects[instance_id]["endpoints"][endpoint_id]["metrics"]`` is a list of parsed
    ``Metric`` objects (see metrics_collector._parse_metrics). Returns a mapping of
    ``(instance_id, endpoint_id) -> usage`` (max across samples when multiple).
    """
    result: dict[tuple[int, int], float] = {}
    if not isinstance(collects, dict):
        return result
    for ins_id, ins_data in collects.items():
        if not isinstance(ins_data, dict):
            continue
        endpoints = ins_data.get("endpoints") or {}
        for ep_id, pod_info in endpoints.items():
            if not isinstance(pod_info, dict):
                continue
            for metric in pod_info.get(_METRICS_KEY) or []:
                if getattr(metric, "name", None) != _KV_USAGE_METRIC_NAME:
                    continue
                values = getattr(metric, "value", None) or []
                if values:
                    usage = max(values)
                    key = (ins_id, ep_id)
                    result[key] = max(result.get(key, 0.0), usage)
    return result


def _untrack_shared_memory(shm) -> None:
    """Unregister an attach-only segment from this process's resource_tracker.

    CPython registers every ``SharedMemory`` (even ``create=False`` attaches) with the
    per-process resource_tracker, which unlinks registered segments at process exit.
    The kv usage segment is created/owned by SchedulerServer: a reader or publisher
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
        logger.debug("kv usage shm resource_tracker unregister failed: %s", e)


class KvUsageShmPublisher:
    """Obs-side publisher: writes MetricsCollector's kv usage into the scheduler shm.

    ``get_shm_name`` returns the kv usage shm name (published by SchedulerServer and
    forwarded to ObsServer via the scheduler client's GET_AVAILABLE_INSTANCES). The
    publisher attaches lazily and re-attaches when the shm name changes (e.g. scheduler
    restart), and never creates/unlinks the region -- SchedulerServer owns it.
    """

    def __init__(
        self,
        get_shm_name: Callable[[], str | None],
        max_entries: int = DEFAULT_KV_USAGE_SHM_MAX_ENTRIES,
    ):
        self._get_shm_name = get_shm_name
        self._max_entries = max_entries
        self._shm = None
        self._writer: "KvUsageShmWriter | None" = None
        self._shm_name: str | None = None

    def publish(self, collects: dict | None) -> None:
        """Extract kv usage from the latest collect and write it to the shm."""
        shm_name = self._get_shm_name()
        if not shm_name:
            return
        if shm_name != self._shm_name or self._writer is None:
            self._attach(shm_name)
            if self._writer is None:
                return
        usages = extract_kv_usage_from_collects(collects)
        try:
            self._writer.write_snapshot(usages)
        except Exception as e:
            logger.warning("KvUsageShmPublisher write failed: %s", e)

    def _attach(self, shm_name: str) -> None:
        self._detach()
        from multiprocessing import shared_memory

        try:
            shm = shared_memory.SharedMemory(name=shm_name, create=False)
        except FileNotFoundError:
            logger.debug("KV usage shm %s not ready yet, will retry", shm_name)
            return
        except Exception as e:
            logger.warning("Failed to attach kv usage shm %s: %s", shm_name, e)
            return
        _untrack_shared_memory(shm)
        self._shm = shm
        self._writer = KvUsageShmWriter(shm, max_entries=self._max_entries)
        self._shm_name = shm_name
        logger.info("KvUsageShmPublisher attached to %s", shm_name)

    def _detach(self) -> None:
        if self._writer is not None:
            try:
                self._writer.release()
            except Exception as e:
                logger.warning("KvUsageShmPublisher release error: %s", e)
            self._writer = None
        if self._shm is not None:
            try:
                self._shm.close()
            except Exception as e:
                logger.warning("KvUsageShmPublisher close error: %s", e)
            self._shm = None
        self._shm_name = None


# ============================ shared memory layout ============================
# Magic: "KVUS" as ASCII -> 0x4B 0x56 0x55 0x53 = 0x4B565553 (little-endian).
_KV_USAGE_MAGIC = 0x4B565553
# Entry layout v2: (instance_id, endpoint_id, kv_usage).
_KV_USAGE_SCHEMA_VERSION = 2
# Header 64B: magic 4B, schema 2B, padding 2B, sequence 8B (seqlock), entry_count 4B,
# max_entries 4B, instance_version 8B, padding 32B.
_KV_USAGE_HEADER_SIZE = 64
_KV_USAGE_HEADER_FMT = "<I H H q I I Q 32x"
# Entry 16B: instance_id 4B, endpoint_id 4B, kv_usage 8B (double).
_KV_USAGE_ENTRY_SIZE = 16
_KV_USAGE_ENTRY_FMT = "<i i d"

# Seqlock 读取在撕裂（写者并发写）时的最大重试次数。
_SEQLOCK_READ_ATTEMPTS = 4


def kv_usage_shm_total_size(max_entries: int) -> int:
    """Total shared memory size in bytes."""
    return _KV_USAGE_HEADER_SIZE + max_entries * _KV_USAGE_ENTRY_SIZE


class KvUsageShmWriter:
    """Writer for the kv usage snapshot in shared memory (used by Obs-side publisher)."""

    def __init__(self, shm, max_entries: int = DEFAULT_KV_USAGE_SHM_MAX_ENTRIES):
        self._shm = shm
        self._buf = memoryview(shm.buf)
        self._max_entries = max_entries
        self._sequence = 0
        self._instance_version = 0

    @property
    def shm_name(self) -> str:
        """Public name of the shared memory block for readers."""
        return self._shm.name if self._shm else ""

    def write_snapshot(self, usages: dict[tuple[int, int], float]) -> None:
        """Write the full kv usage snapshot. Seqlock: odd sequence while writing."""
        entries = [(iid, eid, usage) for (iid, eid), usage in usages.items() if usage is not None]
        if len(entries) > self._max_entries:
            logger.warning(
                "Kv usage shm max_entries=%d exceeded (got %d), truncating",
                self._max_entries,
                len(entries),
            )
            entries = entries[: self._max_entries]
        self._sequence += 1  # odd: writer in progress
        struct.pack_into(
            _KV_USAGE_HEADER_FMT,
            self._buf,
            0,
            _KV_USAGE_MAGIC,
            _KV_USAGE_SCHEMA_VERSION,
            0,
            self._sequence,
            len(entries),
            self._max_entries,
            self._instance_version,
        )
        for slot, (iid, eid, usage) in enumerate(entries):
            offset = _KV_USAGE_HEADER_SIZE + slot * _KV_USAGE_ENTRY_SIZE
            struct.pack_into(_KV_USAGE_ENTRY_FMT, self._buf, offset, iid, eid, usage)
        self._instance_version += 1
        self._sequence += 1  # even: stable snapshot
        struct.pack_into(
            _KV_USAGE_HEADER_FMT,
            self._buf,
            0,
            _KV_USAGE_MAGIC,
            _KV_USAGE_SCHEMA_VERSION,
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


class KvUsageShmReader:
    """Reader for the kv usage snapshot in shared memory (SchedulerServer / workers)."""

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
        """Public name of the kv usage shared memory region this reader attaches to."""
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
                logger.warning("KvUsageShmReader detach error: %s", e)
            self._shm = None

    def read_snapshot(self) -> dict[tuple[int, int], float]:
        """Read the current stable snapshot; returns {} when unavailable or unchanged view."""
        changed, data = self.read_snapshot_ex()
        return data if changed else {}

    def read_snapshot_ex(self) -> tuple[bool, dict[tuple[int, int], float]]:
        """Read the current stable snapshot with seqlock validation.

        Returns ``(changed, data)``. ``changed`` is True only when a NEW stable
        snapshot was fully read (its sequence is committed as ``_last_sequence``);
        a changed-but-empty snapshot lets callers distinguish "publisher cleared
        all entries" from "nothing new". The seqlock protocol validates the header
        sequence both BEFORE and AFTER reading the entries, so a torn read (the
        writer racing mid-cycle in another process) is retried instead of being
        accepted; after exhausting retries the read reports unchanged without
        consuming any sequence. Usage values are clamped to [0, 1] and non-finite
        (torn) doubles are dropped.
        """
        if not self._buf:
            return False, {}
        if len(self._buf) < _KV_USAGE_HEADER_SIZE:
            return False, {}
        try:
            for _attempt in range(_SEQLOCK_READ_ATTEMPTS):
                t = struct.unpack_from(_KV_USAGE_HEADER_FMT, self._buf, 0)
                magic, schema, _pad, sequence, entry_count, max_entries, _instance_version = t[:7]
                if magic != _KV_USAGE_MAGIC or schema != _KV_USAGE_SCHEMA_VERSION or sequence % 2 == 1:
                    return False, {}
                if self._last_sequence is not None and sequence == self._last_sequence:
                    return False, {}
                usages: dict[tuple[int, int], float] = {}
                torn = not (0 <= entry_count <= max_entries)
                if not torn:
                    for slot in range(entry_count):
                        offset = _KV_USAGE_HEADER_SIZE + slot * _KV_USAGE_ENTRY_SIZE
                        if offset + _KV_USAGE_ENTRY_SIZE > len(self._buf):
                            torn = True
                            break
                        e = struct.unpack_from(_KV_USAGE_ENTRY_FMT, self._buf, offset)
                        usage = e[2]
                        if not math.isfinite(usage):
                            # 撕裂/损坏的 double：丢弃该条而不是污染评分。
                            continue
                        # clamp 到 [0,1]，撕裂值不允许进入调度评分。
                        usages[(e[0], e[1])] = min(1.0, max(0.0, usage))
                # 读完 entries 后复查 sequence：写者在读取期间开始新一轮写入则视为撕裂，
                # 丢弃本次数据并重试（不提交 _last_sequence，下轮仍能读到新快照）。
                if struct.unpack_from(_KV_USAGE_HEADER_FMT, self._buf, 0)[3] != sequence:
                    continue
                if torn:
                    continue
                self._last_sequence = sequence
                return True, usages
        except Exception as e:
            logger.warning("Failed to read kv usage shared memory: %s", e)
            return False, {}
        logger.warning("Kv usage shm read stayed torn after %d attempts", _SEQLOCK_READ_ATTEMPTS)
        return False, {}
