# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
WorkloadSharedMemoryReader: Worker-side reader for workload shared memory.
"""

import time
from multiprocessing import shared_memory
from typing import Any

from motor.common.resources.instance import PDRole
from motor.common.resources.endpoint import Workload
from motor.common.logger import get_logger
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    MAGIC,
    HEADER_SIZE,
    ENTRY_SIZE,
    HEARTBEAT_STALE_SEC,
    unpack_header,
    unpack_entry,
    ROLE_PREFILL,
    ROLE_DECODE,
    ROLE_HYBRID,
    ROLE_ENCODE,
)

logger = get_logger(__name__)

STABLE_SNAPSHOT_READ_ATTEMPTS = 3


def _shm_role_to_pdrole(role: int) -> PDRole:
    """Map workload_shm layout role byte to PDRole."""
    if role == ROLE_ENCODE:
        return PDRole.ROLE_E
    if role == ROLE_PREFILL:
        return PDRole.ROLE_P
    if role == ROLE_DECODE:
        return PDRole.ROLE_D
    return PDRole.ROLE_U


class WorkloadSharedMemoryReader:
    """
    Reads workload data from shared memory. Used by Worker process.
    """

    def __init__(self, shm_name: str):
        self._shm_name = shm_name
        self._shm: shared_memory.SharedMemory | None = None
        self._buf: memoryview | None = None
        self._last_sequence: int | None = None
        self._last_heartbeat_value: int = 0
        self._last_heartbeat_time: float = 0.0

    @property
    def last_sequence(self) -> int | None:
        """Last stable workload sequence read from shared memory."""
        return self._last_sequence

    def attach(self) -> None:
        """Attach to existing shared memory."""
        self._shm = shared_memory.SharedMemory(name=self._shm_name, create=False)
        self._buf = memoryview(self._shm.buf)

    def detach(self) -> None:
        """Detach from shared memory. Release buffer before closing shm to avoid BufferError (exported pointers)."""
        if self._shm:
            # Release memoryview first so mmap has no exported pointers when we close.
            self._buf = None
            try:
                self._shm.close()
            except Exception as e:
                logger.warning("WorkloadSharedMemoryReader detach error: %s", e)
            self._shm = None

    def read_and_patch_cache(self, cache: Any) -> tuple[int | None, bool]:
        """
        Read shared memory and patch cache workload.
        Returns (instance_version, heartbeat_stale).
        When heartbeat_stale is True, Scheduler likely restarted; caller should get_available_instances.
        """
        if not self._buf:
            return (None, False)
        try:
            snapshot = None
            for _ in range(STABLE_SNAPSHOT_READ_ATTEMPTS):
                header = unpack_header(self._buf)
                if not self._is_valid_header(header):
                    return (None, False)
                if header.sequence % 2 == 1:
                    continue

                entries = [unpack_entry(self._buf, slot) for slot in range(header.entry_count)]
                header_after = unpack_header(self._buf)
                if (
                    header_after.magic == MAGIC
                    and header_after.sequence == header.sequence
                    and header_after.sequence % 2 == 0
                    and header_after.entry_count == header.entry_count
                    and header_after.instance_version == header.instance_version
                ):
                    snapshot = (header_after, entries)
                    break
            if snapshot is None:
                return (None, False)
            header, entries = snapshot

            heartbeat_stale = self._update_heartbeat_and_check_stale(header)

            self._patch_entries(cache, entries)
            self._last_sequence = header.sequence
            return (header.instance_version, heartbeat_stale)
        except Exception as e:
            logger.debug("WorkloadSharedMemoryReader read error: %s", e)
            return (None, False)

    def _is_valid_header(self, header: Any) -> bool:
        """Validate shm header before reading entries."""
        if header.magic != MAGIC:
            return False
        if header.entry_count < 0 or header.entry_count > header.max_entries:
            logger.debug(
                "WorkloadSharedMemoryReader invalid entry_count=%s max_entries=%s",
                header.entry_count,
                header.max_entries,
            )
            return False
        required_size = HEADER_SIZE + header.entry_count * ENTRY_SIZE
        if not self._buf or required_size > len(self._buf):
            logger.debug(
                "WorkloadSharedMemoryReader buf too small need=%s len=%s",
                required_size,
                len(self._buf) if self._buf else 0,
            )
            return False
        return True

    def _update_heartbeat_and_check_stale(self, header: Any) -> bool:
        """Track heartbeat changes and return whether the writer appears stale."""
        now = time.time()
        if header.heartbeat_sequence != self._last_heartbeat_value:
            self._last_heartbeat_value = header.heartbeat_sequence
            self._last_heartbeat_time = now
        return (
            self._last_heartbeat_time > 0
            and (now - self._last_heartbeat_time) > HEARTBEAT_STALE_SEC
        )

    @staticmethod
    def _patch_entries(cache: Any, entries: list[Any]) -> None:
        """Patch workload cache from shm entries."""
        for entry in entries:
            pdrole = _shm_role_to_pdrole(entry.role)
            cache.patch_workload_from_shm(
                entry.instance_id,
                entry.endpoint_id,
                pdrole,
                entry.active_tokens,
                entry.active_kv_cache,
            )
