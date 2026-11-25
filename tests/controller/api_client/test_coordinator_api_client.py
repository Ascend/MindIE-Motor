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
import tempfile
from unittest.mock import MagicMock, patch

import pytest
import requests

from motor.common.discovery.monitor import (
    DiscoveryUnavailable,
    EndpointState,
)
from motor.config.controller import ControllerConfig
from motor.config.service_discovery import ServiceDiscoveryConfig, ServiceEndpoint
from motor.controller.api_client import coordinator_api_client as cac
from motor.controller.api_client.coordinator_api_client import CoordinatorApiClient
from motor.config.coordinator import CoordinatorConfig, MGMT_API_KEY_HEADER


@pytest.fixture
def probe_monitor(monkeypatch):
    first = ServiceEndpoint("192.0.2.11", {"management": 1026, "observability": 1027})
    second = ServiceEndpoint("192.0.2.12", {"management": 1026, "observability": 1027})
    states = (
        EndpointState(first, eligible=True, ready=False, observed_at=1.0),
        EndpointState(second, eligible=False, ready=False, observed_at=1.0),
    )
    monitor = MagicMock()
    monitor.states.return_value = states
    monitor.select.side_effect = states[0].snapshot
    monitor.members = (first, second)
    discovery = ServiceDiscoveryConfig(endpoints={"coordinator": monitor.members})
    monkeypatch.setattr(CoordinatorApiClient, "discovery_config", discovery)
    monkeypatch.setattr(CoordinatorApiClient, "_monitor", monitor)
    return monitor


def test_probe_readiness_uses_cached_state_and_broadcast_includes_standby(probe_monitor):
    event = MagicMock()
    event.model_dump.return_value = {"event": "SET"}
    with patch.object(cac, "SafeHTTPSClient") as http, patch.object(cac.CoordinatorConfig, "from_json") as load_target:
        assert CoordinatorApiClient.query_status() == {"ready": False}
        http.assert_not_called()
        assert CoordinatorApiClient.send_instance_refresh(event)
    assert [call.kwargs["address"] for call in http.call_args_list] == ["192.0.2.11:1026", "192.0.2.12:1026"]
    load_target.assert_not_called()


@pytest.mark.parametrize("accepted", [(True, False), (False, True), (False, False)])
def test_probe_broadcast_keeps_any_success_and_never_falls_back_to_dns(probe_monitor, accepted):
    """Replacing Pod enumeration must preserve partial-success behavior for every caller."""
    event = MagicMock(instances=[])
    with patch.object(cac, "SafeHTTPSClient") as http, patch.object(cac, "_list_coordinator_pod_ips") as pods:
        http.return_value.__enter__.side_effect = [
            MagicMock() if ok else OSError("member unavailable") for ok in accepted
        ]
        assert CoordinatorApiClient.send_instance_refresh(event) is any(accepted)
    assert [call.kwargs["address"] for call in http.call_args_list] == ["192.0.2.11:1026", "192.0.2.12:1026"]
    probe_monitor.select.assert_not_called()
    pods.assert_not_called()


def test_probe_missing_target_preserves_interface_failures_without_http(probe_monitor):
    probe_monitor.select.side_effect = DiscoveryUnavailable("No available Coordinator")
    with patch.object(cac, "SafeHTTPSClient") as http:
        with pytest.raises(DiscoveryUnavailable):
            CoordinatorApiClient.query_status()
        assert CoordinatorApiClient.get_metrics() is None
        assert CoordinatorApiClient.notify_precision_alarm_cleared(1, 2) is False
    http.assert_not_called()


def test_probe_metrics_and_notifications_use_their_ports_without_replaying(probe_monitor):
    response = MagicMock(status_code=200, ok=True, text="metrics")
    response.json.return_value = {"data": {"dismissed": True}}
    with patch.object(cac, "SafeHTTPSClient") as http:
        http.return_value.do_get.return_value = response
        http.return_value.__enter__.return_value.do_post.return_value = response
        assert CoordinatorApiClient.get_metrics() == "metrics"
        assert CoordinatorApiClient.notify_precision_alarm_cleared(1, 2)
    assert [call.kwargs["address"] for call in http.call_args_list] == ["192.0.2.11:1027", "192.0.2.11:1026"]
    assert http.return_value.__enter__.return_value.do_post.call_count == 1


