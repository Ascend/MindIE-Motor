#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from dataclasses import dataclass


@dataclass
class LoggingConfig:
    """Logging configuration class used by various components"""
    log_level: str = 'INFO'  # Logging level: DEBUG, INFO, WARNING, ERROR
    log_max_line_length: int = 8192
    log_format: str = (
        '(%(processName)s pid=%(process)d) %(levelname)s %(asctime)s '
        '[%(name)s][%(fileinfo)s:%(lineno)d] %(message)s'
    )
    log_date_format: str = '%m-%d %H:%M:%S'
    # Persistent log configuration
    host_log_dir: str = '/root/ascend/log'  # Optional log directory, which is host path, not pod
    log_rotation_size: int = 20  # Log rotation size in MB
    log_rotation_count: int = 10  # Number of log files to keep
    log_compress: bool = False  # Whether to compress log files
    log_compress_level: int = 6  # Compression level, 1-9, where 1 is fastest and 9 is slowest
    log_max_total_size: int = 200  # Maximum total size of all log files in MB, default 100MB
    log_cleanup_interval: int = 1800  # Cleanup log gz interval in seconds, default 1800s=30min