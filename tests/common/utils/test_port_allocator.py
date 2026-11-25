# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import socket
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from motor.common.utils.port_allocator import (
    PortAllocator,
    PortConflictError,
    _conductor_listens_locally,
    _parse_host_port,
    apply_controller_ports,
    apply_coordinator_ports,
    apply_node_manager_ports,
)
from motor.config.etcd import EtcdConfig
from motor.config.port_allocator_config import PortAllocatorConfig


class TestParseHostPort:
    @pytest.mark.parametrize(
        "address,default_port,expected",
        [
            ("127.0.0.1:1025", 8080, ("127.0.0.1", 1025)),
            ("[2001:db8::1]:2379", 8080, ("2001:db8::1", 2379)),
            ("2001:db8::1:2379", 8080, ("2001:db8::1", 2379)),
            ("etcd.local", 8080, ("etcd.local", 8080)),
            ("", 8080, ("", 8080)),
        ],
    )
    def test_parse(self, address, default_port, expected):
        assert _parse_host_port(address, default_port) == expected


class TestPortAllocatorIpv6:
    @patch("motor.common.utils.port_allocator.socket.socket")
    def test_probe_tcp_uses_ipv6_family(self, mock_socket_ctor):
        mock_sock = MagicMock()
        mock_socket_ctor.return_value = mock_sock

        assert PortAllocator.probe_tcp("::1", 5555) is True

        mock_socket_ctor.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM)
        mock_sock.bind.assert_called_once_with(("::1", 5555))

    @patch("motor.common.utils.port_allocator.socket.socket")
    def test_probe_tcp_strips_brackets(self, mock_socket_ctor):
        mock_sock = MagicMock()
        mock_socket_ctor.return_value = mock_sock

        assert PortAllocator.probe_tcp("[::1]", 5555) is True

        mock_socket_ctor.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM)
        mock_sock.bind.assert_called_once_with(("::1", 5555))

    @patch("motor.common.utils.port_allocator.socket.socket")
    def test_probe_tcp_uses_ipv4_family(self, mock_socket_ctor):
        mock_sock = MagicMock()
        mock_socket_ctor.return_value = mock_sock

        assert PortAllocator.probe_tcp("127.0.0.1", 5555) is True

        mock_socket_ctor.assert_called_once_with(socket.AF_INET, socket.SOCK_STREAM)
        mock_sock.bind.assert_called_once_with(("127.0.0.1", 5555))

    @patch("motor.common.utils.port_allocator.socket.socket")
    def test_check_remote_reachable_uses_ipv6_family(self, mock_socket_ctor):
        mock_sock = MagicMock()
        mock_socket_ctor.return_value = mock_sock

        assert PortAllocator.check_remote_reachable("2001:db8::1", 2379) is True

        mock_socket_ctor.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM)
        mock_sock.connect.assert_called_once_with(("2001:db8::1", 2379))


def _coordinator(conductor_service: str, port: int = 13333):
    return SimpleNamespace(
        port_allocator_config=PortAllocatorConfig(enable=True, bind_host="0.0.0.0"),
        api_config=SimpleNamespace(
            coordinator_api_infer_port=1025,
            coordinator_api_mgmt_port=1026,
            coordinator_obs_port=1027,
        ),
        inference_workers_config=SimpleNamespace(worker_metaserver_base_port=0, num_workers=0),
        scheduler_config=SimpleNamespace(
            kv_conductor_config=SimpleNamespace(conductor_service=conductor_service, http_server_port=port)
        ),
        render_config=SimpleNamespace(enable=False),
        prometheus_metrics_config=SimpleNamespace(enable_kv_store_metrics=False),
        etcd_config=SimpleNamespace(enable_etcd_persistence=False),
    )


def _keep_port(host, port, name, **_kwargs):
    return port


def _strict_busy_on_conductor(host, port, name, **_kwargs):
    if port == 13333:
        raise PortConflictError("busy")
    return port


