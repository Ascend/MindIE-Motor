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
import stat
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
import logging
from logging.handlers import RotatingFileHandler


MODULE_NAME = 'ccae-reporter'
UNSET_LOGGER = 'NULL'
FILE_SIZE = 'fs'
FILE_COUNT = 'fc'
FILE_PER_PROCESS = 'r'
TRUE_STR = "true"


def is_true_value(value):
    return value in (TRUE_STR, "1")


def recursive_chmod(cur_path, mode=0o750):
    cur_path = os.path.realpath(cur_path)
    while True:
        parent_path = os.path.dirname(cur_path)
        if parent_path == cur_path:
            break
        os.chmod(cur_path, mode)
        cur_path = parent_path


@dataclass
class LogParams:
    path: str = '/root/ascend/log'
    level: str = 'INFO'
    to_file: bool = True
    to_console: bool = False
    verbose: bool = True
    rotate_options: dict = field(default_factory=dict)


class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


class NoNewlineFormatter(logging.Formatter):
    def format(self, record):
        special_chars = [
            '\n',
            '\r',
            '\f',
            '\t',
            '\v',
            '\b',
            '\u000a',
            '\u000d',
            '\u000c',
            '\u000b',
            '\u0008',
            '\u007f',
            '\u0009',
            '    ',
        ]
        for c in special_chars:
            record.msg = str(record.msg).replace(c, ' ')
        if record.levelname == "WARNING":
            record.levelname = "WARN"
        return super().format(record)

    def formatTime(self, record, datefmt=None):
        timezone_offset = time.timezone
        offset_hours = -timezone_offset // 3600
        dt = datetime.fromtimestamp(record.created, timezone(timedelta(hours=offset_hours)))
        timestamp = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        offset = dt.strftime("%z")
        offset = f"{offset[:3]}:{offset[3:]}"
        return f"{timestamp}{offset} DST" if time.daylight else f"{timestamp}{offset}"


def _change_to_readonly(file_name):
    current_permissions = os.stat(file_name).st_mode
    new_permissions = current_permissions & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    os.chmod(file_name, new_permissions)


def _create_log_file(log_file):
    mode = 0o640
    if not os.path.exists(log_file):
        with os.fdopen(os.open(log_file, os.O_CREAT, mode), "w"):
            pass
    else:
        clean_path = os.path.normpath(log_file)
        if os.path.islink(clean_path):
            err_msg = "Check log file path failed because it's a symbolic."
            raise ValueError(err_msg)
        if len(clean_path) > 1024:
            err_msg = "Path of log file is too long, it should not exceed 1024 character."
            raise ValueError(err_msg)
    os.chmod(log_file, mode)


def init_logger() -> LogParams:
    log_params = LogParams()
    # set log path; keep LogParams default when MOTOR_LOG_PATH is unset
    root_log_path = os.getenv("MOTOR_LOG_PATH")
    if root_log_path:
        log_params.path = root_log_path
    log_params.rotate_options = {FILE_SIZE: 20 * 1024 * 1024, FILE_COUNT: 10, FILE_PER_PROCESS: 10}
    return log_params


def _filter_files(directory, prefix, max_num):
    all_files = [f for f in os.listdir(directory) if f.startswith(prefix)]
    file_num = len(all_files)
    delete_file_num = file_num - max_num
    if delete_file_num <= 0:
        return

    files_with_mtime = []
    for f in all_files:
        try:
            file_path = os.path.join(directory, f)
            if not os.path.isfile(file_path):
                continue

            mtime = os.path.getmtime(file_path)
            files_with_mtime.append((f, mtime))
        except FileNotFoundError:
            delete_file_num -= 1
            if delete_file_num <= 0:
                return
            continue  # File was deleted between listdir and getmtime, skip it
        except PermissionError as e:
            raise PermissionError(f"Permission denied to access file: {f}") from e
        except Exception as e:
            raise RuntimeError(f"Failed to get modification time for file {f}: {e}") from e

    sorted_files = sorted(files_with_mtime, key=lambda x: x[1])

    files_to_delete = sorted_files[:delete_file_num]
    for file in files_to_delete:
        file_path = os.path.join(directory, file[0])
        os.remove(file_path)


def _close_logger(parent_directory: str, base_filename, ts):
    pid = os.getpid()
    new_filename = os.path.join(parent_directory, f'{MODULE_NAME}_{pid}_{ts}.log')
    if os.path.exists(base_filename):
        os.rename(base_filename, new_filename)


