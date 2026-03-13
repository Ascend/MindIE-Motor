#!/usr/bin/env python3
# coding=utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import sys

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

import unittest
from unittest.mock import MagicMock, patch

import logging

from node_manager.framework.client import Client

logging.basicConfig(level=logging.INFO)


class TestClient(unittest.TestCase):

    def setUp(self):
        mock_config = MagicMock()
        mock_config.get_server_engine_ip.return_value = "127.0.0.1"
        mock_config.get_server_engine_port_list.return_value = ["8080"]
        mock_config.http_server_config = {"tls_config": {"client_tls_enable": False}}
        with patch("node_manager.core.config.GeneralConfig", return_value=mock_config):
            self.client = Client(retry_times=1, timeout=1)

    def test_ipv4_full_url_no_brackets(self):
        """IPv4 地址不添加方括号，直接拼入完整 URL。"""
        url = self.client.build_url("http://", "192.168.1.1", 8080, "/v1/path")
        self.assertEqual(url, "http://192.168.1.1:8080/v1/path")

    def test_ipv4_host_port_only_when_path_empty(self):
        """path 为空时返回 host:port 格式。"""
        url = self.client.build_url("", "192.168.1.1", 8080)
        self.assertEqual(url, "192.168.1.1:8080")

    def test_loopback_ipv4_no_brackets(self):
        """IPv4 回环 127.0.0.1 不添加方括号。"""
        url = self.client.build_url("http://", "127.0.0.1", 8080, "/v1/path")
        self.assertEqual(url, "http://127.0.0.1:8080/v1/path")

    def test_ipv6_full_url_wrapped_in_brackets(self):
        """IPv6 完整地址按 RFC 3986 包裹方括号。"""
        url = self.client.build_url("http://", "2001:db8::1", 443, "/api/status")
        self.assertEqual(url, "http://[2001:db8::1]:443/api/status")

    def test_ipv6_host_port_only_when_path_empty(self):
        """path 为空时 IPv6 返回 [host]:port 格式。"""
        url = self.client.build_url("", "::1", 8080)
        self.assertEqual(url, "[::1]:8080")

    def test_loopback_ipv6_wrapped_in_brackets(self):
        """IPv6 回环 ::1 按 RFC 3986 包裹方括号。"""
        url = self.client.build_url("http://", "::1", 8080, "/v1/path")
        self.assertEqual(url, "http://[::1]:8080/v1/path")

    def test_hostname_passed_through_without_modification(self):
        """主机名非 IP 时原样使用，不添加方括号。"""
        url = self.client.build_url("http://", "controller.example.com", 8080, "/v1/alarm")
        self.assertEqual(url, "http://controller.example.com:8080/v1/alarm")


if __name__ == "__main__":
    unittest.main()
