# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import logging
from pathlib import Path

_MOTOR_ROOT = Path(__file__).resolve().parent.parent.parent

LOCATION_FORMAT = "[%(name)s][%(fileinfo)s:%(lineno)d]"


class NewLineFormatter(logging.Formatter):
    """vLLM-aligned formatter: sets fileinfo and aligns multi-line messages."""

    def __init__(self, fmt, datefmt=None, style='%', *, use_relpath: bool = False):
        super().__init__(fmt=fmt, datefmt=datefmt, style=style)
        self.use_relpath = use_relpath

    def format(self, record: logging.LogRecord) -> str:
        record.fileinfo = self._fileinfo(record)
        msg = super().format(record)
        if record.message != "":
            parts = msg.split(record.message)
            msg = msg.replace("\n", "\r\n" + parts[0])
        return msg

    def _fileinfo(self, record: logging.LogRecord) -> str:
        if not self.use_relpath:
            return record.filename

        abs_path = getattr(record, "pathname", None)
        if abs_path:
            try:
                relpath = Path(abs_path).resolve().relative_to(_MOTOR_ROOT)
            except Exception:
                relpath = Path(record.filename)
        else:
            relpath = Path(record.filename)
        return _shrink_path(relpath)


def _shrink_path(relpath: Path) -> str:
    parts = list(relpath.parts)
    new_parts: list[str] = []
    if parts and parts[0] == "motor":
        parts = parts[1:]
    if parts:
        new_parts += parts[:1]
        parts = parts[1:]
    if len(parts) > 2:
        new_parts += ["..."] + parts[-2:]
    else:
        new_parts += parts
    return "/".join(new_parts)


class ColoredFormatter(NewLineFormatter):
    """ANSI colors aligned with vLLM ColoredFormatter."""

    COLORS = {
        "DEBUG": "\033[37m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    GREY = "\033[90m"
    RESET = "\033[0m"

    def __init__(self, fmt, datefmt=None, style='%', *, use_relpath: bool = False):
        if fmt:
            fmt = fmt.replace("%(asctime)s", f"{self.GREY}%(asctime)s{self.RESET}")
            fmt = fmt.replace(
                LOCATION_FORMAT,
                f"{self.GREY}{LOCATION_FORMAT}{self.RESET}",
            )
            fmt = fmt.replace(
                "[%(fileinfo)s:%(lineno)d]",
                f"{self.GREY}[%(fileinfo)s:%(lineno)d]{self.RESET}",
            )
        super().__init__(fmt, datefmt, style, use_relpath=use_relpath)

    def format(self, record: logging.LogRecord) -> str:
        orig_levelname = record.levelname
        if (color_code := self.COLORS.get(record.levelname)) is not None:
            record.levelname = f"{color_code}{record.levelname}{self.RESET}"
        msg = super().format(record)
        record.levelname = orig_levelname
        return msg
