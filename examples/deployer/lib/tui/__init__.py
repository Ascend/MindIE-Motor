# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""
Interactive terminal UI for the MindIE Motor deployer.

Provides a post-deployment interactive session with:
- Main menu with real-time pod startup progress bars
- Log collection toggle with confirmation when already running
- Instant key-press feedback via a status line below the box
- Keyboard-driven navigation (arrow keys and vim-style h/j/k/l)
"""

from .session import DeployInteractiveSession


def run_interactive_session(
    namespace: str, pod_cnt: int, user_config: dict, log_running: bool = False, deployed: bool = True
) -> None:
    """Launch the interactive post-deployment TUI."""
    session = DeployInteractiveSession(namespace, pod_cnt, user_config, log_running=log_running, deployed=deployed)
    session.run()


__all__ = ["run_interactive_session", "DeployInteractiveSession"]
