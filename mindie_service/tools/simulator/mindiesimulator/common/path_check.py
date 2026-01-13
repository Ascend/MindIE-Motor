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

import os
import re
from typing import Tuple, Optional


class PathCheck:

    @classmethod
    def validate_path(cls, path: str, checks: list) -> Tuple[bool, str]:
        for check in checks:
            ret, msg = check(path)
            if not ret:
                return ret, msg
        return True, ""

    @classmethod
    def check_path_full(cls, path: str, is_support_root: bool = True, max_file_size: int = 10 * 1024 * 1024 * 1024,
                        is_size_check: bool = True, is_write_mode: bool = False) -> Tuple[bool, str]:

        checks = [cls.check_path_valid, cls.check_path_link]

        if not is_write_mode:
            checks.append(cls.check_path_exists)

        ret, msg = cls.validate_path(path, checks)
        if not ret:
            return ret, msg

        if not is_write_mode:
            ret, msg = cls.check_path_owner_group_valid(path, is_support_root)
            if not ret:
                return ret, msg

        if not is_write_mode and is_size_check:
            return cls.check_file_size(path, max_file_size)

        return True, ""

    @classmethod
    def check_directory_path(cls, path: str, is_support_root: bool = True) -> Tuple[bool, str]:
        return cls.validate_path(path, [cls.check_path_valid, cls.check_path_link,
                                        lambda p: cls.check_path_owner_group_valid(p, is_support_root)])

    @classmethod
    def check_path_valid(cls, path: str) -> Tuple[bool, str]:
        path = str(path)
        if not path:
            return False, "The path is empty."
        if len(path) > 1024:
            return False, "The length of the path exceeds 1024 characters."
        if re.search(r"[^0-9a-zA-Z_./-]", path) or ".." in path:
            return False, "The path contains invalid characters."
        return True, ""

    @classmethod
    def check_path_exists(cls, path: str) -> Tuple[bool, str]:
        return (True, "") if os.path.exists(path) else (False, "The path does not exist.")

    @classmethod
    def check_path_link(cls, path: str) -> Tuple[bool, str]:
        return (False, "The path is a symbolic link.") if os.path.islink(os.path.normpath(path)) else (True, "")

    @classmethod
    def check_path_owner_group_valid(cls, path: str, is_support_root: bool = True) -> Tuple[bool, str]:
        try:
            file_stat = os.stat(path)
        except FileNotFoundError:
            return True, ""

        uid, gid = os.getuid(), os.getgid()
        file_uid, file_gid = file_stat.st_uid, file_stat.st_gid

        if file_uid == uid and file_gid == gid:
            return True, ""
        if is_support_root and file_uid == 0 and file_gid == 0:
            return True, ""

        errors = []
        if file_uid != uid:
            errors.append("Incorrect path owner.")
        if file_gid != gid:
            errors.append("Incorrect path group.")

        return False, " ".join(errors)

    @classmethod
    def check_system_path(cls, path: str) -> Tuple[bool, str]:
        system_paths = ("/usr/bin/", "/usr/sbin/", "/etc/", "/usr/lib/", "/usr/lib64/")
        return (False, "Invalid path: it is a system path.") if os.path.realpath(path).startswith(system_paths) else (
        True, "")

    @classmethod
    def check_file_size(cls, path: str, max_file_size: int = 10 * 1024 * 1024) -> Tuple[bool, str]:
        try:
            file_size = os.path.getsize(path)
        except FileNotFoundError:
            return True, ""
        if file_size > max_file_size:
            return False, f"Invalid file size, should be no more than {max_file_size} but got {file_size}"
        return True, ""

    @classmethod
    def safe_open(cls, path: str, mode: str = 'r', encoding: Optional[str] = None, errors: Optional[str] = None):

        is_write_mode = any(m in mode for m in ('w', 'a', 'x'))
        is_valid, error_msg = cls.check_path_full(path, is_size_check=True, is_write_mode=is_write_mode)
        if not is_valid:
            raise FileNotFoundError(f"Path validation failed: {error_msg}")

        try:
            return open(path, mode, encoding=encoding, errors=errors)
        except OSError as e:
            raise RuntimeError(f"Safe open operation error: {e}") from e

