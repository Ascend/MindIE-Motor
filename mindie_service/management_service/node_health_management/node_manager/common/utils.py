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
import stat
import threading
from abc import ABCMeta


class _SingletonMeta(ABCMeta):
    _instances = {}
    _lock = threading.RLock()

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    instance = super().__call__(*args, **kwargs)
                    cls._instances[cls] = instance
        return cls._instances[cls]

    def reset_instance(cls) -> None:
        """
        Reset the singleton instance of the current class.

        This is primarily used by tests to ensure isolation between cases,
        without accessing the protected internal cache directly.
        """
        # cls is the singleton class (e.g. LLMDaemonManager), type(cls) is _SingletonMeta
        with type(cls)._lock:
            type(cls)._instances.pop(cls, None)


def safe_open(file, *args, **kwargs):
    if not PathCheck.check_path_full(file):
        raise OSError("Failed to open file %s" % file)
    return open(os.path.realpath(file), *args, **kwargs)


class PathCheckBase(object):

    logger_screen = None

    @classmethod
    def check_path_full(cls, path: str, is_support_root: bool = True, mode: int = None):
        return cls.check_name_valid(path) and cls.check_soft_link(path) \
            and cls.check_exists(path) and cls.check_owner_group(path, is_support_root) \
            and (cls.check_path_mode(mode, path) if mode is not None else True)

    @classmethod
    def check_exists(cls, path: str):
        if not os.path.exists(path):
            return cls._log_error_and_return_false(f"The path {path} does not exist")
        return True

    @classmethod
    def check_soft_link(cls, path: str):
        if os.path.islink(os.path.normpath(path)):
            return cls._log_error_and_return_false(f"The path {path} is a soft link")
        return True

    @classmethod
    def check_owner_group(cls, path: str, is_support_root: bool = True):
        cur_user_id = os.getuid()
        cur_group_id = os.getgid()

        file_info = os.stat(path)
        file_user_id = file_info.st_uid
        file_group_id = file_info.st_gid

        is_owner_match = file_user_id == cur_user_id and file_group_id == cur_group_id
        is_root_owned = file_user_id == 0 and file_group_id == 0
        
        if is_owner_match or (is_support_root and is_root_owned):
            return True
        
        return cls._log_error_and_return_false(f"Check the path {path} owner and group failed")

    @classmethod
    def check_path_mode(cls, mode: int, path: str):
        cur_stat = os.stat(path)
        cur_mode = stat.S_IMODE(cur_stat.st_mode)
        
        if cur_mode == mode:
            return True
        
        return cls._log_error_and_return_false(f"Check the path {path} mode failed")

    @classmethod
    def check_name_valid(cls, file_path: str):
        if not file_path:
            error_msg = f"The path {file_path} is empty"
            return cls._log_error_and_return_false(error_msg)
        if len(file_path) > 2048:
            error_msg = f"The length of path {file_path} exceeds 2048 characters"
            return cls._log_error_and_return_false(error_msg)
        traversal_patterns = ["../", "..\\", ".."]
        if any(pattern in file_path for pattern in traversal_patterns):
            error_msg = "The path contains traversal sequences"
            return cls._log_error_and_return_false(error_msg)
        pattern_name = re.compile(r'[^0-9a-zA-Z_./-]')
        if pattern_name.findall(file_path):
            error_msg = f"The path {file_path} contains special characters"
            return cls._log_error_and_return_false(error_msg)
        return True
    
    @classmethod
    def _log_error_and_return_false(cls, error_message: str) -> bool:
        """
        log模块初始化前，直接将报错信息打屏
        :param error_message: 需要打印的报错信息
        :return: False
        """
        import logging
        import sys
        if not cls.logger_screen:
            cls.logger_screen = logging.getLogger("adaptor_screen")
            cls.logger_screen.setLevel(logging.INFO)
            ch = logging.StreamHandler(sys.stdout)
            cls.logger_screen.addHandler(ch)
        cls.logger_screen.error(error_message)
        return False


class PathCheck(PathCheckBase):

    logger = None

    @classmethod
    def _log_error_and_return_false(cls, error_message: str) -> bool:
        from node_manager.common.logging import Log
        if not cls.logger:
            cls.logger = Log(__name__).getlog()
        cls.logger.error(error_message)
        return False