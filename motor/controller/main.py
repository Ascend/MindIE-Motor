# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import argparse
import select
import signal
import sys
import threading
from typing import Any

from motor.common.standby.standby_manager import CONTROLLER_REPORT_EVENT_KEY, StandbyManager
from motor.common.utils.config_runtime import log_configuration_summary, start_config_file_watcher
from motor.common.utils.config_watcher import ConfigWatcher
from motor.common.logger import get_logger, reconfigure_logging
from motor.config.controller import ControllerConfig
from motor.common.utils.port_allocator import apply_controller_ports, run_port_setup_or_exit
from motor.controller.api_server import ControllerAPI
from motor.controller.core import InstanceAssembler, InstanceManager, EventPusher


logger = get_logger(__name__)

# Global stop event for main loop
stop_event = threading.Event()

# Global modules dictionary
modules: dict[str, Any] = {}

# Global configuration
config: ControllerConfig | None = None

# Global config watcher
config_watcher: ConfigWatcher | None = None

# Global standby manager
standby_manager: StandbyManager | None = None

# Global previous fault tolerance state for change detection
previous_fault_tolerance_enabled: bool = False


def log_config_summary(message_prefix: str | None = None) -> None:
    """Log configuration summary with optional message prefix"""
    log_configuration_summary(config, message_prefix)


def on_config_updated() -> None:
    """Callback function called when configuration is updated"""
    global previous_fault_tolerance_enabled

    if config is None:
        logger.error("Configuration is None in config update callback")
        return

    # Check if fault tolerance configuration has changed
    current_fault_tolerance_enabled = config.fault_tolerance_config.enable_fault_tolerance

    if current_fault_tolerance_enabled != previous_fault_tolerance_enabled:
        if current_fault_tolerance_enabled:
            # Fault tolerance was enabled
            logger.info("Fault tolerance feature enabled, starting FaultManager...")
            try:
                from motor.controller.fault_tolerance import FaultManager

                fault_manager = FaultManager(config)
                modules["FaultManager"] = fault_manager

                # Attach to instance manager as observer
                instance_manager = modules.get("InstanceManager")
                if instance_manager is not None:
                    logger.info("Attaching FaultManager to instance manager")
                    instance_manager.attach(fault_manager)

                # Start the fault manager
                fault_manager.start()

                # Update fault manager with existing instances since it was restarted
                if instance_manager is not None:
                    active_instances = instance_manager.get_active_instances()
                    inactive_instances = instance_manager.get_inactive_instances()
                    all_instances = active_instances + inactive_instances
                    if all_instances:
                        logger.info(
                            "Updating FaultManager with %d existing instances (%d active, %d inactive)",
                            len(all_instances),
                            len(active_instances),
                            len(inactive_instances),
                        )
                        fault_manager.update_instances(all_instances)
            except Exception as e:
                logger.error("Failed to start FaultManager: %s", e)
        else:
            # Fault tolerance was disabled
            logger.info("Fault tolerance feature disabled, stopping FaultManager...")
            try:
                fault_manager = modules.get("FaultManager")
                if fault_manager is not None:
                    # Stop the fault manager
                    fault_manager.stop()
                    logger.info("FaultManager stopped successfully")

                    # Remove from modules
                    modules.pop("FaultManager", None)
                    logger.info("FaultManager removed from modules")
                else:
                    logger.warning("FaultManager not found in modules")
            except Exception as e:
                logger.error("Failed to stop FaultManager: %s", e)

        # Update previous state
        previous_fault_tolerance_enabled = current_fault_tolerance_enabled

    # Update configuration for all modules
    logger.info("Updating configuration for all modules...")
    for module_name, module in modules.items():
        if hasattr(module, 'update_config'):
            try:
                module.update_config(config)
                logger.info("Updated configuration for %s", module_name)
            except Exception as e:
                logger.error("Failed to update configuration for %s: %s", module_name, e)

    # Log configuration summary after reload
    log_config_summary("Configuration reloaded, printing updated summary:")


observers_list = {
    "EventPusher",
    "FaultManager",
}


def init_all_modules() -> None:
    """Initialize all modules but don't start them yet"""

    global config
    if config is None:
        config = ControllerConfig()

    modules["InstanceAssembler"] = InstanceAssembler(config)
    modules["EventPusher"] = EventPusher(config)
    if config.fault_tolerance_config.enable_fault_tolerance:
        from motor.controller.fault_tolerance import FaultManager

        modules["FaultManager"] = FaultManager(config)
    modules["InstanceManager"] = InstanceManager(config)
    if config.observability_config.observability_enable:
        from motor.controller.observability.observability import Observability

        modules["Observability"] = Observability(config)
    modules["ControllerAPI"] = ControllerAPI(config, modules)

    # Attach observers before starting modules
    instance_manager = modules.get("InstanceManager")
    if instance_manager is None:
        logger.error("InstanceManager not found in modules")
        return

    for module_name, module in modules.items():
        if module_name in observers_list:
            logger.info("Attaching %s to instance manager", module_name)
            instance_manager.attach(module)
    logger.info("All observers attached to instance manager")


def start_all_modules(exclude_modules: set[str] | None = None) -> None:
    """Start all modules, optionally excluding some modules"""
    if exclude_modules is None:
        exclude_modules = set()

    for module_name, module in modules.items():
        if module_name in exclude_modules:
            continue
        if hasattr(module, 'start'):
            try:
                logger.info("Starting %s", module_name)
                module.start()
            except Exception as e:
                logger.error("Error starting module %s: %s", module_name, e)
    logger.info("All modules started")


