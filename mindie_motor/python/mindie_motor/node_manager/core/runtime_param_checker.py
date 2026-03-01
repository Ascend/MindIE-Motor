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
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from node_manager.common.utils import _SingletonMeta
from node_manager.common.logging import Log
from node_manager.core.config import GeneralConfig
from node_manager.daemon_manager.llm_daemon_starter import llm_daemon_manager


class RuntimeParamChecker(metaclass=_SingletonMeta):
    def __init__(self):
        self.check_time = 4
        # 检查结果文件轮转, 仅保留最近self.backup_count次的检查结果
        self.backup_count = 10
        self.stop_event = threading.Event()
        self.logger = Log(__name__).getlog()
        self.initialized_success = True

        if not self.has_msprechecker():
            self.logger.warning("Do not install msprechecker.")
            self.initialized_success = False
            return
        
        self.check_rule_path = self.get_check_rule_path()
        self.hardware_type = self.get_hardware_type()
        self.model_name = self.get_model_name()
        self.mies_config_path = self.get_mies_config_path()
        self.output_dir = self.get_output_dir()
        self.deploy_mode = "ep"

    @staticmethod
    def has_msprechecker():
        try:
            import msprechecker
            return True
        except ImportError:
            return False
    
    @staticmethod
    def get_check_rule_path():
        import msprechecker
        pkg_dir = os.path.dirname(os.path.abspath(msprechecker.__file__))
        return os.path.join(pkg_dir, "presets", "mindie.cmate")

    @staticmethod
    def get_hardware_type():
        hw_type = llm_daemon_manager.get_device_type()
        hw_type_to_abbreviation = {"800i_a2": "A2", "800i_a3": "A3"}
        return hw_type_to_abbreviation.get(hw_type, hw_type or "")

    @staticmethod
    def get_model_name():
        return os.getenv("MODEL_NAME", "").lower()

    @staticmethod
    def get_mies_config_path():
        return os.path.join(GeneralConfig().config_path, "config.json")

    @staticmethod
    def get_output_dir():
        mindie_log_path = os.path.abspath(os.getenv("MINDIE_LOG_PATH", "/root/mindie/log"))
        return os.path.join(mindie_log_path, "runtime_param_check")

    @staticmethod
    def is_runtime_param_check_enabled():
        """0或不设置时不开启, 非0值时开启"""
        return os.getenv("RUNTIME_PARAM_CHECK_ENABLED", "0") != "0"

    def run(self):
        if not self.is_runtime_param_check_enabled():
            self.logger.info("Runtime parameter checker is disabled by RUNTIME_PARAM_CHECK_ENABLED.")
            return

        if not self.initialized_success:
            self.logger.warning("Runtime parameter checker failed to initialize, will exit.")
            return

        self.stop_event.clear()
        os.makedirs(self.output_dir, exist_ok=True)
        self.logger.info(f"Runtime parameter check output directory: {self.output_dir}")

        # 拉起服务时, 须进行一次执行检查
        self.run_check()

        self.logger.info(f"Runtime parameter checker started, will check daily at {self.check_time}:00")
        self.check_loop()

    def stop(self):
        self.stop_event.set()
        self.logger.info("Runtime parameter checker stopped")

    def rotate_check_results(self, output_dir=None, backup_count=None):
        if output_dir is None or backup_count is None:
            self.logger.warning("Output directory or backup count is not set, skipping file rotation.")
            return
        all_files = [
            f
            for f in os.listdir(output_dir)
            if f.startswith("msprechecker") and f.endswith(".json")
        ]
        file_num = len(all_files)
        delete_file_num = file_num - backup_count
        if delete_file_num <= 0:
            return

        files_with_mtime = []
        for f in all_files:
            try:
                file_path = os.path.join(output_dir, f)
                if not os.path.isfile(file_path):
                    continue
                mtime = os.path.getmtime(file_path)
                files_with_mtime.append((f, mtime))
            except FileNotFoundError:
                delete_file_num -= 1
                if delete_file_num <= 0:
                    return
            except PermissionError:
                self.logger.warning(f"Permission denied to access file: {f}")
            except Exception as e:
                self.logger.warning(f"Failed to get mtime for file {f}: {e}")

        sorted_files = sorted(files_with_mtime, key=lambda x: x[1])
        files_to_delete = sorted_files[:delete_file_num]
        for file_name, _ in files_to_delete:
            file_path = os.path.join(output_dir, file_name)
            try:
                os.remove(file_path)
                self.logger.debug(f"Removed old runtime param check file: {file_name}")
            except Exception as e:
                self.logger.warning(f"Failed to remove file {file_name}: {e}")

    def run_check(self):
        try:
            # 调用msprechecker工具进行配置检查
            cmd = [
                "msprechecker", "run",
                self.check_rule_path,
                "-c", "env", "mies_config:" + self.mies_config_path,
                "-C", "deploy_mode:" + self.deploy_mode,
                "model_type:" + self.model_name, "npu_type:" + self.hardware_type,
                "--output-path", self.output_dir,
            ]
            self.logger.info(f"Runtime parameter check command: {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False
            )

            if result.returncode != 0:
                self.logger.warning(
                    f"Necessary check rules are not in {self.check_rule_path}, "
                    "check result json file will be empty."
                )

            # 执行文件轮转
            self.rotate_check_results(self.output_dir, self.backup_count)
        except FileNotFoundError:
            self.logger.warning(
                "Do not install msprechecker, skipping runtime parameter check."
            )
        except subprocess.TimeoutExpired:
            self.logger.warning("Runtime parameter check timeout.")
        except Exception as e:
            self.logger.warning(
                f"Runtime parameter check exception: {e}.", exc_info=True
            )

    def get_next_check_time(self):
        now = datetime.now(timezone.utc).astimezone()
        today_check = now.replace(hour=self.check_time, minute=0, second=0, microsecond=0)
        if now >= today_check:
            next_check = today_check + timedelta(days=1)
        else:
            next_check = today_check
        return next_check

    def check_loop(self):
        while not self.stop_event.is_set():
            next_check_time = self.get_next_check_time()
            self.logger.info(
                f"Next runtime parameter check scheduled at {next_check_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            end_timestamp = next_check_time.timestamp()
            while time.time() < end_timestamp:
                if self.stop_event.is_set():
                    return
                time.sleep(1)

            self.run_check()


runtime_param_checker = RuntimeParamChecker()
