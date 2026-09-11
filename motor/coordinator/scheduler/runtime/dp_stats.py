# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Per-DP request-count and active_tokens statistics for the coordinator.

Counts successfully committed ALLOCATE_ONLY requests per
``(instance_id, dp_rank)`` with ``collections.Counter``.  Emission is driven
by the scheduler client's ``window_sec`` timer (worker 0): each tick
snapshots schema-4 SHM ``active_tokens`` and prints them together with the
request counts accumulated since the previous tick.  A DP whose
``(requests, active_tokens)`` pair is unchanged since the last printed
line (implicit baseline ``(0, 0)``) is omitted.  ``record()`` never logs
by itself.  ``window_sec <= 0`` disables emission.

The window comes from ``scheduler_config.dp_stats_window``
(independent of the kv-affinity stats window), so per-DP stats are
emitted in every deployment and policy.
"""

from __future__ import annotations

from collections import Counter, OrderedDict

from motor.common.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_WINDOW_SEC = 60
# Safety valve for dynamic endpoint churn: beyond this many distinct
# (instance_id, dp_rank) keys, the least-recently-seen key is evicted
# with a warning.  A fixed endpoint set keeps this far below the cap.
_MAX_TRACKED_KEYS = 4096


class DpStatsLogger:
    """Per-DP request counters flushed together with SHM token snapshots.

    ``record()`` increments the window request counter for one
    ``(instance_id, dp_rank)``.  ``emit_window()`` is the only printer: it
    logs each DP's request count and current ``active_tokens`` on the
    scheduler client's ``window_sec`` timer (worker 0).
    Unchanged ``(requests, active_tokens)`` snapshots are omitted
    (implicit baseline ``(0, 0)``).  ``window_sec <= 0`` disables emission.

    Threading contract: ``record()`` and ``emit_window()`` run on the
    coordinator worker's asyncio event loop, so no locking is needed and
    the class does not synchronize concurrent callers.
    """

    def __init__(self, window_sec: int = _DEFAULT_WINDOW_SEC) -> None:
        # Truncate JSON floats the same way the affinity aggregator does so a
        # stats_window=30.5-style config cannot produce float buckets.
        try:
            self._window_sec = int(window_sec)
        except (TypeError, ValueError):
            self._window_sec = 0
        self._counter: Counter[tuple[str, str]] = Counter()
        # First-seen / last-seen order used by the eviction cap: move_to_end
        # on every record.
        self._key_order: OrderedDict[tuple[str, str], None] = OrderedDict()
        # Last printed (requests, active_tokens) per DP; used to skip repeats.
        self._last_emitted: dict[tuple[str, str], tuple[int, float]] = {}

    def record(self, instance_id: int, dp_rank: int) -> None:
        """Count one successfully committed ALLOCATE_ONLY request."""
        if self._window_sec <= 0:
            return
        key = (str(instance_id), str(dp_rank))
        self._counter[key] += 1
        if key in self._key_order:
            self._key_order.move_to_end(key)
        else:
            self._key_order[key] = None
            if len(self._key_order) > _MAX_TRACKED_KEYS:
                self._evict_oldest_key()

    def emit_window(self, snapshots: list[tuple[int, int, float]]) -> None:
        """Log this window's request counts together with SHM ``active_tokens``."""
        if self._window_sec <= 0:
            return
        counts = dict(self._counter)
        self._counter.clear()
        tokens: dict[tuple[str, str], float] = {}
        for instance_id, dp_rank, active_tokens in snapshots:
            tokens[(str(instance_id), str(dp_rank))] = float(active_tokens)
        keys = set(counts) | set(tokens)
        if not keys:
            self._last_emitted.clear()
            return
        for instance_id, dp_rank in sorted(keys):
            key = (instance_id, dp_rank)
            requests = int(counts.get(key, 0))
            active_tokens = tokens.get(key, 0.0)
            current = (requests, active_tokens)
            if self._last_emitted.get(key, (0, 0.0)) == current:
                continue
            self._last_emitted[key] = current
            logger.info(
                "dp_stats instance=%s dp_rank=%s requests=%d active_tokens=%s",
                instance_id,
                dp_rank,
                requests,
                active_tokens,
            )
        for key in list(self._last_emitted):
            if key not in keys:
                self._last_emitted.pop(key, None)

    def _evict_oldest_key(self) -> None:
        key, _ = self._key_order.popitem(last=False)
        self._counter.pop(key, None)
        self._last_emitted.pop(key, None)
        logger.warning(
            "dp_stats tracking more than %d distinct (instance, dp_rank) keys; evicting stale key %s",
            _MAX_TRACKED_KEYS,
            key,
        )
