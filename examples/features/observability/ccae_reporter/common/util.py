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
import ipaddress
import re
import stat
import socket


def format_address(host, port):
    try:
        if isinstance(ipaddress.ip_address(host.strip("[]")), ipaddress.IPv6Address):
            return f"[{host.strip('[]')}]:{port}"
    except ValueError:
        pass
    return f"{host}:{port}"


def get_local_ip():
    pod_ip = os.getenv("POD_IP")
    if pod_ip:
        return pod_ip
    for family, _, _, _, sockaddr in socket.getaddrinfo(
        socket.gethostname(),
        None,
        socket.AF_UNSPEC,
        socket.SOCK_DGRAM,
    ):
        if family in (socket.AF_INET, socket.AF_INET6):
            return sockaddr[0]
    return socket.gethostbyname(socket.gethostname())


def safe_open(file, *args, **kwargs):
    if not PathCheck.check_path_full(file):
        error_message = f"Failed to open file {file}"
        raise OSError(error_message)
    if not PathCheck.check_file_size(file):
        error_message = f"Failed to open file {file}"
        raise OSError(error_message)
    encoding = kwargs.pop("encoding", "utf-8")
    return open(os.path.realpath(file), *args, encoding=encoding, **kwargs)


def safe_read(file_path):
    with safe_open(file_path, 'r', encoding='utf-8') as f:
        return f.read()


class PathCheckBase:
    logger_screen = None

    @classmethod
    def log_error(cls, msg: str):
        """
        log模块初始化前，直接将报错信息打屏
        :param msg: 需要打印的报错信息
        :return: None
        """
        import logging
        import sys

        if not cls.logger_screen:
            cls.logger_screen = logging.getLogger("adaptor_screen")
            cls.logger_screen.setLevel(logging.INFO)
            ch = logging.StreamHandler(sys.stdout)
            cls.logger_screen.addHandler(ch)
        cls.logger_screen.error(msg)

    @classmethod
    def check_path_full(cls, path: str, is_support_root: bool = True):
        return (
            cls.check_name_valid(path)
            and cls.check_soft_link(path)
            and cls.check_exists(path)
            and cls.check_owner_group(path, is_support_root)
        )

    @classmethod
    def check_exists(cls, path: str):
        if not os.path.exists(path):
            error_message = f"[CCAE Reporter] The path {path} is not exists"
            cls.log_error(error_message)
            return False
        return True

    @classmethod
    def check_soft_link(cls, path: str):
        if os.path.islink(os.path.normpath(path)):
            error_msg = f"[CCAE Reporter] The path {path} is a soft link"
            cls.log_error(error_msg)
            return False
        return True

    @classmethod
    def check_owner_group(cls, path: str, is_support_root: bool = True):
        cur_user_id = os.getuid()
        cur_group_id = os.getgid()

        try:
            file_info = os.stat(path)
        except FileNotFoundError:
            error_message = f"[OM Adapter] The path {path} does not exist"
            cls.log_error(error_message)
            return False
        file_user_id = file_info.st_uid
        file_group_id = file_info.st_gid

        flag = file_user_id == cur_user_id and file_group_id == cur_group_id
        if is_support_root:
            flag = flag or (file_user_id == 0 and file_group_id == 0)
        if flag:
            return True
        error_msg = f"[CCAE Reporter] Check the path {path} owner and group failed"
        cls.log_error(error_msg)
        return False

    @classmethod
    def check_path_mode(cls, mode: int, path: str):
        cur_stat = os.stat(path)
        cur_mode = stat.S_IMODE(cur_stat.st_mode)
        if cur_mode != mode:
            error_message = f"[CCAE Reporter] Check the {path} %s mode failed"
            cls.log_error(error_message)
            return False
        return True

    @classmethod
    def check_name_valid(cls, path: str):
        if not path:
            error_msg = f"[CCAE Reporter] The path is empty: {path}"
            cls.log_error(error_msg)
            return False
        if len(path) > 2048:
            error_msg = f"[CCAE Reporter] The length of path exceeds 2048 characters: {path}"
            cls.log_error(error_msg)
            return False
        traversal_patterns = ["../", "..\\", ".."]
        if any(pattern in path for pattern in traversal_patterns):
            error_msg = f"[CCAE Reporter] Path contains traversal sequences: {path}"
            cls.log_error(error_msg)
            return False
        if re.search(r'[^0-9a-zA-Z_./-]', path):
            error_msg = f"[CCAE Reporter] The path contains special characters: {path}"
            cls.log_error(error_msg)
            return False
        return True

    @classmethod
    def check_file_size(cls, path: str, max_file_size: int = 1024 * 1024 * 1024):
        try:
            file_size = os.path.getsize(path)
        except FileNotFoundError:
            cls.log_error("[CCAE Reporter] The path %s is not exists" % path)
            return False
        if file_size > max_file_size:
            cls.log_error(
                f"[CCAE Reporter] Invalid file size, should be no more than {max_file_size} but got {file_size}"
            )
            return False
        return True


class PathCheck(PathCheckBase):
    logger = None

    @classmethod
    def log_error(cls, msg: str):
        from ccae_reporter.common.logging import Log

        if not cls.logger:
            cls.logger = Log(__name__).getlog()
        cls.logger.error(msg)
