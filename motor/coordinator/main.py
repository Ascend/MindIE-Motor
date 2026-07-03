# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import sys
import traceback

from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.daemon.coordinator_daemon import CoordinatorDaemon
from motor.common.utils.config_runtime import log_configuration_summary
from motor.common.utils.port_allocator import apply_coordinator_ports, run_port_setup_or_exit
from motor.common.logger import get_logger, reconfigure_logging

logger = get_logger(__name__)


async def main() -> None:
    try:
        logger.info("Starting Motor Coordinator Daemon...")

        config = CoordinatorConfig.from_json()
        reconfigure_logging(config.logging_config)
        run_port_setup_or_exit(apply_coordinator_ports, config)
        if config.config_path:
            logger.info("Loaded configuration from: %s", config.config_path)
        else:
            logger.info("Using default configuration (no config file specified)")

        log_configuration_summary(config)

        daemon = CoordinatorDaemon(config)
        await daemon.run()

    except KeyboardInterrupt:
        logger.info("Received stop signal")
    except asyncio.CancelledError:
        logger.info("Server task cancelled")
    except Exception as e:
        logger.error("Server startup failed: %s", e)
        raise
    finally:
        logger.info("Coordinator daemon shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, server stopped")
    except asyncio.CancelledError:
        logger.info("Server task cancelled")
    except Exception as e:
        logger.error("Startup failed: %s", e)
        logger.error("Traceback: %s", traceback.format_exc())
        sys.exit(1)
