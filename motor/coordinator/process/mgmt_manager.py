# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Mgmt process manager: run_mgmt_server_proc, MgmtProcessManager.
"""

import asyncio
import os
from multiprocessing.process import BaseProcess

try:
    import uvloop
except ImportError:
    uvloop = None

from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.api_server.management_server import ManagementServer
from motor.coordinator.process.base import BaseProcessManager
from motor.common.utils.config_watcher import ConfigWatcher
from motor.common.logger import get_logger, reconfigure_logging

logger = get_logger(__name__)


def run_mgmt_server_proc(
    config: CoordinatorConfig,
    daemon_pid: int | None = None,
) -> None:
    """Mgmt subprocess entry point. daemon_pid is passed by Daemon for orphan detection (no env var)."""
    reconfigure_logging(config.logging_config)

    try:
        import setproctitle

        setproctitle.setproctitle("MgmtServer")
    except ImportError:
        pass

    logger.info("Mgmt server process starting (PID: %s)", os.getpid())

    server = ManagementServer(config, daemon_pid=daemon_pid)
    mgmt_config_watcher = None

    if config.config_path and os.path.exists(config.config_path):
        try:

            def _mgmt_config_updated() -> None:
                server.update_config(config)

            mgmt_config_watcher = ConfigWatcher(
                config_path=config.config_path,
                reload_callback=config.reload,
                config_update_callback=_mgmt_config_updated,
            )
            mgmt_config_watcher.start()
            logger.info("Mgmt process: config watcher started for hot-reload: %s", config.config_path)
        except Exception as e:
            logger.warning("Mgmt process: failed to start config watcher (hot-reload disabled): %s", e)

    try:
        if uvloop is not None:
            uvloop.run(server.run())
        else:
            asyncio.run(server.run())
    finally:
        if mgmt_config_watcher is not None:
            try:
                mgmt_config_watcher.stop()
            except Exception as e:
                logger.warning("Failed to stop Mgmt config watcher during cleanup: %s", e)


class MgmtProcessManager(BaseProcessManager):
    """Single-process Mgmt manager. Daemon injects daemon_pid via set_daemon_pid() before start."""

    daemon_pid: int | None = None  # Set by CoordinatorDaemon via SupportsSpawnContext; passed as process arg

    def __init__(self, config: CoordinatorConfig):
        super().__init__(config, process_name="MgmtServer")

    def set_daemon_pid(self, daemon_pid: int | None) -> None:
        """Implement SupportsSpawnContext. Called by Daemon before start()."""
        self.daemon_pid = daemon_pid

    def _get_process_count(self) -> int:
        return 1

    def _create_process(self, index: int) -> BaseProcess:
        return self._spawn_context.Process(
            target=run_mgmt_server_proc,
            name="MgmtServer",
            args=(self.config, self.daemon_pid),
        )
