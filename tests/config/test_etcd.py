# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Static ETCD address normalization and legacy JSON compatibility."""

import json
from dataclasses import asdict

import pytest

from motor.config.etcd import EtcdConfig


@pytest.mark.parametrize(
    "data,hosts",
    [
        ({}, ["etcd.default.svc.cluster.local"]),
        ({"etcd_host": "etcd.local"}, ["etcd.local"]),
        ({"etcd_host": "etcd.default.svc.cluster.local."}, ["etcd.default.svc.cluster.local."]),
        ({"etcd_hosts": ["[2001:db8::1]"]}, ["[2001:db8::1]"]),
        ({"etcd_hosts": ["192.0.2.1", "192.0.2.2"]}, ["192.0.2.1", "192.0.2.2"]),
        ({"etcd_host": "192.0.2.1", "etcd_hosts": ["192.0.2.1"]}, ["192.0.2.1"]),
        ({"etcd_hosts": ["2001:db8::1", "2001:db8::2"]}, ["2001:db8::1", "2001:db8::2"]),
    ],
)
def test_etcd_normalizes_addresses_to_one_canonical_field(data, hosts):
    config = EtcdConfig.from_dict(data)
    config.validate_hosts()
    assert config.etcd_hosts == hosts
    assert config.etcd_port == 2379
    assert config.etcd_tls_server_name == ""
    assert config.enable_etcd_persistence is False
    assert "etcd_host" not in asdict(config)


@pytest.mark.parametrize(
    "data",
    [
        {"etcd_hosts": []},
        {"etcd_hosts": None},
        {"etcd_hosts": "192.0.2.1"},
        {"etcd_hosts": [""]},
        {"etcd_hosts": [None]},
        {"etcd_hosts": [" 192.0.2.1"]},
        {"etcd_hosts": ["http://192.0.2.1:32379"]},
        {"etcd_hosts": ["192.0.2.1", "2001:db8::1"]},
        {"etcd_hosts": ["etcd-a.local", "etcd-b.local"]},
        {"etcd_host": None},
        {"etcd_host": ""},
        {"etcd_host": "192.0.2.1", "etcd_hosts": ["192.0.2.2"]},
        {"etcd_host": "192.0.2.1", "etcd_hosts": ["192.0.2.1", "192.0.2.2"]},
        {"etcd_host": "192.0.2.1", "etcd_hosts": None},
    ],
)
def test_invalid_explicit_addresses_never_fall_back_to_default(data):
    with pytest.raises(ValueError):
        EtcdConfig.from_dict(data).validate_hosts()


@pytest.mark.parametrize("component", ["controller", "coordinator"])
@pytest.mark.parametrize("field,value", [("etcd_host", "192.0.2.1"), ("etcd_hosts", ["192.0.2.1", "192.0.2.2"])])
def test_component_json_loaders_normalize_old_and_new_addresses(tmp_path, component, field, value):
    from motor.config.controller import ControllerConfig
    from motor.config.coordinator import CoordinatorConfig

    config_type = ControllerConfig if component == "controller" else CoordinatorConfig
    path = tmp_path / "user_config.json"
    section = {"etcd_config": {field: value, "etcd_port": 32379}}
    path.write_text(json.dumps({"motor_" + component + "_config": section}), encoding="utf-8")
    config = config_type.from_json(str(path))
    assert config.etcd_config.etcd_hosts == ([value] if field == "etcd_host" else value)
    assert config.etcd_config.etcd_port == 32379
    assert config.etcd_config.enable_etcd_persistence is False
    assert config.etcd_tls_config.enable_tls is False
    assert "etcd_host" not in config.to_dict()["etcd_config"]

    section["etcd_config"]["etcd_hosts"] = []
    section["etcd_config"]["enable_etcd_persistence"] = True
    path.write_text(json.dumps({"motor_" + component + "_config": section}), encoding="utf-8")
    with pytest.raises(ValueError):
        config_type.from_json(str(path))


@pytest.mark.parametrize("component", ["controller", "coordinator"])
def test_component_multi_host_tls_requires_explicit_certificate_identity(tmp_path, component):
    from motor.config.controller import ControllerConfig
    from motor.config.coordinator import CoordinatorConfig

    config_type = ControllerConfig if component == "controller" else CoordinatorConfig
    path = tmp_path / "user_config.json"
    section = {
        "etcd_config": {"etcd_hosts": ["192.0.2.1", "192.0.2.2"], "enable_etcd_persistence": True},
        "etcd_tls_config": {"enable_tls": True},
    }
    path.write_text(json.dumps({"motor_" + component + "_config": section}), encoding="utf-8")
    with pytest.raises(ValueError, match="Multiple etcd_hosts with TLS"):
        config_type.from_json(str(path))

    section["etcd_config"]["etcd_tls_server_name"] = "motor-etcd.test"
    path.write_text(json.dumps({"motor_" + component + "_config": section}), encoding="utf-8")
    config = config_type.from_json(str(path))
    assert config.etcd_config.etcd_tls_server_name == "motor-etcd.test"
    assert config.etcd_tls_config.enable_tls is True

    del section["etcd_config"]["etcd_tls_server_name"]
    section["etcd_config"]["etcd_hosts"] = ["etcd.default.svc.cluster.local."]
    path.write_text(json.dumps({"motor_" + component + "_config": section}), encoding="utf-8")
    config = config_type.from_json(str(path))
    assert config.etcd_tls_config.enable_tls is True
    assert config.etcd_config.etcd_hosts == ["etcd.default.svc.cluster.local."]
    assert config.etcd_config.etcd_tls_server_name == ""


@pytest.mark.parametrize(
    "name", [None, 123, " ", " motor-etcd.test", "https://motor-etcd.test", "motor-etcd.test:2379"]
)
def test_tls_certificate_identity_rejects_invalid_explicit_values(name):
    config = EtcdConfig(etcd_tls_server_name=name)
    with pytest.raises(ValueError, match="etcd_tls_server_name"):
        config.validate_hosts(enable_tls=True)
    # Disabling TLS leaves certificate settings inactive.
    config.validate_hosts(enable_tls=False)


@pytest.mark.parametrize("ha_enabled,persistence_enabled", [(False, False), (True, False), (False, True)])
def test_controller_validates_legacy_empty_host_only_when_etcd_is_used(tmp_path, ha_enabled, persistence_enabled):
    from motor.config.controller import ControllerConfig

    path = tmp_path / "user_config.json"
    section = {
        "standby_config": {"enable_master_standby": ha_enabled},
        "etcd_config": {"etcd_host": "", "enable_etcd_persistence": persistence_enabled},
    }
    path.write_text(json.dumps({"motor_controller_config": section}), encoding="utf-8")
    if ha_enabled or persistence_enabled:
        with pytest.raises(ValueError, match="etcd_hosts entries must be non-empty"):
            ControllerConfig.from_json(str(path))
    else:
        config = ControllerConfig.from_json(str(path))
        assert config.etcd_config.etcd_hosts == [""]
        assert config.etcd_config.enable_etcd_persistence is False
