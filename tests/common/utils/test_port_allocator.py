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
from unittest.mock import MagicMock, patch

import pytest

from motor.common.utils.port_allocator import PortAllocator, _parse_host_port


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
