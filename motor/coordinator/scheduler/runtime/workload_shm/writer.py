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
WorkloadSharedMemoryOwner: Mgmt-side schema-4 membership snapshot via the Rust .so.

Token allocate/release is done by Infer Workers with per-slot CAS; this owner only
creates the segment, snapshots membership (preserving in-flight tokens), heartbeats,
and sets BLOCKED flags for the circuit breaker.
"""

import os

from motor.common.resources.instance import PDRole
from motor.common.logger import get_logger
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    ROLE_PREFILL,
    ROLE_DECODE,
    ROLE_HYBRID,
    ROLE_ENCODE,
    FLAG_VALID,
    DEFAULT_WORKLOAD_SHM_MAX_ENTRIES,
)
from motor.coordinator.scheduler.runtime.workload_shm.native import NativeWorkloadShmError, WorkloadShm

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
    entries: list[tuple[int, int, int, float]] = []
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
                    tokens = ep.workload.active_tokens if ep.workload is not None else 0.0
                    entries.append((instance.id, ep.id, shm_role, tokens))
    return entries, slot_map


def _lowest_free_slot(used: set[int], max_entries: int) -> int | None:
    """Return the lowest unused slot index, or None if the table is full."""
    slot = 0
    while slot in used:
        slot += 1
    if slot >= max_entries:
        return None
    return slot


class WorkloadSharedMemoryOwner:
    """Mgmt-side schema-4 SHM owner. Membership snapshot + heartbeat + BLOCKED flags."""

    def __init__(
        self,
        instance_manager: "InstanceManager",
        max_entries: int = DEFAULT_WORKLOAD_SHM_MAX_ENTRIES,
        *,
        native: WorkloadShm | None = None,
        shm_name: str | None = None,
    ):
        self._im = instance_manager
        self._max_entries = max_entries
        self._owns_native = native is None
        name = shm_name or (native.name if native is not None and native.name else f"mindie_workload_{os.getpid()}")
        self._name = name
        self._native = native if native is not None else WorkloadShm.create_v4(name, max_entries)
        self._slot_map: dict[tuple[int, int], int] = {}
        self._generation: dict[tuple[int, int], int] = {}

    @property
    def shm_name(self) -> str:
        """Public name of the shared memory block for readers (e.g. Inference workers)."""
        return self._name

    @property
    def native(self) -> WorkloadShm:
        """Underlying schema-4 native handle (CAS / set_blocked)."""
        return self._native

    @property
    def instance_version(self) -> int:
        """Current instance list version (bumped on write_snapshot). Used for PUB push dedup."""
        try:
            return int(self._native.read_header().get("instance_version", 0))
        except (NativeWorkloadShmError, OSError, ValueError):
            return 0

    def release(self) -> None:
        """Close the native handle; unlinks when this owner created the segment."""
        if self._native is not None:
            try:
                self._native.close(unlink=self._owns_native)
            except Exception as e:
                logger.warning("WorkloadSharedMemoryOwner close error: %s", e)
            self._native = None

    def write_heartbeat(self) -> None:
        """Bump heartbeat so Infer can detect a dead control-plane writer."""
        if self._native is None:
            return
        self._native.heartbeat()

    def set_blocked(self, instance_id: int, blocked: bool) -> int:
        """Set/clear BLOCKED on all VALID slots of an instance. Returns slots touched."""
        if self._native is None:
            return 0
        return self._native.set_blocked(instance_id, blocked)

    def write_snapshot(self) -> None:
        """Full membership snapshot. Preserves in-flight tokens and bumps generation on reuse."""
        if self._native is None:
            return
        entries = _collect_entries_and_slot_map(self._im, self._max_entries)[0]
        existing = self._load_existing_pairs()
        live: dict[tuple[int, int], tuple[int, float]] = {}
        for iid, eid, role, im_tokens in entries:
            live[(iid, eid)] = (role, im_tokens)

        used: set[int] = set()
        assignments: dict[tuple[int, int], int] = {}
        for pair, slot in self._slot_map.items():
            if pair in live:
                assignments[pair] = slot
                used.add(slot)

        for pair in live:
            if pair in assignments:
                continue
            slot = _lowest_free_slot(used, self._max_entries)
            if slot is None:
                logger.warning(
                    "Workload shm max_entries=%d exceeded, truncating new endpoints",
                    self._max_entries,
                )
                continue
            used.add(slot)
            assignments[pair] = slot

        n = (max(used) + 1) if used else 0
        v4: list[tuple[int, int, int, int, int, float]] = [(0, 0, 0, 0, 0, 0.0)] * n
        for pair, slot in assignments.items():
            iid, eid = pair
            role, im_tokens = live[pair]
            if pair in existing:
                gen, flags = existing[pair]
                self._generation[pair] = gen
                # Tokens are Worker-owned; native write_entry_v4 leaves them in place.
                v4[slot] = (iid, eid, role, gen, flags | FLAG_VALID, 0.0)
            else:
                gen = self._generation.get(pair, -1) + 1
                self._generation[pair] = gen
                v4[slot] = (iid, eid, role, gen, FLAG_VALID, im_tokens)
        self._slot_map = assignments
        self._native.write_snapshot_v4(v4, bump_instance_version=True)

    def _load_existing_pairs(self) -> dict[tuple[int, int], tuple[int, int]]:
        """Read current VALID slots: (iid, eid) -> (generation, flags)."""
        out: dict[tuple[int, int], tuple[int, int]] = {}
        if self._native is None:
            return out
        try:
            header = self._native.read_header()
        except (NativeWorkloadShmError, OSError, ValueError):
            return out
        count = int(header.get("entry_count", 0) or 0)
        for slot in range(count):
            try:
                entry = self._native.load_entry(slot)
            except NativeWorkloadShmError:
                continue
            if not (int(entry.get("flags", 0)) & FLAG_VALID):
                continue
            out[(int(entry["instance_id"]), int(entry["endpoint_id"]))] = (
                int(entry["generation"]),
                int(entry["flags"]),
            )
        return out
