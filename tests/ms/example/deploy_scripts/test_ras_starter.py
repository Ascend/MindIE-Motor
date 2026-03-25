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
import unittest
from unittest.mock import patch, MagicMock

# Add deploy_scripts to path for ras_starter's utils import
_script_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.normpath(os.path.join(_script_dir, "..", "..", "..", ".."))
_deploy_scripts_dir = os.path.join(_project_root, "mindie_motor", "src", "example", "deploy_scripts")
if _deploy_scripts_dir not in sys.path:
    sys.path.insert(0, _deploy_scripts_dir)

import ras_starter


class TestParseBootArgs(unittest.TestCase):
    """Test parse_boot_args function."""

    def test_parse_boot_args_empty(self):
        """Empty args returns defaults."""
        result = ras_starter.parse_boot_args([])
        self.assertEqual(result["--user_config_path"], "./user_config.json")
        self.assertEqual(result["--conf_path"], "./conf")
        self.assertEqual(result["--deploy_yaml_path"], "./deployment")

    def test_parse_boot_args_full(self):
        """Full args override defaults."""
        args = [
            "--user_config_path", "/path/to/user_config.json",
            "--conf_path", "/path/to/conf",
            "--deploy_yaml_path", "/path/to/deployment"
        ]
        result = ras_starter.parse_boot_args(args)
        self.assertEqual(result["--user_config_path"], "/path/to/user_config.json")
        self.assertEqual(result["--conf_path"], "/path/to/conf")
        self.assertEqual(result["--deploy_yaml_path"], "/path/to/deployment")

    def test_parse_boot_args_partial(self):
        """Partial args override only specified values."""
        args = ["--conf_path", "/custom/conf"]
        result = ras_starter.parse_boot_args(args)
        self.assertEqual(result["--user_config_path"], "./user_config.json")
        self.assertEqual(result["--conf_path"], "/custom/conf")
        self.assertEqual(result["--deploy_yaml_path"], "./deployment")

    def test_parse_boot_args_unknown_raises(self):
        """Unknown boot arg raises ValueError."""
        args = ["--unknown_arg", "value"]
        with self.assertRaises(ValueError) as ctx:
            ras_starter.parse_boot_args(args)
        self.assertIn("Unknown boot argument", str(ctx.exception))

    def test_parse_boot_args_missing_value_raises(self):
        """Boot arg without value raises ValueError."""
        args = ["--user_config_path"]
        with self.assertRaises(ValueError) as ctx:
            ras_starter.parse_boot_args(args)
        self.assertIn("Invalid input args", str(ctx.exception))


class TestGetMetricsValues(unittest.TestCase):
    """Test get_metrics_values function."""

    def setUp(self):
        self.params = ras_starter.CheckParams(
            with_cert=False,
            model_name="test",
            input_content="prompt",
            deployment_dir="./",
            coordinator_port="8080",
            coordinator_manage_port="8081",
            namespace="test_ns"
        )

    @patch.object(ras_starter, 'fetch_ip_with_namespace_and_name')
    def test_get_metrics_values_no_ip_returns_minus_one(self, mock_fetch_ip):
        """When coordinator IP not found, returns -1 for each metric."""
        mock_fetch_ip.return_value = ""
        mock_http = MagicMock()

        result = ras_starter.get_metrics_values(
            mock_http, self.params,
            "request_success_total", "request_failed_total"
        )
        self.assertEqual(result, (-1, -1))

    @patch.object(ras_starter, 'fetch_ip_with_namespace_and_name')
    def test_get_metrics_values_parses_metrics(self, mock_fetch_ip):
        """Parses metric values from metrics API response."""
        mock_fetch_ip.return_value = "10.0.0.1"
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = (
            b"# HELP request_success_total Total success\n"
            b"request_success_total 42\n"
            b"request_failed_total 3\n"
        )
        mock_http = MagicMock()
        mock_http.request.return_value = mock_response

        result = ras_starter.get_metrics_values(
            mock_http, self.params,
            "request_success_total", "request_failed_total"
        )
        self.assertEqual(result, (42, 3))


if __name__ == '__main__':
    unittest.main()
