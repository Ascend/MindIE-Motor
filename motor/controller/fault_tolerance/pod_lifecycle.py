# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Stop fully retired Pods so MindCluster can recycle them."""

from typing import Any

from motor.common.logger import get_logger
from motor.controller.api_client import NodeManagerApiClient

logger = get_logger(__name__)


class PodLifecycle:
    """Stop selected Motor Pods through the established NodeManager contract."""

    @staticmethod
    def stop_recyclable_pods(
        instance: Any,
        pod_ips: list[str],
    ) -> None:
        """Stop selected NodeManagers; MindCluster owns subsequent Pod recycling."""
        targets = sorted(set(pod_ips))
        node_managers = {node_mgr.pod_ip: node_mgr for node_mgr in instance.get_node_managers()}

        for pod_ip in targets:
            node_mgr = node_managers.get(pod_ip)
            if node_mgr is None:
                logger.error(
                    "Cannot stop recyclable Pod %s: NodeManager mapping is unavailable",
                    pod_ip,
                )
                continue
            try:
                if not NodeManagerApiClient.stop(node_mgr):
                    logger.error("Failed to stop Pod %s for MindCluster recycling", pod_ip)
            except Exception as e:
                logger.error("Failed to stop recyclable Pod %s: %s", pod_ip, e)
