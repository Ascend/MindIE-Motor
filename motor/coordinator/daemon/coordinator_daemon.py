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
CoordinatorDaemon: unified process management for Mgmt, Scheduler, Infer.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time

from motor.config.coordinator import (
    CoordinatorConfig,
    ROLE_HEARTBEAT_INTERVAL_SEC,
    ROLE_HEARTBEAT_STALE_SEC,
    ROLE_SHM_MASTER,
    ROLE_SHM_NAME,
    ROLE_SHM_STANDBY,
)
from motor.coordinator.daemon.subprocess_supervisor import SubprocessSupervisor
from motor.coordinator.process.base import BaseProcessManager, SupportsSpawnContext
from motor.coordinator.process.constants import (
    PROCESS_KEY_INFERENCE,
    PROCESS_KEY_MGMT,
    PROCESS_KEY_OBS,
    PROCESS_KEY_SCHEDULER,
    STOP_ORDER,
)
from motor.coordinator.process.inference_manager import (
    InferenceProcessManager,
    create_shared_socket,
)
from motor.coordinator.process.mgmt_manager import MgmtProcessManager
from motor.coordinator.process.obs_manager import ObsProcessManager
from motor.coordinator.process.scheduler_manager import SchedulerProcessManager
from motor.coordinator.daemon.role_shm_holder import RoleShmHolder
from motor.common.standby.standby_manager import StandbyManager
from motor.common.logger import get_logger

logger = get_logger(__name__)


