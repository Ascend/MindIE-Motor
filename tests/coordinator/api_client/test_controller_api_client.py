# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.

import json
from unittest.mock import MagicMock, patch

import pytest

from motor.common.discovery.monitor import DiscoveryUnavailable, EndpointSnapshot
from motor.config.coordinator import CoordinatorConfig
from motor.config.service_discovery import ServiceDiscoveryConfig, ServiceEndpoint
from motor.coordinator.api_client.controller_api_client import ControllerApiClient

MODULE = "motor.coordinator.api_client.controller_api_client"


def setup_function():
    if ControllerApiClient._monitor is not None:
        ControllerApiClient._monitor.stop()
    ControllerApiClient._monitor = None
    ControllerApiClient.controller_config = None
    ControllerApiClient.coordinator_config = None
    ControllerApiClient.discovery_config = ServiceDiscoveryConfig()


def teardown_function():
    if ControllerApiClient._monitor is not None:
        ControllerApiClient._monitor.stop()
    ControllerApiClient._monitor = None
    ControllerApiClient.controller_config = None
    ControllerApiClient.coordinator_config = None
    ControllerApiClient.discovery_config = ServiceDiscoveryConfig()


@pytest.mark.parametrize("coordinator_probe", [False, True])
def test_coordinator_probe_keeps_controller_direct_addressing(tmp_path, coordinator_probe):
    config_path = tmp_path / "full.json"
    data = {
        "motor_controller_config": {},
        "motor_coordinator_config": {},
    }
    if coordinator_probe:
        data["motor_deploy_config"] = {
            "service_discovery_config": {"probe_ips": {"coordinator": ["192.0.2.11"]}},
        }
        data["motor_coordinator_config"] = {
            "api_config": {"coordinator_api_mgmt_port": 1026},
            "port_allocator_config": {"enable": False},
        }
    config_path.write_text(
        json.dumps(data),
        encoding="utf-8",
    )
    coordinator_config = CoordinatorConfig.from_json(str(config_path))
    ControllerApiClient.coordinator_config = coordinator_config
    ControllerApiClient.discovery_config = ServiceDiscoveryConfig.from_user_config(data)

    controller_config = MagicMock()
    controller_config.api_config.controller_api_dns = "controller.example"
    controller_config.api_config.controller_api_port = 1024
    ControllerApiClient.controller_config = controller_config

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"data": {}}
    client = MagicMock()
    client.do_post.return_value = response
    context = MagicMock()
    context.__enter__.return_value = client

    with patch(
        "motor.coordinator.api_client.controller_api_client.SafeHTTPSClient",
        return_value=context,
    ) as client_class:
        with patch(f"{MODULE}.EndpointMonitor") as create:
            ControllerApiClient.start_discovery()
        create.assert_not_called()
        outcome = ControllerApiClient.report_alarms({"alarm_id": "controller"})

    assert outcome["ok"] is True
    assert not ControllerApiClient.uses_probe_discovery()
    assert client_class.call_args.kwargs["address"] == "controller.example:1024"
    client_class.assert_called_once()
    client.do_post.assert_called_once_with("/observability/add_alarm", {"alarm_id": "controller"})


@pytest.fixture
def static_monitor(tmp_path):
    path = tmp_path / "static.json"
    path.write_text(json.dumps({"motor_controller_config": {}, "motor_coordinator_config": {}}), encoding="utf-8")
    config = CoordinatorConfig.from_json(str(path))
    monitor = MagicMock()
    monitor.select.return_value = EndpointSnapshot(ServiceEndpoint("192.0.2.12", {"api": 2026}), "api")
    ControllerApiClient.discovery_config = ServiceDiscoveryConfig(
        endpoints={"controller": [monitor.select.return_value.endpoint]}
    )
    assert ControllerApiClient.uses_probe_discovery()
    with (
        patch(f"{MODULE}.CoordinatorConfig.from_json", return_value=config) as load_config,
        patch(f"{MODULE}.ServiceDiscoveryConfig.from_json") as load_discovery,
        patch(f"{MODULE}.ControllerConfig.from_json") as load_controller,
        patch(f"{MODULE}.EndpointMonitor", return_value=monitor) as create,
    ):
        ControllerApiClient.start_discovery()
    load_config.assert_called_once_with()
    load_discovery.assert_not_called()
    load_controller.assert_not_called()
    create.assert_called_once_with("controller", ControllerApiClient.discovery_config, config.mgmt_tls_config)
    monitor.start.assert_called_once_with()
    return monitor


def test_static_alarm_uses_cached_endpoint_and_preserves_acceptance(static_monitor):
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": {"precision_alarm_cleared_by_auto_recovery": True}}
    with (
        patch(f"{MODULE}.SafeHTTPSClient") as factory,
        patch(f"{MODULE}.ControllerConfig.from_json") as load,
        patch(f"{MODULE}.ServiceDiscoveryConfig.from_json") as load_discovery,
    ):
        factory.return_value.__enter__.return_value.do_post.return_value = response
        outcome = ControllerApiClient.report_alarms({"alarm_id": "precision"})
    assert outcome == {"ok": True, "precision_alarm_cleared": True, "precision_alarm_cleared_by_auto_recovery": True}
    assert factory.call_args.kwargs["address"] == "192.0.2.12:2026"
    load.assert_not_called()
    load_discovery.assert_not_called()
    static_monitor.select.assert_called_once_with("api")


def test_static_alarm_empty_cache_returns_rejected_outcome_without_fallback(static_monitor):
    static_monitor.select.side_effect = DiscoveryUnavailable("no Controller ready")
    with patch(f"{MODULE}.SafeHTTPSClient") as factory, patch(f"{MODULE}.ControllerConfig.from_json") as load:
        assert ControllerApiClient.report_alarms({"alarm_id": "cold-start"})["ok"] is False
    factory.assert_not_called()
    load.assert_not_called()
