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
import sys
import tempfile
import pytest
from pathlib import Path

from motor.common.utils.validators import (
    ClassValidator,
    DirectoryValidator,
    FileValidator,
    IntValidator,
    MapValidator,
    RankSizeValidator,
    StringValidator,
    Validator,
)

BIN_PATH = "/usr/bin"
DIRECTORY_BLACKLIST_PATH = "/abc/d/e"

# Check if running on Windows
IS_WINDOWS = sys.platform == "win32"


def test_validator_should_return_default_if_invalid():
    validation = Validator("aa")
    validation.register_checker(lambda x: len(x) < 5, "length of string should be less than 5")
    assert validation.is_valid()
    validation = Validator("123456")
    validation.register_checker(lambda x: len(x) < 5, "length of string should be less than 5")
    assert not validation.is_valid()
    assert validation.get_value("DEFAULT") == "DEFAULT"


def test_string_validator_max_len_parameter():
    assert not StringValidator("aa.1245", max_len=3).check_string_length().check().is_valid()
    assert StringValidator("aa.1245", max_len=30).check_string_length().check().is_valid()
    # default infinity
    assert StringValidator("aa.1234564646546").check_string_length().check().is_valid()


def test_string_validator_min_len_parameter():
    assert not StringValidator("aa", min_len=3).check_string_length().check().is_valid()
    assert StringValidator("aaa", min_len=3).check_string_length().check().is_valid()
    # default infinity
    assert StringValidator("a").check_string_length().check().is_valid()


def test_string_validator_can_be_transformed2int():
    assert not StringValidator("a").can_be_transformed2int().check().is_valid()
    assert not StringValidator("9" * 20).can_be_transformed2int().check().is_valid()
    assert not StringValidator("1,2").can_be_transformed2int().check().is_valid()
    assert StringValidator("12").can_be_transformed2int().check().is_valid()
    assert not StringValidator("12").can_be_transformed2int(min_value=100, max_value=200).check().is_valid()


def test_string_validator_contain_sensitive_words():
    assert (
        not StringValidator("passwordme")
        .check_not_contain_black_element("pass")
        .check_string_length()
        .check()
        .is_valid()
    )


def test_map_validator_should_contain_inclusive_keys():
    map_validator = MapValidator({"a": True, "b": {"c": "1234"}}, inclusive_keys=["a", "b"])
    assert map_validator.is_valid()


def test_directory_black_list():
    # On Windows, /abc/d/e paths may not resolve correctly, so use temp directory for testing
    if IS_WINDOWS:
        temp_dir = tempfile.mkdtemp()
        test_path = os.path.join(temp_dir, "test_dir")
        os.makedirs(test_path, exist_ok=True)
        try:
            # Test exact match
            assert not DirectoryValidator(test_path).with_blacklist(lst=[test_path]).check().is_valid()
            assert DirectoryValidator(test_path).with_blacklist(lst=[""]).check().is_valid()
            # Test parent path with exact_compare=True (should be valid)
            assert DirectoryValidator(test_path).with_blacklist([temp_dir], exact_compare=True).check().is_valid()
            # Test parent path with exact_compare=False (should be invalid as test_path is child of temp_dir)
            assert not DirectoryValidator(test_path).with_blacklist([temp_dir], exact_compare=False).check().is_valid()
        finally:
            import shutil

            shutil.rmtree(temp_dir, ignore_errors=True)
    else:
        assert (
            not DirectoryValidator(DIRECTORY_BLACKLIST_PATH)
            .with_blacklist(lst=[DIRECTORY_BLACKLIST_PATH])
            .check()
            .is_valid()
        )
        assert DirectoryValidator(DIRECTORY_BLACKLIST_PATH).with_blacklist(lst=[""]).check().is_valid()
        assert (
            DirectoryValidator(DIRECTORY_BLACKLIST_PATH)
            .with_blacklist(["/abc/d/"], exact_compare=True)
            .check()
            .is_valid()
        )
        # if not exact compare, the /abc/d/e is chirldren path of /abc/d/, so it is invalid
        assert (
            not DirectoryValidator(DIRECTORY_BLACKLIST_PATH)
            .with_blacklist(["/abc/d/"], exact_compare=False)
            .check()
            .is_valid()
        )
        assert DirectoryValidator("/usr/bin/bash").with_blacklist().check().is_valid()
        assert not DirectoryValidator("/usr/bin/bash").with_blacklist(exact_compare=False).check().is_valid()


