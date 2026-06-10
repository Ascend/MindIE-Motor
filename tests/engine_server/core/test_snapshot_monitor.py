# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from motor.engine_server.core.snapshot_monitor import SnapshotMonitor


def setup_function():
    if hasattr(SnapshotMonitor, "_instances") and SnapshotMonitor in SnapshotMonitor._instances:
        del SnapshotMonitor._instances[SnapshotMonitor]


def test_snapshot_monitor_singleton():
    monitor1 = SnapshotMonitor()
    monitor2 = SnapshotMonitor()
    assert monitor1 is monitor2


def test_snapshot_monitor_initial_state():
    monitor = SnapshotMonitor()
    assert monitor.is_suspend_done is False
    assert monitor.is_resume_done is False


def test_snapshot_monitor_mark_suspend_and_resume_done():
    monitor = SnapshotMonitor()
    monitor.mark_suspend_done()
    monitor.mark_resume_done()

    assert monitor.is_suspend_done is True
    assert monitor.is_resume_done is True
