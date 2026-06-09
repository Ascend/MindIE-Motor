# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# WITHOUT WARRANTIES OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Scheduler process manager.
Starts and manages the Scheduler process from the CoordinatorDaemon (daemon process).
"""

import os
import asyncio
from multiprocessing.process import BaseProcess

try:
    import uvloop
except ImportError:
    uvloop = None

from motor.coordinator.process.base import BaseProcessManager
from motor.coordinator.scheduler.runtime.scheduler_server import SchedulerServer
from motor.config.coordinator import CoordinatorConfig, DEFAULT_SCHEDULER_PROCESS_CONFIG
from motor.common.utils.config_watcher import ConfigWatcher
from motor.common.logger import get_logger, reconfigure_logging

logger = get_logger(__name__)


def run_scheduler_server_proc(config: CoordinatorConfig) -> None:
    """Scheduler server process entry (uses zmq.asyncio)."""
    # Reconfigure logging so this child process writes to the same log_file as daemon and Worker processes
    reconfigure_logging(config.logging_config)

    # Set process title
    try:
        import setproctitle

        setproctitle.setproctitle("SchedulerServer")
    except ImportError:
        pass

    logger.info("Scheduler server process starting (PID: %s)", os.getpid())

    # Create and start async Scheduler server (first version uses default config)
    server = SchedulerServer(config=config, frontend_address=DEFAULT_SCHEDULER_PROCESS_CONFIG.frontend_address)
    scheduler_config_watcher = None

    # Scheduler process watches config file for hot-reload (config.reload updates in-memory config).
    if config.config_path and os.path.exists(config.config_path):
        try:
            scheduler_config_watcher = ConfigWatcher(
                config_path=config.config_path,
                reload_callback=config.reload,
                config_update_callback=None,
            )
            scheduler_config_watcher.start()
            logger.info(
                "Scheduler process: config watcher started for hot-reload: %s",
                config.config_path,
            )
        except Exception as e:
            logger.warning(
                "Scheduler process: failed to start config watcher (hot-reload disabled): %s",
                e,
            )

    try:
        # Run async server
        if uvloop is not None:
            uvloop.run(server.start())
        else:
            asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Scheduler server process received interrupt signal")
    except Exception as e:
        logger.error("Scheduler server process error: %s", e, exc_info=True)
        raise
    finally:
        if scheduler_config_watcher is not None:
            try:
                scheduler_config_watcher.stop()
            except Exception as e:
                logger.warning("Failed to stop scheduler config watcher during cleanup: %s", e, exc_info=True)
        asyncio.run(server.stop())
        logger.info("Scheduler server process stopped")


class SchedulerProcessManager(BaseProcessManager):
    """
    Manages the lifecycle of the standalone Scheduler process.
    """

    def __init__(self, config: CoordinatorConfig):
        super().__init__(config, process_name="SchedulerServer")
        self.scheduler_config = DEFAULT_SCHEDULER_PROCESS_CONFIG

    def wait_for_completion(self) -> None:
        """Wait for the Scheduler process to complete (for monitoring)."""
        for proc in self._processes:
            try:
                proc.join()
                if proc.exitcode != 0:
                    logger.error("Scheduler process exited with code %s", proc.exitcode)
            except Exception as e:
                logger.error("Error waiting for Scheduler process: %s", e, exc_info=True)

    def _get_process_count(self) -> int:
        return 1

    def _create_process(self, index: int) -> BaseProcess:
        return self._spawn_context.Process(
            target=run_scheduler_server_proc,
            name="SchedulerServer",
            args=(self.config,),
        )
