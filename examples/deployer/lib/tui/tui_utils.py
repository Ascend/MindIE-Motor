# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""TUI terminal and rendering helpers."""

from __future__ import annotations

import re
import shutil
import unicodedata

from lib import constant as C


def get_term_width(min_width: int | None = None, max_width: int | None = None) -> int:
    """Get terminal width, clamped to a usable range."""
    if min_width is None:
        min_width = C.MIN_BOX_WIDTH
    if max_width is None:
        max_width = C.MAX_BOX_WIDTH
    try:
        w = shutil.get_terminal_size().columns
        return max(min_width, min(w - 2, max_width))
    except Exception:
        return min_width


def visible_length(text: str) -> int:
    """Return display length of *text* accounting for ANSI escapes and wide chars."""
    clean = re.sub(r'\033\[[0-9;]*m', '', text)
    width = 0
    for ch in clean:
        ea = unicodedata.east_asian_width(ch)
        width += 2 if ea in ('W', 'F') else 1
    return width


def pad_right(text: str, width: int) -> str:
    """Pad *text* on the right with spaces to reach *width* display columns."""
    need = width - visible_length(text)
    return text + (' ' * max(0, need))


def _fmt_time(seconds: float) -> str:
    """Format seconds as MM:SS (tqdm-style)."""
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def format_progress_bar(
    percent: int,
    pod_label: str,
    bar_width: int = 30,
    line_cnt: int = 0,
    err_cnt: int = 0,
    elapsed: float = 0.0,
) -> str:
    """Return a single-line coloured progress bar string with stats."""
    percent = int(max(0, min(100, percent)))
    filled = int(bar_width * percent / 100)
    empty = bar_width - filled

    if percent >= 100:
        colour = C.Style.GREEN + C.Style.BOLD
    else:
        colour = C.Style.GREEN

    bar_str = f"{colour}{'█' * filled}{C.Style.RESET}{C.Style.DIM}{'░' * empty}{C.Style.RESET}"
    pct_str = f"{percent:3d}%"

    # elapsed / remaining (tqdm-style, fixed width for alignment)
    _TIME_WIDTH = 13  # [MM:SS<MM:SS]
    if elapsed > 0 and 0 < percent < 100:
        remaining = elapsed * (100 - percent) / percent
        time_str = f"[{_fmt_time(elapsed)}<{_fmt_time(remaining)}]"
    else:
        # percent is 0 or 100 — show elapsed only
        time_str = f"[{_fmt_time(elapsed)}]"
    time_str = f"{time_str:<{_TIME_WIDTH}}"

    if line_cnt >= 100000:
        line_info = f"Line:{line_cnt // 1000}k"
    elif line_cnt:
        line_info = f"Line:{line_cnt:<4d}"
    else:
        line_info = "Line:0   "
    line_info = f"{line_info:<9}"

    if err_cnt > 0:
        err_info = f"{C.Style.RED}Err:{err_cnt:<3d}{C.Style.RESET}"
    else:
        err_info = "Err:0  "

    return f"  {pod_label}  {bar_str}  {pct_str}  {time_str}  {line_info}  {err_info}"


def draw_box(
    title: str,
    body_lines: list[str],
    width: int,
    footer_lines: list[str] | None = None,
    double_border: bool = False,
) -> list[str]:
    """Build a bordered box as a list of strings (no trailing newlines)."""
    S = C.Style
    if double_border:
        h, v, tl, tr, bl, br, lt, rt = (
            S.DH,
            S.DV,
            S.DTL,
            S.DTR,
            S.DBL,
            S.DBR,
            S.DLT,
            S.DRT,
        )
    else:
        h, v, tl, tr, bl, br, lt, rt = (
            S.H,
            S.V,
            S.TL,
            S.TR,
            S.BL,
            S.BR,
            S.LT,
            S.RT,
        )

    inner = width - 2
    lines = []

    # --- top border ---
    if title:
        title_visible = visible_length(title)
        left_pad = max(0, (inner - title_visible - 2) // 2)
        right_pad = max(0, inner - title_visible - 2 - left_pad)
        top = f"{tl}{h * left_pad} {C.Style.BOLD}{C.Style.CYAN}{title}{C.Style.RESET} {h * right_pad}{tr}"
    else:
        top = f"{tl}{h * inner}{tr}"
    lines.append(top)

    # --- body ---
    for line in body_lines:
        lines.append(f"{v} {pad_right(line, inner - 2)} {v}")

    # --- separator / footer ---
    if footer_lines:
        lines.append(f"{lt}{h * inner}{rt}")
        for line in footer_lines:
            lines.append(f"{v} {pad_right(line, inner - 2)} {v}")

    # --- bottom border ---
    lines.append(f"{bl}{h * inner}{br}")
    return lines
