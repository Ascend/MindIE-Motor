#!/usr/bin/python3.11
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2024-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler


class Logger:
    _instance = None
    _initialized = False
    _log_file_path = None

    @staticmethod
    def get_logger():
        if Logger._instance is None:
            raise RuntimeError("Logger not initialized. Call init_logger() first.")
        return Logger._instance

    @staticmethod
    def init_logger(
            log_dir: str = "./logs", exec_type: str = "default", level=logging.INFO, save_to_file: bool = False
    ):
        if Logger._initialized:
            return Logger._instance

        logger = logging.getLogger("GlobalLogger")
        logger.setLevel(level)

        log_format = "%(asctime)s [%(levelname)s] %(message)s"
        formatter = logging.Formatter(log_format)

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        if save_to_file:
            cst_timezone = timezone(timedelta(hours=8))
            timestamp = datetime.now(cst_timezone).strftime("%Y%m%d_%H%M%S")

            log_subdir_name = f"{exec_type}_{timestamp}"
            full_log_path = os.path.join(log_dir, log_subdir_name)
            os.makedirs(full_log_path, exist_ok=True, mode=0o750)

            log_file_path = os.path.join(full_log_path, "runtime.log")
            Logger._log_file_path = log_file_path

            file_handler = RotatingFileHandler(
                log_file_path,
                maxBytes=50 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
            logger.propagate = False
            
            logger.info("Logger initialized. Logs will be saved in %s", log_file_path)
        else:
            logger.info("Logger initialized in console mode. No logs will be saved to file.")

        Logger._instance = logger
        Logger._initialized = True
        return logger