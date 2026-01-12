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
import logging
import string
from typing import Tuple

# 配置日志格式和级别
logging.basicConfig(
    level=logging.INFO,
    format=(
        f'[%(asctime)s:%(msecs)04d+08:00] '
        f'[%(process)d] [{os.getppid()}] '
        f'[%(levelname)s] [%(filename)s:%(lineno)d] : %(message)s'
    ),
    datefmt='%Y-%m-%d %H:%M:%S'
)


class PathValidationConfig:
    def __init__(self, allow_empty=False, is_support_root=True, max_file_size=10 * 1024 * 1024 * 1024,
                 is_size_check=True, is_write_mode=False):
        self.allow_empty = allow_empty
        self.is_support_root = is_support_root
        self.max_file_size = max_file_size
        self.is_size_check = is_size_check
        self.is_write_mode = is_write_mode


def logging_info_msg(name: str, path: str, msg: str):
    logging.debug('%s set %s is invalid path! error msg : %s', name, path, msg)


def is_valid_path(name: str, path: str, config: PathValidationConfig = None):
    if config is None:
        config = PathValidationConfig()

    if not path:
        if not config.allow_empty:
            logging.error(f'{name} is empty!')
            return False

    if not isinstance(path, str):
        logging.error('{} type must str!'.format(name))
        return False

    absolute_path = os.path.abspath(path)
    checks = [check_path_valid, check_path_link, check_system_path]
    if not config.is_write_mode:
        checks.append(check_path_exists)
    ret, msg = validate_path(absolute_path, checks)
    if not ret:
        logging_info_msg(name, path, msg)
        return False

    if not config.is_write_mode:
        ret, msg = check_path_owner_group_valid(absolute_path, config.is_support_root)
        if not ret:
            logging_info_msg(name, path, msg)
            return False

    if not config.is_write_mode and config.is_size_check:
        ret, msg = check_file_size(absolute_path, config.max_file_size)
        if not ret:
            logging_info_msg(name, path, msg)
            return False
    return True


def check_path_valid(path: str) -> Tuple[bool, str]:
    if len(path) > 1024:
        return False, "The length of the path exceeds 1024 characters."
    if re.search(r"[^0-9a-zA-Z_./-]", path) or ".." in path:
        return False, "The path contains invalid characters."
    return True, ""


def check_path_link(path: str) -> Tuple[bool, str]:
    return (False, "The path is a symbolic link.") if os.path.islink(os.path.normpath(path)) else (True, "")


def check_path_exists(path: str) -> Tuple[bool, str]:
    return (True, "") if os.path.exists(path) else (False, "The path does not exist.")


def validate_path(path: str, checks: list) -> Tuple[bool, str]:
    for check in checks:
        ret, msg = check(path)
        if not ret:
            return ret, msg
    return True, ""


def check_file_size(path: str, max_file_size: int = 10 * 1024 * 1024) -> Tuple[bool, str]:
    try:
        file_size = os.path.getsize(path)
    except FileNotFoundError:
        return True, ""
    if file_size > max_file_size:
        return False, f"Invalid file size, should be no more than {max_file_size} but got {file_size}"
    return True, ""


def check_path_owner_group_valid(path: str, is_support_root: bool = True) -> Tuple[bool, str]:
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


def check_system_path(path: str) -> Tuple[bool, str]:
    system_paths = ("/usr/bin/", "/usr/sbin/", "/etc/", "/usr/lib/", "/usr/lib64/")
    return (False, "Invalid path: it is a system path.") if os.path.realpath(path).startswith(system_paths) else (
        True, "")


def is_valid_integer(name, value, min_val=None, max_val=None):
    """
    Validates if the integer value lies within the given range (inclusive of the boundaries)

    Args:
        name (str): The name of the configuration item
        value (int): The integer value to be validated
        min_val (int, optional): The minimum allowed value (inclusive), default is no lower limit
        max_val (int, optional): The maximum allowed value (inclusive), default is no upper limit

    Returns:
        bool: Whether the validation is passed

    Example:
        # >>> validate_integer_range(5, 1, 10)
        True
        # >>> validate_integer_range(15, min_val=0)
        True
        # >>> validate_integer_range(3.14, 1, 5)  # Not an integer
        False
    """
    # Type Validation: Ensure the input is an integer
    if not isinstance(value, int):
        raise TypeError('{} type must int!'.format(name))

    # Lower Bound Validation
    if min_val is not None and value < min_val:
        raise ValueError('{} value is {}, must between {} to {}!'.format(name, value, min_val, max_val))

    # Upper Bound Validation
    if max_val is not None and value > max_val:
        raise ValueError('{} value is {}, must between {} to {}!'.format(name, value, min_val, max_val))


