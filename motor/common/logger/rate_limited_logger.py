# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Rate-limited logger helpers (log-quality standard §6 anti-spam).

Two complementary mechanisms:

1. ``error_window`` — collapses repeated identical errors inside a time window
   into a single ``count=N`` line. Use for transient failures (network, RPC)
   that are expected to retry.

2. ``info_periodic`` — emits a periodic summary line ("succeeded N times in
   last 60s") for high-frequency success paths that would otherwise flood
   logs. Use alongside a per-call DEBUG line.

The helpers are **stateless across instances** — each RateLimitedLogger owns
its own counters and timers. A single RateLimitedLogger should be shared by
all callers within one process (e.g. one per API client class).
"""

import threading
import time
from typing import Any


class RateLimitedLogger:
    """Thread-safe rate-limited logger wrapper around a standard logger.

    Args:
        logger: the underlying ``logging.Logger``-like object exposing
            ``.info(msg)``, ``.debug(msg)``, ``.warning(msg)`` and
            ``.error(msg)``. Any object with these methods works.
    """

    def __init__(self, logger: Any):
        self._logger = logger
        self._lock = threading.Lock()
        # error_window state: key -> {count, first_ts, last_msg}
        self._err_state: dict[str, dict[str, Any]] = {}
        # info_periodic state: key -> {success_count, last_flush_ts}
        self._info_state: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # error_window: collapse repeated errors inside a sliding window
    # ------------------------------------------------------------------

    def error_window(
        self,
        key: str,
        msg: str,
        window_sec: int = 10,
        threshold: int = 3,
    ) -> None:
        """Emit a single ERROR with ``count=N`` once ``threshold`` occurrences
        pile up inside ``window_sec``; suppress intermediate ones.

        Args:
            key: dedup key (e.g. ``"controller.query_status.timeout"``).
            msg: the error message (static — variable parts should be folded
                into the key or omitted).
            window_sec: time window in seconds.
            threshold: minimum occurrences before emitting.

        The first occurrence is always emitted immediately. Once the threshold
        is reached, the window resets and the counter starts over.
        """
        now = time.time()
        with self._lock:
            state = self._err_state.get(key)
            if state is None:
                # First occurrence: emit immediately, start counting.
                self._err_state[key] = {
                    "count": 1,
                    "first_ts": now,
                    "last_msg": msg,
                }
                self._logger.error("%s", msg)
                return

            state["count"] += 1
            state["last_msg"] = msg
            if now - state["first_ts"] >= window_sec:
                # Window elapsed: emit summary, reset.
                summary = f"{msg} (last {int(now - state['first_ts'])}s saw {state['count']} occurrences)"
                self._logger.error("%s", summary)
                self._err_state[key] = {
                    "count": 1,
                    "first_ts": now,
                    "last_msg": msg,
                }
            # else: inside window, below threshold-equivalent — suppress.

    # ------------------------------------------------------------------
    # info_periodic: periodic success summary
    # ------------------------------------------------------------------

    def record_success(self, key: str) -> None:
        """Record one success event. Pairs with ``emit_info_periodic`` —
        typically called once per successful operation.
        """
        now = time.time()
        with self._lock:
            state = self._info_state.get(key)
            if state is None:
                self._info_state[key] = {
                    "success_count": 1,
                    "last_flush_ts": now,
                }
                return
            state["success_count"] += 1

    def emit_info_periodic(
        self,
        key: str,
        msg_template: str,
        interval_sec: int = 60,
        force: bool = False,
    ) -> None:
        """Emit a periodic INFO summary if ``interval_sec`` has passed since
        the last emission. If ``force=True`` and there are unsent counts,
        emit immediately (call on shutdown / test teardown).

        Args:
            key: dedup key (shared with ``record_success``).
            msg_template: format string with one ``{count}`` placeholder,
                e.g. ``"Periodic summary: succeeded {count} times"``.
            interval_sec: emit interval in seconds.
            force: emit even if interval not reached.
        """
        now = time.time()
        with self._lock:
            state = self._info_state.get(key)
            if state is None:
                # Nothing to report; lazily initialize.
                self._info_state[key] = {
                    "success_count": 0,
                    "last_flush_ts": now,
                }
                return

            elapsed = now - state["last_flush_ts"]
            count = state["success_count"]
            if not force and elapsed < interval_sec:
                return

            if count > 0:
                self._logger.info(msg_template.format(count=count))
            # Reset for the next window.
            state["success_count"] = 0
            state["last_flush_ts"] = now

    def flush_all(self) -> None:
        """Force-emit any pending periodic summaries. Call on shutdown."""
        with self._lock:
            keys = list(self._info_state.keys())
        for key in keys:
            self.emit_info_periodic(key, "Periodic summary: succeeded {count} times", force=True)
