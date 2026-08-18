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
import io
import os
import subprocess

import pytest

from motor.node_manager.core.services.native_engine.virtual_inference.ai_cube import (
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


@pytest.mark.parametrize(
    "output,expected",
    [
        (b"NpuID(Idx)  AI Core(%)\n0           0\n", 0),
        (b"NpuID  ChipID  AI Core(%)\n0  0  37\n", 37),
    ],
    ids=["two_columns", "three_columns"],
)
def test_get_ai_cube_usage_watch_success(output, expected):
    mock_stdout = mock.MagicMock()
    mock_stdout.fileno.return_value = 1
    mock_proc = mock.MagicMock(stdout=mock_stdout, poll=mock.MagicMock(return_value=None))

    mock_ctx = mock.MagicMock()
    mock_ctx.__enter__.return_value = mock_proc
    mock_ctx.__exit__.return_value = False

    with mock.patch(
        "motor.node_manager.core.services.native_engine.virtual_inference.ai_cube.subprocess.Popen",
        return_value=mock_ctx,
    ) as mock_popen:
        with (
            mock.patch("select.select", return_value=([1], [], [])),
            mock.patch("os.read", return_value=output),
        ):
            usage = get_ai_cube_usage()
            assert usage == expected
            mock_popen.assert_called_once_with(
                ["npu-smi", "info", "watch", "-s", "u"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )


def test_read_first_ai_cube_usage_from_watch_timeout():
    mock_stdout = mock.MagicMock()
    mock_stdout.fileno.return_value = 1
    mock_proc = mock.MagicMock(stdout=mock_stdout, poll=mock.MagicMock(return_value=None))

    with mock.patch("select.select", return_value=([], [], [])):
        with pytest.raises(RuntimeError) as cm:
            _read_first_ai_cube_usage_from_watch(mock_proc)
        assert "AI Cube usage not found in npu-smi watch output (timeout)" in str(cm.value)


def test_read_first_ai_cube_usage_does_not_lose_buffered_data_row():
    """Header and data may arrive in one pipe write; the data row must not be hidden by text buffering."""
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"NpuID  ChipID  AI Core(%)\n0  0  37\n")
    stdout = io.TextIOWrapper(os.fdopen(read_fd, "rb", closefd=True))
    proc = mock.MagicMock(stdout=stdout, poll=mock.MagicMock(return_value=None))

    try:
        with mock.patch(
            "motor.node_manager.core.services.native_engine.virtual_inference.ai_cube._WATCH_READ_TIMEOUT_SEC", 0.05
        ):
            assert _read_first_ai_cube_usage_from_watch(proc) == 37
    finally:
        os.close(write_fd)
        stdout.close()


def test_is_ai_cube_usage_watch_supported_rejects_when_help_missing_u_metric(caplog):
    mock_result = mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = (
        "Usage: npu-smi info watch [Options...]\n"
        "                          a - AI Core Usage\n"
        "                          n - NPU Utilization\n"
    )
    mock_result.stderr = ""

    with mock.patch(
        "motor.node_manager.core.services.native_engine.virtual_inference.ai_cube.subprocess.run",
        return_value=mock_result,
    ):
        assert is_ai_cube_usage_watch_supported() is False
        assert "HDK does not support npu-smi info watch -s u (AI Cube Usage)" in caplog.text


def test_is_ai_cube_usage_watch_supported_accepts_when_help_lists_u_metric(caplog):
    mock_result = mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "                          u - AI Cube Usage\n"
    mock_result.stderr = ""

    with mock.patch(
        "motor.node_manager.core.services.native_engine.virtual_inference.ai_cube.subprocess.run",
        return_value=mock_result,
    ):
        assert is_ai_cube_usage_watch_supported() is True
        assert caplog.text == ""


def test_is_ai_cube_usage_watch_supported_rejects_on_command_failure(caplog):
    with mock.patch(
        "motor.node_manager.core.services.native_engine.virtual_inference.ai_cube.subprocess.run",
        side_effect=OSError("npu-smi not found"),
    ):
        assert is_ai_cube_usage_watch_supported() is False
        assert "npu-smi is not available when checking AI Cube Usage watch support" in caplog.text


def test_get_ai_cube_usage_npu_smi_failure():
    with mock.patch(
        "motor.node_manager.core.services.native_engine.virtual_inference.ai_cube.subprocess.Popen",
        side_effect=OSError("npu-smi not found"),
    ):
        with pytest.raises(RuntimeError) as cm:
            get_ai_cube_usage()
        assert "npu-smi execution failed" in str(cm.value)