def is_valid_bool(name, value):
    if not isinstance(value, bool):
        raise TypeError('{} type must bool!'.format(name))


def is_valid_str(
        name: str,
        value: str,
        min_length: int = 1,
        max_length: int = 64,
        allowed_chars: str = string.ascii_letters + string.digits + ":_-."
):
    """
    Validate whether the string meets length requirements and contains only valid characters
    :param name: Parameter name
    :param value: The string to be validated
    :param min_length: Minimum length (default 1)
    :param max_length: Maximum length (default 64)
    :param allowed_chars: Set of allowed characters (default: letters, digits, _-., and spaces)
    :return: True (valid) / False (invalid)
    """
    # Type Validation: Ensure the input is an integer
    if not isinstance(value, str):
        raise TypeError('{} type must str!'.format(name))

    # Length Validation: Ensure the input meets length requirements
    if not (min_length <= len(value) <= max_length):
        raise ValueError('{} value is {}, must between {} to {}!'.format(name, value, min_length, max_length))

    # If input is variable: Ensure the input variable meet the format of ${var} and var contains only valid chars
    if value.startswith("${") and value.endswith("}"):
        var_name = value[2:-1]
        if not var_name:
            raise ValueError(f"{name} value, which is a variable reference, cannot be empty: {value}")
        if not re.fullmatch(r"[a-zA-Z0-9_]+", var_name):
            raise ValueError(f"{name} value, which is a variable reference, contains invalid characters: {var_name}")
        return
    # Other identifier: Ensure the input contains only valid chars
    for char in value:
        if char not in allowed_chars:
            raise ValueError('{} value is {}, allowed_chars is letters digits :_-.'.format(name, value))


def is_valid_mount(name, values):
    if not isinstance(values, dict):
        raise TypeError('{} type must dict!'.format(name))
    for key, value in values.items():
        if key.startswith("$"):
            is_valid_str(name, key, 1, 1024)
            is_valid_str(name, value, 1, 1024)
        else:
            _ = is_valid_path(name, key)
            _ = is_valid_path(name, value)


def validate_identifier(value, field_name):
    """
    验证标识符字段，只允许字母、数字、连字符和下划线
    用于防止命令注入攻击
    """
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    
    if not re.match(r'^[a-zA-Z0-9_-]+$', value):
        raise ValueError(f"{field_name} contains invalid characters. Only alphanumeric, underscore and hyphen allowed")
    
    if len(value) > 100:  # 限制长度
        raise ValueError(f"{field_name} is too long (max 100 characters)")
    
    return value


def validate_command_part(value, field_name):
    """
    验证命令参数部分，防止命令注入
    """
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    
    # 禁止包含危险字符
    dangerous_chars = [';', '&', '|', '`', '$(', '(', ')', '<', '>', '"', "'", '\\']
    for char in dangerous_chars:
        if char in value:
            raise ValueError(f"{field_name} contains dangerous character: {char}")
    
    return value


def validate_path_part(path, field_name):
    """
    验证路径部分，防止路径遍历攻击
    """
    if not isinstance(path, str):
        raise TypeError(f"{field_name} must be a string")
    
    # 禁止包含危险字符
    dangerous_chars = [';', '&', '|', '`', '$(', '(', ')', '<', '>', '"', "'", '\\']
    for char in dangerous_chars:
        if char in path:
            raise ValueError(f"{field_name} contains dangerous character: {char}")
    
    # 规范化路径
    normalized_path = os.path.normpath(path)
    if '..' in normalized_path:
        raise ValueError(f"{field_name} contains path traversal attempt")
    
    return normalized_path
