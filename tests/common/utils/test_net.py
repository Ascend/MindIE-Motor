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

import pytest

from motor.common.utils.net import (
    detect_family,
    format_address,
    format_host,
    split_address,
)


class TestDetectFamily:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "0.0.0.0",
            "10.0.0.1",
            "192.168.1.1",
        ],
    )
    def test_ipv4_literal(self, host):
        assert detect_family(host) == socket.AF_INET

    @pytest.mark.parametrize(
        "host",
        [
            "::1",
            "::",
            "2001:db8::1",
            "fe80::1",
            "fd00::1",
            "[::1]",
            "[2001:db8::1]",
        ],
    )
    def test_ipv6_literal(self, host):
        assert detect_family(host) == socket.AF_INET6

    @pytest.mark.parametrize(
        "host",
        [
            "localhost",
            "etcd.default.svc.cluster.local",
            "example.com",
            "",
        ],
    )
    def test_domain_or_empty_falls_back_to_ipv4(self, host):
        assert detect_family(host) == socket.AF_INET


class TestFormatHost:
    @pytest.mark.parametrize(
        "host,expected",
        [
            ("127.0.0.1", "127.0.0.1"),
            ("localhost", "localhost"),
            ("example.com", "example.com"),
            ("", ""),
        ],
    )
    def test_pass_through(self, host, expected):
        assert format_host(host) == expected

    @pytest.mark.parametrize(
        "host,expected",
        [
            ("::1", "[::1]"),
            ("2001:db8::1", "[2001:db8::1]"),
            ("fe80::1", "[fe80::1]"),
        ],
    )
    def test_wrap_ipv6(self, host, expected):
        assert format_host(host) == expected

    @pytest.mark.parametrize(
        "host",
        [
            "[::1]",
            "[2001:db8::1]",
        ],
    )
    def test_already_wrapped_is_idempotent(self, host):
        assert format_host(host) == host


class TestFormatAddress:
    @pytest.mark.parametrize(
        "host,port,expected",
        [
            ("127.0.0.1", 1025, "127.0.0.1:1025"),
            ("localhost", "8080", "localhost:8080"),
            ("::1", 1025, "[::1]:1025"),
            ("2001:db8::1", 2379, "[2001:db8::1]:2379"),
            ("[::1]", 1025, "[::1]:1025"),
        ],
    )
    def test_format(self, host, port, expected):
        assert format_address(host, port) == expected


class TestSplitAddress:
    @pytest.mark.parametrize(
        "address,expected",
        [
            ("127.0.0.1:1025", ("127.0.0.1", "1025")),
            ("localhost:8080", ("localhost", "8080")),
            ("[::1]:1025", ("::1", "1025")),
            ("[2001:db8::1]:2379", ("2001:db8::1", "2379")),
        ],
    )
    def test_split_with_port(self, address, expected):
        assert split_address(address) == expected

    @pytest.mark.parametrize(
        "address",
        [
            "",
            "127.0.0.1",
            "localhost",
            "::1",
            "2001:db8::1",
        ],
    )
    def test_split_without_port(self, address):
        host, port = split_address(address)
        assert host == address
        assert port == ""

    @pytest.mark.parametrize(
        "address,expected",
        [
            ("::1:1025", ("::1", "1025")),
            ("2001:db8::1:1025", ("2001:db8::1", "1025")),
        ],
    )
    def test_split_unbracketed_ipv6_with_port(self, address, expected):
        assert split_address(address) == expected

    def test_round_trip_ipv4(self):
        assert split_address(format_address("10.0.0.1", 1025)) == ("10.0.0.1", "1025")

    def test_round_trip_ipv6(self):
        assert split_address(format_address("2001:db8::1", 2379)) == ("2001:db8::1", "2379")
