# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Base class for long-running daemon applications (Controller, NodeManager, etc.).

Provides module lifecycle management, config hot-reload propagation, signal
handling, and a select-based daemon loop.  Subclasses implement
:meth:`init_modules` and optionally override :meth:`run`.
"""

import asyncio
import inspect
import select
import signal
import sys
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from motor.common.logger import get_logger
from motor.common.utils.config_runtime import (
    log_configuration_summary,
    start_config_file_watcher,
)
from motor.common.utils.config_watcher import ConfigWatcher
from motor.common.utils.startup_banner import log_startup_banner

logger = get_logger(__name__)


class Application(ABC):
    """Base class for daemon applications.

    Manages a named collection of modules with consistent start/stop/config-update
    semantics, signal handlers, and a select-based daemon loop.

    Subclasses override :meth:`init_modules` to register their modules via
    :meth:`add_module`, then call :meth:`run`.

    Parameters
    ----------
    check_interval:
        Seconds between daemon loop ticks (health checks, suicide poll, etc.).
        Subclasses may override via their own config.
    """

    def __init__(
        self,
        config: Any,
        *,
        banner_label: str = "",
        check_interval: float = 1.0,
    ) -> None:
        self.config = config
        self._banner_label = banner_label
        self._check_interval = check_interval
        self.modules: dict[str, Any] = {}
        self.stop_event = threading.Event()
        self._config_watcher: ConfigWatcher | None = None

    # ------------------------------------------------------------------
    # module management
    # ------------------------------------------------------------------

    @abstractmethod
    def init_modules(self) -> None:
        """Create and register all application modules via :meth:`add_module`."""
        ...

    def add_module(self, name: str, module: Any) -> None:
        self.modules[name] = module

    def get_module(self, name: str) -> Any | None:
        return self.modules.get(name)

    def _start_modules(self) -> None:
        """Hook called after :meth:`init_modules`.  Subclasses may override to
        start modules that do not auto-start in ``__init__``.

        The default implementation is a no-op — most modules start themselves
        in ``__init__`` (e.g. threads, API servers) or are started later by
        an external trigger (e.g. API handler).  Blindly calling ``start()``
        on every module can cause premature heartbeat / fault reporter
        activation before endpoints are configured.
        """

    def stop_all_modules(self) -> None:
        """Stop all modules in reverse registration order."""
        for name, module in reversed(list(self.modules.items())):
            if hasattr(module, "stop"):
                try:
                    result = module.stop()
                    # Modules may declare async stop() (e.g. the FastAPI
                    # server); the daemon main loop is synchronous, so run
                    # the coroutine to completion instead of dropping it.
                    if inspect.iscoroutine(result):
                        asyncio.run(result)
                except Exception:
                    logger.exception("Failed to stop module %r", name)
        self.modules.clear()
        logger.info("All modules stopped.")

    # ------------------------------------------------------------------
    # config hot-reload
    # ------------------------------------------------------------------

    def _start_config_watcher(self) -> None:
        """Start configuration file watcher (no-op if config path unavailable)."""
        if self._config_watcher is not None:
            return
        self._config_watcher = start_config_file_watcher(self.config, self.on_config_updated)
        if self._config_watcher is not None:
            logger.info("Configuration file watcher started")

    def on_config_updated(self) -> None:
        """Callback invoked by ConfigWatcher when the config file changes.

        1. Refresh the daemon loop check interval from the new config.
        2. Propagate the update to every module that has ``update_config``.
        3. Log the updated configuration summary.

        Subclasses may override to add custom logic before or after propagation.
        """
        self._refresh_check_interval()
        for name, module in self.modules.items():
            if hasattr(module, "update_config"):
                try:
                    module.update_config(self.config)
                    logger.info("Updated configuration for %s (%s)", name, type(module).__name__)
                except Exception:
                    logger.exception("Failed to update configuration for %r", name)
        log_configuration_summary(self.config, "Configuration reloaded, printing updated summary:")

    def _refresh_check_interval(self) -> None:
        """Update :attr:`_check_interval` from config after hot-reload.

        Subclasses override to match their config layout, e.g.::

            self._check_interval = self.config.basic_config.daemon_loop_interval
        """

    # ------------------------------------------------------------------
    # daemon loop
    # ------------------------------------------------------------------

    def setup_signal_handlers(self) -> None:
        """Register SIGINT / SIGTERM to set :attr:`stop_event`."""
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._handle_signal)
            except (ValueError, OSError):
                logger.warning("Cannot register signal handler for %s", sig)

    def _handle_signal(self, sig: int, _frame: Any) -> None:
        logger.info("Received signal %d, shutting down...", sig)
        self.stop_event.set()

    def _daemon_loop(self, *, on_tick: Callable[[], None] | None = None, label: str = "daemon") -> None:
        """Run the main event loop until :attr:`stop_event` is set.

        Each tick:
        1.  Call *on_tick* (if provided) — e.g. for suicide checks.
        2.  Block on ``select([stdin], timeout=self._check_interval)``.
        3.  If stdin is readable, read a line; ``"stop"`` sets the stop event.

        In non-interactive environments (stdin = /dev/null or closed pipe),
        ``readline()`` returns ``""`` (EOF) and we sleep via
        ``stop_event.wait()`` instead of busy-waiting.
        """
        logger.info("Entering %s loop (interval=%.1fs)", label, self._check_interval)
        try:
            while not self.stop_event.is_set():
                if on_tick is not None:
                    on_tick()

                try:
                    readable, _writable, _errors = select.select([sys.stdin], [], [], self._check_interval)
                    if readable:
                        line = sys.stdin.readline()
                        if not line:  # EOF (e.g. /dev/null, closed pipe)
                            self.stop_event.wait(self._check_interval)
                            continue
                        user_input = line.strip().lower()
                        if user_input == "stop":
                            self.stop_event.set()
                            break
                except OSError:
                    self.stop_event.wait(self._check_interval)
        except KeyboardInterrupt:
            self.stop_event.set()
        logger.info("Daemon loop exited.")

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------

    def run(self) -> int:
        """Start the application and block until shutdown.  Return exit code."""
        if self._banner_label:
            log_startup_banner(logger, self._banner_label)

        self.init_modules()
        self._start_modules()
        self.setup_signal_handlers()

        log_configuration_summary(self.config)

        self._daemon_loop(on_tick=self._on_daemon_tick)
        self.shutdown()
        return self.exit_code

    @property
    def exit_code(self) -> int:
        """Exit code returned by :meth:`run`.

        Default is -1 (schedules a pod restart in k8s).  Override to
        return 0 for a clean exit that does not trigger rescheduling.
        """
        return -1

    def _on_daemon_tick(self) -> None:
        """Hook called on each daemon loop tick.  Override for custom checks."""

    def shutdown(self) -> None:
        """Stop all modules and the config watcher."""
        self.stop_all_modules()
        if self._config_watcher is not None:
            self._config_watcher.stop()
            self._config_watcher = None
            logger.info("Configuration file watcher stopped")
