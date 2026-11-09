# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import sys
import types
from pathlib import Path

import pytest

if sys.platform == "win32":
    termios = types.ModuleType("termios")
    termios.TCSANOW = 0
    termios.ECHO = 0
    termios.ICANON = 0
    termios.tcgetattr = lambda _fd: []
    termios.tcsetattr = lambda _fd, _when, _attrs: None
    sys.modules["termios"] = termios

    tty = types.ModuleType("tty")
    tty.setraw = lambda _fd: None
    sys.modules["tty"] = tty

DEPLOYER_ROOT = Path(__file__).resolve().parents[3] / "examples" / "deployer"
sys.path.insert(0, str(DEPLOYER_ROOT))

import lib.constant as C  # noqa: E402
from lib.generator import k8s_utils  # noqa: E402


@pytest.fixture(autouse=True)
def mock_accelerator_type_from_cluster(request, monkeypatch):
    """YAML generation tests should not require a live Kubernetes cluster."""
    if request.module.__name__.endswith("test_accelerator_type"):
        k8s_utils._g_accelerator_type_cache.clear()
        yield
        k8s_utils._g_accelerator_type_cache.clear()
        return

    k8s_utils._g_accelerator_type_cache.clear()
    monkeypatch.setattr(
        k8s_utils,
        "get_accelerator_type_from_cluster",
        lambda _hardware_type: C.ACCELERATOR_TYPE_A3,
    )
    yield
    k8s_utils._g_accelerator_type_cache.clear()
