# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import time
import threading
from typing import Callable

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemMovedEvent

from motor.common.logger import get_logger


logger = get_logger(__name__)


INOTIFY_MAX_USER_INSTANCES_PATH = "/proc/sys/fs/inotify/max_user_instances"
INOTIFY_MAX_USER_WATCHES_PATH = "/proc/sys/fs/inotify/max_user_watches"


class ConfigFileHandler(FileSystemEventHandler):
    """Handler for configuration file changes"""

    def __init__(
        self,
        config_path: str,
        reload_callback: Callable[[], bool],
        config_update_callback: Callable[[], None] | None = None,
        debounce_seconds: float = 1.0,
    ) -> None:
        """
        Initialize the config file handler.

        Args:
            config_path: Path to the configuration file to monitor
            reload_callback: Callback function that performs the reload and returns success status
            config_update_callback: Callback function called after successful configuration reload
            debounce_seconds: Minimum time between reload attempts to debounce rapid changes
        """
        super().__init__()
        self.config_path = os.path.abspath(config_path)
        self.reload_callback = reload_callback
        self.config_update_callback = config_update_callback
        self.debounce_seconds = debounce_seconds
        self.last_reload = 0.0
        self._lock = threading.Lock()

    def on_modified(self, event):
        """Handle file modification events"""
        self._reload_if_matches(event.src_path)

    def on_moved(self, event: FileSystemMovedEvent) -> None:
        """Handle atomic replacements moved onto the configuration path."""
        if not event.is_directory:
            self._reload_if_matches(event.dest_path)

    def _reload_if_matches(self, path: str) -> None:
        if os.path.abspath(path) == self.config_path:
            current_time = time.time()

            with self._lock:
                # Debounce rapid changes
                if current_time - self.last_reload < self.debounce_seconds:
                    return

                logger.info("Configuration file change detected: %s", self.config_path)

                try:
                    if self.reload_callback():
                        logger.info("Configuration reloaded successfully")
                        self.last_reload = current_time

                        # Call config update callback if provided
                        if self.config_update_callback:
                            try:
                                self.config_update_callback()
                                logger.info("Configuration update callback executed successfully")
                            except Exception as e:
                                logger.error("Error during configuration update callback: %s", e)
                    else:
                        logger.error("Configuration reload failed")
                except Exception as e:
                    logger.error("Error during configuration reload: %s", e)


class ConfigWatcher:
    """Configuration file watcher using watchdog"""

    def __init__(
        self,
        config_path: str,
        reload_callback: Callable[[], bool],
        config_update_callback: Callable[[], None] | None = None,
        debounce_seconds: float = 1.0,
    ) -> None:
        """
        Initialize the configuration watcher.

        Args:
            config_path: Path to the configuration file to monitor
            reload_callback: Callback function that performs the reload and returns success status
            config_update_callback: Callback function called after successful configuration reload
            debounce_seconds: Minimum time between reload attempts
        """
        self.config_path = config_path
        self.reload_callback = reload_callback
        self.config_update_callback = config_update_callback
        self.debounce_seconds = debounce_seconds

        self.observer: Observer | None = None
        self.event_handler: ConfigFileHandler | None = None
        self.watch_directory = os.path.dirname(os.path.abspath(config_path))

    @staticmethod
    def _get_inotify_limit() -> int:
        """Get current inotify max_user_instances limit"""
        try:
            with open(INOTIFY_MAX_USER_INSTANCES_PATH, 'r', encoding='utf-8') as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return -1

    @staticmethod
    def _get_inotify_watch_limit() -> int:
        """Get current inotify max_user_watches limit"""
        try:
            with open(INOTIFY_MAX_USER_WATCHES_PATH, 'r', encoding='utf-8') as f:
                return int(f.read().strip())
        except (FileNotFoundError, ValueError):
            return -1

    def start(self):
        """Start watching the configuration file"""
        if not os.path.exists(self.config_path):
            logger.warning("Configuration file does not exist: %s", self.config_path)
            return

        try:
            self.event_handler = ConfigFileHandler(
                config_path=self.config_path,
                reload_callback=self.reload_callback,
                config_update_callback=self.config_update_callback,
                debounce_seconds=self.debounce_seconds,
            )

            self.observer = Observer()
            self.observer.schedule(self.event_handler, self.watch_directory, recursive=False)
            self.observer.start()

            logger.info("Started watching configuration file: %s", self.config_path)
        except OSError as e:
            if "inotify instance limit reached" in str(e):
                logger.error(
                    "Failed to start config watcher: inotify instance limit reached. "
                    "Please increase fs.inotify.max_user_instances limit. "
                    "Current limit: %s",
                    self._get_inotify_limit(),
                )
                logger.error(
                    "You can temporarily fix this by running: echo 1024 | sudo tee %s", INOTIFY_MAX_USER_INSTANCES_PATH
                )
                logger.error("For permanent fix, add 'fs.inotify.max_user_instances=1024' to /etc/sysctl.conf")
            elif "inotify watch limit reached" in str(e):
                logger.error(
                    "Failed to start config watcher: inotify watch limit reached. "
                    "Please increase fs.inotify.max_user_watches limit. "
                    "Current limit: %s",
                    self._get_inotify_watch_limit(),
                )
                logger.error(
                    "You can temporarily fix this by running: echo 524288 | sudo tee %s", INOTIFY_MAX_USER_WATCHES_PATH
                )
                logger.error("For permanent fix, add 'fs.inotify.max_user_watches=524288' to /etc/sysctl.conf")
            else:
                logger.error("Failed to start config watcher: %s", e)
            raise

    def stop(self):
        """Stop watching the configuration file"""
        if self.observer:
            try:
                self.observer.stop()
                self.observer.join(timeout=5.0)
                if self.observer.is_alive():
                    logger.warning("Config watcher did not terminate gracefully")
                else:
                    logger.info("Config watcher stopped successfully")
            except Exception as e:
                logger.error("Error stopping config watcher: %s", e)
            finally:
                # Ensure observer is cleaned up
                self.observer = None

    def is_alive(self) -> bool:
        """Check if the watcher is running"""
        return self.observer is not None and self.observer.is_alive()
