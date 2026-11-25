# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from motor.common.discovery.monitor import DiscoveryUnavailable, EndpointSnapshot
from motor.common.http.http_client import HttpStatusError
from motor.config.controller import ControllerConfig
from motor.config.node_manager import NodeManagerConfig
from motor.config.service_discovery import ServiceDiscoveryConfig, ServiceEndpoint
from motor.config.tls_config import TLSConfig

# The direct-mode client initializes its own configuration at import time. Isolate
# that existing behavior from the test runner's CONFIG_PATH directory.
with (
    patch.object(ControllerConfig, "from_json", return_value=ControllerConfig()),
    patch.object(NodeManagerConfig, "from_json", return_value=NodeManagerConfig()),
    patch.object(ServiceDiscoveryConfig, "from_json", return_value=ServiceDiscoveryConfig()),
):
    from motor.node_manager.api_client import controller_api_client as module
    from motor.node_manager.api_client.controller_api_client import ControllerApiClient

MODULE = "motor.node_manager.api_client.controller_api_client"


@pytest.fixture(autouse=True)
def isolated_client(monkeypatch):
    monkeypatch.setattr(ControllerApiClient, "controller_config", ControllerConfig())
    monkeypatch.setattr(ControllerApiClient, "discovery_config", ServiceDiscoveryConfig())
    monkeypatch.setattr(ControllerApiClient, "_monitor", None)
    monkeypatch.setattr(module, "_HEARTBEAT_CLIENT", None)
    monkeypatch.setattr(module, "_HEARTBEAT_ADDRESS", None)
    monkeypatch.setattr(module.time, "sleep", lambda _delay: None)
    yield
    if ControllerApiClient._monitor is not None:
        ControllerApiClient._monitor.stop()
    ControllerApiClient.reset_heartbeat_client()


def snapshot(host="192.0.2.11"):
    return EndpointSnapshot(ServiceEndpoint(host, {"api": 2026}), "api")


def start_static(monkeypatch):
    monitor = MagicMock()
    monitor.select.return_value = snapshot()
    config = SimpleNamespace(config_path="local.json", mgmt_tls_config=TLSConfig())
    monkeypatch.setattr(ControllerApiClient, "nodemanager_config", config)
    discovery = ServiceDiscoveryConfig(endpoints={"controller": [monitor.select.return_value.endpoint]})
    monkeypatch.setattr(ControllerApiClient, "discovery_config", discovery)
    assert ControllerApiClient.uses_probe_discovery()
    with (
        patch(f"{MODULE}.EndpointMonitor", return_value=monitor),
        patch(
            f"{MODULE}.ControllerConfig.from_json",
            side_effect=AssertionError("Probe startup must not reload Controller config"),
        ),
    ):
        ControllerApiClient.start_discovery()
    monitor.start.assert_called_once_with()
    return monitor


def test_static_registration_reads_cached_target_without_loading_controller(monkeypatch):
    monitor = start_static(monkeypatch)
    client = MagicMock()
    client.post.return_value = {"message": "registered"}
    with (
        patch(f"{MODULE}.ControllerConfig.from_json") as load_controller,
        patch(f"{MODULE}.SafeHTTPSClient") as factory,
        patch(f"{MODULE}.ServiceDiscoveryConfig.from_json") as load_discovery,
    ):
        factory.return_value.__enter__.return_value = client
        assert ControllerApiClient.register(MagicMock()) is True
        assert factory.call_args.kwargs["address"] == "192.0.2.11:2026"
        load_controller.assert_not_called()
        load_discovery.assert_not_called()
    monitor.select.assert_called_once_with("api")


def test_heartbeat_reuses_connection_until_target_address_changes(monkeypatch):
    monitor = start_static(monkeypatch)
    first, second = MagicMock(), MagicMock()
    with patch(f"{MODULE}.SafeHTTPSClient", side_effect=[first, second]) as factory:
        ControllerApiClient.report_heartbeat(MagicMock())
        monitor.select.return_value = snapshot()
        ControllerApiClient.report_heartbeat(MagicMock())
        assert factory.call_count == 1
        monitor.select.return_value = snapshot("192.0.2.12")
        ControllerApiClient.report_heartbeat(MagicMock())
        assert factory.call_count == 2
    first.close.assert_called_once_with()
    assert first.post.call_count == 2
    second.post.assert_called_once()
    assert monitor.select.call_count == 3


def test_heartbeat_retries_from_new_snapshot_after_connection_failure(monkeypatch):
    monitor = start_static(monkeypatch)
    old, new = snapshot(), snapshot("192.0.2.12")
    monitor.select.side_effect = [old, new]
    first, second = MagicMock(), MagicMock()
    failure = RuntimeError("connection refused")
    first.post.side_effect = failure
    with patch(f"{MODULE}.SafeHTTPSClient", side_effect=[first, second]) as factory:
        ControllerApiClient.report_heartbeat(MagicMock())
    assert [call.kwargs["address"] for call in factory.call_args_list] == [old.address, new.address]
    assert monitor.select.call_count == 2
    monitor.report_failure.assert_not_called()
    second.post.assert_called_once()


@pytest.mark.parametrize("method", ["register", "re_register", "report_software_fault"])
def test_empty_cache_does_not_fall_back_to_direct_address(monkeypatch, method):
    monitor = start_static(monkeypatch)
    monitor.select.side_effect = DiscoveryUnavailable("no candidate ready")
    with patch(f"{MODULE}.ControllerConfig.from_json") as load, patch(f"{MODULE}.SafeHTTPSClient") as factory:
        assert getattr(ControllerApiClient, method)(MagicMock()) is False
    load.assert_not_called()
    factory.assert_not_called()


@pytest.mark.parametrize("coordinator_probe", [False, True])
def test_coordinator_probe_keeps_node_manager_direct_retry_budget(monkeypatch, coordinator_probe):
    config = SimpleNamespace(config_path="direct.json", mgmt_tls_config=TLSConfig())
    monkeypatch.setattr(ControllerApiClient, "nodemanager_config", config)
    controller = SimpleNamespace(
        api_config=SimpleNamespace(controller_api_dns="controller.service", controller_api_port=1026)
    )
    monkeypatch.setattr(ControllerApiClient, "controller_config", controller)
    failure = HttpStatusError(503, "http response error 503")
    endpoints = {"coordinator": [ServiceEndpoint("192.0.2.12", {"management": 1026})]} if coordinator_probe else {}
    monkeypatch.setattr(ControllerApiClient, "discovery_config", ServiceDiscoveryConfig(endpoints=endpoints))
    with (
        patch(f"{MODULE}.EndpointMonitor") as create,
        patch(f"{MODULE}.SafeHTTPSClient") as factory,
    ):
        factory.return_value.post.side_effect = failure
        ControllerApiClient.start_discovery()
        with pytest.raises(HttpStatusError) as raised:
            ControllerApiClient.report_heartbeat(MagicMock())
    assert raised.value is failure
    assert not ControllerApiClient.uses_probe_discovery()
    assert factory.call_count == 3
    assert factory.call_args.kwargs["address"] == "controller.service:1026"
    create.assert_not_called()
