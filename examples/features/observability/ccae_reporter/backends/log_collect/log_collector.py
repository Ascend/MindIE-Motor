# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import re

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from ccae_reporter.common.logging import Log
from ccae_reporter.common.util import PathCheck
from ccae_reporter.backends.log_collect.log_processor import LogDataProcessor
from ccae_reporter.backends.log_collect.data_class import LogFile


DEFAULT_COLLECT_PATH = os.getenv("MOTOR_LOG_PATH", "/root/ascend/log")

MONITOR_INTERVAL_MILLION_SECONDS = 3
# {pod_name}_{pid}.log
LOG_PATTERNS = "^.*-(controller|coordinator)-[a-zA-Z0-9]+-[a-zA-Z0-9]+_[0-9]+\\.log$"


class CollectHandler(FileSystemEventHandler):
    def __init__(self, identity=None):
        self.logger = Log(__name__).getlog()
        self.identity = identity
        self.log_processor = LogDataProcessor()

    def on_created(self, event):
        try:
            if self._check_valid_file(event):
                self.logger.info("[CCAE Reporter] File %s is created" % event.src_path)
                self.log_processor.watch_files[event.src_path] = LogFile(file_path=event.src_path)
                self.log_processor.modified_log_files.add(event.src_path)
        except Exception as e:
            self.logger.error(f"[CCAE Reporter] Failed to handle file creation event for {event.src_path}: {e}")

    def on_modified(self, event):
        try:
            if self._check_valid_file(event):
                self.logger.debug("[CCAE Reporter] File %s is modified", event.src_path)  # 文件内容更新频率高
                if event.src_path not in self.log_processor.watch_files:
                    self.log_processor.watch_files[event.src_path] = LogFile(file_path=event.src_path)
                self.log_processor.modified_log_files.add(event.src_path)
        except Exception as e:
            self.logger.error(f"[CCAE Reporter] Failed to handle file modified event for {event.src_path}: {e}")

    def on_deleted(self, event):
        try:
            if self._check_valid_file(event):
                self.logger.info("[CCAE Reporter] File %s is deleted" % event.src_path)
                self.log_processor.watch_files.pop(event.src_path)
                self.log_processor.modified_log_files.pop(event.src_path)
        except Exception as e:
            self.logger.error(f"[CCAE Reporter] Failed to handle file deleted event for {event.src_path}: {e}")

    def on_moved(self, event):
        try:
            if self._check_valid_file(event):
                self.logger.info("[CCAE Reporter] File %s is changed to %s" % (event.src_path, event.dest_path))
                src_log_file = self.log_processor.watch_files.pop(event.src_path, LogFile(file_path=event.dest_path))
                src_log_file.file_path = event.dest_path
                src_log_file.last_read_position = 0  # 文件轮转后，更新读取位置
                self.log_processor.watch_files[event.dest_path] = src_log_file
        except Exception as e:
            self.logger.error(
                f"[CCAE Reporter] Failed to handle file moved event from {event.src_path} to {event.dest_path}: {e}"
            )

    def _check_valid_file(self, event):
        if event.is_directory:
            return False

        filename = os.path.basename(event.src_path)

        pattern = LOG_PATTERNS
        if self.identity:
            identity_escaped = re.escape(self.identity.lower())
            pattern = f"^.*-({identity_escaped})-[a-zA-Z0-9]+-[a-zA-Z0-9]+_[0-9]+\\.log$"

        if not bool(re.match(pattern, filename, re.IGNORECASE)):
            return False

        return PathCheck.check_path_full(event.src_path)


class Collector:
    def __init__(self, collect_path=DEFAULT_COLLECT_PATH, identity=None):
        self.logger = Log(__name__).getlog()

        if not os.path.exists(collect_path):
            raise RuntimeError("[CCAE Reporter] The log monitor path %s is not exists" % collect_path)

        host_name = os.getenv("HOST_NAME", "")
        parts = host_name.split("-")
        if len(parts) > 2:
            self.log_path = os.path.join(collect_path, "-".join(parts[:-2]))
        else:
            self.log_path = os.path.join(collect_path, host_name)
        if not os.path.exists(self.log_path):
            os.makedirs(self.log_path, exist_ok=True)
            self.logger.info("[CCAE Reporter] Create log monitor path %s" % self.log_path)
        self.logger.info("[CCAE Reporter] Log monitor path is %s" % self.log_path)

        self.collect_handler = CollectHandler(identity)
        self.collect_observer = Observer()
        self.collect_observer.schedule(self.collect_handler, self.log_path, recursive=True)
        try:
            self.collect_observer.start()
        except Exception as e:
            self.logger.error(f"[CCAE Reporter] Failed to start collect_observer: {e}")
            self.running = False
            raise RuntimeError(f"[CCAE Reporter] Observer startup failed: {e}") from e

    def stop(self):
        self.collect_observer.stop()
        self.collect_observer.join()
