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
WorkloadSharedMemoryReader: Worker-side reader for schema-4 workload shared memory.

Attaches via the Rust .so (not CPython SharedMemory) so resource_tracker cannot unlink
a live segment. Token values are atomic-loaded every scoring pass; membership seqlock
only guards torn snapshots.
"""

import time
from typing import Any

from motor.common.resources.instance import PDRole
from motor.common.logger import get_logger
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    HEARTBEAT_STALE_SEC,
    SCHEMA_VERSION,
    FLAG_VALID,
    ROLE_PREFILL,
    ROLE_DECODE,
    ROLE_ENCODE,
)
from motor.coordinator.scheduler.runtime.workload_shm.native import (
    NativeWorkloadShmError,
    WorkloadShm,
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
    """Reads schema-4 workload data from shared memory. Used by Worker process."""

    def __init__(self, shm_name: str):
        self._shm_name = shm_name
        self._native: WorkloadShm | None = None
        self._last_heartbeat_value: int = 0
        self._last_heartbeat_time: float = 0.0
        self._meta: dict[tuple[int, int], dict[str, Any]] = {}

    @property
    def native(self) -> WorkloadShm | None:
        """Attached native handle, or None before attach()."""
        return self._native

    def entry_meta(self, instance_id: int, endpoint_id: int) -> dict[str, Any] | None:
        """Last loaded schema-4 slot for (instance_id, endpoint_id), or None."""
        return self._meta.get((instance_id, endpoint_id))

    def attach(self) -> None:
        """Attach to existing shared memory via the Rust .so (no CPython resource_tracker)."""
        self._native = WorkloadShm.attach(self._shm_name)

    def detach(self) -> None:
        """Detach from shared memory without unlinking (Mgmt owns unlink)."""
        if self._native is not None:
            try:
                self._native.close(unlink=False)
            except Exception as e:
                logger.warning("WorkloadSharedMemoryReader detach error: %s", e)
            self._native = None
        self._meta = {}

    def read_and_patch_cache(self, cache: Any, role: PDRole | None = None) -> tuple[int | None, bool]:
        """
        Atomic-load tokens and patch cache workload.

        Returns (instance_version, heartbeat_stale). Always loads tokens (schema 4 multi-writer);
        membership seqlock retries reject a torn snapshot.

        Heartbeat is checked before the stable-snapshot loop: it is bumped by its own atomic store
        outside the seqlock, so it stays readable even while ``sequence`` is stuck odd.
        """
        if self._native is None:
            return (None, False)
        try:
            header = self._native.read_header()
            if not self._is_valid_header(header):
                return (None, False)
            heartbeat_stale = self._update_heartbeat_and_check_stale(int(header["heartbeat"]))
            snapshot = None
            for _ in range(STABLE_SNAPSHOT_READ_ATTEMPTS):
                header = self._native.read_header()
                if not self._is_valid_header(header):
                    return (None, heartbeat_stale)
                sequence = int(header["sequence"])
                if sequence % 2 == 1:
                    continue
                entries = self._load_entries(int(header["entry_count"]))
                header_after = self._native.read_header()
                if (
                    int(header_after.get("schema_version", -1)) == SCHEMA_VERSION
                    and int(header_after["sequence"]) == sequence
                    and int(header_after["sequence"]) % 2 == 0
                    and int(header_after["entry_count"]) == int(header["entry_count"])
                    and int(header_after["instance_version"]) == int(header["instance_version"])
                ):
                    snapshot = (header_after, entries)
                    break
            if snapshot is None:
                return (None, heartbeat_stale)
            header, entries = snapshot
            self._patch_entries(cache, entries, role=role)
            return (int(header["instance_version"]), heartbeat_stale)
        except Exception as e:
            logger.debug("WorkloadSharedMemoryReader read error: %s", e)
            return (None, False)

    def _is_valid_header(self, header: dict[str, int]) -> bool:
        """Validate shm header before reading entries."""
        schema = int(header.get("schema_version", -1))
        if schema != SCHEMA_VERSION:
            logger.error(
                "WorkloadSharedMemoryReader schema mismatch: expect %s got %s, refusing read",
                SCHEMA_VERSION,
                schema,
            )
            return False
        entry_count = int(header.get("entry_count", -1))
        if entry_count < 0:
            return False
        return True

    def _load_entries(self, entry_count: int) -> list[dict[str, Any]]:
        if self._native is None:
            return []
        try:
            return self._native.load_entries(entry_count)
        except NativeWorkloadShmError:
            return []

    def _update_heartbeat_and_check_stale(self, heartbeat_sequence: int) -> bool:
        """Track heartbeat changes and return whether the writer appears stale."""
        now = time.time()
        if heartbeat_sequence != self._last_heartbeat_value:
            self._last_heartbeat_value = heartbeat_sequence
            self._last_heartbeat_time = now
        return self._last_heartbeat_time > 0 and (now - self._last_heartbeat_time) > HEARTBEAT_STALE_SEC

    def _patch_entries(self, cache: Any, entries: list[dict[str, Any]], *, role: PDRole | None = None) -> None:
        """Patch workload cache from shm entries and refresh local slot meta."""
        meta: dict[tuple[int, int], dict[str, Any]] = {}
        for entry in entries:
            if not (int(entry.get("flags", 0)) & FLAG_VALID):
                continue
            pdrole = _shm_role_to_pdrole(int(entry["role"]))
            pair = (int(entry["instance_id"]), int(entry["endpoint_id"]))
            meta[pair] = entry
            if role in {PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_U} and pdrole != role:
                continue
            cache.patch_workload_from_shm(
                pair[0],
                pair[1],
                pdrole,
                float(entry["active_tokens"]),
            )
        self._meta = meta
