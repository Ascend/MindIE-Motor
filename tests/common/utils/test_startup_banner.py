# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from unittest.mock import MagicMock

from motor import __version__
from motor.common.utils.startup_banner import log_startup_banner, render_startup_banner


def test_render_startup_banner_uses_motor_version_and_role():
    banner = render_startup_banner("coordinator")
    assert f"v{__version__}" in banner
    assert "Coordinator" in banner
    assert banner.startswith("\n")
    assert banner.endswith("\n")


def test_render_startup_banner_formats_node_manager_role():
    assert "NodeManager.prefill" in render_startup_banner("node_manager.prefill")


def test_log_startup_banner_logs_once():
    logger = MagicMock()
    log_startup_banner(logger, "coordinator")
    logger.info.assert_called_once()
    assert "Coordinator" in logger.info.call_args.args[1]


def test_log_startup_banner_disabled_by_env(monkeypatch):
    monkeypatch.setenv("MOTOR_DISABLE_LOG_LOGO", "1")
    logger = MagicMock()
    log_startup_banner(logger, "coordinator")
    logger.info.assert_called_once_with(
        "MindIE-Motor version %s, role %s",
        __version__,
        "Coordinator",
    )
    assert "███╗" not in str(logger.info.call_args)
