# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

# tests/examples/deployer/test_config_validator.py
import sys
from pathlib import Path
import pytest

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

import lib.constant as C  # noqa: E402
from lib.config_validator import (  # noqa: E402
    validate_reserved_labels,
    resolve_config_paths,
    strip_instance_nums,
    _get_hardware_node_labels,
    validate_deploy_mode_value,
)


def test_validate_reserved_labels():
    # 正常情况（不包含保留字）
    assert validate_reserved_labels({"comp": {"additional_labels": {"custom": "val"}}}) is None
    # 异常情况（尝试覆盖 "app" 保留字）
    with pytest.raises(ValueError, match="reserved labels"):
        validate_reserved_labels({"comp": {"additional_labels": {"app": "my-app"}}})
    # 兼容非字典输入
    assert validate_reserved_labels("not-a-dict") is None


def test_resolve_config_paths_missing_all():
    # 什么都不传，应该直接报错
    with pytest.raises(ValueError, match="Missing required configuration"):
        resolve_config_paths(None, None, None)


def test_resolve_config_paths_dir_valid(tmp_path):
    # 模拟一个包含所需文件的目录
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "user_config.json").touch()
    (config_dir / "env.json").touch()

    u_path, e_path = resolve_config_paths(str(config_dir), None, None)
    assert u_path == str(config_dir / "user_config.json")
    assert e_path == str(config_dir / "env.json")


def test_resolve_config_paths_dir_missing_files(tmp_path):
    # 目录存在，但里面没有对应的 json
    config_dir = tmp_path / "empty_dir"
    config_dir.mkdir()

    with pytest.raises(FileNotFoundError, match="user_config.json not found"):
        resolve_config_paths(str(config_dir), None, None)


def test_strip_instance_nums():
    import copy

    # 验证扩缩容校验时，实例数会被临时剔除
    config = {
        "motor_deploy_config": {
            C.E_INSTANCES_NUM: 1,
            C.P_INSTANCES_NUM: 2,
            C.D_INSTANCES_NUM: 3,
            C.HYBRID_INSTANCES_NUM: 4,
            C.DEPLOY_MODE_CONFIG_KEY: "mode",
            "other_key": "keep_me",
        }
    }

    # 保留一份深拷贝，用于验证原 config 未被修改
    original_config = copy.deepcopy(config)

    cleaned = strip_instance_nums(config)

    # 断言：返回的 cleaned 中，所有 instance_num 和 deploy_mode 都被移除了
    assert C.E_INSTANCES_NUM not in cleaned["motor_deploy_config"]
    assert C.P_INSTANCES_NUM not in cleaned["motor_deploy_config"]
    assert C.D_INSTANCES_NUM not in cleaned["motor_deploy_config"]
    assert C.HYBRID_INSTANCES_NUM not in cleaned["motor_deploy_config"]
    assert C.DEPLOY_MODE_CONFIG_KEY not in cleaned["motor_deploy_config"]

    # 断言：其他无关的键仍然保留
    assert cleaned["motor_deploy_config"]["other_key"] == "keep_me"

    # 断言：原 config 未被修改（证明是深拷贝）
    assert config == original_config
    assert C.P_INSTANCES_NUM in config["motor_deploy_config"]
    assert C.DEPLOY_MODE_CONFIG_KEY in config["motor_deploy_config"]


def test_validate_deploy_mode_value():
    # 正常的模式 (使用 sorted 避免 set 无序导致的不稳定)
    assert validate_deploy_mode_value(sorted(C.VALID_DEPLOY_MODES)[0]) is None
    # 异常模式
    with pytest.raises(ValueError, match="invalid"):
        validate_deploy_mode_value("fake_mode")


def test_get_hardware_node_labels():
    # A5 硬件标签 (使用 sorted 避免 set 无序导致的不稳定)
    labels_a5 = _get_hardware_node_labels(sorted(C.HARDWARE_TYPE_A5)[0])
    assert labels_a5 == {C.ACCELERATOR: C.ACCELERATOR_A5}

    # 未知硬件报错
    with pytest.raises(ValueError, match="Unknown hardware_type"):
        _get_hardware_node_labels("fake_hardware")