@pytest.mark.parametrize("controller_probe", [False, True])
def test_absent_coordinator_probe_does_not_start_monitor_or_require_observability_port(monkeypatch, controller_probe):
    endpoints = {"controller": [ServiceEndpoint("192.0.2.11", {"api": 2026})]} if controller_probe else {}
    config = ControllerConfig()
    config.observability_config.observability_enable = True
    monkeypatch.setattr(CoordinatorApiClient, "_monitor", None)
    monkeypatch.setattr(CoordinatorApiClient, "controller_config", config)
    monkeypatch.setattr(CoordinatorApiClient, "discovery_config", ServiceDiscoveryConfig(endpoints=endpoints))
    with patch.object(cac, "EndpointMonitor") as create:
        CoordinatorApiClient.start_discovery()
    create.assert_not_called()
    assert not CoordinatorApiClient.uses_probe_discovery()


@pytest.mark.parametrize("observability_enabled,has_obs_port", [(False, False), (True, True), (True, False)])
def test_monitor_requires_observability_port_only_when_metrics_enabled(
    monkeypatch, observability_enabled, has_obs_port
):
    ports = {"management": 1026}
    if has_obs_port:
        ports["observability"] = 1027
    config = ControllerConfig()
    config.observability_config.observability_enable = observability_enabled
    discovery = ServiceDiscoveryConfig(endpoints={"coordinator": [ServiceEndpoint("192.0.2.11", ports)]})
    monkeypatch.setattr(CoordinatorApiClient, "_monitor", None)
    monkeypatch.setattr(CoordinatorApiClient, "controller_config", config)
    monkeypatch.setattr(CoordinatorApiClient, "discovery_config", discovery)
    with patch.object(cac, "EndpointMonitor") as create:
        if observability_enabled and not has_obs_port:
            with pytest.raises(ValueError, match="observability ports"):
                CoordinatorApiClient.start_discovery()
            create.assert_not_called()
        else:
            CoordinatorApiClient.start_discovery()
            create.return_value.start.assert_called_once_with()


def _mock_safe_https_client(mock_cls: MagicMock, response: MagicMock) -> MagicMock:
    client = mock_cls.return_value.__enter__.return_value
    client.do_post.return_value = response
    return client


def test_notify_precision_alarm_cleared_returns_true_when_dismissed() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "status": "success",
        "data": {"dismissed": True},
    }

    with (
        patch.object(CoordinatorApiClient, "_generate_client_args", return_value={"address": "127.0.0.1:1234"}),
        patch("motor.controller.api_client.coordinator_api_client.SafeHTTPSClient") as mock_client_cls,
    ):
        client = _mock_safe_https_client(mock_client_cls, response)
        assert CoordinatorApiClient.notify_precision_alarm_cleared(1, 2) is True

    client.do_post.assert_called_once_with(
        "/precision/alarm_cleared",
        data={"p_instance_id": 1, "d_instance_id": 2},
    )


def test_notify_precision_alarm_cleared_returns_false_when_not_dismissed() -> None:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "status": "success",
        "data": {"dismissed": False},
    }

    with (
        patch.object(CoordinatorApiClient, "_generate_client_args", return_value={"address": "127.0.0.1:1234"}),
        patch("motor.controller.api_client.coordinator_api_client.SafeHTTPSClient") as mock_client_cls,
    ):
        _mock_safe_https_client(mock_client_cls, response)
        assert CoordinatorApiClient.notify_precision_alarm_cleared(1, 2) is False


def test_notify_precision_alarm_cleared_returns_false_on_http_error() -> None:
    http_error = requests.HTTPError("404 Client Error")
    http_error.response = MagicMock(status_code=404)

    with (
        patch.object(CoordinatorApiClient, "_generate_client_args", return_value={"address": "127.0.0.1:1234"}),
        patch("motor.controller.api_client.coordinator_api_client.SafeHTTPSClient") as mock_client_cls,
    ):
        client = mock_client_cls.return_value.__enter__.return_value
        client.do_post.side_effect = http_error
        assert CoordinatorApiClient.notify_precision_alarm_cleared(1, 2) is False


def test_notify_precision_alarm_cleared_preserves_management_key_errors(monkeypatch, tmp_path):
    config = CoordinatorConfig()
    config.mgmt_api_key_config.enable_api_key = True
    config.mgmt_api_key_config.api_key_file = str(tmp_path / "missing-management.key")
    monkeypatch.setattr(CoordinatorApiClient, "coordinator_config", config)
    monkeypatch.setattr(CoordinatorApiClient, "discovery_config", ServiceDiscoveryConfig())
    with (
        patch.object(cac, "SafeHTTPSClient") as http,
        pytest.raises(ValueError, match="Failed to read management API key file"),
    ):
        CoordinatorApiClient.notify_precision_alarm_cleared(1, 2)
    http.assert_not_called()


