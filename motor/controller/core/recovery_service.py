# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unified instance recovery: logical separation then node-manager stop."""

from __future__ import annotations

from motor.common.logger import get_logger
from motor.controller.api_client import NodeManagerApiClient
from motor.controller.core.instance_manager import InstanceManager

logger = get_logger(__name__)


def terminate_instance_for_recovery(instance_id: int, reason: str) -> bool:
    """Isolate instance from scheduling, then request stop on all node managers.

    Used by manual terminate API, precision auto-recovery, and northbound (e.g. CCAE) callbacks.

    Returns:
        True if instance existed and stop was attempted for all node managers (all returned True).
        False if instance missing after separation or initially not found.
    """
    instance = InstanceManager().get_instance(instance_id)
    if instance is None:
        logger.error("Recovery: instance %s not found (reason=%s)", instance_id, reason)
        return False
    logger.warning("Recovery: separate_instance id=%s reason=%s", instance_id, reason)
    InstanceManager().separate_instance(instance_id)
    instance = InstanceManager().get_instance(instance_id)
    if instance is None:
        logger.error("Recovery: instance %s missing after separate_instance", instance_id)
        return False
    ok = True
    for node_mgr in instance.get_node_managers():
        ok = NodeManagerApiClient.stop(node_mgr) and ok
    return ok
