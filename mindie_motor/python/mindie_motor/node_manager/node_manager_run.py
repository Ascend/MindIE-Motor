#!/usr/bin/env python3
# coding=utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import sys
import threading

from node_manager.daemon_manager.llm_daemon_starter import llm_daemon_manager
from node_manager.framework.server import app as http_server
from node_manager.core.heartbeat_mng import heartbeat_mng
from node_manager.core.runtime_param_checker import runtime_param_checker
from node_manager.common.logging import Log


def main():
    logger = Log(__name__).getlog()

    def run_http_server():
        try:
            http_server.run()
        except Exception as e:
            logger.error(f"HTTP Server error: {e}", exc_info=True)

    def run_heartbeat_manager():
        try:
            heartbeat_mng.run()
        except Exception as e:
            logger.error(f"Heartbeat Manager error: {e}", exc_info=True)

    def run_runtime_param_checker():
        try:
            runtime_param_checker.run()
        except Exception as e:
            logger.error(f"Runtime Param Checker error: {e}", exc_info=True)

    # Create and start threads
    http_thread = threading.Thread(target=run_http_server, daemon=False)
    heartbeat_thread = threading.Thread(target=run_heartbeat_manager, daemon=False)
    runtime_param_thread = threading.Thread(target=run_runtime_param_checker, daemon=False)
    http_thread.start()
    heartbeat_thread.start()
    runtime_param_thread.start()

    try:
        result = llm_daemon_manager.run()
        return result if isinstance(result, int) else 0
    finally:
        # Shutdown services independently to avoid cascading failures
        try:
            http_server.shutdown()
        except Exception as e:
            logger.error(f"HTTP Server shutdown error: {e}", exc_info=True)

        try:
            heartbeat_mng.stop()
        except Exception as e:
            logger.error(f"Heartbeat Manager stop error: {e}", exc_info=True)

        try:
            runtime_param_checker.stop()
        except Exception as e:
            logger.error(f"Runtime Param Checker stop error: {e}", exc_info=True)

        # Wait for threads to finish
        for thread in [http_thread, heartbeat_thread, runtime_param_thread]:
            if thread and thread.is_alive():
                thread.join(timeout=2.0)

        logger.info("node_manager application shutdown complete")


if __name__ == "__main__":
    sys.exit(main())
