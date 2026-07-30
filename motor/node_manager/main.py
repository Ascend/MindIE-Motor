# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""NodeManager entry point — thin wrapper around :class:`NodeManager`."""

import sys

from motor.common.logger import get_logger, reconfigure_logging
from motor.common.utils.port_allocator import (
    apply_node_manager_ports,
    run_port_setup_or_exit,
)
from motor.common.utils.process_utils import set_process_title
from motor.config.node_manager import NodeManagerConfig
from motor.node_manager.node_manager import NodeManager

set_process_title("NodeManager")

logger = get_logger(__name__)


def main() -> int:
    config = NodeManagerConfig.from_json()
    reconfigure_logging(config.logging_config)
    run_port_setup_or_exit(apply_node_manager_ports, config)

    nm = NodeManager(config)
    return nm.run()


if __name__ == "__main__":
    exit_code = main()
    logger.info("exit_code: %s", exit_code)
    sys.exit(exit_code)
