#!/usr/bin/env python
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
import json
import sys
import traceback
import logging
from file_utils import safe_open

DEFAULT_MIN_FILE_SIZE = 1
DEFAULT_MAX_FILE_SIZE = 501 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format=(
        f'[%(asctime)s:%(msecs)04d+08:00] '
        f'[%(process)d] [{os.getppid()}] '
        f'[%(levelname)s] [%(filename)s:%(lineno)d] : %(message)s'
    ),
    datefmt='%Y-%m-%d %H:%M:%S'
)


class _FileUtils:
    """
    This is a class with some class methods
    to handle some file path check
    """

    @classmethod
    def check_file_exists(cls, file_path):
        return os.path.exists(file_path)

    @classmethod
    def regular_file_path(cls, file_path, base_dir="/", allow_symlink=False):
        """
        regular file path;
            1. check is path empty?
            2. file_path length more than 1024?
            3. if not allow symlink; check is symlink?
            4. linux shell: realpath file_path?
            5. path is in base dir[realpath]?

        :param base_dir: base_dir must a realpath; file path must in base dir
        :param file_path: path
        :param allow_symlink: default is False
        :return: check_status[True or False], err_msg[if False], real_file_path[if True]
        """
        if not file_path or not isinstance(file_path, str):
            err_msg = f"The file path is empty or not a string type."
            return False, err_msg, None

        if not base_dir or not isinstance(base_dir, str):
            err_msg = f"The base dir path is empty or not a string type."
            return False, err_msg, None

        if len(file_path) > 1024:
            err_msg = f"The file path exceeds the maximum length."
            return False, err_msg, None

        if not allow_symlink and os.path.islink(file_path):
            err_msg = f"The file path is a link."
            return False, err_msg, None

        try:
            real_file_path = os.path.realpath(file_path)
        except Exception as e:
            err_msg = f"Realpath parsing failed"
            return False, err_msg, None

        base_dir = base_dir if base_dir[-1] == "/" else base_dir + '/'
        if not cls.is_base_dir_path(base_dir, real_file_path):
            err_msg = f'the file path is not in base dir'
            return False, err_msg, None

        return True, None, real_file_path

    @classmethod
    def is_base_dir_path(cls, base_dir, path):
        abs_path = os.path.abspath(path)
        base_abs_path = os.path.abspath(base_dir)
        return os.path.commonpath([abs_path, base_abs_path]) == base_abs_path

    @classmethod
    def check_file_size(cls, file_path):
        """
        safe check file size

        :param file_path: path
        :return: check status, err_msg[if False]
        """
        # Check if the file exists
        if not cls.check_file_exists(file_path):
            err_msg = f"Error: File not found."
            return False, err_msg

        # Get the real_file_path
        flag, err_msg, real_file_path = cls.regular_file_path(file_path)
        if not flag:
            err_msg = f"Regular_file_path failed by: {err_msg}"
            return False, err_msg

        try:
            # Open the file in binary read mode
            with safe_open(real_file_path, "rb", permission_mode=0o640) as fp:
                # Seek to the end of the file
                fp.seek(0, os.SEEK_END)
                # Get the file size
                file_size = fp.tell()
            if file_size < DEFAULT_MIN_FILE_SIZE or file_size > DEFAULT_MAX_FILE_SIZE:
                err_msg = f"Read input file failed, file size is invalid"
                return False, err_msg
            return True, None
        except Exception as e:
            err_msg = f"Error: {str(e)}"
            return False, err_msg

    @classmethod
    def constrain_owner(cls, file_path, check_owner):
        try:
            file_stat = os.stat(file_path)
        except FileNotFoundError:
            err_msg = f"Error: File not found."
            return False, err_msg
        except PermissionError:
            err_msg = f"Error: Permission denied to access file: {file_path}"
            return False, err_msg
        except OSError as e:
            err_msg = f"Error accessing file {file_path}: {str(e)}"
            return False, err_msg
        except Exception as e:
            err_msg = f"Unexpected error accessing file {file_path}: {str(e)}"
            return False, err_msg

        current_user_id = os.getuid()
        file_owner_id = file_stat.st_uid

        if file_owner_id != current_user_id:
            err_msg = f"File owner ID mismatch. Current process user ID: {current_user_id}, " \
                      f"file owner ID: {file_owner_id} "
            if check_owner:
                return False, err_msg
            else:
                return True, err_msg

        return True, None

    @classmethod
    def constrain_permission(cls, file_path, mode, check_permission):
        try:
            file_stat = os.stat(file_path)
        except FileNotFoundError:
            err_msg = f"Error: File not found."
            return False, err_msg

        current_permissions = file_stat.st_mode & 0o777
        required_permissions = mode & 0o777

        for i in range(3):
            cur_perm = (current_permissions >> (i * 3)) & 0o7
            max_perm = (required_permissions >> (i * 3)) & 0o7
            if (cur_perm | max_perm) != max_perm:
                err_msg = f"Check {['Other group', 'Owner group', 'Owner'][i]} permission failed: " \
                          f"Current permission is {cur_perm}, but required no greater than {max_perm}. "
                if check_permission:
                    return False, err_msg
                else:
                    return True, err_msg
        return True, None

    @classmethod
    def is_file_valid(cls, file_path, mode=0o640, check_owner=True, check_permission=True) -> (bool, str):
        if not cls.check_file_exists(file_path):
            return False, "Error: File not found."

        check_flag, err_msg = cls.check_file_size(file_path)
        if not check_flag:
            return False, err_msg

        check_flag, err_msg = cls.constrain_owner(file_path, check_owner)
        if not check_flag:
            return False, err_msg

        check_flag, err_msg = cls.constrain_permission(file_path, mode, check_permission)
        if not check_flag:
            return False, err_msg

        return True, None


