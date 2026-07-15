# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Tests for Coordinator observability TLS configuration support.

Covers:
  1. ObservabilityServer TLS support in run() (via mock-based behavior verification)
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Early mock of the OpenSSL-dependent modules so that ObservabilityServer
# and other coordinator modules can be imported in this environment.
for _mod_name in [
    "motor.common.http",
    "motor.common.http.cert_util",
    "motor.common.http.key_encryption",
    "motor.common.http.http_client",
    "motor.common.http.security_utils",
    "motor.common.http.http_response",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = MagicMock()

from motor.coordinator.api_server.observability_server import ObservabilityServer  # noqa: E402
from motor.config.coordinator import CoordinatorConfig  # noqa: E402


# =========================================================================
# ObservabilityServer – TLS support in run()
# =========================================================================


class TestObservabilityServerTls:
    """ObservabilityServer.run() correctly handles TLS using mgmt_tls_config."""

    # ObservabilityServer is already imported at module level after mocking
    # the OpenSSL-dependent modules.  Tests use patch.object to avoid
    # triggering __init__ side effects (_register_routes calls AppBuilder).

    def _make_server(self, **kwargs):
        """Helper: create an ObservabilityServer with _register_routes mocked."""
        config = CoordinatorConfig()
        for k, v in kwargs.items():
            setattr(config.mgmt_tls_config, k, v)
        with patch.object(ObservabilityServer, "_register_routes", return_value=None):
            return ObservabilityServer(config=config)

    def test_obs_ssl_config_stored_in_init(self):
        """_obs_ssl_config is stored in __init__ from coordinator_config.mgmt_tls_config."""
        server = self._make_server(enable_tls=True)
        assert server._obs_ssl_config is not None
        assert server._obs_ssl_config.enable_tls is True

    def test_obs_ssl_config_disabled_by_default(self):
        """_obs_ssl_config defaults to disabled when TLS is not configured."""
        server = self._make_server()
        assert server._obs_ssl_config.enable_tls is False

    def test_obs_ssl_config_cert_paths_from_config(self):
        """_obs_ssl_config uses the cert paths from CoordinatorConfig.mgmt_tls_config."""
        server = self._make_server(
            enable_tls=True,
            cert_file="/custom/obs/cert.pem",
            key_file="/custom/obs/key.pem",
        )
        assert server._obs_ssl_config.cert_file == "/custom/obs/cert.pem"
        assert server._obs_ssl_config.key_file == "/custom/obs/key.pem"

    def test_apply_config_changes_enables_tls(self):
        """_apply_config_changes updates _obs_ssl_config on hot-reload (enable)."""
        server = self._make_server()
        assert server._obs_ssl_config.enable_tls is False

        new_config = CoordinatorConfig()
        new_config.mgmt_tls_config.enable_tls = True
        server._apply_config_changes(new_config)
        assert server._obs_ssl_config.enable_tls is True

    def test_apply_config_changes_disables_tls(self):
        """_apply_config_changes can disable TLS on hot-reload."""
        server = self._make_server(enable_tls=True)
        assert server._obs_ssl_config.enable_tls is True

        new_config = CoordinatorConfig()
        new_config.mgmt_tls_config.enable_tls = False
        server._apply_config_changes(new_config)
        assert server._obs_ssl_config.enable_tls is False

    def test_obs_tls_config_uses_mgmt_tls_config(self):
        """_obs_ssl_config reflects mgmt_tls_config (not a separate obs config)."""
        config = CoordinatorConfig()
        config.mgmt_tls_config.enable_tls = True
        config.infer_tls_config.enable_tls = True
        with patch.object(ObservabilityServer, "_register_routes", return_value=None):
            server = ObservabilityServer(config=config)
        # obs_ssl should follow mgmt_tls_config, so it should be True
        assert server._obs_ssl_config.enable_tls is True

    def test_apply_config_changes_replaces_ssl_config_object(self):
        """_apply_config_changes replaces _obs_ssl_config with the new config's TLSConfig."""
        server = self._make_server()
        original_ref = server._obs_ssl_config

        new_config = CoordinatorConfig()
        new_config.mgmt_tls_config.enable_tls = True
        server._apply_config_changes(new_config)

        # The reference should now point to the new config's TLSConfig
        assert server._obs_ssl_config is new_config.mgmt_tls_config
        assert server._obs_ssl_config is not original_ref

    # ------------------------------------------------------------------
    # Mock-based behavior tests for the run() method's TLS branch.
    # These tests mock uvicorn.Config and uvicorn.Server to avoid the
    # blocking server.serve() call, and verify the TLS behavior
    # through call assertions rather than AST source parsing.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_run_creates_ssl_context_when_tls_enabled(self):
        """run() creates SSL context and sets uv_config.ssl when TLS is enabled."""
        server = self._make_server(enable_tls=True)
        mock_ssl_ctx = MagicMock()
        mock_uv_config = MagicMock()
        mock_uv_server = AsyncMock()

        with (
            patch("motor.coordinator.api_server.observability_server.uvicorn.Config", return_value=mock_uv_config),
            patch("motor.coordinator.api_server.observability_server.uvicorn.Server", return_value=mock_uv_server),
            patch(
                "motor.coordinator.api_server.observability_server.CertUtil.create_ssl_context",
                return_value=mock_ssl_ctx,
            ) as mock_create_ssl,
        ):
            await server.run()

            mock_create_ssl.assert_called_once()
            assert mock_uv_config.ssl is mock_ssl_ctx
            mock_uv_server.serve.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_skips_ssl_when_tls_disabled(self):
        """run() does not create SSL context when TLS is disabled."""
        server = self._make_server(enable_tls=False)
        mock_uv_config = MagicMock()
        mock_uv_server = AsyncMock()

        with (
            patch("motor.coordinator.api_server.observability_server.uvicorn.Config", return_value=mock_uv_config),
            patch("motor.coordinator.api_server.observability_server.uvicorn.Server", return_value=mock_uv_server),
            patch("motor.coordinator.api_server.observability_server.CertUtil.create_ssl_context") as mock_create_ssl,
        ):
            await server.run()

            mock_create_ssl.assert_not_called()
            mock_uv_server.serve.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_logs_warning_when_ssl_context_fails(self):
        """run() logs a warning when CertUtil.create_ssl_context returns None."""
        server = self._make_server(enable_tls=True)
        mock_uv_config = MagicMock()
        mock_uv_server = AsyncMock()

        with (
            patch("motor.coordinator.api_server.observability_server.uvicorn.Config", return_value=mock_uv_config),
            patch("motor.coordinator.api_server.observability_server.uvicorn.Server", return_value=mock_uv_server),
            patch("motor.coordinator.api_server.observability_server.CertUtil.create_ssl_context", return_value=None),
            patch("motor.coordinator.api_server.observability_server.logger.warning") as mock_warning,
        ):
            await server.run()

            mock_warning.assert_called_once()
            assert "Failed to create SSL context" in mock_warning.call_args[0][0]
            mock_uv_server.serve.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_logs_https_url_when_tls_enabled(self):
        """run() logs https:// URL when TLS is enabled and context is created."""
        server = self._make_server(enable_tls=True)
        mock_ssl_ctx = MagicMock()
        mock_uv_config = MagicMock()
        mock_uv_server = AsyncMock()

        with (
            patch("motor.coordinator.api_server.observability_server.uvicorn.Config", return_value=mock_uv_config),
            patch("motor.coordinator.api_server.observability_server.uvicorn.Server", return_value=mock_uv_server),
            patch(
                "motor.coordinator.api_server.observability_server.CertUtil.create_ssl_context",
                return_value=mock_ssl_ctx,
            ),
            patch("motor.coordinator.api_server.observability_server.logger.info") as mock_info,
        ):
            await server.run()

            mock_info.assert_called_once()
            assert "https://" in mock_info.call_args[0][0]
            mock_uv_server.serve.assert_called_once()
