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
from dataclasses import FrozenInstanceError

import pytest

from motor.config.service_discovery import (
    ServiceDiscoveryConfig,
)


def test_loader_defaults_to_direct_without_component_dependencies(tmp_path, monkeypatch):
    monkeypatch.delenv("USER_CONFIG_PATH", raising=False)
    assert not ServiceDiscoveryConfig.from_json().endpoints
    path = tmp_path / "user_config.json"
    path.write_text(json.dumps({"motor_controller_config": {"model_config": "unrelated"}}), encoding="utf-8")
    assert not ServiceDiscoveryConfig.from_json(str(path)).endpoints


def test_public_config_is_read_from_own_file_and_copies_members(tmp_path, monkeypatch):
    data = {
        "motor_deploy_config": {"service_discovery_config": {"probe_ips": {"controller": ["192.0.2.11"]}}},
        "motor_controller_config": {
            "api_config": {"controller_api_port": 2026},
            "model_config": "unrelated",
            "standby_config": {"enable_master_standby": False},
        },
    }
    path = tmp_path / "user_config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("USER_CONFIG_PATH", str(path))
    loaded = ServiceDiscoveryConfig.from_json()
    parsed = ServiceDiscoveryConfig.from_user_config(data)
    data["motor_controller_config"]["api_config"]["controller_api_port"] = 9999
    endpoint = parsed.endpoints["controller"][0]
    assert endpoint.address("api") == "192.0.2.11:2026"
    assert endpoint.identity == loaded.endpoints["controller"][0].identity == "192.0.2.11"
    assert parsed.uses_probe("controller")
    assert not parsed.uses_probe("coordinator")
    with pytest.raises(TypeError):
        endpoint.ports["api"] = 9999
    with pytest.raises(FrozenInstanceError):
        endpoint.host = "192.0.2.99"


@pytest.mark.parametrize(
    "discovery",
    [
        {"mode": "direct"},
        {"endpoints": {}},
        {"probe_ips": {"controller": []}},
        {"probe_ips": {"controller": "192.0.2.11"}},
        {"probe_ips": {"controller": None}},
        {"probe_ips": {"unknown": ["192.0.2.11"]}},
        {"probe_ips": {"controller": ["192.0.2.11", "192.0.2.11"]}},
        {"probe_ips": {"controller": ["2001:db8::1", "2001:db8:0:0:0:0:0:1"]}},
        {"probe_ips": {"controller": [""]}},
        {"probe_ips": {"controller": ["example.com"]}},
        {"probe_timeout_seconds": 0},
        {"probe_interval_seconds": True},
        {"max_staleness_seconds": float("nan")},
    ],
)
def test_invalid_explicit_discovery_config_does_not_fall_back(discovery):
    with pytest.raises(ValueError):
        ServiceDiscoveryConfig.from_user_config(
            {
                "motor_deploy_config": {"service_discovery_config": discovery},
                "motor_controller_config": {"api_config": {"controller_api_port": 2026}},
            }
        )


def test_coordinator_probe_uses_shared_management_and_optional_observability_ports():
    data = {
        "motor_deploy_config": {
            "service_discovery_config": {"probe_ips": {"coordinator": ["2001:db8::1", "2001:db8::2"]}},
        },
        "motor_coordinator_config": {
            "api_config": {"coordinator_api_mgmt_port": 3126, "coordinator_obs_port": 3127},
            "port_allocator_config": {"enable": False},
        },
    }
    config = ServiceDiscoveryConfig.from_user_config(data)
    assert not config.uses_probe("controller")
    assert config.uses_probe("coordinator")
    assert [member.address("management") for member in config.endpoints["coordinator"]] == [
        "[2001:db8::1]:3126",
        "[2001:db8::2]:3126",
    ]
    assert config.endpoints["coordinator"][0].ports == {"management": 3126, "observability": 3127}
    data["motor_coordinator_config"]["api_config"].pop("coordinator_obs_port")
    assert ServiceDiscoveryConfig.from_user_config(data).endpoints["coordinator"][0].ports == {"management": 3126}


@pytest.mark.parametrize(
    "service,field",
    [
        ("controller", "controller_api_port"),
        ("coordinator", "coordinator_api_mgmt_port"),
    ],
)
@pytest.mark.parametrize("port", [None, True, 0, 65536, "2026"])
def test_probe_requires_explicit_valid_shared_port(service, field, port):
    api = {} if port is None else {field: port}
    with pytest.raises(ValueError):
        ServiceDiscoveryConfig.from_user_config(
            {
                "motor_deploy_config": {"service_discovery_config": {"probe_ips": {service: ["192.0.2.11"]}}},
                "motor_" + service + "_config": {"api_config": api, "port_allocator_config": {"enable": False}},
            }
        )


@pytest.mark.parametrize("allocator", [{}, {"enable": True}, {"enable": 0}, {"enable": "false"}])
def test_coordinator_probe_rejects_automatic_port_allocation(allocator):
    with pytest.raises(ValueError, match="port_allocator_config.enable=false"):
        ServiceDiscoveryConfig.from_user_config(
            {
                "motor_deploy_config": {"service_discovery_config": {"probe_ips": {"coordinator": ["192.0.2.11"]}}},
                "motor_coordinator_config": {
                    "api_config": {"coordinator_api_mgmt_port": 3126},
                    "port_allocator_config": allocator,
                },
            }
        )
