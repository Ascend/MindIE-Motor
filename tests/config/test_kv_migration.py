# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
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

import pytest

from motor.config.controller import ControllerConfig
from motor.config.node_manager import NodeManagerConfig


@pytest.fixture(autouse=True)
def node_manager_environment(monkeypatch):
    monkeypatch.setenv("ROLE", "decode")
    # The production safe_open enforces Linux path and ownership rules.
    if os.name == "nt":
        monkeypatch.setattr("motor.config.node_manager.safe_open", open)


@pytest.mark.parametrize("loader", [ControllerConfig.from_json, NodeManagerConfig.from_json])
@pytest.mark.parametrize(
    "settings, expected", [({}, False), ({"enable_kv_migration": True}, True), ({"enable_kv_migration": False}, False)]
)
def test_load_kv_migration_gate_from_shared_config(tmp_path, loader, settings, expected):
    path = tmp_path / "user_config.json"
    path.write_text(json.dumps({"motor_controller_config": {"fault_tolerance_config": settings}}), encoding="utf-8")
    assert loader(str(path)).fault_tolerance_config.enable_kv_migration is expected


@pytest.mark.parametrize("loader", [ControllerConfig.from_json, NodeManagerConfig.from_json])
@pytest.mark.parametrize("value", ["false", 0, None])
def test_migration_gate_requires_a_json_boolean(tmp_path, loader, value):
    path = tmp_path / "user_config.json"
    config = {"motor_controller_config": {"fault_tolerance_config": {"enable_kv_migration": value}}}
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError, match="enable_kv_migration must be a boolean"):
        loader(str(path))


@pytest.mark.parametrize("startup", [True, False])
def test_reload_preserves_startup_gate_and_updates_migration_budget(tmp_path, startup):
    path = tmp_path / "config.json"
    settings = {"enable_kv_migration": startup}
    data = {"motor_controller_config": {"fault_tolerance_config": settings}}
    path.write_text(json.dumps(data), encoding="utf-8")
    config = ControllerConfig.from_json(str(path))
    settings.update(enable_kv_migration=not startup, kv_migration_timeout_sec=18)
    path.write_text(json.dumps(data), encoding="utf-8")
    config.last_modified = None
    assert config.reload()
    assert config.fault_tolerance_config.enable_kv_migration is startup
    assert config.fault_tolerance_config.kv_migration_timeout_sec == 18
    assert ControllerConfig.from_json(str(path)).fault_tolerance_config.enable_kv_migration is not startup


@pytest.mark.parametrize(
    "settings, expected",
    [
        ({}, False),
        ({"enable_kv_migration_on_reconfiguration": True}, True),
        ({"enable_hw_evacuate_migration": True}, True),
        ({"enable_hw_evacuate_migration": False}, False),
        ({"enable_hw_evacuate_migration": True, "enable_kv_migration_on_reconfiguration": False}, False),
        ({"enable_kv_migration_on_reconfiguration": True, "enable_hw_evacuate_migration": False}, True),
    ],
)
def test_load_reconfiguration_migration_switch(tmp_path, settings, expected):
    path = tmp_path / "user_config.json"
    settings = {"enable_kv_migration": True, **settings}
    path.write_text(json.dumps({"motor_controller_config": {"fault_tolerance_config": settings}}), encoding="utf-8")
    ft_config = ControllerConfig.from_json(str(path)).fault_tolerance_config
    assert ft_config.enable_kv_migration_on_reconfiguration is expected


@pytest.mark.parametrize("field", ["enable_kv_migration_on_reconfiguration", "enable_hw_evacuate_migration"])
def test_reconfiguration_migration_switch_requires_a_boolean(tmp_path, field):
    path = tmp_path / "user_config.json"
    settings = {"enable_kv_migration": False, field: "false"}
    path.write_text(json.dumps({"fault_tolerance_config": settings}), encoding="utf-8")
    ControllerConfig.from_json(str(path))
    settings["enable_kv_migration"] = True
    path.write_text(json.dumps({"fault_tolerance_config": settings}), encoding="utf-8")
    with pytest.raises(ValueError, match="enable_kv_migration_on_reconfiguration must be a boolean"):
        ControllerConfig.from_json(str(path))
