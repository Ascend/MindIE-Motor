# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.


import multiprocessing

PROCESS_NAME_PREFIX = "MindIE-Motor"


def set_process_title(name: str, *, prefix: str = PROCESS_NAME_PREFIX) -> None:
    """Set OS process title (ps/top) with prefix; logging uses short name only."""
    os_title = f"{prefix}::{name}" if prefix else name
    try:
        multiprocessing.current_process().name = name
    except (AttributeError, TypeError, ValueError):
        pass

    try:
        import setproctitle
    except ImportError:
        return

    setproctitle.setproctitle(os_title)
