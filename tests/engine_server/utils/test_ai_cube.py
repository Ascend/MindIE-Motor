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
import subprocess

import pytest

from motor.engine_server.utils.ai_cube import (
    _parse_usage_from_line,
    _read_first_ai_cube_usage_from_watch,
    get_ai_cube_usage,
    is_ai_cube_usage_watch_supported,
)


def test_parse_usage_from_line():
    assert _parse_usage_from_line("") is None
    assert _parse_usage_from_line("NpuID  ChipID  AI Core(%)\n") is None
    assert _parse_usage_from_line("NpuID(Idx)  AI Core(%)\n") is None
    assert _parse_usage_from_line("NpuID(Idx)  ChipId(Idx) AI Core(%)\n") is None
    assert _parse_usage_from_line("0           0\n") == 0
    assert _parse_usage_from_line("0           0           3\n") == 3
    assert _parse_usage_from_line("0  0  37\n") == 37


def test_get_ai_cube_usage_watch_success_two_column_format():
    mock_stdout = mock.MagicMock()
    mock_stdout.readline.side_effect = ["NpuID(Idx)  AI Core(%)\n", "0           0\n"]
    mock_stdout.fileno.return_value = 1
    mock_proc = mock.MagicMock(stdout=mock_stdout, poll=mock.MagicMock(return_value=None))

    mock_ctx = mock.MagicMock()
    mock_ctx.__enter__.return_value = mock_proc
    mock_ctx.__exit__.return_value = False

    with mock.patch("motor.engine_server.utils.ai_cube.subprocess.Popen", return_value=mock_ctx) as mock_popen:
        with mock.patch("select.select", return_value=([1], [], [])):
            usage = get_ai_cube_usage()
            assert usage == 0
            mock_popen.assert_called_once_with(
                ["npu-smi", "info", "watch", "-s", "u"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )


def test_get_ai_cube_usage_watch_success():
    mock_stdout = mock.MagicMock()
    mock_stdout.readline.side_effect = ["NpuID  ChipID  AI Core(%)\n", "0  0  37\n"]
    mock_stdout.fileno.return_value = 1
    mock_proc = mock.MagicMock(stdout=mock_stdout, poll=mock.MagicMock(return_value=None))

    mock_ctx = mock.MagicMock()
    mock_ctx.__enter__.return_value = mock_proc
    mock_ctx.__exit__.return_value = False

    with mock.patch("motor.engine_server.utils.ai_cube.subprocess.Popen", return_value=mock_ctx):
        with mock.patch("select.select", return_value=([1], [], [])):
            usage = get_ai_cube_usage()
            assert usage == 37


def test_read_first_ai_cube_usage_from_watch_timeout():
    mock_stdout = mock.MagicMock()
    mock_stdout.readline.return_value = "NpuID  ChipID  AI Core(%)\n"
    mock_stdout.fileno.return_value = 1
    mock_proc = mock.MagicMock(stdout=mock_stdout, poll=mock.MagicMock(return_value=None))

    with mock.patch("select.select", return_value=([], [], [])):
        with pytest.raises(RuntimeError) as cm:
            _read_first_ai_cube_usage_from_watch(mock_proc)
        assert "AI Cube usage not found in npu-smi watch output (timeout)" in str(cm.value)


def test_get_ai_cube_usage_watch_timeout():
    mock_stdout = mock.MagicMock()
    mock_stdout.readline.return_value = "NpuID  ChipID  AI Core(%)\n"
    mock_stdout.fileno.return_value = 1
    mock_proc = mock.MagicMock(stdout=mock_stdout, poll=mock.MagicMock(return_value=None))

    mock_ctx = mock.MagicMock()
    mock_ctx.__enter__.return_value = mock_proc
    mock_ctx.__exit__.return_value = False

    with mock.patch("motor.engine_server.utils.ai_cube.subprocess.Popen", return_value=mock_ctx):
        with mock.patch("select.select", return_value=([], [], [])):
            with pytest.raises(RuntimeError) as cm:
                get_ai_cube_usage()
            assert "AI Cube usage not found in npu-smi watch output (timeout)" in str(cm.value)


def test_is_ai_cube_usage_watch_supported_rejects_when_help_missing_u_metric(caplog):
    mock_result = mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = (
        "Usage: npu-smi info watch [Options...]\n"
        "                          a - AI Core Usage\n"
        "                          n - NPU Utilization\n"
    )
    mock_result.stderr = ""

    with mock.patch("motor.engine_server.utils.ai_cube.subprocess.run", return_value=mock_result):
        assert is_ai_cube_usage_watch_supported() is False
        assert "HDK does not support npu-smi info watch -s u (AI Cube Usage)" in caplog.text


def test_is_ai_cube_usage_watch_supported_accepts_when_help_lists_u_metric(caplog):
    mock_result = mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "                          u - AI Cube Usage\n"
    mock_result.stderr = ""

    with mock.patch("motor.engine_server.utils.ai_cube.subprocess.run", return_value=mock_result):
        assert is_ai_cube_usage_watch_supported() is True
        assert caplog.text == ""


def test_is_ai_cube_usage_watch_supported_rejects_on_command_failure(caplog):
    with mock.patch(
        "motor.engine_server.utils.ai_cube.subprocess.run",
        side_effect=OSError("npu-smi not found"),
    ):
        assert is_ai_cube_usage_watch_supported() is False
        assert "npu-smi is not available when checking AI Cube Usage watch support" in caplog.text


def test_is_ai_cube_usage_watch_supported_rejects_on_help_timeout(caplog):
    with mock.patch(
        "motor.engine_server.utils.ai_cube.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["npu-smi"], timeout=5),
    ):
        assert is_ai_cube_usage_watch_supported() is False
        assert "npu-smi info watch -h timed out when checking AI Cube Usage watch support" in caplog.text


def test_is_ai_cube_usage_watch_supported_rejects_on_nonzero_exit(caplog):
    mock_result = mock.MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "error: driver not ready"
    mock_result.stderr = ""

    with mock.patch("motor.engine_server.utils.ai_cube.subprocess.run", return_value=mock_result):
        assert is_ai_cube_usage_watch_supported() is False
        assert "npu-smi info watch -h failed with exit code 1" in caplog.text


def test_is_ai_cube_usage_watch_supported_rejects_on_nonzero_exit_even_with_marker(caplog):
    mock_result = mock.MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = "                          u - AI Cube Usage\n"
    mock_result.stderr = ""

    with mock.patch("motor.engine_server.utils.ai_cube.subprocess.run", return_value=mock_result):
        assert is_ai_cube_usage_watch_supported() is False
        assert "npu-smi info watch -h failed with exit code 1" in caplog.text
        assert "HDK does not support" not in caplog.text


def test_get_ai_cube_usage_npu_smi_failure():
    with mock.patch(
        "motor.engine_server.utils.ai_cube.subprocess.Popen",
        side_effect=OSError("npu-smi not found"),
    ):
        with pytest.raises(RuntimeError) as cm:
            get_ai_cube_usage()
        assert "npu-smi execution failed" in str(cm.value)
