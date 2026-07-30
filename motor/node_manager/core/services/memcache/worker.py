# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Memcache LocalService worker entry point.

Launched by MemcacheService.pull() as a subprocess via
``sys.executable -m motor.node_manager.core.services.memcache.worker``.
"""

import os
import threading

from motor.common.logger import get_logger
from motor.common.utils.process_utils import set_process_title

logger = get_logger(__name__)


def main() -> None:
    """Run the memcache distributed object store and block forever."""
    set_process_title("LocalService")
    logger.info("LocalService process starting (PID: %s)", os.getpid())

    from memcache_hybrid import DistributedObjectStore

    DistributedObjectStore().init(0)
    threading.Event().wait()


if __name__ == "__main__":
    main()
