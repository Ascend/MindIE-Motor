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
Observability process manager: run_obs_server_proc, ObsProcessManager.
"""

import os
from multiprocessing.process import BaseProcess

import uvloop

from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.api_server.observability_server import ObservabilityServer
from motor.coordinator.metrics.metrics_collector import MetricsCollector
from motor.coordinator.process.base import BaseProcessManager
from motor.common.utils.config_watcher import ConfigWatcher
from motor.common.logger import get_logger, reconfigure_logging

logger = get_logger(__name__)


def run_obs_server_proc(
    config: CoordinatorConfig,
    daemon_pid: int | None = None,
) -> None:
    """Observability subprocess entry point."""
    reconfigure_logging(config.logging_config)

    try:
        import setproctitle

        setproctitle.setproctitle("ObsServer")
    except ImportError:
        pass

    logger.info("Observability server process starting (PID: %s)", os.getpid())

    # Initialize MetricsCollector singleton (used by /metrics endpoint)
    MetricsCollector(config)

    server = ObservabilityServer(config, daemon_pid=daemon_pid)
    obs_config_watcher = None

    if config.config_path and os.path.exists(config.config_path):
        try:

            def _obs_config_updated() -> None:
                server.update_config(config)
                MetricsCollector().update_config(config)

            obs_config_watcher = ConfigWatcher(
                config_path=config.config_path,
                reload_callback=config.reload,
                config_update_callback=_obs_config_updated,
            )
            obs_config_watcher.start()
            logger.info("Obs process: config watcher started for hot-reload: %s", config.config_path)
        except Exception as e:
            logger.warning("Obs process: failed to start config watcher (hot-reload disabled): %s", e)

    try:
        uvloop.run(server.run())
    finally:
        if obs_config_watcher is not None:
            try:
                obs_config_watcher.stop()
            except Exception as e:
                logger.warning("Failed to stop Obs config watcher during cleanup: %s", e)


class ObsProcessManager(BaseProcessManager):
    """Observability process manager.  Daemon injects daemon_pid via set_daemon_pid() before start."""

    daemon_pid: int | None = None

    def __init__(self, config: CoordinatorConfig):
        super().__init__(config, process_name="ObsServer")

    def set_daemon_pid(self, daemon_pid: int | None) -> None:
        self.daemon_pid = daemon_pid

    def _get_process_count(self) -> int:
        return 1

    def _create_process(self, index: int) -> BaseProcess:
        return self._spawn_context.Process(
            target=run_obs_server_proc,
            name="ObsServer",
            args=(self.config, self.daemon_pid),
        )
