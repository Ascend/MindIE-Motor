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

    def test_parse_boot_args_unknown_positional_raises(self):
        """Stray token (not a known option name) raises ValueError."""
        args = ["orphan", "--conf_path", "/c"]
        with self.assertRaises(ValueError) as ctx:
            ras_starter.parse_boot_args(args)
        self.assertIn("Unknown boot argument", str(ctx.exception))
        self.assertIn("orphan", str(ctx.exception))

    def test_parse_boot_args_unknown_short_flag_raises(self):
        """Single-dash token raises ValueError like other unknown args."""
        args = ["-x", "y"]
        with self.assertRaises(ValueError) as ctx:
            ras_starter.parse_boot_args(args)
        self.assertIn("Unknown boot argument", str(ctx.exception))

    def test_parse_boot_args_prefix_match_unique(self):
        """Unique prefix resolves to full boot arg name."""
        args = ["--user", "/u.json"]
        result = ras_starter.parse_boot_args(args)
        self.assertEqual(result["--user_config_path"], "/u.json")

    def test_parse_boot_args_ambiguous_prefix_raises(self):
        """Prefix matching more than one key raises ValueError."""
        args = ["--", "/x"]
        with self.assertRaises(ValueError) as ctx:
            ras_starter.parse_boot_args(args)
        self.assertIn("mismatch", str(ctx.exception))

    def test_parse_boot_args_attach_not_recognized(self):
        """--attach is not a valid boot arg unless filtered earlier."""
        with self.assertRaises(ValueError) as ctx:
            ras_starter.parse_boot_args(["--attach"])
        self.assertIn("Unknown boot argument", str(ctx.exception))

    def test_parse_boot_args_attach_equals_not_recognized(self):
        """--attach= form is not stripped; treated as unknown option name."""
        with self.assertRaises(ValueError) as ctx:
            ras_starter.parse_boot_args(["--attach=1", "--conf_path", "/c"])
        self.assertIn("Unknown boot argument", str(ctx.exception))


class TestFilterDeprecatedAttachFromBootArgs(unittest.TestCase):
    """Test filter_deprecated_attach_from_boot_args (main() step before parse_boot_args)."""

    def test_no_attach_unchanged(self):
        args = ["--conf_path", "/c"]
        self.assertEqual(
            ras_starter.filter_deprecated_attach_from_boot_args(list(args)),
            args,
        )

    def test_strips_standalone_attach(self):
        args = ["--attach", "--conf_path", "/c"]
        self.assertEqual(
            ras_starter.filter_deprecated_attach_from_boot_args(args),
            ["--conf_path", "/c"],
        )

    def test_strips_attach_with_value(self):
        args = ["--conf_path", "/c", "--attach", "1", "--deploy_yaml_path", "/d"]
        self.assertEqual(
            ras_starter.filter_deprecated_attach_from_boot_args(args),
            ["--conf_path", "/c", "--deploy_yaml_path", "/d"],
        )

    def test_attach_before_next_option_does_not_consume_option(self):
        """Next token starting with '-' is kept (not treated as attach value)."""
        args = ["--attach", "--conf_path", "/c"]
        self.assertEqual(
            ras_starter.filter_deprecated_attach_from_boot_args(args),
            ["--conf_path", "/c"],
        )

    def test_logs_when_attach_removed(self):
        with self.assertLogs(level="WARNING") as cm:
            ras_starter.filter_deprecated_attach_from_boot_args(["--attach"])
        self.assertTrue(
            any("deprecated" in m.lower() and "attach" in m.lower() for m in cm.output),
            msg=f"expected deprecated attach log, got: {cm.output}",
        )

    def test_filter_then_parse_boot_args(self):
        """Pipeline used in main(): strip --attach then parse."""
        raw = ["--attach", "--conf_path", "/c"]
        filtered = ras_starter.filter_deprecated_attach_from_boot_args(raw)
        result = ras_starter.parse_boot_args(filtered)
        self.assertEqual(result["--conf_path"], "/c")


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
