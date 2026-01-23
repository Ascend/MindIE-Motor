#!/usr/bin/env python3
# coding=utf-8
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
import signal
import subprocess
import time
import threading
from abc import ABC, abstractmethod
from typing import List, Dict, Optional

from node_manager.common.logging import Log
from node_manager.common.utils import PathCheck
from .process_info import ProcessInfo


class BaseDaemonManager(ABC):

    def __init__(self, daemon_type: str = "MindIE-LLM"):
        self.daemon_type = daemon_type
        self.setup_logging()

        self.process_info: List[ProcessInfo] = []
        self.running = True
        self.shutting_down = False
        self._shutdown_lock = threading.Lock()  # avoid repeated calls to terminate method

        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        signal.signal(signal.SIGCHLD, self.child_process_handler)

        self.init_daemon_config()

    def setup_logging(self):
        self.log_instance = Log(__name__)
        self.logger = self.log_instance.getlog()

    @abstractmethod
    def init_daemon_config(self):
        pass

    @abstractmethod
    def build_daemon_command(self, config_file: Optional[str], instance_name: str, **kwargs) -> List[str]:
        """
        Args:
            config_file: Configuration file path (optional)
            instance_name: Name of the instance
            **kwargs: Additional parameters specific to the daemon type
        Returns:
            List[str]: Command and arguments to execute
        """
        pass

    @abstractmethod
    def parse_daemon_arguments(self) -> Dict:
        pass

    @abstractmethod
    def start_daemon_instances(self, daemon_args: Dict):
        pass

    @abstractmethod
    def is_child_process_detected(self, pid: int) -> bool:
        pass

    def child_process_handler(self, signum, frame):
        with self._shutdown_lock:
            if self.shutting_down:
                return
        pid, status = os.waitpid(-1, os.WNOHANG)
        exit_flag = pid > 0
        while pid > 0:
            if os.WIFEXITED(status):
                proc_info = self._find_process_by_pid(pid)
                exit_code = os.WEXITSTATUS(status)
                if proc_info:
                    proc_info.has_exited = True
                if exit_code == 0:
                    self.logger.info(f"Process PID {pid} exited normally with code 0")
                    exit_flag = False
            elif os.WIFSIGNALED(status):
                signal_num = os.WTERMSIG(status)
                self.logger.error(f"Process PID {pid} was killed by signal {signal_num}")
            elif os.WIFSTOPPED(status):
                signal_num = os.WSTOPSIG(status)
                self.logger.error(f"Process PID {pid} was stopped by signal {signal_num}")
            else:
                self.logger.error(f"Process PID {pid} exited abnormally with unknown status")
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except OSError:
                break  # No more child processes
        if exit_flag:
            while self.is_child_process_detected(pid):
                self.logger.info("waiting for child processes to terminate!")
                time.sleep(1)
            self._terminate_all_processes()

    def signal_handler(self, signum, frame):
        self.logger.info(f"Received signal {signum}, shutting down...")
        self._terminate_all_processes()

    def kill_process_group(self, pgid: int, sig=signal.SIGTERM):
        try:
            os.killpg(pgid, sig)
            self.logger.debug(f"Sent signal {sig} to PGID {pgid}")
        except ProcessLookupError:
            self.logger.debug(f"Process group {pgid} has already exited before sending signal {sig}")
        except Exception as e:
            self.logger.warning(f"Failed to signal PGID {pgid}: {e}")

    def terminate_all_processes(self):
        self.logger.info("External termination request received")
        self._terminate_all_processes()

    def is_valid_daemon_command(self, command: List[str]) -> bool:
        allowed_commands = {
            "taskset", "-c", "--config-file", "--expert-parallel", "true"
        }
        for item in command:
            if item in allowed_commands:
                continue
            if re.match(r'^\d+(,\d+)*$', item):
                continue
            if PathCheck.check_path_full(item):
                continue
            return False
        return True

    def start_daemon_process(self, config_file: Optional[str], instance_name: str, **kwargs) -> subprocess.Popen:
        cmd = self.build_daemon_command(config_file, instance_name, **kwargs)
        cwd = kwargs.get('working_dir', '.')
        if not self.is_valid_daemon_command(cmd):
            raise Exception(f"Invalid daemon command {cmd}")
        if not PathCheck.check_path_full(cwd):
            raise RuntimeError(f"Invalid working directory: {cwd}")
        try:
            process = subprocess.Popen(cmd, cwd=cwd)
            proc_info = ProcessInfo(
                process=process,
                name=instance_name,
                config_file=config_file,
                command=' '.join(cmd)
            )
            self.process_info.append(proc_info)
            self.logger.info(f"Started {instance_name} with PID {process.pid} (cmd: {' '.join(cmd)})")
            return process
        except Exception as e:
            self.logger.error(f"Failed to start {instance_name}: {e}")
            raise

    def run(self):
        try:
            daemon_args = self.parse_daemon_arguments()
            self.start_daemon_instances(daemon_args)

            if not self.process_info:
                self.logger.error("No processes started")
                return -1
            self.logger.info(f"Started {len(self.process_info)} {self.daemon_type} processes")

            return self._wait_for_completion()
        except KeyboardInterrupt:
            self.logger.info("Received keyboard interrupt")
            self._terminate_all_processes()
            return 0
        except Exception as e:
            self.logger.error(f"Error in main execution: {e}")
            self._terminate_all_processes()
            return -1

    def _terminate_all_processes(self):
        if self.shutting_down:
            return
        with self._shutdown_lock:
            if self.shutting_down:
                return
            self.shutting_down = True

        if not self.process_info:
            self.logger.info("No processes to terminate")
            return

        self.logger.info(f"Terminating {len(self.process_info)} child processes...")
        for proc_info in self.process_info:
            try:
                self.logger.info(f"Terminating {proc_info.name} (PID: {proc_info.pid})")
                self.kill_process_group(proc_info.pid, signal.SIGTERM)
            except Exception as e:
                self.logger.error(f"Error terminating {proc_info.name}: {e}")
        time.sleep(3)
        for proc_info in self.process_info:
            try:
                if proc_info.is_alive:
                    self.logger.warning(f"Force killing {proc_info.name} (PID: {proc_info.pid})")
                    self.kill_process_group(proc_info.pid, signal.SIGKILL)
            except Exception as e:
                self.logger.error(f"Error killing {proc_info.name}: {e}")
        self.running = False

    def _wait_for_completion(self):
        while self.running and self.process_info:
            alive_count = sum(1 for p in self.process_info if not p.has_exited)
            if alive_count == 0:
                self.logger.info("All processes have normally exited")
                return 0
            time.sleep(1)
        return -1

    def _find_process_by_pid(self, pid: int) -> Optional[ProcessInfo]:
        for proc_info in self.process_info:
            if proc_info.pid == pid:
                return proc_info
        return None