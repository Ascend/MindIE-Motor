# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 license for more details.

"""Tests for WorkloadSharedMemoryOwner public API and membership helpers."""

import os
import unittest
from unittest.mock import MagicMock

import pytest

from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.common.resources.http_msg_spec import EventType
from motor.common.resources.instance import Instance, InsStatus, PDRole, ParallelConfig
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain.instance_manager import InstanceManager
from motor.coordinator.scheduler.runtime.workload_shm.writer import (
    WorkloadSharedMemoryOwner,
    _pdrole_to_shm_role,
    _collect_entries_and_slot_map,
    _lowest_free_slot,
)
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    ROLE_PREFILL,
    ROLE_DECODE,
    ROLE_HYBRID,
    SCHEMA_VERSION,
)
from motor.coordinator.scheduler.runtime.workload_shm.native import (
    STATUS_OK,
    NativeWorkloadShmUnavailable,
    load_native_library,
)
from motor.coordinator.scheduler.runtime.workload_shm.reader import WorkloadSharedMemoryReader


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


# ===================================================================
# TestPdRoleToShmRole
# ===================================================================


class TestPdRoleToShmRole(unittest.TestCase):
    """Test _pdrole_to_shm_role mapping function."""

    def test_role_p(self):
        """PDRole.ROLE_P maps to ROLE_PREFILL."""
        self.assertEqual(_pdrole_to_shm_role(PDRole.ROLE_P), ROLE_PREFILL)

    def test_role_d(self):
        """PDRole.ROLE_D maps to ROLE_DECODE."""
        self.assertEqual(_pdrole_to_shm_role(PDRole.ROLE_D), ROLE_DECODE)

    def test_role_u_hybrid(self):
        """PDRole.ROLE_U and other roles map to ROLE_HYBRID."""
        self.assertEqual(_pdrole_to_shm_role(PDRole.ROLE_U), ROLE_HYBRID)


# ===================================================================
# TestCollectEntriesAndSlotMap
# ===================================================================


class TestCollectEntriesAndSlotMap(unittest.TestCase):
    """Test _collect_entries_and_slot_map helper."""

    def _make_endpoint(self, eid, tokens):
        ep = MagicMock()
        ep.id = eid
        ep.workload = MagicMock()
        ep.workload.active_tokens = tokens
        return ep

    def _make_instance(self, iid, endpoints_dict):
        inst = MagicMock()
        inst.id = iid
        inst.endpoints = endpoints_dict
        return inst

    # ---------------------------------------------------------------
    def test_collect_entries(self):
        """Collects entries from all roles and builds correct slot_map."""
        im = MagicMock()

        # ROLE_P instance with one endpoint
        ep_p = self._make_endpoint(10, 100.0)
        inst_p = self._make_instance(1, {"g1": {10: ep_p}})

        # ROLE_D instance with one endpoint
        ep_d = self._make_endpoint(20, 300.0)
        inst_d = self._make_instance(2, {"g1": {20: ep_d}})

        # ROLE_U instance with one endpoint
        ep_u = self._make_endpoint(30, 500.0)
        inst_u = self._make_instance(3, {"g1": {30: ep_u}})

        def get_available_instances_side_effect(role):
            if role == PDRole.ROLE_P:
                return {1: inst_p}
            if role == PDRole.ROLE_D:
                return {2: inst_d}
            if role == PDRole.ROLE_U:
                return {3: inst_u}
            return {}

        im.get_available_instances = MagicMock(
            side_effect=get_available_instances_side_effect,
        )

        entries, slot_map = _collect_entries_and_slot_map(im, max_entries=100)

        self.assertEqual(len(entries), 3)
        self.assertEqual(len(slot_map), 3)

        # slots assigned in order: P, D, U
        self.assertEqual(slot_map, {(1, 10): 0, (2, 20): 1, (3, 30): 2})

        self.assertEqual(
            entries[0],
            (1, 10, ROLE_PREFILL, 100.0),
        )
        self.assertEqual(
            entries[1],
            (2, 20, ROLE_DECODE, 300.0),
        )
        self.assertEqual(
            entries[2],
            (3, 30, ROLE_HYBRID, 500.0),
        )

    # ---------------------------------------------------------------
    def test_truncate_at_max_entries(self):
        """When entries exceed max_entries, only max_entries are returned."""
        im = MagicMock()

        eps = {i: self._make_endpoint(i, float(i)) for i in range(5)}
        inst = self._make_instance(1, {"g1": eps})

        im.get_available_instances = MagicMock(
            return_value={1: inst},
        )

        entries, slot_map = _collect_entries_and_slot_map(im, max_entries=2)

        self.assertEqual(len(entries), 2)
        self.assertEqual(len(slot_map), 2)
        self.assertEqual(slot_map, {(1, 0): 0, (1, 1): 1})

    def test_lowest_free_slot(self):
        """Stable-slot allocator picks the lowest hole and refuses a full table."""
        self.assertEqual(_lowest_free_slot(set(), 8), 0)
        self.assertEqual(_lowest_free_slot({0, 1, 3}, 8), 2)
        self.assertIsNone(_lowest_free_slot({0, 1, 2}, 3))


