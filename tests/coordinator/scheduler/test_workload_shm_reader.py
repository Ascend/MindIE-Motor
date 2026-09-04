# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 license for more details.

"""Tests for WorkloadSharedMemoryReader role mapping. Roundtrip/CAS live in sibling files."""

import unittest

from motor.coordinator.scheduler.runtime.workload_shm.reader import _shm_role_to_pdrole
from motor.common.resources.instance import PDRole
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    ROLE_PREFILL,
    ROLE_DECODE,
    ROLE_HYBRID,
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
