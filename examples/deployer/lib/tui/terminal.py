# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Terminal context manager for raw-mode interactive input."""

from __future__ import annotations

import os
import select
import signal
import sys
import termios
import tty

from lib import constant as C
from .tui_utils import get_term_width


class TerminalContext:
    """Context manager that puts the terminal in raw mode for interactive input."""

    def __init__(self):
        self._fd = sys.stdin.fileno()
        self._stderr_fd = sys.stderr.fileno()
        self._old_settings: list | None = None
        self._resized = False
        self.width: int = C.MIN_BOX_WIDTH

    def __enter__(self) -> 'TerminalContext':
        self._old_settings = termios.tcgetattr(self._fd)
        tty.setraw(self._fd)
        self._write('\033[?25l')  # hide cursor
        self._write('\033[?1049h')  # switch to alternate screen buffer
        self.width = get_term_width()
        self.clear()
        signal.signal(signal.SIGWINCH, self._on_resize)
        return self

    def __exit__(self, *args) -> None:
        signal.signal(signal.SIGWINCH, signal.SIG_DFL)
        self._write('\033[?1049l')  # restore screen buffer
        self._write('\033[?25h')  # show cursor
        if self._old_settings is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_settings)

    def _on_resize(self, _signum, _frame):
        self._resized = True
        self.width = get_term_width()

    def _write(self, text: str) -> None:
        """Write directly to the original stderr fd, bypassing sys.stderr.

        This ensures TUI output is unaffected when sys.stderr is temporarily
        redirected (e.g. during deploy log capture).
        """
        os.write(self._stderr_fd, text.encode('utf-8'))

    def clear(self) -> None:
        """Clear entire screen and move cursor to (1,1)."""
        self._write('\033[2J\033[H')

    def move_to(self, row: int, col: int = 1) -> None:
        self._write(f'\033[{row};{col}H')

    def erase_line(self) -> None:
        self._write('\033[K')

    def write_at(self, row: int, col: int, text: str) -> None:
        self.move_to(row, col)
        self._write(text)

    def poll_key(self, timeout: float = C.KEY_POLL_INTERVAL) -> str | None:
        """Non-blocking single-byte read via the raw file descriptor.

        Uses *os.read* on the bare fd to avoid Python's TextIOWrapper
        buffering, which can swallow escape-sequence bytes in raw mode.
        """
        r, _, _ = select.select([self._fd], [], [], timeout)
        if r:
            return os.read(self._fd, 1).decode('latin-1')
        return None

    def flush_input(self) -> None:
        while self.poll_key(timeout=0):
            pass

    @property
    def was_resized(self) -> bool:
        r = self._resized
        self._resized = False
        return r
