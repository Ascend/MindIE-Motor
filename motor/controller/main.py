# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Controller entry point — thin wrapper around :class:`Controller`."""

import argparse
import sys

from motor.common.logger import get_logger, reconfigure_logging
from motor.common.utils.port_allocator import (
    apply_controller_ports,
    run_port_setup_or_exit,
)
from motor.config.controller import ControllerConfig
from motor.controller.controller import Controller

logger = get_logger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(description="Motor Controller")
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help="Path to configuration file (default: auto-detect from environment)",
    )
    args = parser.parse_args()

    config = ControllerConfig.from_json(args.config)
    reconfigure_logging(config.logging_config)
    run_port_setup_or_exit(apply_controller_ports, config)

    controller = Controller(config)
    return controller.run()


if __name__ == "__main__":
    exit_code = main()
    logger.info("exit_code: %s", exit_code)
    sys.exit(exit_code)
