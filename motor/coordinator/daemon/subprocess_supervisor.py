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
SubprocessSupervisor: health check and auto-restart of child processes.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Callable

from motor.coordinator.process.base import BaseProcessManager
from motor.common.logger import get_logger

logger = get_logger(__name__)

CHECK_INTERVAL = 2.0
MAX_RESTART_PER_MINUTE = 5
RESTART_WINDOW_SECONDS = 60.0


class RestartLimiter:
    """Sliding window: track restart timestamps, enforce max per minute."""

    def __init__(self, max_per_minute: int = MAX_RESTART_PER_MINUTE):
        self._max = max_per_minute
        self._timestamps: deque[float] = deque()

    def record(self) -> None:
        self._timestamps.append(time.monotonic())

    def can_restart(self) -> bool:
        now = time.monotonic()
        self._prune_old(now)
        return len(self._timestamps) < self._max

    def _prune_old(self, now: float) -> None:
        cutoff = now - RESTART_WINDOW_SECONDS
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()


class SubprocessSupervisor:
    """Subprocess health monitoring and auto-restart."""

    def __init__(
        self,
        process_managers: dict[str, BaseProcessManager],
        get_supervised_keys: Callable[[], set[str]] | None = None,
    ):
        self._managers = process_managers
        self._check_interval = CHECK_INTERVAL
        self._restart_limits: dict[str, RestartLimiter] = {}
        self._get_supervised_keys = get_supervised_keys

    async def run(self, stop_event: asyncio.Event) -> None:
        """Main loop: periodic check, restart on failure."""
        while not stop_event.is_set():
            try:
                await asyncio.sleep(self._check_interval)
                supervised = (
                    self._get_supervised_keys() if self._get_supervised_keys is not None else set(self._managers)
                )
                for name, mgr in self._managers.items():
                    if name not in supervised:
                        continue
                    if not mgr.is_running() and not stop_event.is_set():
                        if self._can_restart(name):
                            if hasattr(mgr, "restart_dead_workers"):
                                logger.warning("Restarting dead %s worker(s)", name)
                                started = mgr.restart_dead_workers()
                            else:
                                logger.warning("Restarting %s (detected exit)", name)
                                mgr.stop()
                                started = mgr.start()
                            if started:
                                self._record_restart(name)
                            else:
                                logger.error("Failed to restart %s", name)
                        else:
                            logger.error("Restart limit exceeded for %s", name)
            except asyncio.CancelledError:
                # Re-raise so task cancellation propagates (asyncio requirement).
                logger.debug("Subprocess supervisor cancelled")
                raise
            except Exception as e:
                logger.exception("Subprocess supervisor loop error: %s", e)

    def _can_restart(self, name: str) -> bool:
        if name not in self._restart_limits:
            self._restart_limits[name] = RestartLimiter(max_per_minute=MAX_RESTART_PER_MINUTE)
        return self._restart_limits[name].can_restart()

    def _record_restart(self, name: str) -> None:
        if name not in self._restart_limits:
            self._restart_limits[name] = RestartLimiter(max_per_minute=MAX_RESTART_PER_MINUTE)
        self._restart_limits[name].record()
