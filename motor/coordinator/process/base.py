# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Base process manager: unified start/stop/health check/terminate strategy.
"""

from __future__ import annotations

import multiprocessing
import weakref
from abc import ABC, abstractmethod
from multiprocessing.process import BaseProcess
from typing import Any, Protocol, runtime_checkable
from motor.common.logger import get_logger
from motor.config.coordinator import CoordinatorConfig


@runtime_checkable
class SupportsSpawnContext(Protocol):
    """Optional protocol for process managers that need Daemon context before spawn (e.g. daemon_pid)."""

    def set_daemon_pid(self, daemon_pid: int | None) -> None:
        """Set daemon PID so the child can detect orphan (getppid() != daemon_pid). Called by Daemon before start()."""
        ...


logger = get_logger(__name__)

TERMINATE_TIMEOUT = 10.0  # Fixed constant, not configurable


class BaseProcessManager(ABC):
    """Base process manager: start/stop, health check, termination policy."""

    def __init__(self, config: CoordinatorConfig, process_name: str):
        self.config = config
        self.process_name = process_name
        self._processes: list[BaseProcess] = []
        self._finalizer: Any | None = None
        self._spawn_context = multiprocessing.get_context("spawn")

    @staticmethod
    def _shutdown_processes(procs: list[BaseProcess]) -> None:
        """Shutdown processes (cannot be a bound method for gc)"""
        for proc in procs:
            if proc.is_alive():
                proc.terminate()
        for proc in procs:
            proc.join(timeout=TERMINATE_TIMEOUT)
            if proc.is_alive():
                logger.warning("%s (PID: %s) did not terminate, killing", proc.name, proc.pid)
                proc.kill()

    @abstractmethod
    def _create_process(self, index: int) -> BaseProcess:
        """Create process for given index. Subclass implements."""
        ...

    @abstractmethod
    def _get_process_count(self) -> int:
        """Return number of processes to manage. Subclass implements."""
        ...

    def start(self) -> bool:
        """Unified start logic. Returns True if is_running() (idempotent)."""
        if self.is_running():
            logger.warning("%s already running", self.process_name)
            return True

        self._processes = []
        try:
            for i in range(self._get_process_count()):
                proc = self._create_process(i)
                self._processes.append(proc)
                proc.start()
                logger.info("Started %s process %s (PID: %s)", self.process_name, i, proc.pid)
        except Exception as e:
            logger.error("Failed to start %s: %s", self.process_name, e, exc_info=True)
            self._processes = []
            return False

        if not self.is_running():
            logger.error("%s process(es) exited immediately after startup", self.process_name)
            return False

        self._finalizer = weakref.finalize(self, self._shutdown_processes, self._processes)
        return True

    def stop(self) -> None:
        """Unified stop logic."""
        if self._finalizer is not None:
            self._finalizer()
            self._finalizer = None

        for proc in self._processes:
            self._terminate_process(proc)

        self._processes = []
        logger.info("%s stopped", self.process_name)

    def is_running(self) -> bool:
        """Check whether any managed process is running."""
        return any(p.is_alive() for p in self._processes)

    def _terminate_process(self, proc: BaseProcess, timeout: float = TERMINATE_TIMEOUT) -> None:
        """Unified termination policy: terminate -> join -> kill."""
        if not proc.is_alive():
            return
        logger.info("Stopping %s (PID: %s)...", proc.name, proc.pid)
        proc.terminate()
        proc.join(timeout=timeout)
        if proc.is_alive():
            logger.warning("%s (PID: %s) did not terminate, killing", proc.name, proc.pid)
            proc.kill()
