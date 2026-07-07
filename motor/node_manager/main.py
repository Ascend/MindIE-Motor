# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import signal
import time
import sys

from motor.common.utils.process_utils import set_process_title
from motor.common.logger import get_logger, reconfigure_logging
from motor.node_manager.api_server.node_manager_api import NodeManagerAPI
from motor.config.node_manager import NodeManagerConfig
from motor.common.utils.port_allocator import apply_node_manager_ports, run_port_setup_or_exit
from motor.node_manager.core.daemon import Daemon
from motor.node_manager.core.engine_manager import EngineManager
from motor.node_manager.core.heartbeat_manager import HeartbeatManager
from motor.common.utils.config_runtime import log_configuration_summary, start_config_file_watcher
from motor.common.utils.config_watcher import ConfigWatcher
from motor.common.utils.env import Env

set_process_title("NodeManager")

logger = get_logger(__name__)


modules = []
_should_exit = False

# Global configuration
config: NodeManagerConfig | None = None

# Global config watcher
config_watcher: ConfigWatcher | None = None


def log_config_summary(message_prefix: str | None = None) -> None:
    """Log configuration summary with optional message prefix"""
    log_configuration_summary(config, message_prefix)


def on_config_updated() -> None:
    """Callback function called when configuration is updated"""
    logger.info("Configuration reloaded, printing updated summary:")
    log_config_summary()

    for module in modules:
        if hasattr(module, 'update_config'):
            try:
                module.update_config(config)
                logger.info("Updated configuration for %s", type(module).__name__)
            except Exception as e:
                logger.error("Failed to update configuration for %s: %s", type(module).__name__, e)


def init_all_modules(config_path: str | None = None) -> None:
    """Initialize all modules but don't start them yet"""

    global config
    if config is None:
        config = NodeManagerConfig.from_json(config_path)
        reconfigure_logging(config.logging_config)
        run_port_setup_or_exit(apply_node_manager_ports, config)

    modules.append(config)
    modules.append(NodeManagerAPI(config=config))
    modules.append(Daemon(config))
    modules.append(EngineManager(config))
    modules.append(HeartbeatManager(config))
    logger.info("All modules initialized")


def stop_all_modules() -> None:
    while modules:
        module = modules.pop()
        if hasattr(module, 'stop'):
            try:
                module.stop()
            except Exception as e:
                logger.error("Failed to stop %s: %s", type(module).__name__, e)
    logger.info("All modules stopped.")


def signal_handler(sig, frame) -> None:
    global _should_exit
    if _should_exit:
        return
    _should_exit = True
    logger.info("\nReceive signal %s,exit gracefully...", sig)

    # Stop config watcher
    if config_watcher:
        config_watcher.stop()

    stop_all_modules()


def suicide_procedure() -> None:
    """
    Suicide procedure: stop all node_manager modules, kill engine servers,
    and exit the program with return code -1.
    """
    logger.error("Starting suicide procedure...")

    if config_watcher:
        try:
            config_watcher.stop()
            logger.info("Config watcher stopped")
        except Exception as e:
            logger.error("Failed to stop config watcher: %s", e)

    # Stop all other modules
    stop_all_modules()


def main() -> int:
    global _should_exit, config_watcher

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C
    signal.signal(signal.SIGTERM, signal_handler)  # kill

    # Initialize all modules
    # Prefer mounted user_config when provided, fallback to CONFIG_PATH
    config_path = Env.user_config_path or Env.config_path
    init_all_modules(config_path)

    # Log configuration summary
    log_config_summary()

    # Start configuration file watcher
    # Disabled when container snapshot is enabled, container snapshot does not support inotify ops
    if config.snapshot_config.enable_snapshot:
        logger.info("[snapshot] Snapshot enabled, configuration file watcher disabled")
    else:
        config_watcher = start_config_file_watcher(config, on_config_updated)

    logger.info("All modules started, monitoring...")

    logger.info("Press Ctrl+C or type 'stop' to exit.")
    try:
        while not _should_exit:
            # Check if suicide is needed
            if HeartbeatManager().should_suicide():
                logger.error("Detected suicide flag from HeartbeatManager")
                suicide_procedure()
                return -1

            try:
                user_input = input().strip().lower()
                if user_input == 'stop':
                    _should_exit = True
                elif user_input:
                    logger.warning("Unknown command: %s", user_input)
            except EOFError:
                if not _should_exit:
                    time.sleep(1)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received, shutting down...")
        _should_exit = True
    finally:
        # Stop config watcher
        if config_watcher:
            config_watcher.stop()
            logger.info("Configuration file watcher stopped")

        stop_all_modules()

    # -1: rescheduling; 0: restart
    return -1


if __name__ == '__main__':
    exit_code = main()
    logger.info("exit_code: %s", exit_code)
    sys.exit(exit_code)
