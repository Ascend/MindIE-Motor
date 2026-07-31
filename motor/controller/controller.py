# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Controller — master daemon that manages inference instances and fault tolerance."""

from motor.common.app.application import Application
from motor.common.logger import get_logger
from motor.common.standby.standby_manager import CONTROLLER_REPORT_EVENT_KEY, StandbyManager
from motor.config.controller import ControllerConfig
from motor.controller.api_server import ControllerAPI
from motor.controller.core import EventPusher, InstanceAssembler, InstanceManager

logger = get_logger(__name__)

_OBSERVER_NAMES = {"EventPusher", "FaultManager"}


class Controller(Application):
    """Orchestrates Controller modules — instance assembly, event pushing, fault tolerance.

    Supports two modes:

    * **standalone** — all modules start immediately.
    * **master/standby** — only ControllerAPI starts; other modules follow
      master/standby transitions via :class:`StandbyManager`.
    """

    def __init__(self, config: ControllerConfig) -> None:
        super().__init__(
            config,
            banner_label="controller",
            check_interval=config.daemon_loop_interval,
        )
        self._standby_manager: StandbyManager | None = None
        self._previous_fault_tolerance_enabled = config.fault_tolerance_config.enable_fault_tolerance
        self._standby_enabled = config.standby_config.enable_master_standby

    def _refresh_check_interval(self) -> None:
        self._check_interval = self.config.daemon_loop_interval

    # ------------------------------------------------------------------
    # modules
    # ------------------------------------------------------------------

    def init_modules(self) -> None:
        self.add_module("InstanceAssembler", InstanceAssembler(self.config))
        self.add_module("EventPusher", EventPusher(self.config))

        if self.config.fault_tolerance_config.enable_fault_tolerance:
            from motor.controller.fault_tolerance import FaultManager

            self.add_module("FaultManager", FaultManager(self.config))

        self.add_module("InstanceManager", InstanceManager(self.config))

        if self.config.observability_config.observability_enable:
            from motor.controller.observability.observability import Observability

            self.add_module("Observability", Observability(self.config))

        self.add_module("ControllerAPI", ControllerAPI(self.config, self.modules))

        # Attach observers to InstanceManager
        instance_manager = self.get_module("InstanceManager")
        if instance_manager is None:
            logger.error("InstanceManager not found in modules")
            return

        for name, module in self.modules.items():
            if name in _OBSERVER_NAMES:
                logger.info("Attaching %s to instance manager", name)
                instance_manager.attach(module)
        logger.info("All observers attached to instance manager")

    # ------------------------------------------------------------------
    # startup (override to support master/standby mode)
    # ------------------------------------------------------------------

    def _start_modules(self) -> None:
        self._start_config_watcher()

        if self._standby_enabled:
            self._start_standby_mode()
        else:
            logger.info("Master/standby feature is disabled, running in standalone mode")
            for name, module in self.modules.items():
                if hasattr(module, "start"):
                    try:
                        module.start()
                    except Exception:
                        logger.exception("Failed to start module %r", name)
            logger.info("All modules started")

    def _start_standby_mode(self) -> None:
        """Start only ControllerAPI, then start StandbyManager.

        Other modules are started/stopped by master/standby callbacks.
        """
        exclude = {"InstanceManager", "InstanceAssembler", "EventPusher"}
        if self.config.fault_tolerance_config.enable_fault_tolerance:
            exclude.add("FaultManager")
        if self.config.observability_config.observability_enable:
            exclude.add("Observability")

        for name, module in self.modules.items():
            if name in exclude:
                continue
            if hasattr(module, "start"):
                try:
                    logger.info("Starting %s (%s)", name, type(module).__name__)
                    module.start()
                except Exception:
                    logger.exception("Failed to start module %r", name)

        self._standby_manager = StandbyManager(self.config)
        self._standby_manager.start(
            on_become_master=self._on_become_master,
            on_become_standby=self._on_become_standby,
            report_event_key=CONTROLLER_REPORT_EVENT_KEY,
        )
        logger.info("Controller started in standby mode, waiting to become master...")

    # ------------------------------------------------------------------
    # master / standby transitions
    # ------------------------------------------------------------------

    def _on_become_master(self, should_report_event: bool) -> None:
        """Callback when becoming master — start all modules except ControllerAPI."""
        logger.info("Becoming master, starting all modules except ControllerAPI...")
        if not self.modules:
            self.init_modules()

        for name, module in self.modules.items():
            if name == "ControllerAPI":
                continue
            if hasattr(module, "start"):
                try:
                    logger.info("Starting %s (%s)", name, type(module).__name__)
                    module.start()
                except Exception:
                    logger.exception("Failed to start module %r", name)

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

    def _on_become_standby(self) -> None:
        """Callback when becoming standby — stop all modules except ControllerAPI."""
        logger.info("Becoming standby, stopping all modules except ControllerAPI...")
        for name, module in reversed(list(self.modules.items())):
            if name == "ControllerAPI":
                continue
            if module is not None and hasattr(module, "stop"):
                # Skip thread-based modules that are already dead
                if hasattr(module, "is_alive") and not module.is_alive():
                    continue
                try:
                    module.stop()
                except Exception:
                    logger.exception("Failed to stop module %r", name)

    # ------------------------------------------------------------------
    # config hot-reload (override for fault tolerance dynamic toggle)
    # ------------------------------------------------------------------

    def on_config_updated(self) -> None:
        """Handle fault tolerance on/off transitions, then propagate to all modules."""
        current = self.config.fault_tolerance_config.enable_fault_tolerance
        previous = self._previous_fault_tolerance_enabled

        if current != previous:
            if current:
                self._enable_fault_tolerance()
            else:
                self._disable_fault_tolerance()
            self._previous_fault_tolerance_enabled = current

        super().on_config_updated()

    def _enable_fault_tolerance(self) -> None:
        """Dynamically start FaultManager and attach to InstanceManager."""
        logger.info("Fault tolerance feature enabled, starting FaultManager...")
        try:
            from motor.controller.fault_tolerance import FaultManager

            fault_manager = FaultManager(self.config)
            self.add_module("FaultManager", fault_manager)

            instance_manager = self.get_module("InstanceManager")
            if instance_manager is not None:
                logger.info("Attaching FaultManager to instance manager")
                instance_manager.attach(fault_manager)

            fault_manager.start()

            if instance_manager is not None:
                active = instance_manager.get_active_instances()
                inactive = instance_manager.get_inactive_instances()
                all_instances = active + inactive
                if all_instances:
                    logger.info(
                        "Updating FaultManager with %d existing instances (%d active, %d inactive)",
                        len(all_instances),
                        len(active),
                        len(inactive),
                    )
                    fault_manager.update_instances(all_instances)
        except Exception:
            logger.exception("Failed to start FaultManager")

    def _disable_fault_tolerance(self) -> None:
        """Dynamically stop FaultManager and detach from InstanceManager."""
        logger.info("Fault tolerance feature disabled, stopping FaultManager...")
        try:
            fault_manager = self.modules.pop("FaultManager", None)
            if fault_manager is not None:
                fault_manager.stop()
                logger.info("FaultManager stopped and removed from modules")
            else:
                logger.warning("FaultManager not found in modules")
        except Exception:
            logger.exception("Failed to stop FaultManager")

    # ------------------------------------------------------------------
    # lifecycle overrides
    # ------------------------------------------------------------------

    @property
    def exit_code(self) -> int:
        return 0

    def shutdown(self) -> None:
        logger.info("Shutting down Controller...")
        if self._standby_manager is not None:
            logger.info("Stopping standby manager...")
            self._standby_manager.stop()
            logger.info("Standby manager stopped")
        super().shutdown()
        logger.info("Controller shutdown complete")
