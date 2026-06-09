# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
from collections.abc import Callable
from typing import Any

from motor.common.logger import get_logger
from motor.common.utils.config_watcher import ConfigWatcher

logger = get_logger(__name__)


def log_configuration_summary(config: Any, message_prefix: str | None = None) -> None:
    if config is None:
        return
    if message_prefix:
        logger.info(message_prefix)
    for line in config.get_config_summary().splitlines():
        if line.strip():
            logger.info(line)


def start_config_file_watcher(
    config: Any,
    on_config_updated: Callable[[], None],
) -> ConfigWatcher | None:
    if config.config_path and os.path.exists(config.config_path):
        watcher = ConfigWatcher(
            config_path=config.config_path,
            reload_callback=config.reload,
            config_update_callback=on_config_updated,
        )
        watcher.start()
        logger.info("Configuration file watcher started")
        return watcher
    return None