class CoordinatorDaemon:
    """Coordinator daemon: starts and monitors Mgmt / Scheduler / Infer processes.

    Scheduler and Mgmt run on both master and standby (Scheduler first so Mgmt can connect).
    With master/standby enabled: only Infer is started on master and stopped on standby
    (on_become_master / on_become_standby). Role shm is created by Daemon; role byte is written
    in on_role_changed callback so StandbyManager stays shm-agnostic (controller does not use shm).
    """

    def __init__(self, config: CoordinatorConfig):
        self.config = config
        self._process_managers: dict[str, BaseProcessManager] = {}
        self._supervisor: SubprocessSupervisor | None = None
        self._standby_manager: StandbyManager | None = None
        self._role_shm_holder: RoleShmHolder | None = None
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        """Daemon main loop."""
        self._initialize_process_managers()

        # Inject daemon PID into any process manager that supports spawn context (e.g. Mgmt for orphan detection).
        # When Daemon is PID 1, ppid check is unreliable; Mgmt also uses role shm heartbeat.
        daemon_pid = os.getpid()
        for key, mgr in self._process_managers.items():
            if isinstance(mgr, SupportsSpawnContext):
                mgr.set_daemon_pid(daemon_pid)
                logger.debug("[Daemon] set_daemon_pid=%s for %s", daemon_pid, key)

        sc = self.config.standby_config
        need_shm = sc.enable_master_standby or ROLE_HEARTBEAT_INTERVAL_SEC > 0
        if need_shm:
            # When master/standby is enabled, initial role must be standby (0) so Mgmt does
            # not report master before etcd lock is acquired. See coordinator-master-standby-ipc-analysis.md.
            self._role_shm_holder = RoleShmHolder(
                ROLE_SHM_NAME,
                ROLE_HEARTBEAT_INTERVAL_SEC,
                stale_sec=ROLE_HEARTBEAT_STALE_SEC,
                initial_role_master=not sc.enable_master_standby,
            )
            if not self._role_shm_holder.start():
                self._role_shm_holder = None
            elif self.config.standby_config.enable_master_standby:
                # Initial role standby so Mgmt does not report master until etcd lock is acquired.
                self._write_role_shm_byte(ROLE_SHM_STANDBY)

        # Scheduler first (both master and standby), then Mgmt, then Obs, so Mgmt connect() succeeds.
        self._start_processes([PROCESS_KEY_SCHEDULER, PROCESS_KEY_MGMT, PROCESS_KEY_OBS])

        if self.config.standby_config.enable_master_standby:
            self._standby_manager = StandbyManager(self.config)
            self._standby_manager.start(
                on_become_master=self._on_become_master,
                on_become_standby=self._on_become_standby,
            )
            get_supervised_keys = self._get_supervised_keys
        else:
            self._start_processes([PROCESS_KEY_INFERENCE])
            get_supervised_keys = None

        self._supervisor = SubprocessSupervisor(
            self._process_managers,
            get_supervised_keys=get_supervised_keys,
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig,
                    self._on_stop_signal,
                )
            except (ValueError, OSError):
                logger.warning("Cannot add signal handler for %s", sig)

        try:
            await self._supervisor.run(self._stop_event)
        finally:
            self._stop_all_processes()
            if self._standby_manager is not None:
                self._standby_manager.stop()
                logger.info("Standby manager stopped")
            if self._role_shm_holder is not None:
                self._role_shm_holder.stop()
                self._role_shm_holder = None

    def _on_become_master(self, should_report_event: bool = False) -> None:
        """Called when this node becomes master: write role shm (if any), then start Inference only."""
        if self._role_shm_holder is not None:
            self._write_role_shm_byte(ROLE_SHM_MASTER)
        self._start_processes([PROCESS_KEY_INFERENCE])

    def _on_become_standby(self) -> None:
        """Called when this node becomes standby: write role shm (if any), then stop Inference only."""
        if self._role_shm_holder is not None:
            self._write_role_shm_byte(ROLE_SHM_STANDBY)
        self._stop_all_processes(exclude_processes={PROCESS_KEY_MGMT, PROCESS_KEY_OBS, PROCESS_KEY_SCHEDULER})

    def _initialize_process_managers(self) -> None:
        """Initialize Mgmt / Scheduler / Infer process managers."""
        self._process_managers[PROCESS_KEY_SCHEDULER] = SchedulerProcessManager(self.config)

        self._process_managers[PROCESS_KEY_MGMT] = MgmtProcessManager(self.config)

        self._process_managers[PROCESS_KEY_OBS] = ObsProcessManager(self.config)

        host = self.config.api_config.coordinator_api_host
        port = self.config.api_config.coordinator_api_infer_port
        sock = create_shared_socket(host, port)
        if sock is not None:
            num_workers = self.config.inference_workers_config.num_workers
            self._process_managers[PROCESS_KEY_INFERENCE] = InferenceProcessManager(
                self.config, (host, port), sock, num_workers
            )
        else:
            logger.warning("Shared socket not available, inference workers disabled")

    def _start_processes(self, names: list[str]) -> None:
        """Start given process managers in order; sleep(2) after Scheduler."""
        for name in names:
            mgr = self._process_managers.get(name)
            if mgr is None:
                continue
            logger.info("Starting %s...", name)
            try:
                started = mgr.start()
                if not started:
                    logger.error("Failed to start %s", name)
                    continue
                logger.info("%s started successfully", name)
            except Exception as e:
                logger.error("Error starting %s: %s", name, e, exc_info=True)
                continue
            if name == PROCESS_KEY_SCHEDULER:
                time.sleep(2)

    def _stop_all_processes(self, exclude_processes: set[str] | None = None) -> None:
        """Stop in order: Infer -> Mgmt -> Scheduler. Skip specified processes when exclude is set."""
        exclude = exclude_processes or set()
        for name in STOP_ORDER:
            if name in exclude:
                logger.info("Skipping %s (excluded)", name)
                continue
            mgr = self._process_managers.get(name)
            if mgr is not None and hasattr(mgr, "stop"):
                logger.info("Stopping %s...", name)
                try:
                    mgr.stop()
                except Exception as e:
                    logger.error("Error stopping %s: %s", name, e)
        logger.info("All processes stopped")

    def _on_stop_signal(self) -> None:
        """Handle SIGTERM/SIGINT."""
        logger.info("Received stop signal")
        self._stop_event.set()

    def _write_role_shm_byte(self, byte_val: int) -> None:
        """Write role byte to shm for Mgmt readiness. No-op if no holder."""
        if self._role_shm_holder is None:
            return
        shm = self._role_shm_holder.get_shm()
        if shm is None:
            return
        try:
            shm.buf[0] = byte_val
            logger.debug("[Daemon] Role shm written: byte=%s", byte_val)
        except Exception as e:
            logger.warning("Failed to write role shm: %s", e)

    def _get_supervised_keys(self) -> set[str]:
        """Return process keys to supervise this round"""
        if not self.config.standby_config.enable_master_standby:
            return set(self._process_managers)
        if self._standby_manager is not None and self._standby_manager.is_master():
            return set(self._process_managers)
        return {PROCESS_KEY_SCHEDULER, PROCESS_KEY_MGMT, PROCESS_KEY_OBS}
