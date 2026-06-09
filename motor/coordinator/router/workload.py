# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Compute workload_change by WorkloadAction (ALLOCATION / RELEASE_KV / RELEASE_TOKENS) and update RequestManager.
"""

from __future__ import annotations

from typing import Tuple

from motor.common.resources.endpoint import Workload, WorkloadAction
from motor.common.resources.instance import PDRole
from motor.common.logger import get_logger
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.domain import ScheduledResource
from motor.coordinator.domain.workload_calculator import calculate_demand_workload
from motor.coordinator.models.request import RequestInfo

logger = get_logger(__name__)


class WorkloadActionHandler:
    """
    Compute workload_change by WorkloadAction and update RequestManager state.
    Does not call Scheduler; caller (e.g. BaseRouter) calls scheduler.update_workload with workload_change.
    """

    def __init__(self, request_manager: RequestManager) -> None:
        self._request_manager = request_manager

    @staticmethod
    def _normalize_role(resource: ScheduledResource) -> PDRole | None:
        role_raw = resource.instance.role
        if role_raw is None:
            logger.debug(
                "resource.instance.role is None; instance_id=%s endpoint_id=%s",
                resource.instance.id,
                resource.endpoint.id,
            )
            return None
        try:
            role = PDRole(role_raw) if isinstance(role_raw, str) else role_raw
        except (ValueError, TypeError):
            logger.debug(
                "resource.instance.role invalid for PDRole: %r (type=%s), instance_id=%s",
                role_raw,
                type(role_raw).__name__,
                resource.instance.id,
            )
            return None
        if role is None or not isinstance(role, PDRole):
            logger.debug("role is None or not PDRole after normalize: %r", role)
            return None
        return role

    async def compute_and_update(
        self,
        resource: ScheduledResource,
        req_id: str,
        action: WorkloadAction,
        req_info: RequestInfo,
        attempt_seq: int | None = None,
    ) -> Tuple[Workload | None, PDRole | None]:
        """
        Get/compute workload_change from RequestManager by action, update RequestManager, return (change, role).
        If action is invalid or not computable (e.g. not allocated so cannot release), return (None, None).

        Returns:
            (workload_change, role) for caller to pass to scheduler.update_workload; (None, None) if no update.
        """
        if not (resource and isinstance(resource, ScheduledResource) and resource.instance and resource.endpoint):
            logger.warning("WorkloadActionHandler: resource is empty")
            return (None, None)

        role = self._normalize_role(resource)
        if role is None:
            return (None, None)

        request_mgr = self._request_manager
        workload_change: Workload | None = None

        if action == WorkloadAction.ALLOCATION:
            allocate_workload = calculate_demand_workload(role, req_info)
            if attempt_seq is None:
                added = await request_mgr.add_req_workload(req_id, role, allocate_workload)
            else:
                added = await request_mgr.add_req_attempt_workload(req_id, attempt_seq, role, allocate_workload)
            if not added:
                logger.debug(
                    "Request %s attempt %s already allocated for role %s, allocation ignored", req_id, attempt_seq, role
                )
                return (None, None)
            workload_change = allocate_workload

        elif action == WorkloadAction.RELEASE_KV:
            current_workload = (
                await request_mgr.get_req_workload(req_id, role)
                if attempt_seq is None
                else await request_mgr.get_req_attempt_workload(req_id, attempt_seq, role)
            )
            if not current_workload:
                logger.debug(
                    "Request %s attempt %s not allocated for role %s, KV release ignored", req_id, attempt_seq, role
                )
                return (None, None)
            workload_change = Workload(active_kv_cache=-current_workload.active_kv_cache)
            current_workload.active_kv_cache = 0
            if attempt_seq is None:
                await request_mgr.update_req_workload(req_id, role, current_workload)
            else:
                await request_mgr.update_req_attempt_workload(req_id, attempt_seq, role, current_workload)
            if current_workload.active_tokens <= 0:
                if attempt_seq is None:
                    await request_mgr.del_req_workload(req_id, role)
                else:
                    await request_mgr.del_req_attempt_workload(req_id, attempt_seq, role)

        elif action == WorkloadAction.RELEASE_TOKENS:
            current_workload = (
                await request_mgr.get_req_workload(req_id, role)
                if attempt_seq is None
                else await request_mgr.get_req_attempt_workload(req_id, attempt_seq, role)
            )
            if not current_workload:
                logger.debug(
                    "Request %s attempt %s not allocated for role %s, tokens release ignored", req_id, attempt_seq, role
                )
                return (None, None)
            workload_change = Workload(active_tokens=-current_workload.active_tokens)
            current_workload.active_tokens = 0
            if attempt_seq is None:
                await request_mgr.update_req_workload(req_id, role, current_workload)
            else:
                await request_mgr.update_req_attempt_workload(req_id, attempt_seq, role, current_workload)
            if current_workload.active_kv_cache <= 0:
                if attempt_seq is None:
                    await request_mgr.del_req_workload(req_id, role)
                else:
                    await request_mgr.del_req_attempt_workload(req_id, attempt_seq, role)

        else:
            logger.warning("Unknown workload action: %s", action)
            return (None, None)

        return (workload_change, role)
