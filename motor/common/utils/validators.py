# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
from typing import Callable, Any
from typing import List, Tuple

from motor.common.engine_constants import (
    MAX_DEVICE_NUM,
    MAX_FILE_NUMS,
    MAX_RANK_SIZE,
    MAX_SIZE,
    MIN_DEVICE_NUM,
    MIN_RANK_SIZE,
    MIN_SIZE,
    MUSK_PRIVILEGE,
)


class Validator:
    """
    A validator to check the input parameters
    """

    def __init__(self, value, msg="value is invalid"):
        """
        :param value: the value for validation
        :param msg: default error msg
        """
        self.value = value
        self.msg = msg
        self.checkers = []
        self.is_valid_state = None

    def register_checker(self, checker: Callable[[Any], bool], msg: str = None):
        self.checkers.append((checker, msg if msg else self.msg))

    def check(self):
        if self.is_valid_state is None:
            self.is_valid_state = True
        for ck, msg in self.checkers:
            self.is_valid_state &= ck(self.value)
            if not self.is_valid_state:
                self.msg = msg
                break
        if self.is_valid_state:
            self.msg = None
        return self

    def is_valid(self):
        if self.is_valid_state is None:
            try:
                self.check()
            except Exception:
                self.is_valid_state = False
        return self.is_valid_state

    def get_value(self, default=None):
        return self.value if self.is_valid() else default


class ClassValidator(Validator):
    """
    Check class validator.
    """

    def __init__(self, value, classes):
        super().__init__(value)
        self.classes = classes

    def check_isinstance(self):
        """Check arg isinstance of classes"""
        self.register_checker(lambda path: isinstance(self.value, self.classes), "Invalid parameter type")
        return self


class StringValidator(Validator):
    """
    String type validator.
    """

    def __init__(self, value, max_len=None, min_len=0):
        super().__init__(value)
        self.max_len = max_len
        self.min_len = min_len
        self.register_checker(lambda x: isinstance(x, str), "type is not str")

    def check_string_length(self):
        if self.min_len is not None:
            self.register_checker(lambda x: len(x) >= self.min_len, f"length is less than {self.min_len}")
        if self.max_len is not None:
            self.register_checker(lambda x: len(x) <= self.max_len, f"length is bigger than {self.max_len}")
        return self

    def check_not_contain_black_element(self, element):
        self.register_checker(lambda x: x is not None and element is not None and x.find(element) == -1)
        return self

    def can_be_transformed2int(self, min_value: int = None, max_value: int = None):
        if min_value is None:
            min_value = MIN_RANK_SIZE
        if max_value is None:
            max_value = MAX_RANK_SIZE

        can_transformed = self.value.isdigit()
        try:
            if can_transformed and (min_value > int(self.value) or max_value < int(self.value)):
                can_transformed = False
        except ValueError:
            can_transformed = False
        finally:
            if self.is_valid_state is not None:
                self.is_valid_state &= can_transformed
            else:
                self.is_valid_state = can_transformed
        return self


class IntValidator(Validator):
    """
    Int type validator
    """

    def __init__(self, value: int, min_value: int = None, max_value: int = None):
        super().__init__(value)
        self.min_value = min_value
        self.max_value = max_value
        self.register_checker(lambda x: isinstance(x, int), "type is not int")

    def check_value(self):
        if self.min_value is not None:
            self.register_checker(lambda x: x >= self.min_value, f"value is less than {self.min_value}")
        if self.max_value is not None:
            self.register_checker(lambda x: x <= self.max_value, f"value is bigger than {self.max_value}")
        return self


class MapValidator(Validator):
    """
    Map type validator.
    """

    def __init__(self, value: dict, inclusive_keys: list = None):
        super().__init__(value)
        self.register_checker(lambda x: isinstance(x, dict), "type is not dict")
        if inclusive_keys is None:
            inclusive_keys = []
        for key in inclusive_keys:
            self.register_checker(lambda x, k=key: k in self.value, "Key error for the value of dict type")


class RankSizeValidator(IntValidator):
    """
    Distributed training job size validator
    """

    def check_rank_size_valid(self):
        super().__init__(self.value)
        self.register_checker(lambda x: MIN_RANK_SIZE <= self.value <= MAX_RANK_SIZE,
                              "Invalid rank size")
        return self

    def check_device_num_valid(self):
        super().__init__(self.value)
        self.register_checker(lambda x: MIN_DEVICE_NUM <= self.value <= MAX_DEVICE_NUM,
                              "Invalid device num")
        return self