class TestLocalKvConductorPort:
    def test_same_pod_ip_is_local(self, monkeypatch):
        monkeypatch.setenv("POD_IP", "10.0.0.8")
        assert _conductor_listens_locally("10.0.0.8", "0.0.0.0") is True
        assert _conductor_listens_locally("10.1.1.1", "0.0.0.0") is False
        assert _conductor_listens_locally("127.0.0.1", "0.0.0.0") is True

    @patch("motor.common.utils.port_allocator.PortAllocator.print_matrix")
    @patch("motor.common.utils.port_allocator.PortAllocator.allocate_auto", side_effect=_keep_port)
    @patch("motor.common.utils.port_allocator.PortAllocator.allocate_strict", side_effect=_keep_port)
    @patch("motor.common.utils.port_allocator.PortAllocator.check_remote_reachable", return_value=False)
    def test_local_conductor_is_checked_and_not_shifted(self, _reachable, strict, _auto, printed, monkeypatch):
        monkeypatch.setenv("POD_IP", "10.0.0.8")
        config = _coordinator("10.0.0.8")
        apply_coordinator_ports(config)

        assert config.scheduler_config.kv_conductor_config.http_server_port == 13333
        assert "kv_conductor_http_port" in [call.args[2] for call in strict.call_args_list]
        conductor = [row for row in printed.call_args[0][0] if row.component == "Conductor"]
        assert len(conductor) == 1
        assert conductor[0].strategy == "strict"
        assert conductor[0].port == 13333
        assert conductor[0].purpose == "KV conductor HTTP"

    @patch("motor.common.utils.port_allocator.PortAllocator.print_matrix")
    @patch("motor.common.utils.port_allocator.PortAllocator.allocate_auto", side_effect=_keep_port)
    @patch("motor.common.utils.port_allocator.PortAllocator.allocate_strict", side_effect=_keep_port)
    @patch("motor.common.utils.port_allocator.PortAllocator.check_remote_reachable", return_value=True)
    def test_local_conductor_already_listening_keeps_configured_port(
        self, _reachable, strict, _auto, printed, monkeypatch
    ):
        monkeypatch.setenv("POD_IP", "10.0.0.8")
        config = _coordinator("10.0.0.8")
        apply_coordinator_ports(config)

        assert config.scheduler_config.kv_conductor_config.http_server_port == 13333
        assert "kv_conductor_http_port" not in [call.args[2] for call in strict.call_args_list]
        conductor = [row for row in printed.call_args[0][0] if row.component == "Conductor"]
        assert conductor[0].strategy == "config"
        assert "listening" in conductor[0].purpose

    @patch("motor.common.utils.port_allocator.PortAllocator.print_matrix")
    @patch("motor.common.utils.port_allocator.PortAllocator.allocate_auto", side_effect=_keep_port)
    @patch(
        "motor.common.utils.port_allocator.PortAllocator.allocate_strict",
        side_effect=_strict_busy_on_conductor,
    )
    @patch("motor.common.utils.port_allocator.PortAllocator.check_remote_reachable", return_value=False)
    def test_local_conductor_port_in_use_aborts(self, _reachable, _strict, _auto, _printed, monkeypatch):
        monkeypatch.setenv("POD_IP", "10.0.0.8")
        with pytest.raises(PortConflictError):
            apply_coordinator_ports(_coordinator("10.0.0.8"))

    @patch("motor.common.utils.port_allocator.PortAllocator.print_matrix")
    @patch("motor.common.utils.port_allocator.PortAllocator.allocate_auto", side_effect=_keep_port)
    @patch("motor.common.utils.port_allocator.PortAllocator.allocate_strict", side_effect=_keep_port)
    @patch("motor.common.utils.port_allocator.PortAllocator.check_remote_reachable", return_value=False)
    def test_remote_conductor_stays_remote(self, _reachable, strict, auto, printed, monkeypatch):
        monkeypatch.setenv("POD_IP", "10.0.0.8")
        config = _coordinator("10.1.1.1")
        apply_coordinator_ports(config)

        assert "kv_conductor_http_port" not in [call.args[2] for call in strict.call_args_list]
        assert "http_server_port" in [call.args[2] for call in auto.call_args_list]
        conductor = [row for row in printed.call_args[0][0] if row.component == "Conductor"]
        assert conductor[0].strategy == "remote"
        assert "reachable=False" in conductor[0].purpose

    @patch("motor.common.utils.port_allocator.PortAllocator.print_matrix")
    @patch("motor.common.utils.port_allocator.PortAllocator.allocate_auto", side_effect=_keep_port)
    @patch("motor.common.utils.port_allocator._load_user_config")
    def test_single_container_reserves_conductor_port(self, load_config, allocate_auto, _printed):
        load_config.return_value = {"kv_conductor_config": {"http_server_port": 13333}}
        config = SimpleNamespace(
            port_allocator_config=PortAllocatorConfig(enable=True),
            api_config=SimpleNamespace(node_manager_port=3026),
            endpoint_config=SimpleNamespace(service_ports=[], base_port=10000, bootstrap_port=None),
            basic_config=SimpleNamespace(role="prefill"),
            single_container_config=SimpleNamespace(single_container_flag=True, kv_port=None, dp_rpc_port=None),
            config_path="/tmp/user_config.json",
            kv_cache_store_config=SimpleNamespace(enable=False),
        )
        apply_node_manager_ports(config)

        skipped = set()
        for call in allocate_auto.call_args_list:
            skipped |= set(call.kwargs.get("skip_ports") or ())
        assert 13333 in skipped


@pytest.mark.parametrize("persistence", [False, True])
def test_fixed_controller_ports_report_all_etcd_members_without_probing(persistence):
    config = SimpleNamespace(
        port_allocator_config=PortAllocatorConfig(enable=False),
        api_config=SimpleNamespace(controller_api_port=2026, observability_api_port=2027),
        etcd_config=EtcdConfig(
            etcd_hosts=["192.0.2.21", "192.0.2.22"],
            etcd_port=32379,
            enable_etcd_persistence=persistence,
        ),
    )
    with patch.object(PortAllocator, "print_matrix") as matrix, patch.object(PortAllocator, "probe_tcp") as probe:
        apply_controller_ports(config)
    probe.assert_not_called()
    assert (config.api_config.controller_api_port, config.api_config.observability_api_port) == (2026, 2027)
    rows = matrix.call_args.args[0]
    remote = [(row.bind_host, row.port) for row in rows if row.component == "etcd"]
    assert remote == ([("192.0.2.21", 32379), ("192.0.2.22", 32379)] if persistence else [])