MINDIE_CONFIG_FILE_NAME = "conf/config.json"

ENV_KEY_MINDIE_PREFILL_BATCH_SIZE = "MINDIE_PREFILL_BATCH_SIZE"
ENV_KEY_MINDIE_INFER_MODE = "MINDIE_INFER_MODE"
ENV_KEY_MINDIE_DECODE_BATCH_SIZE = "MINDIE_DECODE_BATCH_SIZE"
ENV_KEY_MINDIE_MAX_SEQ_LEN = "MINDIE_MAX_SEQ_LEN"
ENV_KEY_MINDIE_MAX_ITER_TIMES = "MINDIE_MAX_ITER_TIMES"
ENV_KEY_MINDIE_MODEL_NAME = "MINDIE_MODEL_NAME"
ENV_KEY_MINDIE_MODEL_WEIGHT_PATH = "MINDIE_MODEL_WEIGHT_PATH"
ENV_KEY_MINDIE_ENDPOINT_HTTPS_ENABLED = "MINDIE_ENDPOINT_HTTPS_ENABLED"
ENV_KEY_MINDIE_INTER_COMM_TLS_ENABLED = "MINDIE_INTER_COMM_TLS_ENABLED"

CFG_KEY_SERVER_CONFIG = "ServerConfig"
CFG_KEY_SCHEDULE_CONFIG = "ScheduleConfig"
CFG_KEY_BACKEND_CONFIG = "BackendConfig"
CFG_KEY_MODEL_DEPLOY_CONFIG = "ModelDeployConfig"
CFG_KEY_MODEL_CONFIG = "ModelConfig"


logging.basicConfig(level=logging.INFO, format='%(asctime)s |%(levelname)s| %(message)s', stream=sys.stdout)


def __is_jsonobj_valid(cfg_json, key) -> bool:
    if key not in cfg_json:
        logging.error("%s missed in config file", key)
        return False
    if not isinstance(cfg_json[key], dict):
        logging.error("%s in config file should be an object", key)
        return False
    return True


def __is_model_config_valid(cfg_json) -> bool:
    if not __is_jsonobj_valid(cfg_json, CFG_KEY_BACKEND_CONFIG):
        return False
    if CFG_KEY_MODEL_DEPLOY_CONFIG not in cfg_json[CFG_KEY_BACKEND_CONFIG]:
        logging.error("%s.%s missed in config file", CFG_KEY_BACKEND_CONFIG, CFG_KEY_MODEL_DEPLOY_CONFIG)
        return False
    if CFG_KEY_MODEL_CONFIG not in cfg_json[CFG_KEY_BACKEND_CONFIG][CFG_KEY_MODEL_DEPLOY_CONFIG]:
        logging.error("%s.%s.%s missed in config file",
            CFG_KEY_BACKEND_CONFIG, CFG_KEY_MODEL_DEPLOY_CONFIG, CFG_KEY_MODEL_CONFIG)
        return False
    if not isinstance(cfg_json[CFG_KEY_BACKEND_CONFIG][CFG_KEY_MODEL_DEPLOY_CONFIG][CFG_KEY_MODEL_CONFIG], list):
        logging.error("invalid type of %s.%s.%s",
            CFG_KEY_BACKEND_CONFIG, CFG_KEY_MODEL_DEPLOY_CONFIG, CFG_KEY_MODEL_CONFIG)
        return False
    return True


def __is_schedule_config_valid(cfg_json) -> bool:
    if not __is_jsonobj_valid(cfg_json, CFG_KEY_BACKEND_CONFIG):
        return False
    if CFG_KEY_SCHEDULE_CONFIG not in cfg_json[CFG_KEY_BACKEND_CONFIG]:
        logging.error("%s.%s missed in config file", CFG_KEY_BACKEND_CONFIG, CFG_KEY_SCHEDULE_CONFIG)
        return False
    if not isinstance(cfg_json[CFG_KEY_BACKEND_CONFIG][CFG_KEY_SCHEDULE_CONFIG], dict):
        logging.error("invalid type of %s.%s", CFG_KEY_BACKEND_CONFIG, CFG_KEY_SCHEDULE_CONFIG)
        return False
    return True