def stop_all_modules(exclude_modules: set[str] | None = None) -> None:
    """Stop all modules, optionally excluding some modules"""
    if exclude_modules is None:
        exclude_modules = set()

    for module_name, module in modules.items():
        if module_name in exclude_modules:
            continue
        if module is not None and hasattr(module, 'stop') and module.is_alive():
            try:
                module.stop()
            except Exception as e:
                logger.error("Error stopping module %s: %s", module_name, e)
    logger.info("All modules stopped.")


def on_become_master(should_report_event: bool) -> None:
    """Callback when becoming master - start all modules except ControllerAPI (which runs always)"""
    logger.info("Becoming master, starting all modules except ControllerAPI...")
    if not modules:  # Only initialize if not already initialized
        init_all_modules()
    # Start all modules except ControllerAPI, which should always be running
    start_all_modules(exclude_modules={"ControllerAPI"})

    if should_report_event:
        from motor.common.alarm.master_to_slave_event import (
            MasterToSlaveComponent,
            MasterToSlaveEvent,
            MasterToSlaveReason,
        )
        from motor.controller.observability.observability import Observability

        event = MasterToSlaveEvent(
            component=MasterToSlaveComponent.CONTROLLER,
            reason_id=MasterToSlaveReason.MASTER_COMPONENT_EXCEPTION,
        )
        Observability().add_alarm(event)
        logger.info("Reported ControllerToSlave event")


def on_become_standby() -> None:
    """Callback when becoming standby - stop all modules except ControllerAPI (which runs always)"""
    logger.info("Becoming standby, stopping all modules except ControllerAPI...")
    # Stop all modules except ControllerAPI, which should always be running
    stop_all_modules(exclude_modules={"ControllerAPI"})


def signal_handler(sig, frame) -> None:
    logger.warning("Receive signal %d, exit gracefully...", sig)
    stop_event.set()
    stop_all_modules()

    # Stop standby manager if it was started
    if standby_manager:
        logger.info("Stopping standby manager...")
        standby_manager.stop()
        logger.info("Standby manager stopped")

    # Stop config watcher
    if config_watcher:
        config_watcher.stop()


def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='Motor Controller')
    parser.add_argument(
        '--config', '-c', type=str, default=None, help='Path to configuration file (default: auto-detect)'
    )
    return parser.parse_args()


def main() -> None:
    global config, config_watcher, previous_fault_tolerance_enabled, standby_manager

    args = parse_arguments()

    if args.config:
        config = ControllerConfig.from_json(args.config)
        logger.info("Using configuration file: %s", args.config)
    else:
        # Read from environment variable
        config = ControllerConfig.from_json()
        logger.info("Using configuration from environment variable USER_CONFIG_PATH")

    reconfigure_logging(config.logging_config)

    run_port_setup_or_exit(apply_controller_ports, config)

    # Initialize previous fault tolerance state
    previous_fault_tolerance_enabled = config.fault_tolerance_config.enable_fault_tolerance

    # Log configuration summary
    log_config_summary()

    config_watcher = start_config_file_watcher(config, on_config_updated)

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start master/standby management if enabled
    if config.standby_config.enable_master_standby:
        # Initialize ControllerAPI first (it should always run)
        init_all_modules()
        # Start only ControllerAPI, other modules will be started when becoming master
        exclude_modules = {"InstanceManager", "InstanceAssembler", "EventPusher"}
        if config.fault_tolerance_config.enable_fault_tolerance:
            exclude_modules.add("FaultManager")
        if config.observability_config.observability_enable:
            exclude_modules.add("Observability")
        start_all_modules(exclude_modules=exclude_modules)

        # Get singleton instance and initialize/start it
        standby_manager = StandbyManager(config)
        standby_manager.start(
            on_become_master=on_become_master,
            on_become_standby=on_become_standby,
            report_event_key=CONTROLLER_REPORT_EVENT_KEY,
        )
        logger.info("Controller started in standby mode, waiting to become master...")
    else:
        logger.info("Master/standby feature is disabled, running in standalone mode")
        # Initialize and start all modules for standalone mode
        logger.info("Initializing all modules...")
        init_all_modules()
        logger.info("Starting all modules...")
        start_all_modules()

    logger.info("Press Ctrl+C or type 'stop' to exit.")
    try:
        while not stop_event.is_set():
            try:
                # Use select to make input non-blocking with timeout
                if select.select([sys.stdin], [], [], 1.0)[0]:
                    user_input = input().strip().lower()
                    if user_input == 'stop':
                        stop_event.set()
                        break
                    if user_input:
                        logger.error("Unknown command: %s", user_input)
            except EOFError:
                # In non-interactive environment, just continue
                pass
            except OSError:
                # select not available or stdin not available
                stop_event.wait(1)
    except KeyboardInterrupt:
        stop_event.set()

    # Cleanup
    stop_all_modules()

    if standby_manager is not None:
        # Stop standby manager if it was started
        standby_manager.stop()
        logger.info("Standby manager stopped")

    # Stop config watcher
    if config_watcher:
        config_watcher.stop()
        logger.info("Configuration file watcher stopped")

    logger.info("Controller shutdown complete")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logger.error("Unhandled exception: %s", e)
        sys.exit(0)
