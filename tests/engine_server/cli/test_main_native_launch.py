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

"""Tests for motor.engine_server.cli.main native launch mode."""

import importlib
import signal
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixture: import main once with engine-server fakes installed
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def es_main():
    """Import and return the main module with engine-server mocks installed.

    Session-scoped: the mocks are installed once and cleaned up after the
    last test, so no global state leaks to other test suites.
    """
    _install_engine_fakes()
    import motor.engine_server.cli.main as main_mod

    importlib.reload(main_mod)
    yield main_mod
    _uninstall_engine_fakes()
    del sys.modules["motor.engine_server.cli.main"]


def _install_engine_fakes():
    """Install engine-server module fakes needed to import main.py."""
    mock_config = MagicMock()
    mock_ep = MagicMock()
    mock_ep.engine_type = "vllm"
    mock_ep.dp_rank = 0
    mock_ep.snapshot_metadata = None
    mock_config.get_endpoint_config.return_value = mock_ep
    mock_config.get_cli_args.return_value = ["--model", "/m"]

    mock_cf_cls = MagicMock()
    mock_cf_cls.return_value.parse.return_value = mock_config
    mock_cf_mod = MagicMock()
    mock_cf_mod.ConfigFactory = mock_cf_cls

    mock_infer_instance = MagicMock()
    mock_ef_cls = MagicMock()
    mock_ef_cls.return_value.get_infer_endpoint.return_value = mock_infer_instance
    mock_ef_mod = MagicMock()
    mock_ef_mod.EndpointFactory = mock_ef_cls

    mock_infer_mod = MagicMock()
    mock_infer_mod.InferEndpoint = MagicMock()
    mock_mgmt_mod = MagicMock()
    mock_mgmt_mod.MgmtEndpoint = MagicMock()
    mock_prometheus = MagicMock()

    mock_endpoint_cfg_mod = MagicMock()
    mock_endpoint_cfg_mod.EndpointConfig.init_endpoint_config.return_value = mock_ep

    fakes = {
        "motor.engine_server.factory.config_factory": mock_cf_mod,
        "motor.engine_server.factory.endpoint_factory": mock_ef_mod,
        "motor.engine_server.core.infer_endpoint": mock_infer_mod,
        "motor.engine_server.core.mgmt_endpoint": mock_mgmt_mod,
        "motor.engine_server.utils.prometheus": mock_prometheus,
        "motor.config.endpoint": mock_endpoint_cfg_mod,
    }
    for mod_name, fake in fakes.items():
        sys.modules[mod_name] = fake


def _uninstall_engine_fakes():
    """Remove engine-server module fakes that were added by _install_engine_fakes."""
    for mod_name in (
        "motor.engine_server.factory.config_factory",
        "motor.engine_server.factory.endpoint_factory",
        "motor.engine_server.core.infer_endpoint",
        "motor.engine_server.core.mgmt_endpoint",
        "motor.engine_server.utils.prometheus",
        "motor.config.endpoint",
    ):
        sys.modules.pop(mod_name, None)


# ---------------------------------------------------------------------------
# Tests for _build_native_launch_cmd — no main() needed, inline import
# ---------------------------------------------------------------------------


class TestBuildNativeLaunchCmd:
    """Unit tests for :func:`_build_native_launch_cmd`."""

    @staticmethod
    def _get_func():
        import motor.engine_server.cli.main as m

        return m._build_native_launch_cmd

    def test_vllm_command_structure(self):
        func = self._get_func()
        config = _make_mock_config("vllm", ["--model", "/m", "--port", "8000"])
        cmd = func(config)
        assert cmd[:2] == ["vllm", "serve"]
        assert "--model" in cmd
        assert "/m" in cmd
        assert "--port" in cmd

    def test_vllm_appends_cli_args(self):
        func = self._get_func()
        config = _make_mock_config("vllm", ["--model", "/m", "--host", "0.0.0.0", "--port", "8000"])
        cmd = func(config)
        assert cmd == ["vllm", "serve", "--model", "/m", "--host", "0.0.0.0", "--port", "8000"]

    def test_sglang_command_structure(self):
        func = self._get_func()
        config = _make_mock_config("sglang", ["--model-path", "/m", "--port", "8000"])
        cmd = func(config)
        assert cmd[:3] == ["python3", "-m", "sglang.launch_server"]

    def test_sglang_appends_cli_args(self):
        func = self._get_func()
        config = _make_mock_config("sglang", ["--model-path", "/m", "--host", "0.0.0.0"])
        cmd = func(config)
        assert cmd == [
            "python3",
            "-m",
            "sglang.launch_server",
            "--model-path",
            "/m",
            "--host",
            "0.0.0.0",
        ]

    def test_empty_cli_args(self):
        func = self._get_func()
        config = _make_mock_config("vllm", [])
        cmd = func(config)
        assert cmd == ["vllm", "serve"]

    def test_unsupported_engine_raises(self):
        func = self._get_func()
        config = _make_mock_config("unknown_engine", ["--some-arg"])
        with pytest.raises(ValueError, match="unknown_engine"):
            func(config)


