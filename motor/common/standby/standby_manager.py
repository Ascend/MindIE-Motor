# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import threading
import time
from typing import Callable, Protocol
from enum import Enum

from motor.common.logger import get_logger
from motor.common.etcd.etcd_client import EtcdClient
from motor.config.standby import StandbyConfig
from motor.common.utils.singleton import ThreadSafeSingleton


logger = get_logger(__name__)

CONTROLLER_REPORT_EVENT_KEY = "/controller/should_report_event"
COORDINATOR_REPORT_EVENT_KEY = "/coordinator/should_report_event"


class StandbyConfigProvider(Protocol):
    """Protocol for configuration objects that provide standby configuration"""

    standby_config: StandbyConfig


class StandbyRole(Enum):
    STANDBY = "standby"
    MASTER = "master"


class StandbyManager(ThreadSafeSingleton):
    """Master/standby management class"""

    def __init__(self, config: StandbyConfigProvider | None = None):
        # Prevent re-initialization for singleton
        if hasattr(self, '_initialized'):
            return

        # First time initialization must have config
        if config is None:
            raise ValueError("config must be provided for first initialization of StandbyManager singleton")
        self.config = config
        self.etcd_client = EtcdClient(etcd_config=config.etcd_config, tls_config=config.etcd_tls_config)
        standby_config = config.standby_config
        self.current_role = StandbyRole.STANDBY
        self.role_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.stanyby_loop_thread: threading.Thread | None = None
        self.is_running = False
        self.has_set_role = False

        # Enhanced lock configuration for better reliability (loaded from config)
        self.lock_ttl = standby_config.master_lock_ttl
        self.lock_retry_interval = standby_config.master_lock_retry_interval
        self.max_lock_failures = standby_config.master_lock_max_failures

        # Callbacks (on_become_master receives should_report_event from etcd)
        self.on_become_master: Callable[[bool], None] | None = None
        self.on_become_standby: Callable[[], None] | None = None
        self.report_event_key = CONTROLLER_REPORT_EVENT_KEY

        self.stanyby_loop_thread = threading.Thread(
            target=self._master_standby_loop, name="MasterStandbyManager", daemon=False
        )

        self._initialized = True

    @classmethod
    def is_initialized(cls) -> bool:
        """Check if the singleton instance has been initialized"""
        instance = cls._instances.get(cls)
        return instance is not None and hasattr(instance, '_initialized')

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton instance, removing it from the instances dict"""
        with cls._lock:
            if cls in cls._instances:
                del cls._instances[cls]
                logger.info("StandbyManager singleton instance has been reset")

    def start(
        self,
        on_become_master: Callable[[bool], None],
        on_become_standby: Callable[[], None],
        report_event_key: str = CONTROLLER_REPORT_EVENT_KEY,
    ) -> None:
        """Start the master/standby management thread."""
        if self.is_running:
            logger.warning("Master/standby manager is already running")
            return

        # Set callbacks
        self.on_become_master = on_become_master
        self.on_become_standby = on_become_standby
        self.report_event_key = report_event_key

        # Reset stop_event if it was previously set (for singleton reuse)
        if self.stop_event.is_set():
            self.stop_event.clear()

        # Start the pre-created thread
        self.stanyby_loop_thread.start()
        self.is_running = True
        logger.info("Master/standby manager started")

    def stop(self) -> None:
        """Stop the master/standby management thread"""
        if not self.is_running:
            return

        logger.info("Stopping master/standby manager...")
        self.stop_event.set()

        if self.stanyby_loop_thread and self.stanyby_loop_thread.is_alive():
            self.stanyby_loop_thread.join(timeout=30)
            if self.stanyby_loop_thread.is_alive():
                logger.warning("Master/standby manager thread did not finish within timeout")
            else:
                logger.info("Master/standby manager thread stopped successfully")

        self.is_running = False

        # Close etcd client
        if hasattr(self, 'etcd_client'):
            self.etcd_client.close()

        # Reset callbacks for singleton reuse (but keep stop_event set)
        self.on_become_master = None
        self.on_become_standby = None

    def is_master(self) -> bool:
        """Check if current pod is master"""
        with self.role_lock:
            return self.current_role == StandbyRole.MASTER

    def set_role(self, role: StandbyRole) -> None:
        """Set current pod role"""
        with self.role_lock:
            if self.current_role != role:
                old_role = self.current_role
                self.current_role = role
                logger.info(
                    "[Standby] Role changed from %s to %s",
                    old_role.value,
                    role.value,
                )

    def _master_standby_loop(self) -> None:
        """Master/standby management loop"""

        while not self.stop_event.is_set():
            try:
                if self.is_master():
                    # As master, renew lock
                    if not self._renew_master_lock():
                        logger.warning("Failed to renew master lock, becoming standby")
                        self.set_role(StandbyRole.STANDBY)
                        if self.on_become_standby:
                            self.on_become_standby()
                        continue
                else:
                    # As standby, try to become master
                    if self._try_become_master():
                        logger.info("Became master, starting modules")
                        if self.on_become_master:
                            self.on_become_master(self.etcd_client.get_bool(key=self.report_event_key))
                            self.etcd_client.set_bool(key=self.report_event_key, value=True)
                    self.has_set_role = True

            except Exception as e:
                logger.error("Error in master/standby manager: %s", e)

            time.sleep(self.config.standby_config.master_standby_check_interval)

        # Thread is stopping, release master lock if we hold it and update role shm
        if self.is_master():
            self._release_master_lock()
            self.set_role(StandbyRole.STANDBY)
            logger.info("Master/standby manager thread stopped and released lock")

    def _renew_master_lock(self) -> bool:
        """Renew master lock lease with retry on transient failures.

        Mirrors the retry logic in _try_become_master() so that *keeping*
        the master role is at least as resilient as *acquiring* it.
        With TTL=15s, a 3s keepalive timeout, and up to 3 attempts
        (2 retries × 1s gaps), the worst-case renewal sequence is
        3×3s + 2×1s = 11s < 15s TTL, so the lease never expires
        while retries are in flight.
        """
        if not self.is_master():
            return False

        max_retries = 2  # 3 attempts total
        retry_delay = 1  # seconds between retries

        for attempt in range(max_retries + 1):
            try:
                if self.etcd_client.renew_lease(self.config.standby_config.master_lock_key):
                    return True
            except Exception as e:
                logger.error(
                    "Error renewing master lock (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries + 1,
                    e,
                )

            if attempt < max_retries:
                time.sleep(retry_delay)

        logger.warning(
            "Failed to renew master lock after %d attempts, becoming standby",
            max_retries + 1,
        )
        return False

    def _release_master_lock(self) -> None:
        """Release master lock"""
        try:
            self.etcd_client.release_lock(self.config.standby_config.master_lock_key)
            logger.info("Released master lock")
        except Exception as e:
            logger.error(f"Error releasing master lock: {e}")

    def _try_become_master(self) -> bool:
        """Try to become master pod using ETCD lock with enhanced reliability"""
        consecutive_failures = 0

        while consecutive_failures < self.max_lock_failures and not self.stop_event.is_set():
            try:
                # Try to acquire master lock with increased TTL
                lease_id = self.etcd_client.acquire_lock(
                    lock_key=self.config.standby_config.master_lock_key, ttl=self.lock_ttl
                )
                if lease_id:
                    self.set_role(StandbyRole.MASTER)
                    logger.info("Successfully became master with TTL %ds", self.lock_ttl)
                    return True
                else:
                    # Lock is held by another pod, remain standby
                    self.set_role(StandbyRole.STANDBY)
                    return False

            except Exception as e:
                consecutive_failures += 1
                logger.warning(
                    "Failed to acquire master lock (attempt %d/%d): %s", consecutive_failures, self.max_lock_failures, e
                )

                if consecutive_failures < self.max_lock_failures:
                    # Wait before retrying, but allow interruption by stop_event
                    if self.stop_event.wait(self.lock_retry_interval):
                        # stop_event was set, exit the loop
                        break
                else:
                    logger.error("Max consecutive failures reached, giving up master acquisition")
                    self.set_role(StandbyRole.STANDBY)
                    return False

        return False
