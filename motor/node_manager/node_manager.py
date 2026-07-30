# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""NodeManager — daemon application that orchestrates engine and KV-store services."""

from motor.common.app import Application
from motor.common.logger import get_logger
from motor.common.utils.env import Env
from motor.config.node_manager import NodeManagerConfig
from motor.node_manager.api_server.node_manager_api import NodeManagerAPI
from motor.node_manager.core.daemon import Daemon
from motor.node_manager.core.engine_manager import EngineManager
from motor.node_manager.core.heartbeat_manager import HeartbeatManager

logger = get_logger(__name__)


class NodeManager(Application):
    """Orchestrates NodeManager modules and daemon lifecycle.

    Module set depends on the active service profile:
    * engine active  → EngineManager + HeartbeatManager (registration, heartbeats)
    * engine absent  → only Daemon + NodeManagerAPI (KV-only pod)
    """

    def __init__(self, config: NodeManagerConfig) -> None:
        role = f"NodeManager.{Env.role}" if Env.role else "NodeManager"
        super().__init__(
            config,
            banner_label=role,
            check_interval=config.basic_config.daemon_loop_interval,
        )

    # ------------------------------------------------------------------
    # modules
    # ------------------------------------------------------------------

    def init_modules(self) -> None:
        daemon = Daemon(self.config)
        self.add_module("Daemon", daemon)
        self.add_module("NodeManagerAPI", NodeManagerAPI(config=self.config))

        if daemon.has_engine:
            self.add_module("EngineManager", EngineManager(self.config))
            self.add_module("HeartbeatManager", HeartbeatManager(self.config))

        logger.info(
            "All modules initialized (has_engine=%s, services=%d)",
            daemon.has_engine,
            len(self.modules),
        )

    @property
    def daemon(self) -> Daemon:
        return self.get_module("Daemon")  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # lifecycle overrides
    # ------------------------------------------------------------------

    def _refresh_check_interval(self) -> None:
        self._check_interval = self.config.basic_config.daemon_loop_interval

    def _start_modules(self) -> None:
        self._start_config_watcher()

    def _start_config_watcher(self) -> None:
        # Config hot-reload is disabled when snapshot is enabled — snapshot
        # restore does not support inotify operations.
        if self.config.snapshot_config.enable_snapshot:
            logger.info("[snapshot] Snapshot enabled, configuration file watcher disabled")
            return
        super()._start_config_watcher()

    def _on_daemon_tick(self) -> None:
        if self._check_suicide():
            logger.error("Detected suicide flag from HeartbeatManager")
            self.stop_event.set()

    def shutdown(self) -> None:
        logger.info("Shutting down NodeManager...")
        super().shutdown()
        logger.info("NodeManager shutdown complete (exit_code=%d)", self.exit_code)

    # ------------------------------------------------------------------
    # suicide
    # ------------------------------------------------------------------

    def _check_suicide(self) -> bool:
        """True when HeartbeatManager requests pod rescheduling."""
        hb = self.get_module("HeartbeatManager")
        if hb is None:
            return False
        return hb.should_suicide()