# ---------------------------------------------------------------------------
# Tests for main() — NATIVE_LAUNCH_ENABLED routing
# ---------------------------------------------------------------------------


class TestMainNativeLaunchRouting:
    """Test that :func:`main` routes to the native or invasive path."""

    _es = None  # set by _patch_main fixture

    @pytest.fixture(autouse=True)
    def _patch_main(self, es_main):
        """Attach main module to the test instance so individual tests can reference it."""
        self._es = es_main

    @pytest.fixture(autouse=True)
    def _reset_mocks(self):
        """Reset module-level mock call state before each test to avoid cross-test leakage."""
        yield
        for mod_name in (
            "motor.engine_server.core.infer_endpoint",
            "motor.engine_server.core.mgmt_endpoint",
            "motor.engine_server.factory.endpoint_factory",
            "motor.engine_server.factory.config_factory",
        ):
            sys.modules[mod_name].reset_mock()

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _ef():
        return sys.modules["motor.engine_server.factory.endpoint_factory"].EndpointFactory

    @staticmethod
    def _mgmt_cls():
        return sys.modules["motor.engine_server.core.mgmt_endpoint"].MgmtEndpoint

    # -- NATIVE_LAUNCH_ENABLED=True → calls _run_native -------------------

    @patch("motor.engine_server.cli.main._run_native")
    def test_native_enabled_calls_run_native(self, mock_run_native):
        with patch.object(self._es, "NATIVE_LAUNCH_ENABLED", True):
            self._es.main()
        mock_run_native.assert_called_once()

    @patch("motor.engine_server.cli.main._run_native")
    def test_native_enabled_starts_mgmt_and_skips_infer(self, mock_run_native):
        with patch.object(self._es, "NATIVE_LAUNCH_ENABLED", True):
            self._es.main()
        self._mgmt_cls().assert_called_once()
        self._mgmt_cls().return_value.run.assert_called_once()
        self._ef().return_value.get_infer_endpoint.assert_not_called()

    # -- NATIVE_LAUNCH_ENABLED=False (default) → goes invasive path -------

    def test_native_disabled_goes_invasive(self):
        self._es.main()
        self._mgmt_cls().return_value.run.assert_called_once()
        self._ef().return_value.get_infer_endpoint.return_value.run.assert_called_once()


# ---------------------------------------------------------------------------
# Tests for _run_native — subprocess launch and signal handling
# ---------------------------------------------------------------------------


class TestRunNative:
    """Unit tests for :func:`_run_native`."""

    @staticmethod
    def _get_func():
        import motor.engine_server.cli.main as m

        return m._run_native

    @patch("motor.engine_server.cli.main.subprocess.Popen")
    @patch("motor.engine_server.cli.main.signal.signal")
    @patch("motor.engine_server.cli.main.logger")
    def test_popen_called_with_correct_command(self, _logger, _signal, mock_popen):
        func = self._get_func()
        config = _make_mock_config("vllm", ["--model", "/m"])
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        mock_popen.return_value.__enter__.return_value = mock_proc
        func(config)
        mock_popen.assert_called_once_with(["vllm", "serve", "--model", "/m"])

    @patch("motor.engine_server.cli.main.subprocess.Popen")
    @patch("motor.engine_server.cli.main.signal.signal")
    @patch("motor.engine_server.cli.main.logger")
    def test_signal_handlers_registered(self, _logger, mock_signal, mock_popen):
        func = self._get_func()
        config = _make_mock_config("vllm", [])
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        mock_popen.return_value.__enter__.return_value = mock_proc
        func(config)
        signal_calls = [c.args[0] for c in mock_signal.call_args_list]
        assert signal.SIGTERM in signal_calls
        assert signal.SIGINT in signal_calls

    @patch("motor.engine_server.cli.main.subprocess.Popen")
    @patch("motor.engine_server.cli.main.signal.signal")
    @patch("motor.engine_server.cli.main.logger")
    def test_popen_wait_called(self, _logger, _signal, mock_popen):
        func = self._get_func()
        config = _make_mock_config("vllm", [])
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        mock_popen.return_value.__enter__.return_value = mock_proc
        func(config)
        mock_proc.wait.assert_called()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_mock_config(engine_type: str, cli_args: list[str] | None = None):
    """Create a mock config consumed by ``_build_native_launch_cmd``."""
    mock_config = MagicMock()
    mock_endpoint_config = MagicMock()
    mock_endpoint_config.engine_type = engine_type
    mock_config.get_endpoint_config.return_value = mock_endpoint_config
    mock_config.get_cli_args.return_value = cli_args or []
    return mock_config
