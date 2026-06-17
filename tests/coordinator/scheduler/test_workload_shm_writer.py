# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 license for more details.

"""Tests for WorkloadSharedMemoryWriter."""

import asyncio
import struct
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from motor.coordinator.scheduler.runtime.workload_shm.writer import (
    WorkloadSharedMemoryWriter,
    _pdrole_to_shm_role,
    _collect_entries_and_slot_map,
)
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    ROLE_PREFILL,
    ROLE_DECODE,
    ROLE_HYBRID,
    HEADER_SIZE,
    ENTRY_SIZE,
    HEARTBEAT_OFFSET,
    MAGIC,
    WorkloadShmEntry,
    WorkloadShmHeader,
    pack_entry,
    unpack_header,
)
from motor.common.resources.instance import PDRole


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_mock_shm(buf_size=None):
    """Create a mock SharedMemory whose .buf is a real bytearray (memoryview-compatible)."""
    if buf_size is None:
        buf_size = HEADER_SIZE + 10 * ENTRY_SIZE
    shm = MagicMock()
    shm.name = "test_workload_shm"
    shm.buf = bytearray(buf_size)
    return shm


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

    def _make_endpoint(self, eid, tokens, kv):
        ep = MagicMock()
        ep.id = eid
        ep.workload = MagicMock()
        ep.workload.active_tokens = tokens
        ep.workload.active_kv_cache = kv
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
        ep_p = self._make_endpoint(10, 100.0, 200.0)
        inst_p = self._make_instance(1, {"g1": {10: ep_p}})

        # ROLE_D instance with one endpoint
        ep_d = self._make_endpoint(20, 300.0, 400.0)
        inst_d = self._make_instance(2, {"g1": {20: ep_d}})

        # ROLE_U instance with one endpoint
        ep_u = self._make_endpoint(30, 500.0, 600.0)
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
            entries[0], (1, 10, ROLE_PREFILL, 100.0, 200.0),
        )
        self.assertEqual(
            entries[1], (2, 20, ROLE_DECODE, 300.0, 400.0),
        )
        self.assertEqual(
            entries[2], (3, 30, ROLE_HYBRID, 500.0, 600.0),
        )

    # ---------------------------------------------------------------
    def test_truncate_at_max_entries(self):
        """When entries exceed max_entries, only max_entries are returned."""
        im = MagicMock()

        eps = {i: self._make_endpoint(i, float(i), float(i * 2)) for i in range(5)}
        inst = self._make_instance(1, {"g1": eps})

        im.get_available_instances = MagicMock(
            return_value={1: inst},
        )

        entries, slot_map = _collect_entries_and_slot_map(im, max_entries=2)

        self.assertEqual(len(entries), 2)
        self.assertEqual(len(slot_map), 2)
        self.assertEqual(slot_map, {(1, 0): 0, (1, 1): 1})


# ===================================================================
# TestWorkloadSharedMemoryWriter
# ===================================================================

