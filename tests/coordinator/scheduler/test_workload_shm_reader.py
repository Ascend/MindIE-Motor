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


from motor.coordinator.scheduler.runtime.workload_shm.reader import (
    WorkloadSharedMemoryReader,
    _shm_role_to_pdrole,
)
from motor.common.resources.instance import PDRole
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    ROLE_PREFILL,
    ROLE_DECODE,
    ROLE_HYBRID,
    SCHEMA_VERSION,
    HEADER_SIZE,
    ENTRY_SIZE,
    MAGIC,
    WorkloadShmHeader,
    WorkloadShmEntry,
    pack_header,
    pack_entry,
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
        self,
        mock_unpack_header,
        mock_unpack_entry,
        mock_time,
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

    def test_read_and_patch_cache_records_role_sequences(self):
        """Reader exposes per-role workload sequences from the stable shm header."""
        buf = self._make_buf(0)
        header = WorkloadShmHeader(
            magic=MAGIC,
            schema_version=SCHEMA_VERSION,
            sequence=8,
            entry_count=0,
            max_entries=10,
            instance_version=3,
            heartbeat_sequence=1,
            prefill_sequence=4,
            decode_sequence=6,
            hybrid_sequence=2,
        )
        buf[:HEADER_SIZE] = pack_header(header)
        self.reader._buf = memoryview(buf)

        result = self.reader.read_and_patch_cache(MagicMock())

        self.assertEqual(result, (3, False))
        self.assertEqual(self.reader.last_sequence, 8)
        self.assertEqual(self.reader.last_sequence_for_role(PDRole.ROLE_P), 4)
        self.assertEqual(self.reader.last_sequence_for_role(PDRole.ROLE_D), 6)
        self.assertEqual(self.reader.last_sequence_for_role(PDRole.ROLE_U), 2)

    def test_legacy_schema_uses_global_sequence_fallback(self):
        """Legacy schema headers must not expose role sequences from old padding bytes."""
        buf = self._make_buf(0)
        header = WorkloadShmHeader(
            magic=MAGIC,
            schema_version=1,
            sequence=8,
            entry_count=0,
            max_entries=10,
            instance_version=3,
            heartbeat_sequence=1,
            prefill_sequence=4,
            decode_sequence=6,
            hybrid_sequence=2,
        )
        buf[:HEADER_SIZE] = pack_header(header)
        self.reader._buf = memoryview(buf)
        self.reader._last_role_sequences[PDRole.ROLE_D] = 99

        result = self.reader.read_and_patch_cache(MagicMock(), role=PDRole.ROLE_D)

        self.assertEqual(result, (3, False))
        self.assertEqual(self.reader.last_sequence, 8)
        self.assertIsNone(self.reader.last_sequence_for_role(PDRole.ROLE_P))
        self.assertIsNone(self.reader.last_sequence_for_role(PDRole.ROLE_D))
        self.assertIsNone(self.reader.last_sequence_for_role(PDRole.ROLE_U))

    def test_role_patch_only_updates_matching_entries_and_sequence(self):
        """Role-scoped patching must not mark other role caches as current."""
        buf = self._make_buf(2)
        header = WorkloadShmHeader(
            magic=MAGIC,
            schema_version=SCHEMA_VERSION,
            sequence=8,
            entry_count=2,
            max_entries=10,
            instance_version=3,
            heartbeat_sequence=1,
            prefill_sequence=4,
            decode_sequence=6,
            hybrid_sequence=2,
        )
        buf[:HEADER_SIZE] = pack_header(header)
        buf[HEADER_SIZE : HEADER_SIZE + ENTRY_SIZE] = pack_entry(
            WorkloadShmEntry(
                instance_id=1,
                endpoint_id=10,
                role=ROLE_PREFILL,
                active_tokens=11.0,
                active_kv_cache=12.0,
            )
        )
        buf[HEADER_SIZE + ENTRY_SIZE : HEADER_SIZE + 2 * ENTRY_SIZE] = pack_entry(
            WorkloadShmEntry(
                instance_id=2,
                endpoint_id=20,
                role=ROLE_DECODE,
                active_tokens=21.0,
                active_kv_cache=22.0,
            )
        )
        self.reader._buf = memoryview(buf)
        mock_cache = MagicMock()

        result = self.reader.read_and_patch_cache(mock_cache, role=PDRole.ROLE_D)

        self.assertEqual(result, (3, False))
        mock_cache.patch_workload_from_shm.assert_called_once_with(
            2,
            20,
            PDRole.ROLE_D,
            21.0,
            22.0,
        )
        self.assertIsNone(self.reader.last_sequence_for_role(PDRole.ROLE_P))
        self.assertEqual(self.reader.last_sequence_for_role(PDRole.ROLE_D), 6)
        self.assertIsNone(self.reader.last_sequence_for_role(PDRole.ROLE_U))

    @patch("motor.coordinator.scheduler.runtime.workload_shm.reader.unpack_entry")
    def test_role_sequence_unchanged_skips_entry_scan(self, mock_unpack_entry):
        """When the selected role sequence is unchanged, no entries are unpacked or patched."""
        buf = self._make_buf(3)
        header = WorkloadShmHeader(
            magic=MAGIC,
            schema_version=SCHEMA_VERSION,
            sequence=10,
            entry_count=3,
            max_entries=10,
            instance_version=3,
            heartbeat_sequence=1,
            prefill_sequence=4,
            decode_sequence=6,
            hybrid_sequence=2,
        )
        buf[:HEADER_SIZE] = pack_header(header)
        self.reader._buf = memoryview(buf)
        self.reader._last_role_sequences[PDRole.ROLE_D] = 6
        mock_cache = MagicMock()

        result = self.reader.read_and_patch_cache(mock_cache, role=PDRole.ROLE_D)

        self.assertEqual(result, (3, False))
        mock_unpack_entry.assert_not_called()
        mock_cache.patch_workload_from_shm.assert_not_called()
        self.assertEqual(self.reader.last_sequence_for_role(PDRole.ROLE_D), 6)

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
