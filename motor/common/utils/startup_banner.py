# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Startup ASCII banner for MindIE-Motor components."""

from __future__ import annotations

import logging
import os

from motor import __version__ as _MOTOR_VERSION

_BANNER_TEMPLATE = """\

   ███╗   ███╗  ██████╗  ████████╗  ██████╗  ██████╗
   ████╗ ████║ ██╔═══██╗ ╚══██╔══╝ ██╔═══██╗ ██╔══██╗
   ██╔████╔██║ ██║   ██║    ██║    ██║   ██║ ██████╔╝
   ██║╚██╔╝██║ ██║   ██║    ██║    ██║   ██║ ██╔══██╗
   ██║ ╚═╝ ██║ ╚██████╔╝    ██║    ╚██████╔╝ ██║  ██║
   ╚═╝     ╚═╝  ╚═════╝     ╚═╝     ╚═════╝  ╚═╝  ╚═╝
   MindIE-Motor - v{version} - {role}
"""


def _logo_disabled() -> bool:
    """Return True when MOTOR_DISABLE_LOG_LOGO is set (aligned with vLLM)."""
    return bool(int(os.getenv("MOTOR_DISABLE_LOG_LOGO", "0")))


def _format_role(role: str) -> str:
    if role.startswith("node_manager"):
        return "NodeManager" + role[len("node_manager") :]
    if role.startswith("NodeManager"):
        return role
    return role[:1].upper() + role[1:] if role else role


def render_startup_banner(role: str, version: str | None = None) -> str:
    return _BANNER_TEMPLATE.format(
        version=version if version is not None else _MOTOR_VERSION,
        role=_format_role(role),
    )


def log_startup_banner(logger: logging.Logger, role: str, version: str | None = None) -> None:
    ver = version if version is not None else _MOTOR_VERSION
    role_label = _format_role(role)
    if _logo_disabled():
        logger.info("MindIE-Motor version %s, role %s", ver, role_label)
        return
    logger.info("%s", render_startup_banner(role, version=ver))