class TestWorkloadSharedMemoryWriter(unittest.TestCase):
    """Test WorkloadSharedMemoryWriter."""

    # ---------------------------------------------------------------
    def test_shm_name(self):
        """shm_name property returns the underlying SharedMemory name."""
        shm = _make_mock_shm()
        writer = WorkloadSharedMemoryWriter(shm, MagicMock())
        self.assertEqual(writer.shm_name, "test_workload_shm")

    # ---------------------------------------------------------------
    def test_instance_version(self):
        """instance_version starts at 0 and is bumped after write_snapshot."""
        shm = _make_mock_shm()
        writer = WorkloadSharedMemoryWriter(shm, MagicMock())
        self.assertEqual(writer.instance_version, 0)

        with patch(
            "motor.coordinator.scheduler.runtime.workload_shm.writer."
            "_collect_entries_and_slot_map",
            return_value=([], {}),
        ):
            writer.write_snapshot()

        self.assertEqual(writer.instance_version, 1)

    # ---------------------------------------------------------------
    def test_release(self):
        """release() clears buf and shm references."""
        shm = _make_mock_shm()
        writer = WorkloadSharedMemoryWriter(shm, MagicMock())
        self.assertIsNotNone(writer._buf)
        self.assertIsNotNone(writer._shm)

        writer.release()

        self.assertIsNone(writer._buf)
        self.assertIsNone(writer._shm)

    # ---------------------------------------------------------------
    def test_write_heartbeat(self):
        """write_heartbeat increments heartbeat_sequence in the buffer."""
        shm = _make_mock_shm()
        writer = WorkloadSharedMemoryWriter(shm, MagicMock())

        self.assertEqual(writer._heartbeat_sequence, 0)

        writer.write_heartbeat()

        self.assertEqual(writer._heartbeat_sequence, 1)

        written = struct.unpack(
            "<Q",
            bytes(writer._buf[HEARTBEAT_OFFSET: HEARTBEAT_OFFSET + 8]),
        )[0]
        self.assertEqual(written, 1)

    # ---------------------------------------------------------------
    def test_write_single_entry_existing_slot(self):
        """write_single_entry updates an existing slot without full snapshot."""
        shm = _make_mock_shm()
        im = MagicMock()
        writer = WorkloadSharedMemoryWriter(shm, im)

        # Pre-populate slot_map
        writer._slot_map = {(1, 1): 0}
        writer._entry_count = 1
        writer._sequence = 5
        writer._instance_version = 2

        # Mock get_endpoint_workload
        mock_workload = MagicMock()
        mock_workload.active_tokens = 999.0
        mock_workload.active_kv_cache = 888.0
        im.get_endpoint_workload = AsyncMock(
            return_value=(PDRole.ROLE_P, mock_workload),
        )

        # Patch _write_entry_at_slot and _write_header so we can inspect calls
        with patch.object(WorkloadSharedMemoryWriter, "_write_entry_at_slot") as mock_wes:
            with patch.object(WorkloadSharedMemoryWriter, "_write_header") as mock_wh:
                asyncio.run(writer.write_single_entry(1, 1))

        # Verify slot 0 was updated
        mock_wes.assert_called()
        call_args = mock_wes.call_args_list[-1]  # Last call is our write
        _args, _kwargs = call_args
        slot_arg, entry_arg = _args
        self.assertEqual(slot_arg, 0)
        self.assertIsInstance(entry_arg, WorkloadShmEntry)
        self.assertEqual(entry_arg.instance_id, 1)
        self.assertEqual(entry_arg.endpoint_id, 1)
        self.assertEqual(entry_arg.role, ROLE_PREFILL)
        self.assertEqual(entry_arg.active_tokens, 999.0)
        self.assertEqual(entry_arg.active_kv_cache, 888.0)

        # Header should be rewritten
        mock_wh.assert_called()

    # ---------------------------------------------------------------
    def test_write_single_entry_missing_slot(self):
        """When slot_map misses (instance_id, endpoint_id), falls back to write_snapshot."""
        shm = _make_mock_shm()
        im = MagicMock()
        writer = WorkloadSharedMemoryWriter(shm, im)
        writer._slot_map = {}  # nothing cached

        with patch.object(WorkloadSharedMemoryWriter, "write_snapshot") as mock_snapshot:
            asyncio.run(writer.write_single_entry(1, 1))

        mock_snapshot.assert_called_once()

    # ---------------------------------------------------------------
    def test_write_header_preserves_heartbeat(self):
        """_write_header reads the larger heartbeat from buf and keeps it."""
        shm = _make_mock_shm()
        writer = WorkloadSharedMemoryWriter(shm, MagicMock())

        # Writer starts with heartbeat_sequence = 0
        self.assertEqual(writer._heartbeat_sequence, 0)

        # Pre-populate buffer with a heartbeat value larger than internal
        larger_heartbeat = 42
        writer._buf[HEARTBEAT_OFFSET:HEARTBEAT_OFFSET + 8] = struct.pack(
            "<Q", larger_heartbeat,
        )

        writer._write_header()

        # Internal value should have been bumped
        self.assertEqual(writer._heartbeat_sequence, larger_heartbeat)

        # The written header should also contain the larger value
        current = struct.unpack(
            "<Q",
            bytes(writer._buf[HEARTBEAT_OFFSET:HEARTBEAT_OFFSET + 8]),
        )[0]
        self.assertEqual(current, larger_heartbeat)

    # ---------------------------------------------------------------
    def test_write_entry_at_slot(self):
        """_write_entry_at_slot writes entry bytes at the correct offset."""
        shm = _make_mock_shm()
        writer = WorkloadSharedMemoryWriter(shm, MagicMock())

        entry = WorkloadShmEntry(
            instance_id=100,
            endpoint_id=200,
            role=ROLE_PREFILL,
            active_tokens=12.5,
            active_kv_cache=34.5,
        )
        expected_data = pack_entry(entry)

        writer._write_entry_at_slot(3, entry)

        offset = HEADER_SIZE + 3 * ENTRY_SIZE
        actual = bytes(writer._buf[offset:offset + ENTRY_SIZE])
        self.assertEqual(actual, expected_data)

        # Other slots remain zeroed
        for slot in (0, 1, 2, 4):
            off = HEADER_SIZE + slot * ENTRY_SIZE
            self.assertEqual(
                bytes(writer._buf[off:off + ENTRY_SIZE]),
                b"\x00" * ENTRY_SIZE,
                msg=f"Slot {slot} should be untouched",
            )
