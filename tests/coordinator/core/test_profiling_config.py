# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""CoordinatorConfig.profiling_config defaults, JSON loading and validation."""

import json

import pytest

from motor.config.coordinator import CoordinatorConfig, ProfilingConfig


def test_profiling_config_defaults_are_disabled():
    config = CoordinatorConfig()

    assert config.profiling_config == ProfilingConfig()
    assert config.profiling_config.enable is False
    assert config.profiling_config.request_timeout_seconds == 10.0
    assert config.profiling_config.max_targets == 1024


def test_profiling_config_loads_from_user_config(tmp_path):
    user_config = {
        "motor_coordinator_config": {
            "profiling_config": {
                "enable": True,
                "request_timeout_seconds": 5,
                "max_targets": 64,
            }
        }
    }
    config_path = tmp_path / "user_config.json"
    config_path.write_text(json.dumps(user_config), encoding="utf-8")

    config = CoordinatorConfig.from_json(str(config_path))

    assert config.profiling_config.enable is True
    assert config.profiling_config.request_timeout_seconds == 5
    assert config.profiling_config.max_targets == 64


def test_profiling_config_round_trips_through_to_dict():
    config = CoordinatorConfig()
    config.profiling_config.enable = True
    config.profiling_config.max_targets = 3

    serialized = config.to_dict()

    assert serialized["profiling_config"] == {"enable": True, "request_timeout_seconds": 10.0, "max_targets": 3}


@pytest.mark.parametrize(
    ("field_name", "value", "expected"),
    [
        ("request_timeout_seconds", 0, "request_timeout_seconds must be greater than 0"),
        ("max_targets", -1, "max_targets must be greater than 0"),
    ],
)
def test_profiling_config_rejects_invalid_values(field_name, value, expected):
    config = CoordinatorConfig()
    setattr(config.profiling_config, field_name, value)

    with pytest.raises(ValueError, match=expected):
        config.validate_config()


def test_profiling_config_rejects_non_boolean_enable():
    config = CoordinatorConfig()
    config.profiling_config.enable = "yes"

    with pytest.raises(ValueError, match="profiling_config.enable must be a bool"):
        config.validate_config()