@pytest.mark.parametrize("probe_enabled", [False, True])
def test_generate_client_args_loads_management_api_key_from_file(probe_enabled) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as key_file:
        key_file.write("controller-management-key\n")
        api_key_file = key_file.name
    try:
        config = CoordinatorConfig()
        config.mgmt_api_key_config.enable_api_key = True
        config.mgmt_api_key_config.api_key_file = api_key_file

        monitor = MagicMock() if probe_enabled else None
        endpoint = ServiceEndpoint("192.0.2.11", {"management": 1026})
        discovery = ServiceDiscoveryConfig(endpoints={"coordinator": [endpoint]} if probe_enabled else {})
        if monitor is not None:
            monitor.select.return_value.address = endpoint.address("management")
        with (
            patch.object(CoordinatorApiClient, "coordinator_config", config),
            patch.object(CoordinatorApiClient, "discovery_config", discovery),
            patch.object(CoordinatorApiClient, "_monitor", monitor),
        ):
            client_args = CoordinatorApiClient._generate_client_args()
            probe_client_args = CoordinatorApiClient._generate_client_args(include_mgmt_api_key=False)

        assert client_args["tls_config"] is CoordinatorApiClient.controller_config.mgmt_tls_config
        assert client_args["headers"] == {MGMT_API_KEY_HEADER: "controller-management-key"}
        assert "headers" not in probe_client_args
    finally:
        os.remove(api_key_file)


def test_list_coordinator_pod_ips_skips_without_namespace() -> None:
    cac._POD_IP_CACHE = ()
    cac._POD_IP_CACHE_AT = 0.0
    with (
        patch.dict(os.environ, {"POD_NAMESPACE": ""}, clear=False),
        patch.object(cac, "_k8s_client") as mock_k8s,
    ):
        assert cac._list_coordinator_pod_ips() == []
        mock_k8s.assert_not_called()


def test_list_coordinator_pod_ips_uses_injected_label_without_prefix_fallback() -> None:
    cac._POD_IP_CACHE = ()
    cac._POD_IP_CACHE_AT = 0.0
    k8s = MagicMock()
    k8s.list_pod_ips.return_value = ["10.0.0.8"]
    env = {
        "POD_NAMESPACE": "mindie",
        "COORDINATOR_POD_LABEL": "app=mindie-motor-coordinator-aaa111",
    }
    with (
        patch.dict(os.environ, env, clear=False),
        patch.object(cac, "_k8s_client", return_value=k8s),
    ):
        assert cac._list_coordinator_pod_ips() == ["10.0.0.8"]
    k8s.list_pod_ips.assert_called_once_with(
        "mindie",
        label_selector="app=mindie-motor-coordinator-aaa111",
    )
    cac._POD_IP_CACHE = ()
    cac._POD_IP_CACHE_AT = 0.0


def test_list_coordinator_pod_ips_prefix_fallback_excludes_named_services() -> None:
    cac._POD_IP_CACHE = ()
    cac._POD_IP_CACHE_AT = 0.0
    k8s = MagicMock()
    k8s.list_pod_ips.side_effect = [[], ["10.0.0.9"]]
    env = {"POD_NAMESPACE": "mindie", "COORDINATOR_POD_LABEL": ""}
    with (
        patch.dict(os.environ, env, clear=False),
        patch.object(cac, "_k8s_client", return_value=k8s),
    ):
        assert cac._list_coordinator_pod_ips() == ["10.0.0.9"]
    assert k8s.list_pod_ips.call_args_list[0] == (
        ("mindie",),
        {"label_selector": "app=mindie-motor-coordinator"},
    )
    assert k8s.list_pod_ips.call_args_list[1] == (
        ("mindie",),
        {
            "label_selector": "!motor.mindie.io/service",
            "name_prefix": "mindie-motor-coordinator",
        },
    )
    cac._POD_IP_CACHE = ()
    cac._POD_IP_CACHE_AT = 0.0


def test_send_instance_refresh_falls_back_to_service_dns() -> None:
    event_msg = MagicMock()
    event_msg.instances = []
    event_msg.event = "SET"
    event_msg.model_dump.return_value = {}

    client = MagicMock()
    client.post.return_value = {"text": "ok"}

    with (
        patch.object(cac, "_instance_refresh_hosts", return_value=["10.0.0.1"]),
        patch.object(CoordinatorApiClient, "_generate_client_args", return_value={"address": "dns:1025"}),
        patch("motor.controller.api_client.coordinator_api_client.SafeHTTPSClient") as mock_client_cls,
    ):
        mock_client_cls.return_value.__enter__.side_effect = [OSError("ssl"), client]
        assert CoordinatorApiClient.send_instance_refresh(event_msg) is True
    assert client.post.call_count == 1
