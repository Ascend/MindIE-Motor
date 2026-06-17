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

from unittest import mock

import pytest

from motor.engine_server.utils.aicore import (
    _parse_usage_from_line,
    _read_first_aicore_usage_from_watch,
    get_aicore_usage,
)


def test_parse_usage_from_line():
    assert _parse_usage_from_line("") is None
    assert _parse_usage_from_line("NpuID  ChipID  AI Core(%)\n") is None
    assert _parse_usage_from_line("0  0  37\n") == 37


def test_get_aicore_usage_watch_success():
    mock_proc = mock.MagicMock()
    mock_proc.stdout.readline.side_effect = ["NpuID  ChipID  AI Core(%)\n", "0  0  37\n"]
    mock_proc.stdout.fileno.return_value = 1
    mock_proc.poll.return_value = None

    mock_ctx = mock.MagicMock()
    mock_ctx.__enter__.return_value = mock_proc
    mock_ctx.__exit__.return_value = False

    with mock.patch("motor.engine_server.utils.aicore.subprocess.Popen", return_value=mock_ctx):
        with mock.patch("select.select", return_value=([1], [], [])):
            usage = get_aicore_usage()
            assert usage == 37


def test_read_first_aicore_usage_from_watch_timeout():
    mock_proc = mock.MagicMock()
    mock_proc.stdout.readline.return_value = "NpuID  ChipID  AI Core(%)\n"
    mock_proc.stdout.fileno.return_value = 1
    mock_proc.poll.return_value = None

    with mock.patch("select.select", return_value=([], [], [])):
        with pytest.raises(RuntimeError) as cm:
            _read_first_aicore_usage_from_watch(mock_proc)
        assert "AI Core usage not found in npu-smi watch output (timeout)" in str(cm.value)


def test_get_aicore_usage_watch_timeout():
    mock_proc = mock.MagicMock()
    mock_proc.stdout.readline.return_value = "NpuID  ChipID  AI Core(%)\n"
    mock_proc.stdout.fileno.return_value = 1
    mock_proc.poll.return_value = None

    mock_ctx = mock.MagicMock()
    mock_ctx.__enter__.return_value = mock_proc
    mock_ctx.__exit__.return_value = False

    with mock.patch("motor.engine_server.utils.aicore.subprocess.Popen", return_value=mock_ctx):
        with mock.patch("select.select", return_value=([], [], [])):
            with pytest.raises(RuntimeError) as cm:
                get_aicore_usage()
            assert "AI Core usage not found in npu-smi watch output (timeout)" in str(cm.value)


def test_get_aicore_usage_npu_smi_failure():
    with mock.patch(
        "motor.engine_server.utils.aicore.subprocess.Popen",
        side_effect=OSError("npu-smi not found"),
    ):
        with pytest.raises(RuntimeError) as cm:
            get_aicore_usage()
        assert "npu-smi execution failed" in str(cm.value)
