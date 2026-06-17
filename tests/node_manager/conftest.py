#!/usr/bin/env python3
# -*- coding: utf-8 -*-
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

import pytest
from unittest.mock import mock_open, patch

from motor.common.resources.instance import ParallelConfig, PDRole


TEST_ENV_VARS = {
    'JOB_NAME': 'test_job',
    'CONFIG_PATH': 'tests/jsons',
}


def setup_test_environment():
    return patch.dict('os.environ', TEST_ENV_VARS)


_env_patcher = setup_test_environment()
_env_patcher.start()


def teardown_test_environment():
    _env_patcher.stop()


@pytest.fixture(name="config_data")
def _config_data():
    return {
        "parallel_config": {"tp_size": 2, "pp_size": 1},
        "role": "both",
        "controller_api_dns": "localhost",
        "controller_api_port": 8080,
        "node_manager_port": 8080,
        "model_name": "vllm",
    }


def create_config_mock(cfg):
    def mock_side_effect(file_path, mode):
        file_path_str = str(file_path)
        if "user_config.json" in file_path_str:
            return mock_open(read_data=json.dumps(cfg)).return_value
        return mock_open().return_value

    return mock_side_effect


def apply_node_manager_test_config(config, cfg):
    config.basic_config.parallel_config = ParallelConfig(
        tp_size=cfg["parallel_config"]["tp_size"], pp_size=cfg["parallel_config"]["pp_size"]
    )
    config.basic_config.job_name = cfg.get("model_name", "test_job")
    config.basic_config.role = PDRole(cfg.get("role", "both"))
    config.api_config.node_manager_port = cfg.get("node_manager_port", 8080)
