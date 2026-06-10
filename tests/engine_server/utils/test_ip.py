#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import pytest

from motor.engine_server.utils.ip import ip_valid_check, port_valid_check, is_valid_ipv6_address, build_endpoint


class TestIpUtils:
    """Tests for IP utility functions"""

    def test_ip_valid_check_valid_ipv4(self):
        """Test ip_valid_check with valid IPv4 addresses"""
        # These should not raise any exceptions
        valid_ips = [
            "127.0.0.1",  # Loopback
            "192.168.1.1",  # Private IP
            "8.8.8.8",  # Public IP
            "10.0.0.1",  # Private IP
            "172.16.0.1",  # Private IP
        ]

        for ip in valid_ips:
            ip_valid_check(ip)  # Should not raise exception

    def test_ip_valid_check_valid_ipv6(self):
        """Test ip_valid_check with valid IPv6 addresses"""
        # These should not raise any exceptions
        valid_ipv6s = [
            "::1",  # Loopback
            "2001:0db8:85a3:0000:0000:8a2e:0370:7334",  # Public IP
            "fe80::1",  # Link-local
            "fd00::1",  # Unique local
        ]

        for ipv6 in valid_ipv6s:
            ip_valid_check(ipv6)  # Should not raise exception

    def test_ip_valid_check_invalid_format(self):
        """Test ip_valid_check with invalid IP formats"""
        invalid_ips = [
            "not_an_ip",
            "256.0.0.1",  # Invalid octet
            "192.168.1.256",  # Invalid octet
            "192.168.1",  # Missing octet
            "192.168.1.1.1",  # Extra octet
            "::g::",  # Invalid IPv6 character
            "2001:::3",  # Invalid IPv6 format
        ]

        for ip in invalid_ips:
            with pytest.raises(ValueError) as excinfo:
                ip_valid_check(ip)
            assert "parse to ip failed" in str(excinfo.value)

    def test_ip_valid_check_all_zeros_ip(self):
        """Test ip_valid_check with all zeros IP addresses"""
        all_zeros_ips = [
            "0.0.0.0",  # IPv4 all zeros
            "::",  # IPv6 all zeros
            "0000:0000:0000:0000:0000:0000:0000:0000",  # IPv6 all zeros (expanded)
        ]

        for ip in all_zeros_ips:
            with pytest.raises(ValueError) as excinfo:
                ip_valid_check(ip)
            assert "is all zeros ip" in str(excinfo.value)

    def test_ip_valid_check_multicast_ip(self):
        """Test ip_valid_check with multicast IP addresses"""
        multicast_ips = [
            "224.0.0.1",  # IPv4 multicast (local network)
            "239.255.255.255",  # IPv4 multicast (administrative scope)
            "ff02::1",  # IPv6 link-local multicast
            "ff05::1:3",  # IPv6 site-local multicast
        ]

        for ip in multicast_ips:
            with pytest.raises(ValueError) as excinfo:
                ip_valid_check(ip)
            assert "is multicast ip" in str(excinfo.value)

    def test_port_valid_check_valid_ports(self):
        """Test port_valid_check with valid port numbers"""
        valid_ports = [
            1024,  # Minimum valid port
            8080,  # Common HTTP alternative
            9001,  # Example from test_vllm_config.py
            65535,  # Maximum valid port
        ]

        for port in valid_ports:
            port_valid_check(port)  # Should not raise exception

    def test_port_valid_check_invalid_ports_below_range(self):
        """Test port_valid_check with ports below 1024"""
        invalid_ports = [
            0,  # Reserved port
            1,  # System port
            80,  # HTTP
            443,  # HTTPS
            1023,  # Maximum system port
        ]

        for port in invalid_ports:
            with pytest.raises(ValueError) as excinfo:
                port_valid_check(port)
            assert "port must be between 1024 and 65535" in str(excinfo.value)

    def test_port_valid_check_invalid_ports_above_range(self):
        """Test port_valid_check with ports above 65535"""
        invalid_ports = [
            65536,  # One above maximum
            100000,  # Much higher than maximum
            2**16,  # 65536
        ]

        for port in invalid_ports:
            with pytest.raises(ValueError) as excinfo:
                port_valid_check(port)
            assert "port must be between 1024 and 65535" in str(excinfo.value)

    def test_is_valid_ipv6_address_valid(self):
        """Test is_valid_ipv6_address with valid IPv6 addresses"""
        valid_ipv6s = [
            "::1",  # Loopback
            "2001:0db8:85a3:0000:0000:8a2e:0370:7334",  # Public IP (full)
            "2001:db8:85a3::8a2e:370:7334",  # Public IP (shortened)
            "fe80::1",  # Link-local
            "fd00::1",  # Unique local
            "0000:0000:0000:0000:0000:0000:0000:0001",  # Full loopback
            "2001:0db8::1",  # Double colon at end
            "::2001:0db8",  # Double colon at start
        ]

        for ipv6 in valid_ipv6s:
            assert is_valid_ipv6_address(ipv6) is True

    def test_is_valid_ipv6_address_invalid(self):
        """Test is_valid_ipv6_address with invalid IPv6 addresses"""
        invalid_ipv6s = [
            "not_an_ip",
            "::g::",  # Invalid character
            "2001:::3",  # Too many colons
            "2001:0db8:85a3:0000:0000:8a2e:0370:7334:7335",  # Too many groups
            "2001:0db8:85a3:0000:0000:8a2e:0370",  # Too few groups
            "256.0.0.1",  # Invalid IPv4 (should still return False for IPv6 check)
            "192.168.1.1",  # IPv4 (should return False)
            "",
        ]

        for ipv6 in invalid_ipv6s:
            assert is_valid_ipv6_address(ipv6) is False

    def test_is_valid_ipv6_address_ipv4(self):
        """Test is_valid_ipv6_address with IPv4 addresses (should return False)"""
        ipv4_addresses = [
            "127.0.0.1",  # Loopback
            "192.168.1.1",  # Private
            "8.8.8.8",  # Public
            "0.0.0.0",  # All zeros
        ]

        for ipv4 in ipv4_addresses:
            assert is_valid_ipv6_address(ipv4) is False

    def test_is_valid_ipv6_address_none(self):
        assert is_valid_ipv6_address(None) is False

    def test_build_endpoint_ipv4(self):
        assert build_endpoint("127.0.0.1", 8080) == "127.0.0.1:8080"

    def test_build_endpoint_ipv6(self):
        assert build_endpoint("::1", 8080) == "[::1]:8080"

    def test_build_endpoint_rejects_empty_ip(self):
        with pytest.raises(ValueError, match="ip must not be empty"):
            build_endpoint(None, 8080)
        with pytest.raises(ValueError, match="ip must not be empty"):
            build_endpoint("", 8080)
