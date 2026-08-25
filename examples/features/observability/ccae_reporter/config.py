# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import json
import os
from typing import Any

from ccae_reporter.common.logging import Log
from ccae_reporter.common.util import safe_open
from ccae_reporter.common.util import PathCheck


class ConfigUtil:
    config = None
    logger = None

    @classmethod
    def log_warning(cls, msg: str):
        if not cls.logger:
            cls.logger = Log(__name__).getlog()
        cls.logger.warning(msg)

    @classmethod
    def get_config(cls, key: str, default: Any = None) -> Any:
        if not cls.config:
            config_dir = os.getenv('CONFIG_PATH')
            if config_dir is None:
                raise RuntimeError("Environment variable $CONFIG_PATH is not set.")
            config_dir = os.path.realpath(config_dir)
            if not PathCheck.check_path_full(config_dir):
                raise RuntimeError("Failed to check `CONFIG_PATH`")
            config_file_path = os.path.join(config_dir, "user_config.json")
            with safe_open(config_file_path) as f:
                cls.config = json.loads(f.read())

        # support jsonpath key, such as "motor_deploy_config.tls_config"
        keys = key.split('.')
        value = cls.config

        try:
            for k in keys:
                if not isinstance(value, dict):
                    return default
                value = value.get(k, None)
            if value is None:
                return default
            return value
        except (KeyError, TypeError):
            cls.log_warning("%s is not in config of ms controller" % key)
            return default
