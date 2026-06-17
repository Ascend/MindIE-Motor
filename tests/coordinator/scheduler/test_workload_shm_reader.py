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

"""Tests for WorkloadSharedMemoryReader."""

import unittest
from unittest.mock import MagicMock, patch

import pytest

from motor.coordinator.scheduler.runtime.workload_shm.reader import (
    WorkloadSharedMemoryReader,
    _shm_role_to_pdrole,
)
from motor.common.resources.instance import PDRole
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    ROLE_PREFILL,
    ROLE_DECODE,
    ROLE_HYBRID,
    HEADER_SIZE,
    ENTRY_SIZE,
    MAGIC,
    HEARTBEAT_STALE_SEC,
)


class TestShmRoleToPDRole(unittest.TestCase):
    """Test _shm_role_to_pdrole mapping function."""

    def test_role_prefill(self):
        """ROLE_PREFILL maps to PDRole.ROLE_P."""
        self.assertEqual(_shm_role_to_pdrole(ROLE_PREFILL), PDRole.ROLE_P)

    def test_role_decode(self):
        """ROLE_DECODE maps to PDRole.ROLE_D."""
        self.assertEqual(_shm_role_to_pdrole(ROLE_DECODE), PDRole.ROLE_D)

    def test_role_hybrid_unknown(self):
        """ROLE_HYBRID and unknown values map to PDRole.ROLE_U."""
        self.assertEqual(_shm_role_to_pdrole(ROLE_HYBRID), PDRole.ROLE_U)
        self.assertEqual(_shm_role_to_pdrole(99), PDRole.ROLE_U)


class TestWorkloadSharedMemoryReader(unittest.TestCase):
    """Test WorkloadSharedMemoryReader."""

    def setUp(self):
        self.reader = WorkloadSharedMemoryReader("test_shm")

    def _make_buf(self, entry_count=3):
        """Helper: create a bytearray buffer large enough for header + entries."""
        return bytearray(HEADER_SIZE + entry_count * ENTRY_SIZE)

    # --- attach / detach ---

    @patch("motor.coordinator.scheduler.runtime.workload_shm.reader.shared_memory.SharedMemory")
    def test_attach_and_detach(self, mock_shm_class):
        """attach() opens existing shm with create=False; detach() closes it and clears buf."""
        mock_instance = MagicMock()
        mock_instance.buf = b"test"
        mock_shm_class.return_value = mock_instance

        self.reader.attach()

        mock_shm_class.assert_called_once_with(name="test_shm", create=False)
        self.assertIsNotNone(self.reader._shm)
        self.assertIsNotNone(self.reader._buf)

        self.reader.detach()

        self.assertIsNone(self.reader._buf)
        self.assertIsNone(self.reader._shm)
        mock_instance.close.assert_called_once()

    # --- read_and_patch_cache: stale heartbeat ---

    @patch("motor.coordinator.scheduler.runtime.workload_shm.reader.time.time")
    @patch("motor.coordinator.scheduler.runtime.workload_shm.reader.unpack_entry")
    @patch("motor.coordinator.scheduler.runtime.workload_shm.reader.unpack_header")
    def test_read_and_patch_cache_stale_heartbeat(
        self, mock_unpack_header, mock_unpack_entry, mock_time,
    ):
        """Heartbeat stale when heartbeat_sequence unchanged for longer than HEARTBEAT_STALE_SEC."""
        buf = self._make_buf(0)
        self.reader._buf = buf

        # Pre-set reader state: already seen heartbeat_sequence=1 at t=100
        self.reader._last_heartbeat_value = 1
        self.reader._last_heartbeat_time = 100.0
        mock_time.return_value = 200.0  # 100 seconds later >> HEARTBEAT_STALE_SEC

        mock_header = MagicMock()
        mock_header.magic = MAGIC
        mock_header.entry_count = 0
        mock_header.max_entries = 10
        mock_header.heartbeat_sequence = 1  # unchanged
        mock_header.instance_version = 7
        mock_unpack_header.return_value = mock_header

        mock_cache = MagicMock()

        result = self.reader.read_and_patch_cache(mock_cache)

        self.assertIn(result, [(7, True), (None, False)])

    # --- read_and_patch_cache: error / edge cases ---

    def test_read_and_patch_cache_no_buf(self):
        """When _buf is None, returns (None, False)."""
        self.reader._buf = None
        result = self.reader.read_and_patch_cache(MagicMock())
        self.assertEqual(result, (None, False))

    @patch("motor.coordinator.scheduler.runtime.workload_shm.reader.unpack_header")
    def test_read_and_patch_cache_bad_magic(self, mock_unpack_header):
        """When magic does not match MAGIC, returns (None, False)."""
        self.reader._buf = self._make_buf(1)

        mock_header = MagicMock()
        mock_header.magic = 0xDEADBEEF
        mock_unpack_header.return_value = mock_header

        result = self.reader.read_and_patch_cache(MagicMock())
        self.assertEqual(result, (None, False))

    @patch("motor.coordinator.scheduler.runtime.workload_shm.reader.unpack_header")
    def test_read_and_patch_cache_invalid_entry_count(self, mock_unpack_header):
        """When entry_count is negative or exceeds max_entries, returns (None, False)."""
        self.reader._buf = self._make_buf(3)

        mock_header = MagicMock()
        mock_header.magic = MAGIC
        mock_header.entry_count = -1
        mock_header.max_entries = 10
        mock_unpack_header.return_value = mock_header

        result = self.reader.read_and_patch_cache(MagicMock())
        self.assertEqual(result, (None, False))

        # entry_count > max_entries
        mock_header.entry_count = 20
        mock_header.max_entries = 10
        result = self.reader.read_and_patch_cache(MagicMock())
        self.assertEqual(result, (None, False))

    @patch("motor.coordinator.scheduler.runtime.workload_shm.reader.unpack_header")
    def test_read_and_patch_cache_buffer_too_small(self, mock_unpack_header):
        """When buffer is smaller than required_size, returns (None, False)."""
        self.reader._buf = self._make_buf(1)

        mock_header = MagicMock()
        mock_header.magic = MAGIC
        mock_header.entry_count = 100
        mock_header.max_entries = 200
        mock_unpack_header.return_value = mock_header

        result = self.reader.read_and_patch_cache(MagicMock())
        self.assertEqual(result, (None, False))

    @patch("motor.coordinator.scheduler.runtime.workload_shm.reader.unpack_header")
    def test_read_and_patch_cache_exception(self, mock_unpack_header):
        """When unpack_header raises, returns (None, False)."""
        self.reader._buf = self._make_buf(1)
        mock_unpack_header.side_effect = RuntimeError("unexpected")

        result = self.reader.read_and_patch_cache(MagicMock())
        self.assertEqual(result, (None, False))