# MINDIE_DECODE_BATCH_SIZE
def __update_mindie_decode_batch_size(cfg_json) -> bool:
    value = os.getenv(ENV_KEY_MINDIE_DECODE_BATCH_SIZE)
    if value is None:
        return True
    if not __is_schedule_config_valid(cfg_json):
        return False
    try:
        value = int(value)
    except ValueError:
        logging.error("invalid value of %s, should be an integer", ENV_KEY_MINDIE_DECODE_BATCH_SIZE)
        return False
    cfg_json[CFG_KEY_BACKEND_CONFIG][CFG_KEY_SCHEDULE_CONFIG]["maxBatchSize"] = value
    return True


# MINDIE_MAX_ITER_TIMES
def __update_mindie_max_iter_times(cfg_json) -> bool:
    value = os.getenv(ENV_KEY_MINDIE_MAX_ITER_TIMES)
    if value is None:
        return True
    if not __is_schedule_config_valid(cfg_json):
        return False
    try:
        value = int(value)
    except ValueError:
        logging.error("invalid value of %s, should be an integer", ENV_KEY_MINDIE_MAX_ITER_TIMES)
        return False
    cfg_json[CFG_KEY_BACKEND_CONFIG][CFG_KEY_SCHEDULE_CONFIG]["maxIterTimes"] = value
    return True


# MINDIE_MAX_SEQ_LEN
def __update_mindie_max_seq_len(cfg_json) -> bool:
    value = os.getenv(ENV_KEY_MINDIE_MAX_SEQ_LEN)
    if value is None:
        return True
    if not __is_jsonobj_valid(cfg_json, CFG_KEY_BACKEND_CONFIG):
        return False
    if CFG_KEY_MODEL_DEPLOY_CONFIG not in cfg_json[CFG_KEY_BACKEND_CONFIG]:
        logging.error("%s.%s missed in config file", CFG_KEY_BACKEND_CONFIG, CFG_KEY_MODEL_DEPLOY_CONFIG)
        return False
    if not isinstance(cfg_json[CFG_KEY_BACKEND_CONFIG][CFG_KEY_MODEL_DEPLOY_CONFIG], dict):
        logging.error("invalid type of %s.%s", CFG_KEY_BACKEND_CONFIG, CFG_KEY_MODEL_DEPLOY_CONFIG)
        return False
    try:
        value = int(value)
    except ValueError:
        logging.error("invalid value of %s, should be an integer", ENV_KEY_MINDIE_MAX_SEQ_LEN)
        return False
    cfg_json[CFG_KEY_BACKEND_CONFIG][CFG_KEY_MODEL_DEPLOY_CONFIG]["maxSeqLen"] = value
    return True


# MINDIE_MODEL_NAME
def __update_mindie_model_name(cfg_json) -> bool:
    value = os.getenv(ENV_KEY_MINDIE_MODEL_NAME)
    if value is None:
        return True
    if not __is_model_config_valid(cfg_json):
        return False
    cfg_json[CFG_KEY_BACKEND_CONFIG][CFG_KEY_MODEL_DEPLOY_CONFIG][CFG_KEY_MODEL_CONFIG][0]["modelName"] = value
    return True


# MINDIE_MODEL_WEIGHT_PATH
def __update_mindie_model_weight_path(cfg_json) -> bool:
    value = os.getenv(ENV_KEY_MINDIE_MODEL_WEIGHT_PATH)
    if value is None:
        return True
    if not __is_model_config_valid(cfg_json):
        return False
    cfg_json[CFG_KEY_BACKEND_CONFIG][CFG_KEY_MODEL_DEPLOY_CONFIG][CFG_KEY_MODEL_CONFIG][0]["modelWeightPath"] = value
    return True


# MINDIE_PREFILL_BATCH_SIZE
def __update_mindie_prefill_batch_size(cfg_json) -> bool:
    value = os.getenv(ENV_KEY_MINDIE_PREFILL_BATCH_SIZE)
    if value is None:
        return True
    if not __is_schedule_config_valid(cfg_json):
        return False
    try:
        value = int(value)
    except ValueError:
        logging.error("invalid value of %s, should be an integer", ENV_KEY_MINDIE_PREFILL_BATCH_SIZE)
        return False
    cfg_json[CFG_KEY_BACKEND_CONFIG][CFG_KEY_SCHEDULE_CONFIG]["maxPrefillBatchSize"] = value
    return True