class CustomRotatingFileHandler(RotatingFileHandler):
    def __init__(
        self, filename, file_per_process, mode="a", maxBytes=0, backupCount=0, encoding=None, delay=False, errors=None
    ):
        super().__init__(filename, mode, maxBytes, backupCount, encoding, delay, errors)
        self.file_per_process = file_per_process
        self.backupCount = backupCount - 1
        self.log_id = 0
        current_time = time.time()
        local_time = time.localtime(current_time)
        ts = time.strftime("%Y%m%d%H%M%S", local_time)
        milliseconds = int((current_time - int(current_time)) * 1000)
        self.ts = f"{ts}{milliseconds:03d}"
        self.first_log = True

    def rotate(self, source, dest):
        pid = os.getpid()
        parent_directory = os.path.dirname(source)
        self.log_id = (self.log_id % self.backupCount) + 1
        if self.log_id == 1 and not self.first_log:
            current_time = time.time()
            local_time = time.localtime(current_time)
            ts = time.strftime("%Y%m%d%H%M%S", local_time)
            milliseconds = int((current_time - int(current_time)) * 1000)
            self.ts = f"{ts}{milliseconds:03d}"
        self.first_log = False
        new_filename = f'{parent_directory}/{MODULE_NAME}_{pid}_{self.ts}.{self.log_id:02d}log'

        super().rotate(source, new_filename)
        _change_to_readonly(new_filename)
        _create_log_file(source)

        # limit file nums in the same process
        prefix = f'{MODULE_NAME}_{pid}'
        _filter_files(parent_directory, prefix, self.file_per_process)
        # limit total file nums
        if self.backupCount > 0:
            prefix = f'{MODULE_NAME}'
            _filter_files(parent_directory, prefix, self.backupCount)

    def close(self):
        if self.stream:
            self.stream.close()
            parent_directory = os.path.dirname(self.baseFilename)
            _close_logger(parent_directory=parent_directory, base_filename=self.baseFilename, ts=self.ts)
            self.stream = None
            # limit total file nums
            if self.backupCount > 0:
                prefix = f'{MODULE_NAME}'
                _filter_files(parent_directory, prefix, self.backupCount)


class Log(metaclass=Singleton):
    MODULE_KEY_NAME = 'ccae-reporter'

    def __init__(self, logger=None):
        self._logger = logging.getLogger(self.MODULE_KEY_NAME)
        # 从环境变量获取日志路径与级别
        log_params = init_logger()
        self.log_level = log_params.level
        self.log_to_file = log_params.to_file
        self.log_file = ""
        self.verbose = log_params.verbose
        self.rotate_options = log_params.rotate_options
        self.to_console = log_params.to_console

        levels = {
            'DEBUG': logging.DEBUG,
            'INFO': logging.INFO,
            'WARN': logging.WARNING,
            'ERROR': logging.ERROR,
            'CRITICAL': logging.CRITICAL,
            UNSET_LOGGER: logging.CRITICAL + 1,
        }
        # 根据配置的日志级别设置日志记录器的级别
        self._logger.setLevel(levels.get(self.log_level.upper(), logging.INFO))

        # 设置日志格式
        if self.verbose:
            file_logging_format = NoNewlineFormatter(
                '[%(asctime)s] [%(process)d] [%(thread)d] [%(name)s] [%(levelname)s] '
                '[%(filename)s:%(lineno)s] %(message)s'
            )
        else:
            file_logging_format = NoNewlineFormatter('[%(asctime)s] [%(levelname)s] %(message)s')

        # 输出日志文件
        if log_params.to_file and self.log_level.upper() != UNSET_LOGGER:
            # 创建文件处理器并将日志写入到指定的文件中
            log_path = os.path.expanduser(log_params.path)
            log_path = os.path.join(log_path, 'ccae_reporter')
            os.makedirs(log_path, exist_ok=True)
            recursive_chmod(log_path)
            pid = os.getpid()
            base_filename = f'{MODULE_NAME}_{pid}.log'
            self.log_file = os.path.join(log_path, base_filename)
            if self.log_level.upper() != UNSET_LOGGER:
                _create_log_file(self.log_file)
                os.chmod(self.log_file, 0o640)

            file_handler = CustomRotatingFileHandler(
                filename=self.log_file,
                file_per_process=self.rotate_options.get(FILE_PER_PROCESS),
                mode='a',
                maxBytes=self.rotate_options.get(FILE_SIZE),
                backupCount=self.rotate_options.get(FILE_COUNT, 64),
            )
            file_handler.setLevel(levels.get(self.log_level.upper(), logging.INFO))
            file_handler.setFormatter(file_logging_format)

            # 添加文件处理器到日志记录器
            self._logger.addHandler(file_handler)

        # 添加控制台输出
        if self.to_console and self.log_level.upper() != UNSET_LOGGER:
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(file_logging_format)
            self._logger.addHandler(stream_handler)

    @property
    def logger(self):
        return self._logger

    def getlog(self):
        return self._logger

    def set_log_file_permission(self, perm=0o440):
        # 结束后修改日志权限
        if self.log_level.upper() == UNSET_LOGGER or not self.log_to_file:
            return
        os.chmod(self.log_file, perm)
