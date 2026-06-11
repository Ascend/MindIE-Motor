# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Scheduler client connection lifecycle.
Creates, connects, disconnects SchedulerClient for ManagementServer/InferenceServer and tests.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from motor.common.logger import get_logger
from motor.config.coordinator import CoordinatorConfig, DEFAULT_SCHEDULER_PROCESS_CONFIG
from motor.coordinator.scheduler.runtime.scheduler_client import (
    AsyncSchedulerClient as SchedulerClient,
    SchedulerClientConfig,
)

logger = get_logger(__name__)

_CONNECT_MAX_RETRIES = 3
_CONNECT_RETRY_SLEEP = 1.0


class SchedulerConnectionManager:
    """
    Manages SchedulerClient creation and connect/disconnect lifecycle.
    """

    def __init__(
        self,
        client: Any | None = None,
        client_config: Any | None = None,
    ) -> None:
        self._client = client
        self._client_config = client_config
        self._connected = False  # True only after connect(); False after disconnect(); disconnect is idempotent

    @classmethod
    def from_config(
        cls,
        coordinator_config: CoordinatorConfig,
        on_instance_refreshed: Callable[[list[tuple[str, str]]], Awaitable[None]] | None = None,
    ) -> SchedulerConnectionManager:
        """
        Build SchedulerClient from CoordinatorConfig and return Manager.
        Same construction logic as used by ManagementServer/InferenceServer.
        """
        scheduler_config = DEFAULT_SCHEDULER_PROCESS_CONFIG
        inference_workers_config = coordinator_config.inference_workers_config
        scheduler_type = coordinator_config.scheduler_config.scheduler_type.value

        if coordinator_config.worker_index is not None:
            client_index = coordinator_config.worker_index
            client_count = inference_workers_config.num_workers
        else:
            client_index = 0
            client_count = 1

        client_config = SchedulerClientConfig(
            scheduler_address=scheduler_config.frontend_address,
            instance_pub_address=scheduler_config.instance_pub_address,
            timeout=scheduler_config.timeout,
            reconnect_interval=scheduler_config.reconnect_interval,
            scheduler_type=scheduler_type,
            client_index=client_index,
            client_count=client_count,
            endpoint_instance_score_weight=(coordinator_config.scheduler_config.endpoint_instance_score_weight),
            kv_affinity_mode=(coordinator_config.scheduler_config.kv_affinity_mode),
            kv_affinity_load_weight=(coordinator_config.scheduler_config.kv_affinity_load_weight),
            kv_affinity_overlap_credit=(coordinator_config.scheduler_config.kv_affinity_overlap_credit),
            kv_affinity_prefill_load_scale=(coordinator_config.scheduler_config.kv_affinity_prefill_load_scale),
            kv_affinity_load_gate_topn=(coordinator_config.scheduler_config.kv_affinity_load_gate_topn),
            tls_config=coordinator_config.infer_tls_config,
            on_instance_refreshed=on_instance_refreshed,
        )
        client = SchedulerClient(client_config)
        return cls(client=client, client_config=scheduler_config)

    async def connect(self) -> None:
        """Connect Scheduler client (with retries; no exception on failure so service can keep starting)."""
        if not self._client or not self._client_config:
            return
        for i in range(_CONNECT_MAX_RETRIES):
            try:
                connected = await self._client.connect()
                if connected:
                    self._connected = True
                    logger.info(
                        "Scheduler client connected to %s",
                        self._client_config.frontend_address,
                    )
                    return
                logger.warning(
                    "Failed to connect to scheduler process (attempt %d/%d)",
                    i + 1,
                    _CONNECT_MAX_RETRIES,
                )
            except Exception as e:
                logger.error(
                    "Error connecting to scheduler process (attempt %d/%d): %s",
                    i + 1,
                    _CONNECT_MAX_RETRIES,
                    e,
                )
            if i < _CONNECT_MAX_RETRIES - 1:
                await asyncio.sleep(_CONNECT_RETRY_SLEEP)
        logger.error("Failed to connect to scheduler process after retries")

    async def ensure_connected(self) -> None:
        if self._connected:
            return
        await self.connect()

    async def disconnect(self) -> None:
        """Disconnect Scheduler client (idempotent: only first call does work)."""
        if not self._client or not self._connected:
            return
        try:
            await self._client.disconnect()
            logger.info("Scheduler client disconnected")
        except Exception as e:
            logger.warning("Error disconnecting scheduler client: %s", e)
        finally:
            self._connected = False

    def get_client(self):
        """Return current SchedulerClient when connected; None otherwise."""
        return self._client if self._connected else None