# MINDIE_INFER_MODE
def __update_mindie_infer_mode(cfg_json) -> bool:
    value = os.getenv(ENV_KEY_MINDIE_INFER_MODE)
    if value is None:
        return True
    if not __is_jsonobj_valid(cfg_json, CFG_KEY_SERVER_CONFIG):
        return False
    cfg_json[CFG_KEY_SERVER_CONFIG]["inferMode"] = value
    return True


# MINDIE_ENDPOINT_HTTPS_ENABLED
def __update_mindie_endpoint_https_enabled(cfg_json) -> bool:
    value = os.getenv(ENV_KEY_MINDIE_ENDPOINT_HTTPS_ENABLED)
    if value is None or (value != "true" and value != "false"):
        return True
    if not __is_jsonobj_valid(cfg_json, CFG_KEY_SERVER_CONFIG):
        return False
    if value == "true":
        cfg_json[CFG_KEY_SERVER_CONFIG]["httpsEnabled"] = True
    elif value == "false":
        cfg_json[CFG_KEY_SERVER_CONFIG]["httpsEnabled"] = False
    return True


# MINDIE_INTER_COMM_TLS_ENABLED
def __update_mindie_inter_comm_tls_enabled(cfg_json) -> bool:
    value = os.getenv(ENV_KEY_MINDIE_INTER_COMM_TLS_ENABLED)
    if value is None or (value != "true" and value != "false"):
        return True
    if not __is_jsonobj_valid(cfg_json, CFG_KEY_SERVER_CONFIG):
        return False
    if value == "true":
        cfg_json[CFG_KEY_SERVER_CONFIG]["interCommTLSEnabled"] = True
    elif value == "false":
        cfg_json[CFG_KEY_SERVER_CONFIG]["interCommTLSEnabled"] = False
    return True


ENV_HANDLERS = {
    ENV_KEY_MINDIE_INFER_MODE: __update_mindie_infer_mode,
    ENV_KEY_MINDIE_PREFILL_BATCH_SIZE: __update_mindie_prefill_batch_size,
    ENV_KEY_MINDIE_MAX_ITER_TIMES: __update_mindie_max_iter_times,
    ENV_KEY_MINDIE_DECODE_BATCH_SIZE: __update_mindie_decode_batch_size,
    ENV_KEY_MINDIE_MAX_SEQ_LEN: __update_mindie_max_seq_len,
    ENV_KEY_MINDIE_MODEL_NAME: __update_mindie_model_name,
    ENV_KEY_MINDIE_MODEL_WEIGHT_PATH: __update_mindie_model_weight_path,
    ENV_KEY_MINDIE_ENDPOINT_HTTPS_ENABLED: __update_mindie_endpoint_https_enabled,
    ENV_KEY_MINDIE_INTER_COMM_TLS_ENABLED: __update_mindie_inter_comm_tls_enabled
}


class MindIEConfigHelper:
    @staticmethod
    def update_mindie_config() -> bool:

        config_file_path, check_permission = MindIEConfigHelper.__get_config_file_path()
        if config_file_path == "":
            logging.error("get config file failed")
            return False
        file_valid, err_msg = _FileUtils.is_file_valid(config_file_path, check_permission=check_permission)
        if not file_valid:
            logging.error("config file is not valid: %s", err_msg)
            return False
        try:
            fd = os.open(config_file_path, os.O_RDONLY, 0o600)
            with os.fdopen(fd, 'r') as file:
                cfg_json = json.load(file)
                for handler in ENV_HANDLERS.values():
                    if not handler(cfg_json):
                        return False
            fd = os.open(config_file_path, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as file:
                json.dump(cfg_json, file, indent=4)
        except json.JSONDecodeError as e:
            logging.error("invalid json: %s", e)
            traceback.print_exc()
            return False
        except FileNotFoundError as e:
            logging.error("config.json not found: %s", e)
            traceback.print_exc()
            return False
        except Exception as e:
            logging.error("parse config.json failed: %s", e)
            traceback.print_exc()
            return False

        return True

    @staticmethod
    def __get_config_file_path() -> (str, bool):
        user_defined_config_file_path = os.getenv("MIES_CONFIG_JSON_PATH")
        if user_defined_config_file_path is not None and user_defined_config_file_path != "":
            return user_defined_config_file_path, os.getenv("MINDIE_CHECK_INPUTFILES_PERMISSION") != "0"
        root_path = os.getenv('MIES_INSTALL_PATH')
        if root_path is None or root_path == "":
            logging.error("env MIES_INSTALL_PATH not found.")
            return "", True
        return os.path.join(root_path, MINDIE_CONFIG_FILE_NAME), True


if __name__ == '__main__':
    result = MindIEConfigHelper.update_mindie_config()
    if not result:
        logging.error("Update MindIE Config failed!")
        sys.exit(1)