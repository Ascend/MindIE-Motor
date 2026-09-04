# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import threading
import time
import uuid
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.models.request import RequestInfo
from motor.common.resources.endpoint import Workload
from motor.common.resources.instance import PDRole
from motor.common.logger import get_logger

logger = get_logger(__name__)


class RequestManager:
    """
    Request/workload state. Hot-path methods use asyncio.Lock to avoid blocking the event loop.
    """

    def __init__(self, config: CoordinatorConfig | None = None):
        if config is None:
            config = CoordinatorConfig()
        self._rate_limit_config = config.rate_limit_config
        self._config_lock = threading.RLock()

        # Counter and req/workload dicts: asyncio.Lock so hot path does not block event loop
        self._counter = 0
        self._last_timestamp = 0
        self._lock = asyncio.Lock()

        self._req_info_dict: dict[str, RequestInfo] = {}

        # Request workload dictionary.
        # Legacy routers use (req_id, role). Unified P/D dispatch uses
        # (root_request_id, attempt_seq, role) so late cleanup from an older
        # attempt cannot release a newer attempt's allocation.
        self._req_workload_dict: dict[tuple, Workload] = {}
        # Same keys as _req_workload_dict; value is (instance_id, endpoint_id) for residual reclaim.
        self._req_workload_owner: dict[tuple, tuple[int, int]] = {}
        logger.info("RequestManager initialized")

    async def generate_request_id(self) -> str:
        """
        Generate globally unique request ID (async, does not block event loop).
        Returns: Pure ID string in format: timestamp(16 digits) + counter(4 digits) + random(8 chars)
        """
        try:
            async with self._lock:
                current_timestamp = int(time.time() * 1000000)
                if current_timestamp == self._last_timestamp:
                    self._counter += 1
                else:
                    self._counter = 0
                    self._last_timestamp = current_timestamp
                counter_part = f"{self._counter:04d}"
            random_suffix = uuid.uuid4().hex[:8]
            request_id = f"{current_timestamp}{counter_part}{random_suffix}"
            logger.info("Generated request ID: %s", request_id)
            return request_id
        except Exception as e:
            logger.error("Failed to generate request ID: %s", e, exc_info=True)
            return uuid.uuid4().hex

    async def get_req_info(self, req_id: str) -> RequestInfo | None:
        """Get request info by req_id (async, does not block event loop)."""
        async with self._lock:
            return self._req_info_dict.get(req_id)

    async def add_req_info(self, req_info: RequestInfo) -> bool:
        try:
            async with self._lock:
                if req_info.req_id in self._req_info_dict:
                    logger.debug("Request ID %s already exists", req_info.req_id)
                    return False
                self._req_info_dict[req_info.req_id] = req_info
            logger.debug("Added request info for ID: %s", req_info.req_id)
            return True
        except Exception as e:
            logger.error("Failed to add request info for ID %s: %s", req_info.req_id, e)
            return False

    async def del_req_info(self, req_id: str) -> bool:
        try:
            async with self._lock:
                if req_id not in self._req_info_dict:
                    logger.debug("Request ID %s not found for deletion", req_id)
                    return False
                del self._req_info_dict[req_id]
                keys_to_delete = [k for k in self._req_workload_dict if k[0] == req_id]
                for k in keys_to_delete:
                    residual = self._req_workload_dict.pop(k)
                    owner = self._req_workload_owner.pop(k, None)
                    logger.error(
                        "Orphan workload record dropped at request deletion; ledger tokens are leaked: "
                        "req_id=%s key=%s active_tokens=%s owner=%s",
                        req_id,
                        k,
                        residual.active_tokens,
                        owner,
                    )
            logger.debug("Deleted request info and workloads for ID: %s", req_id)
            return True
        except Exception as e:
            logger.error("Failed to delete request info for ID %s: %s", req_id, e)
            return False

    # ==================== Workload Management (async, hot path) ====================

    @staticmethod
    def _workload_key(req_id: str, role: PDRole, attempt_seq: int | None = None) -> tuple:
        if attempt_seq is None:
            return (req_id, role)
        return (req_id, attempt_seq, role)

    async def add_req_workload(
        self,
        req_id: str,
        role: PDRole,
        workload: Workload,
        *,
        instance_id: int | None = None,
        endpoint_id: int | None = None,
    ) -> bool:
        """Add workload record for a request and role (async, does not block event loop)."""
        return await self.add_req_attempt_workload(
            req_id,
            None,
            role,
            workload,
            instance_id=instance_id,
            endpoint_id=endpoint_id,
        )

    async def add_req_attempt_workload(
        self,
        req_id: str,
        attempt_seq: int | None,
        role: PDRole,
        workload: Workload,
        *,
        instance_id: int | None = None,
        endpoint_id: int | None = None,
    ) -> bool:
        """Add workload record for a request/attempt/role."""
        try:
            async with self._lock:
                key = self._workload_key(req_id, role, attempt_seq)
                if key in self._req_workload_dict:
                    logger.debug("Workload for key %s already exists", key)
                    return False
                self._req_workload_dict[key] = workload
                if instance_id is not None and endpoint_id is not None:
                    self._req_workload_owner[key] = (instance_id, endpoint_id)
            logger.debug("Added workload for key %s", key)
            return True
        except Exception as e:
            logger.error("Failed to add workload for request %s, role %s: %s", req_id, role, e)
            return False

    async def get_req_workload(self, req_id: str, role: PDRole) -> Workload | None:
        return await self.get_req_attempt_workload(req_id, None, role)

    async def get_req_attempt_workload(self, req_id: str, attempt_seq: int | None, role: PDRole) -> Workload | None:
        async with self._lock:
            return self._req_workload_dict.get(self._workload_key(req_id, role, attempt_seq))

    async def update_req_workload(self, req_id: str, role: PDRole, workload: Workload) -> bool:
        return await self.update_req_attempt_workload(req_id, None, role, workload)

    async def update_req_attempt_workload(
        self,
        req_id: str,
        attempt_seq: int | None,
        role: PDRole,
        workload: Workload,
    ) -> bool:
        try:
            async with self._lock:
                key = self._workload_key(req_id, role, attempt_seq)
                if key not in self._req_workload_dict:
                    logger.debug("Workload for key %s not found", key)
                    return False
                self._req_workload_dict[key] = workload
            logger.debug("Updated workload for key %s", key)
            return True
        except Exception as e:
            logger.error("Failed to update workload for request %s, role %s: %s", req_id, role, e)
            return False

    async def del_req_workload(self, req_id: str, role: PDRole) -> bool:
        return await self.del_req_attempt_workload(req_id, None, role)

    async def del_req_attempt_workload(self, req_id: str, attempt_seq: int | None, role: PDRole) -> bool:
        try:
            async with self._lock:
                key = self._workload_key(req_id, role, attempt_seq)
                if key not in self._req_workload_dict:
                    logger.debug("Workload for key %s not found", key)
                    return False
                del self._req_workload_dict[key]
                self._req_workload_owner.pop(key, None)
            logger.debug("Deleted workload for key %s", key)
            return True
        except Exception as e:
            logger.error("Failed to delete workload for request %s, role %s: %s", req_id, role, e)
            return False

    async def pop_residual_workloads(self, req_id: str) -> list[tuple[tuple, Workload, tuple[int, int] | None]]:
        """Atomically pop workload records that survived the request lifecycle.

        A residual record means a scheduler ledger commit (CAS add) was never
        released; the caller is responsible for releasing it back to the ledger.
        """
        async with self._lock:
            keys = [k for k in self._req_workload_dict if k[0] == req_id]
            return [(k, self._req_workload_dict.pop(k), self._req_workload_owner.pop(k, None)) for k in keys]

    def update_config(self, config: CoordinatorConfig) -> None:
        """Update configuration for the request manager"""
        with self._config_lock:
            self._rate_limit_config = config.rate_limit_config
        logger.info("RequestManager configuration updated")
