# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _add_ccae_path(monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    ccae_root = repo_root / "examples" / "features" / "observability"
    monkeypatch.syspath_prepend(str(ccae_root))


@pytest.fixture
def ccae_mods(monkeypatch: pytest.MonkeyPatch):
    _add_ccae_path(monkeypatch)
    config_mod = importlib.import_module("ccae_reporter.config")
    backend_mod = importlib.import_module("ccae_reporter.backends.motor_backend")
    base_mod = importlib.import_module("ccae_reporter.backends.base_backend")
    original_config = config_mod.ConfigUtil.config
    yield config_mod, backend_mod, base_mod
    config_mod.ConfigUtil.config = original_config


def test_get_config_returns_default_when_controller_api_port_omitted(ccae_mods) -> None:
    """Reporter must not treat Motor's omitted controller_api_port as None."""
    config_mod, _, _ = ccae_mods
    config_mod.ConfigUtil.config = {
        "motor_controller_config": {
            "api_config": {
                "observability_api_port": 1027,
            }
        }
    }

    assert config_mod.ConfigUtil.get_config("motor_controller_config.api_config.controller_api_port", 1026) == 1026
    assert config_mod.ConfigUtil.get_config("motor_controller_config.api_config.controller_api_port") is None
    assert config_mod.ConfigUtil.get_config("motor_controller_config.api_config.observability_api_port", 1027) == 1027


def test_get_config_keeps_explicit_zero_instead_of_default(ccae_mods) -> None:
    config_mod, _, _ = ccae_mods
    config_mod.ConfigUtil.config = {"api_config": {"controller_api_port": 0}}

    assert config_mod.ConfigUtil.get_config("api_config.controller_api_port", 1026) == 0


def test_format_http_address_raises_clear_error_when_port_is_none(ccae_mods) -> None:
    """Missing ports must fail with RuntimeError, not TypeError from %d."""
    _, backend_mod, _ = ccae_mods

    with pytest.raises(RuntimeError, match="controller_api_port"):
        backend_mod._format_http_address("10.0.0.1", None, "motor_controller_config.api_config.controller_api_port")


def test_motor_backend_falls_back_to_default_controller_port(ccae_mods, monkeypatch: pytest.MonkeyPatch) -> None:
    """Docs-style config omits controller_api_port; probe client must still use 1026."""
    config_mod, backend_mod, base_mod = ccae_mods
    config_mod.ConfigUtil.config = {
        "motor_controller_config": {
            "api_config": {
                "observability_api_port": 1027,
            }
        }
    }
    addresses: list[str] = []

    class FakeClient:
        def __init__(self, address: str | None = None, **_kwargs) -> None:
            addresses.append(address)

    monkeypatch.setattr(base_mod, "Collector", MagicMock)
    monkeypatch.setattr(backend_mod, "SafeHTTPSClient", FakeClient)
    monkeypatch.setattr(backend_mod, "Log", MagicMock())
    monkeypatch.setenv("POD_IP", "10.0.0.1")

    backend_mod.MotorBackend("Controller")

    assert "10.0.0.1:1027" in addresses
    assert "10.0.0.1:1026" in addresses


def test_motor_backend_uses_explicit_controller_port(ccae_mods, monkeypatch: pytest.MonkeyPatch) -> None:
    config_mod, backend_mod, base_mod = ccae_mods
    config_mod.ConfigUtil.config = {
        "motor_controller_config": {
            "api_config": {
                "controller_api_port": 2026,
                "observability_api_port": 1027,
            }
        }
    }
    addresses: list[str] = []

    class FakeClient:
        def __init__(self, address: str | None = None, **_kwargs) -> None:
            addresses.append(address)

    monkeypatch.setattr(base_mod, "Collector", MagicMock)
    monkeypatch.setattr(backend_mod, "SafeHTTPSClient", FakeClient)
    monkeypatch.setattr(backend_mod, "Log", MagicMock())
    monkeypatch.setenv("POD_IP", "10.0.0.1")

    backend_mod.MotorBackend("Controller")

    assert "10.0.0.1:2026" in addresses
    assert "10.0.0.1:1026" not in addresses