def _unique(tag: str) -> str:
    return f"mw{os.getpid()}{tag}"[:24]


def _make_real_instance(instance_id: int, endpoint_id: int, tokens: float) -> Instance:
    inst = Instance(
        job_name=f"p-{instance_id}",
        model_name="test_model",
        id=instance_id,
        role=PDRole.ROLE_P,
        status=InsStatus.ACTIVE,
        parallel_config=ParallelConfig(dp_size=1),
    )
    inst.add_endpoints(
        f"pod-{instance_id}",
        {
            0: Endpoint(
                id=endpoint_id,
                ip="10.0.0.1",
                business_port="8080",
                status=EndpointStatus.NORMAL,
                workload=Workload(active_tokens=tokens),
            )
        },
    )
    return inst


@pytest.fixture
def native_lib():
    try:
        return load_native_library()
    except NativeWorkloadShmUnavailable as e:
        pytest.skip(f"native workload-shm library not built: {e}")
        return None


@pytest.mark.asyncio
async def test_writer_snapshot_and_heartbeat(native_lib):
    """Public writer API: first ADD snapshot seeds 0, heartbeat bumps, reader patches cache."""
    del native_lib
    config = CoordinatorConfig()
    im = InstanceManager(config)
    await im.refresh_instances(EventType.ADD, [_make_real_instance(1, 10, 0.0)])
    name = _unique("ws")
    writer = WorkloadSharedMemoryOwner(im, max_entries=8, shm_name=name)
    reader = WorkloadSharedMemoryReader(name)
    try:
        writer.write_snapshot()
        writer.write_heartbeat()
        header = writer.native.read_header()
        assert header["schema_version"] == SCHEMA_VERSION == 4
        assert header["sequence"] % 2 == 0
        assert header["heartbeat"] == 1
        reader.attach()
        patched: dict[tuple[int, int], tuple[PDRole, float]] = {}

        class _Cache:
            def patch_workload_from_shm(self, instance_id, endpoint_id, role, active_tokens) -> None:
                patched[(instance_id, endpoint_id)] = (role, active_tokens)

        version, stale = reader.read_and_patch_cache(_Cache(), role=None)
        assert version == 1
        assert stale is False
        assert patched == {(1, 10): (PDRole.ROLE_P, 0.0)}
    finally:
        reader.detach()
        writer.release()


@pytest.mark.asyncio
async def test_writer_set_blocked(native_lib):
    """set_blocked marks the instance so allocate CAS returns BLOCKED."""
    from motor.coordinator.scheduler.runtime.workload_shm.native import STATUS_BLOCKED

    del native_lib
    config = CoordinatorConfig()
    im = InstanceManager(config)
    await im.refresh_instances(EventType.ADD, [_make_real_instance(3, 30, 0.0)])
    name = _unique("wb")
    writer = WorkloadSharedMemoryOwner(im, max_entries=8, shm_name=name)
    try:
        writer.write_snapshot()
        assert writer.set_blocked(3, True) >= 1
        meta = writer.native.load_entry(0)
        status, actual = writer.native.cas_add(3, 30, int(meta["generation"]), 0.0, 1.0)
        assert status == STATUS_BLOCKED
        assert actual == 0.0
    finally:
        writer.release()


@pytest.mark.asyncio
async def test_writer_snapshot_preserves_cas_tokens(native_lib):
    """Refresh snapshot must not reset tokens a Worker already CAS-added (IM seed stays 0)."""
    del native_lib
    config = CoordinatorConfig()
    im = InstanceManager(config)
    await im.refresh_instances(EventType.ADD, [_make_real_instance(1, 10, 0.0)])
    name = _unique("wt")
    writer = WorkloadSharedMemoryOwner(im, max_entries=8, shm_name=name)
    try:
        writer.write_snapshot()
        meta = writer.native.load_entry(0)
        status, actual = writer.native.cas_add(1, 10, int(meta["generation"]), 0.0, 10.0)
        assert status == STATUS_OK
        assert actual == 10.0
        await im.refresh_instances(EventType.ADD, [_make_real_instance(2, 20, 0.0)])
        writer.write_snapshot()
        first = writer.native.load_entry(0)
        assert first["instance_id"] == 1
        assert first["active_tokens"] == 10.0
        second = writer.native.load_entry(1)
        assert second["instance_id"] == 2
        assert second["active_tokens"] == 0.0
    finally:
        writer.release()
