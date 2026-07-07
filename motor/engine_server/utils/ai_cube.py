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

import select
import subprocess
import time

from motor.common.logger import get_logger

logger = get_logger(__name__)

# `npu-smi info watch` streams repeatedly; only wait for the first data row.
_WATCH_READ_TIMEOUT_SEC = 5
_WATCH_HELP_TIMEOUT_SEC = 5
_AI_CUBE_USAGE_HELP_MARKER = "u - AI Cube Usage"


def _parse_usage_from_line(line: str) -> int | None:
    """Return usage percent from a watch data row, or None for header/blank lines."""
    stripped = line.strip()
    if not stripped or stripped.startswith("NpuID"):
        return None
    parts = stripped.split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[-1])
    except ValueError:
        return None


def _read_first_ai_cube_usage_from_watch(proc: subprocess.Popen) -> int:
    """Read stdout line-by-line until the first data row appears, then stop."""
    if proc.stdout is None:
        raise RuntimeError("npu-smi watch stdout pipe is not available")

    fd = proc.stdout.fileno()
    deadline = time.monotonic() + _WATCH_READ_TIMEOUT_SEC
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        line = proc.stdout.readline()
        if line == "" and proc.poll() is not None:
            break
        usage = _parse_usage_from_line(line)
        if usage is not None:
            return usage

    raise RuntimeError("AI Cube usage not found in npu-smi watch output (timeout)")


def _stop_watch_process(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.communicate()


def is_ai_cube_usage_watch_supported() -> bool:
    """Return whether the current HDK supports `npu-smi info watch -s u` (AI Cube Usage)."""
    cmd = ["npu-smi", "info", "watch", "-h"]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_WATCH_HELP_TIMEOUT_SEC,
            check=False,
        )
    except OSError:
        logger.warning("npu-smi is not available when checking AI Cube Usage watch support")
        return False
    except subprocess.TimeoutExpired:
        logger.warning("npu-smi info watch -h timed out when checking AI Cube Usage watch support")
        return False

    if result.returncode != 0:
        logger.warning(
            "npu-smi info watch -h failed with exit code %s when checking AI Cube Usage watch support",
            result.returncode,
        )
        return False

    combined = (result.stdout or "") + (result.stderr or "")
    if _AI_CUBE_USAGE_HELP_MARKER in combined:
        return True

    logger.warning("HDK does not support npu-smi info watch -s u (AI Cube Usage)")
    return False


def get_ai_cube_usage():
    """
    Get AI Cube usage rate.

    `npu-smi info watch -s u` prints continuously. Parse the first data row
    (e.g. ``0  0  0`` after the header) and terminate the process immediately.
    """
    cmd = ["npu-smi", "info", "watch", "-s", "u"]
    try:
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        ) as proc:
            try:
                return _read_first_ai_cube_usage_from_watch(proc)
            finally:
                _stop_watch_process(proc)
    except OSError as e:
        raise RuntimeError(f"npu-smi execution failed: {e}") from e
