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
Probe: Daemon liveness + role from role shm; liveness/readiness decision (no HTTP).
"""

import asyncio
import os
import struct
import time
from dataclasses import dataclass
from enum import Enum
from multiprocessing import shared_memory as shm_mod
from typing import Protocol

from motor.common.logger import get_logger
from motor.config.coordinator import (
    ROLE_HEARTBEAT_STALE_SEC,
    ROLE_SHM_MASTER,
    ROLE_SHM_NAME,
    ROLE_SHM_SIZE,
)
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.domain.scheduling import InstanceReadiness

logger = get_logger(__name__)


def is_master_from_role_shm(shm_name: str = ROLE_SHM_NAME) -> bool:
    """Read role byte from Daemon-owned role shm."""
    try:
        shm = shm_mod.SharedMemory(name=shm_name, create=False)
        try:
            return shm.buf[0] == ROLE_SHM_MASTER
        finally:
            shm.close()
    except (OSError, IndexError) as e:
        logger.warning("Role shm read failed (name=%s): %s", shm_name, e)
        return False


@dataclass(frozen=True)
class RoleHeartbeatResult:
    """Result of reading role shm: master/standby and Daemon heartbeat liveness."""

    is_master: bool
    heartbeat_stale: bool
    orphaned: bool  # True if Mgmt is orphaned (parent != Daemon)


class LivenessResult(str, Enum):
    """Liveness probe decision. Route layer maps to HTTP 200/503."""

    OK = "ok"
    DAEMON_EXITED = "daemon_exited"  # orphaned
    HEARTBEAT_STALE = "heartbeat_stale"


class ReadinessResult(str, Enum):
    """Readiness probe decision. Route layer maps to HTTP 200/503."""

    OK_MASTER = "ok_master"
    OK_STANDBY = "ok_standby"
    NOT_MASTER = "not_master"
    HEARTBEAT_STALE = "heartbeat_stale"
    DAEMON_EXITED = "daemon_exited"


@dataclass
class ReadinessProbeOutput:
    """Output of ReadinessProbe.check() for route to build response."""

    result: ReadinessResult
    is_ready: bool  # for response body "ready"
    instance_readiness: InstanceReadiness | None  # for logging/debug


class DaemonLivenessProvider(Protocol):
    """Provides Daemon liveness and master/standby role from role shm. No HTTP."""

    def read_role_and_heartbeat(self) -> RoleHeartbeatResult:
        """
        Read role shm (and optional orphan check). Never raises.
        - orphaned=True: parent is not Daemon (getppid != daemon_pid).
        - heartbeat_stale=True: heartbeat bytes older than threshold.
        - is_master: byte0 == ROLE_SHM_MASTER.
        """
        ...


class RoleShmDaemonLivenessProvider(DaemonLivenessProvider):
    """Reads role shm and heartbeat; manages shm lifecycle internally. Never raises."""

    def __init__(
        self,
        daemon_pid: int | None,
        role_shm_name: str = ROLE_SHM_NAME,
        stale_sec: float = ROLE_HEARTBEAT_STALE_SEC,
    ):
        self._daemon_pid = daemon_pid
        self._role_shm_name = role_shm_name
        self._stale_sec = stale_sec

    def read_role_and_heartbeat(self) -> RoleHeartbeatResult:
        if self._daemon_pid:
            ppid = os.getppid()
            if ppid != self._daemon_pid:
                logger.warning("Mgmt orphaned (parent not Daemon): ppid=%s daemon_pid=%s", ppid, self._daemon_pid)
                return RoleHeartbeatResult(is_master=False, heartbeat_stale=False, orphaned=True)
            logger.debug(
                "Mgmt parent check: ppid=%s daemon_pid=%s role_shm=%s", ppid, self._daemon_pid, self._role_shm_name
            )

        try:
            shm = shm_mod.SharedMemory(name=self._role_shm_name, create=False)
        except FileNotFoundError:
            logger.warning(
                "Role shm not found: name=%s (Daemon may not have created it yet) -> standby", self._role_shm_name
            )
        except Exception as e:
            logger.warning("Read role shm %s failed: %s, treat as standby", self._role_shm_name, e)
        else:
            try:
                is_master = shm.buf[0] == ROLE_SHM_MASTER
                heartbeat_stale = self._is_heartbeat_stale(shm.buf)
                logger.debug(
                    "Role shm read: name=%s byte=%s is_master=%s heartbeat_stale=%s",
                    self._role_shm_name,
                    shm.buf[0],
                    is_master,
                    heartbeat_stale,
                )
                return RoleHeartbeatResult(is_master=is_master, heartbeat_stale=heartbeat_stale, orphaned=False)
            finally:
                shm.close()

        return RoleHeartbeatResult(is_master=False, heartbeat_stale=False, orphaned=False)

    def _is_heartbeat_stale(self, shm_buf: memoryview) -> bool:
        """True if heartbeat bytes (1..ROLE_SHM_SIZE-1) are older than _stale_sec."""
        if len(shm_buf) < ROLE_SHM_SIZE:
            logger.warning("Role shm size=%s, need %s for heartbeat", len(shm_buf), ROLE_SHM_SIZE)
            logger.warning("Set role_heartbeat_interval_sec>0 and restart Daemon.")
            return False
        try:
            heartbeat_ns = struct.unpack("<Q", bytes(shm_buf[1:ROLE_SHM_SIZE]))[0]
        except (struct.error, IndexError, ValueError) as e:
            logger.debug("Heartbeat parse error: %s, not treating as stale", e)
            return False
        if heartbeat_ns == 0:
            return False
        now_ns = time.monotonic_ns()
        age_ns = now_ns - heartbeat_ns
        stale_threshold_ns = int(self._stale_sec * 1e9)
        if age_ns < 0:
            return False
        if age_ns <= stale_threshold_ns:
            logger.debug("Heartbeat OK: age_sec=%.1f stale_sec=%.1f", age_ns / 1e9, self._stale_sec)
            return False
        logger.warning(
            "Daemon heartbeat stale: last_ns=%s age_sec=%.1f stale_sec=%.1f",
            heartbeat_ns,
            age_ns / 1e9,
            self._stale_sec,
        )
        return True


class LivenessProbe:
    """Liveness decision only. No HTTP."""

    def __init__(self, provider: DaemonLivenessProvider):
        self._provider = provider

    def check(self) -> LivenessResult:
        r = self._provider.read_role_and_heartbeat()
        if r.orphaned:
            return LivenessResult.DAEMON_EXITED
        if r.heartbeat_stale:
            return LivenessResult.HEARTBEAT_STALE
        return LivenessResult.OK


class ReadinessProbe:
    """Readiness decision only. No HTTP."""

    def __init__(
        self,
        daemon_liveness: DaemonLivenessProvider,
        instance_manager: InstanceManager,
        enable_master_standby: bool,
    ):
        self._daemon = daemon_liveness
        self._instance_manager = instance_manager
        self._enable_master_standby = enable_master_standby

    @property
    def instance_manager(self) -> InstanceManager:
        """Public accessor for tests and for G.CLS.11 (avoid protected access)."""
        return self._instance_manager

    @instance_manager.setter
    def instance_manager(self, value: InstanceManager) -> None:
        """Allow callers (e.g. ManagementServer tests) to inject a custom instance manager."""
        self._instance_manager = value

    async def check(self) -> ReadinessProbeOutput:
        readiness = await asyncio.to_thread(
            self._instance_manager.get_required_instances_status,
        )
        is_run = readiness.is_run()

        r = self._daemon.read_role_and_heartbeat()
        if r.orphaned:
            result = ReadinessResult.DAEMON_EXITED
        elif r.heartbeat_stale:
            result = ReadinessResult.HEARTBEAT_STALE
        elif self._enable_master_standby:
            result = ReadinessResult.OK_MASTER if r.is_master else ReadinessResult.NOT_MASTER
        else:
            result = ReadinessResult.OK_STANDBY
        # Only report ready when result is OK_*; otherwise force False (orphaned/heartbeat_stale/not_master).
        out_ready = (result in (ReadinessResult.OK_MASTER, ReadinessResult.OK_STANDBY)) and is_run
        return ReadinessProbeOutput(result, out_ready, readiness)
