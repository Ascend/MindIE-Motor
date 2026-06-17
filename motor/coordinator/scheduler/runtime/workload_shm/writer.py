# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
WorkloadSharedMemoryWriter: Scheduler-side writer for workload shared memory.
"""

import struct
from multiprocessing import shared_memory

from motor.common.resources.instance import PDRole
from motor.common.logger import get_logger
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    MAGIC,
    SCHEMA_VERSION,
    ROLE_PREFILL,
    ROLE_DECODE,
    ROLE_HYBRID,
    ROLE_ENCODE,
    HEADER_SIZE,
    ENTRY_SIZE,
    HEARTBEAT_OFFSET,
    DEFAULT_WORKLOAD_SHM_MAX_ENTRIES,
    pack_header,
    pack_entry,
    total_size,
    WorkloadShmHeader,
    WorkloadShmEntry,
)

logger = get_logger(__name__)


def _pdrole_to_shm_role(role: PDRole) -> int:
    """Map PDRole to workload_shm layout role byte."""
    if role == PDRole.ROLE_E:
        return ROLE_ENCODE
    if role == PDRole.ROLE_P:
        return ROLE_PREFILL
    if role == PDRole.ROLE_D:
        return ROLE_DECODE
    return ROLE_HYBRID


def _collect_entries_and_slot_map(instance_manager: InstanceManager, max_entries: int):
    """
    Collect (instance_id, endpoint_id, role, workload) from all pools and build slot_map.
    Returns (entries list, slot_map dict).
    """
    entries: list[tuple[int, int, int, float, float]] = []
    slot_map: dict[tuple[int, int], int] = {}

    for role in (PDRole.ROLE_E, PDRole.ROLE_P, PDRole.ROLE_D, PDRole.ROLE_U):
        instances = instance_manager.get_available_instances(role)
        shm_role = _pdrole_to_shm_role(role)
        for instance in instances.values():
            for pod_eps in (instance.endpoints or {}).values():
                for ep in (pod_eps or {}).values():
                    if len(entries) >= max_entries:
                        logger.warning(
                            "Workload shm max_entries=%d exceeded, truncating",
                            max_entries,
                        )
                        return entries, slot_map
                    slot = len(entries)
                    slot_map[(instance.id, ep.id)] = slot
                    entries.append(
                        (
                            instance.id,
                            ep.id,
                            shm_role,
                            ep.workload.active_tokens,
                            ep.workload.active_kv_cache,
                        )
                    )
    return entries, slot_map


class WorkloadSharedMemoryWriter:
    """
    Writes workload data to shared memory. Used by Scheduler process.
    Full snapshot on instance change, incremental on workload change.
    """

    def __init__(
        self,
        shm: shared_memory.SharedMemory,
        instance_manager: "InstanceManager",
        max_entries: int = DEFAULT_WORKLOAD_SHM_MAX_ENTRIES,
    ):
        self._shm = shm
        self._im = instance_manager
        self._max_entries = max_entries
        self._buf = memoryview(shm.buf)
        self._slot_map: dict[tuple[int, int], int] = {}
        self._sequence = 0
        self._entry_count = 0
        self._instance_version = 0
        self._heartbeat_sequence = 0

    @property
    def shm_name(self) -> str:
        """Public name of the shared memory block for readers (e.g. Inference workers)."""
        return self._shm.name if self._shm else ""

    @property
    def instance_version(self) -> int:
        """Current instance list version (bumped on write_snapshot). Used for PUB push dedup."""
        return self._instance_version

    @property
    def sequence(self) -> int:
        """Current workload sequence. Even values are stable; odd values mean write in progress."""
        return self._sequence

    def release(self) -> None:
        """Release buffer reference before owner closes SharedMemory. Prevents BufferError (exported pointers)."""
        self._buf = None
        self._shm = None

    def write_heartbeat(self) -> None:
        """Write only heartbeat_sequence (called periodically by Scheduler). Infer treats no-change as stale."""
        self._heartbeat_sequence = (self._heartbeat_sequence + 1) % (1 << 64)
        self._buf[HEARTBEAT_OFFSET: HEARTBEAT_OFFSET + 8] = struct.pack(
            "<Q", self._heartbeat_sequence
        )

    def write_snapshot(self) -> None:
        """Full snapshot: rebuild slot_map and write all entries. Bumps instance_version."""
        entries, self._slot_map = _collect_entries_and_slot_map(
            self._im, self._max_entries
        )
        self._entry_count = len(entries)
        self._begin_write()
        for slot, (iid, eid, role, tokens, kv) in enumerate(entries):
            self._write_entry_at_slot(
                slot,
                WorkloadShmEntry(
                    instance_id=iid,
                    endpoint_id=eid,
                    role=role,
                    active_tokens=tokens,
                    active_kv_cache=kv,
                ),
            )
        self._instance_version += 1
        self._end_write()

    async def write_single_entry(self, instance_id: int, endpoint_id: int) -> None:
        """Incremental write: only update the changed slot (~1-5 µs)."""
        slot = self._slot_map.get((instance_id, endpoint_id))
        if slot is None:
            self.write_snapshot()
            return
        role, workload = await self._im.get_endpoint_workload(instance_id, endpoint_id)
        if role is None or workload is None:
            return
        shm_role = _pdrole_to_shm_role(role)
        self._begin_write()
        self._write_entry_at_slot(
            slot,
            WorkloadShmEntry(
                instance_id=instance_id,
                endpoint_id=endpoint_id,
                role=shm_role,
                active_tokens=workload.active_tokens,
                active_kv_cache=workload.active_kv_cache,
            ),
        )
        self._end_write()

    def _begin_write(self) -> None:
        """Mark workload shm as being updated (odd sequence)."""
        if self._sequence % 2 == 0:
            self._sequence += 1
        else:
            self._sequence += 2
        self._write_header()

    def _end_write(self) -> None:
        """Mark workload shm as stable after update (even sequence)."""
        if self._sequence % 2 == 1:
            self._sequence += 1
        else:
            self._sequence += 2
        self._write_header()

    def _write_header(self) -> None:
        """Write header to shared memory. Preserves heartbeat if heartbeat_loop wrote a newer value."""
        try:
            current_in_buf = struct.unpack(
                "<Q",
                bytes(self._buf[HEARTBEAT_OFFSET: HEARTBEAT_OFFSET + 8]),
            )[0]
            self._heartbeat_sequence = max(self._heartbeat_sequence, current_in_buf)
        except (ValueError, IndexError):
            pass
        header = pack_header(
            WorkloadShmHeader(
                magic=MAGIC,
                schema_version=SCHEMA_VERSION,
                sequence=self._sequence,
                entry_count=self._entry_count,
                max_entries=self._max_entries,
                instance_version=self._instance_version,
                heartbeat_sequence=self._heartbeat_sequence,
            )
        )
        self._buf[:HEADER_SIZE] = header

    def _write_entry_at_slot(self, slot: int, entry: WorkloadShmEntry) -> None:
        """Write single entry at slot offset."""
        offset = HEADER_SIZE + slot * ENTRY_SIZE
        data = pack_entry(entry)
        self._buf[offset: offset + ENTRY_SIZE] = data