class DirectoryValidator(StringValidator):
    def __init__(self, value, max_len=None, min_len=1):
        """
        @param value: the path, should not be emtpy string, should not contain double dot(../)
        """
        super().__init__(value, max_len, min_len)
        self.register_checker(lambda x: isinstance(x, str), "type is not str")

    @staticmethod
    def remove_prefix(string: str | None, prefix: str | None) -> Tuple[bool, str | None]:
        if string is None or prefix is None or len(string) < len(prefix):
            return False, string
        if string.startswith(prefix):
            return True, string[len(prefix):]
        else:
            return False, string

    @staticmethod
    def check_is_children_path(path_: str, target_: str):
        if not target_:
            return False

        try:
            realpath_ = os.path.realpath(path_)
        except (TypeError, ValueError, OSError):
            return False

        try:
            realpath_target = os.path.realpath(target_)
        except (TypeError, ValueError, OSError):
            return False

        is_prefix, rest_part = DirectoryValidator.remove_prefix(realpath_target, realpath_)

        if rest_part.startswith(os.path.sep):
            rest_part = rest_part.lstrip(os.path.sep)
        if is_prefix:
            joint_path = os.path.join(realpath_, rest_part)
            return os.path.realpath(joint_path) == realpath_target
        else:
            return False

    @staticmethod
    def __check_with_sensitive_words(path: str, words: List):
        _, name = os.path.split(path)
        if name:
            return not any(map(lambda x: x in path, words))
        else:
            return True

    def check_directory_permissions(self, target_mode: int):
        stat_info = os.stat(self.value)
        mode = stat_info.st_mode & MUSK_PRIVILEGE
        self.register_checker(lambda path: mode == target_mode, "permission error")
        return self

    def check_is_not_none(self):
        self.register_checker(lambda path: self.value is not None and len(self.value) > 0,
                              "Invalid directory parameter")
        return self

    def check_dir_name(self):
        def is_path_valid(path):
            return not ('..' in self.value or len(self.value) < self.min_len or (
                len(self.value) > self.max_len if self.max_len is not None else False))

        self.register_checker(is_path_valid, "the path parameter is invalid")
        return self

    def check_not_soft_link(self):
        self.register_checker(lambda path: os.path.realpath(self.value) == os.path.normpath(self.value),
                              "soft link or relative path should not be in the path parameter")
        return self

    def check_dir_file_number(self):
        files = os.listdir(self.value)
        self.register_checker(lambda path: len(files) <= MAX_FILE_NUMS,
                              "Too many files in directory")
        return self

    def path_should_exist(self, is_file=True, msg=None):
        self.register_checker(lambda path: os.path.exists(self.value),
                              msg if msg else "path parameter does not exist")
        if is_file:
            self.register_checker(lambda path: os.path.isfile(self.value),
                                  msg if msg else "path parameter is not a file")
        return self

    def path_should_not_exist(self):
        self.register_checker(lambda path: not os.path.exists(self.value), "path parameter does not exist")
        return self

    def with_blacklist(self, lst: List = None, exact_compare: bool = True, msg: str = None):
        if lst is None:
            lst = ["/usr/bin", "/usr/sbin", "/etc", "/usr/lib", "/usr/lib64"]
        if len(lst) == 0:
            return self
        if msg is None:
            msg = "path should is in blacklist"
        if exact_compare:
            self.register_checker(lambda path: path not in [os.path.realpath(each) for each in lst], msg)
        else:
            self.register_checker(
                lambda path: not any([DirectoryValidator.check_is_children_path(each, path) for each in lst]), msg
            )
        return self

    def should_not_contains_sensitive_words(self, words: List = None, msg=None):
        if words is None:
            words = ["Key", "password", "privatekey"]
        self.register_checker(lambda path: DirectoryValidator.__check_with_sensitive_words(path, words), msg)
        return self

    def check_user_group(self):
        process_uid = os.geteuid()
        process_gid = os.getegid()
        stat_info = os.stat(self.value)
        dir_uid = stat_info.st_uid
        dir_gid = stat_info.st_gid
        self.register_checker(lambda path: process_uid == dir_uid or process_gid == dir_gid,
                              "Invalid dir user or group.")
        return self


class FileValidator(StringValidator):
    def __init__(self, value):
        """
        @param value: the file path, should not be emtpy string, should not contain double dot(../)
        """
        super().__init__(value)
        self.register_checker(lambda x: isinstance(x, str), "type is not str")
        self.register_checker(lambda x: os.path.isfile(x), "type is not file")

    def check_file_size(self, max_size=MAX_SIZE, min_size=MIN_SIZE):
        self.register_checker(lambda path: min_size < os.path.getsize(self.value) <= max_size,
                              "file size is invalid")
        return self

    def check_not_soft_link(self):
        self.register_checker(lambda path: os.path.realpath(self.value) == self.value,
                              "soft link or relative path should not be in the path parameter")
        return self

    def check_user_group(self):
        process_uid = os.geteuid()
        process_gid = os.getegid()
        stat_info = os.stat(self.value)
        file_uid = stat_info.st_uid
        file_gid = stat_info.st_gid
        self.register_checker(lambda path: process_uid == file_uid or process_gid == file_gid,
                              "Invalid file user or group.")
        return self