def test_remove_prefix():
    assert DirectoryValidator.remove_prefix(BIN_PATH, None)[1] == BIN_PATH
    assert DirectoryValidator.remove_prefix(BIN_PATH, "")[1] == BIN_PATH
    assert DirectoryValidator.remove_prefix(None, "abc")[1] is None
    assert DirectoryValidator.remove_prefix("/usr/bin/python", BIN_PATH)[1] == "/python"


def test_directory_white_list():
    assert DirectoryValidator.check_is_children_path("/abc/d", DIRECTORY_BLACKLIST_PATH)
    assert DirectoryValidator.check_is_children_path("/abc/d", "/abc/d")
    assert not DirectoryValidator.check_is_children_path("/abc/d", "/abc/de")
    assert DirectoryValidator.check_is_children_path("/usr/bin", "/usr/bin/bash")


def test_directory_soft_link():
    # Skip on Windows as creating symlinks requires admin privileges or developer mode
    if IS_WINDOWS:
        pytest.skip("Symlink creation requires admin privileges on Windows")

    temp_dir = tempfile.mkdtemp()
    path = os.path.join(temp_dir, "link.ink")
    with tempfile.NamedTemporaryFile(delete=True) as tmp:
        # make a soft link
        os.symlink(tmp.name, path)
        try:
            # do stuff with temp
            tmp.write(b"stuff")
            assert not DirectoryValidator(path).check_not_soft_link().check().is_valid()
        finally:
            os.remove(path)
            os.removedirs(temp_dir)


def test_directory_check():
    assert not DirectoryValidator("a/b/.././c/a.txt").check_is_not_none().check_dir_name().check().is_valid()
    assert not DirectoryValidator("").check_is_not_none().check_dir_name().check().is_valid()
    assert not DirectoryValidator(None).check_is_not_none().check_dir_name().check().is_valid()
    assert DirectoryValidator("a/bc/d").check_is_not_none().check_dir_name().check().is_valid()
    assert (
        DirectoryValidator("/user/restore/fault/config", max_len=255)
        .check_is_not_none()
        .check_dir_name()
        .path_should_exist(is_file=True, msg="can not find the fault ranks config file")
        .should_not_contains_sensitive_words()
        .with_blacklist()
        .check()
    )
    assert (
        DirectoryValidator(os.path.dirname(__file__), max_len=255)
        .check_is_not_none()
        .check_dir_name()
        .check_dir_file_number()
        .path_should_exist(is_file=False, msg="can not find the fault ranks config file")
        .should_not_contains_sensitive_words()
        .with_blacklist()
        .check()
    )
    assert not DirectoryValidator(os.path.dirname(__file__)).path_should_not_exist().check().is_valid()


def test_check_directory_permissions():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        test_dir = temp_path / "test_dir"
        test_dir.mkdir()
        os.chmod(test_dir, 0o777)
        target_mode = 0o750
        assert not DirectoryValidator(test_dir).check_directory_permissions(target_mode).check().is_valid()


def test_rank_size_check():
    assert not RankSizeValidator(4096).check_rank_size_valid().check().is_valid()
    assert not RankSizeValidator(0).check_device_num_valid().check().is_valid()
    assert RankSizeValidator(1).check_rank_size_valid().check().is_valid()


def test_file_check():
    file_path = os.path.join(os.path.dirname(__file__), "test_data", "test.txt")
    assert not FileValidator(file_path).check_file_size().check().is_valid()
    assert FileValidator(file_path).check_not_soft_link().check().is_valid()
    # Skip chown and check_user_group on Windows as os.chown, os.geteuid, os.getegid don't exist
    if not IS_WINDOWS:
        os.chown(file_path, os.getuid(), os.getgid())
        assert FileValidator(file_path).check_user_group().check().is_valid()


def test_int_check():
    assert IntValidator(1, min_value=0, max_value=12).check_value().check().is_valid()


def test_class_check():
    assert ClassValidator(2, int).check_isinstance().is_valid()
