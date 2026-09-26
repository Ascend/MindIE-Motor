# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 license for more details.

"""Tests for SchedulerServer scheduling metrics published via shared memory."""

import subprocess
import sys
import threading
import time
import unittest
from multiprocessing import shared_memory

from motor.coordinator.scheduler.runtime.scheduler_metrics import (
    ROLE_DECODE,
    ROLE_PREFILL,
    SchedulerMetricsCollector,
    SchedulerMetricsShmReader,
    SchedulerMetricsShmWriter,
    _pack_role,
    _unpack_role,
    _untrack_shared_memory,
    sched_metrics_shm_total_size,
    sched_metrics_snapshot_to_motor_metrics,
)
from motor.common.resources.instance import PDRole


def _make_shm():
    # macOS POSIX shm 名上限 31 字符，测试名保持短前缀 + 短随机后缀。
    name = f"tsm_{time.monotonic_ns() % 1_000_000_000}"
    size = sched_metrics_shm_total_size(16)
    shm = shared_memory.SharedMemory(name=name, create=True, size=size)
    return shm


class TestRoleEncoding(unittest.TestCase):
    """Test role byte packing/unpacking."""

    def test_pack_role(self):
        self.assertEqual(_pack_role(PDRole.ROLE_P), ROLE_PREFILL)
        self.assertEqual(_pack_role(PDRole.ROLE_D), ROLE_DECODE)
        self.assertEqual(_pack_role("prefill"), ROLE_PREFILL)
        self.assertEqual(_pack_role("decode"), ROLE_DECODE)

    def test_unpack_role(self):
        self.assertEqual(_unpack_role(ROLE_PREFILL), "prefill")
        self.assertEqual(_unpack_role(ROLE_DECODE), "decode")
        self.assertEqual(_unpack_role(99), "union")


class TestSchedulerMetricsShm(unittest.TestCase):
    """Test the scheduling-metrics shared-memory writer/reader round-trip."""

    def test_writer_reader_round_trip(self):
        shm = _make_shm()
        writer = SchedulerMetricsShmWriter(shm, max_entries=16)
        reader = None
        try:
            snapshot = {
                (1, 1): (ROLE_PREFILL, 10.5, 20.0, 3, 100),
                (2, 1): (ROLE_DECODE, 0.25, 5.0, 7, 200),
            }
            writer.write_snapshot(snapshot)

            reader = SchedulerMetricsShmReader(writer.shm_name)
            reader.attach()
            result = reader.read_snapshot()
            self.assertEqual(result, snapshot)
            # Unchanged sequence returns {} (no new snapshot).
            self.assertEqual(reader.read_snapshot(), {})
        finally:
            if reader:
                reader.detach()
            writer.release()
            shm.close()
            shm.unlink()

    def test_reader_missing_shm_returns_empty(self):
        reader = SchedulerMetricsShmReader("no_such_shm_name")
        self.assertEqual(reader.read_snapshot(), {})
        self.assertEqual(reader.shm_name, "no_such_shm_name")

    @unittest.skipIf(sys.platform == "win32", "POSIX shared memory only")
    def test_attached_reader_process_exit_does_not_unlink_segment(self):
        """Reader（Obs 进程）退出不得 unlink SchedulerServer 拥有的段。

        CPython 把 create=False 的 attach 也注册进本进程 resource_tracker，退出时
        tracker 会 unlink 同名段；attach 后必须 unregister（参见
        scheduler_metrics._untrack_shared_memory）。
        """
        shm = _make_shm()
        name = shm.name.lstrip("/")
        try:
            child = (
                "from motor.coordinator.scheduler.runtime.scheduler_metrics import SchedulerMetricsShmReader;"
                f"r = SchedulerMetricsShmReader('{name}');"
                "r.attach()"
            )
            proc = subprocess.run(
                [sys.executable, "-c", child], capture_output=True, text=True, timeout=30, check=False
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("leaked shared_memory", proc.stderr)
            # 段仍可 attach：未被子进程退出时 unlink。
            probe = shared_memory.SharedMemory(name=name, create=False)
            _untrack_shared_memory(probe)  # 探测 attach 不得污染本进程 tracker
            probe.close()
        finally:
            shm.close()
            shm.unlink()

    def test_reader_drops_non_finite_entries(self):
        """Torn (NaN) doubles are dropped instead of being published as NaN metrics."""
        shm = _make_shm()
        writer = SchedulerMetricsShmWriter(shm, max_entries=16)
        reader = None
        try:
            writer.write_snapshot({(1, 1): (ROLE_DECODE, float("nan"), 2.0, 0, 0)})
            reader = SchedulerMetricsShmReader(writer.shm_name)
            reader.attach()
            self.assertEqual(reader.read_snapshot(), {})
        finally:
            if reader:
                reader.detach()
            writer.release()
            shm.close()
            shm.unlink()

    def test_reader_rejects_torn_header_and_recovers(self):
        """A torn header (entry_count > max_entries) is rejected without consuming the sequence."""
        import struct

        shm = _make_shm()
        writer = SchedulerMetricsShmWriter(shm, max_entries=16)
        reader = None
        try:
            writer.write_snapshot({(1, 1): (ROLE_DECODE, 1.0, 2.0, 0, 0)})
            reader = SchedulerMetricsShmReader(writer.shm_name)
            reader.attach()
            self.assertEqual(reader.read_snapshot(), {(1, 1): (ROLE_DECODE, 1.0, 2.0, 0, 0)})

            # 模拟撕裂 header：entry_count 超过 max_entries，序列号为新的偶数。
            # 直接用 shm.buf（临时 memoryview，语句结束即释放，避免占用导出导致 close 失败）。
            struct.pack_into(
                "<I H H q I I Q 32x",
                shm.buf,
                0,
                0x53484D54,  # _SCHED_METRICS_MAGIC ("SHMT")
                2,  # schema version
                0,
                100,  # 新的偶数序列号
                999,  # entry_count > max_entries=16 -> 撕裂
                16,
                0,
            )
            self.assertEqual(reader.read_snapshot(), {})
            self.assertEqual(reader.read_snapshot(), {})
            # 序列号未被消费：写者发布有效快照后仍能读到。
            writer.write_snapshot({(2, 1): (ROLE_PREFILL, 0.5, 1.0, 0, 0)})
            self.assertEqual(reader.read_snapshot(), {(2, 1): (ROLE_PREFILL, 0.5, 1.0, 0, 0)})
        finally:
            if reader:
                reader.detach()
            writer.release()
            shm.close()
            shm.unlink()


class TestSchedMetricsToMotorMetrics(unittest.TestCase):
    """Test conversion of a scheduling-metrics snapshot to a motor:endpoint_state family."""

    def test_conversion(self):
        snapshot = {
            (1, 1): (ROLE_PREFILL, 10.5, 20.0, 3, 100),
            (2, 1): (ROLE_DECODE, 0.25, 5.0, 7, 200),
        }
        metrics = sched_metrics_snapshot_to_motor_metrics(snapshot)
        # Merged into a single family with 2 endpoints x 4 stats = 8 samples.
        self.assertEqual(len(metrics), 1)

        metric = metrics[0]
        self.assertEqual(metric.name, "motor:endpoint_state")
        self.assertEqual(len(metric.label), 8)
        self.assertEqual(len(metric.value), 8)

        by_label = dict(zip(metric.label, metric.value))
        self.assertEqual(
            by_label['motor:endpoint_state{instance_id="2",endpoint_id="1",role="decode",stat="request_count"}'],
            7.0,
        )
        self.assertEqual(
            by_label['motor:endpoint_state{instance_id="1",endpoint_id="1",role="prefill",stat="fresh_load"}'],
            10.5,
        )
        self.assertEqual(
            by_label['motor:endpoint_state{instance_id="2",endpoint_id="1",role="decode",stat="active_tokens"}'],
            5.0,
        )
        self.assertEqual(
            by_label['motor:endpoint_state{instance_id="1",endpoint_id="1",role="prefill",stat="total_cnt"}'],
            100.0,
        )

    def test_empty_snapshot(self):
        self.assertEqual(sched_metrics_snapshot_to_motor_metrics({}), [])


class TestSchedulerMetricsCollector(unittest.TestCase):
    """Test the background scheduler-metrics collector."""

    def test_collects_and_publishes(self):
        published = []

        def _snapshot():
            return {(1, 1): (ROLE_DECODE, 1.0, 2.0, 3, 30)}

        def _on_publish(snapshot):
            published.append(snapshot)

        collector = SchedulerMetricsCollector(_snapshot, _on_publish, interval=0.5)
        collector.start()
        try:
            deadline = time.monotonic() + 5.0
            while not published and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(published)
            self.assertEqual(published[-1], {(1, 1): (ROLE_DECODE, 1.0, 2.0, 3, 30)})
        finally:
            collector.stop()

    def test_start_stop_idempotent(self):
        collector = SchedulerMetricsCollector(lambda: {}, lambda snap: None, interval=0.5)
        collector.start()
        collector.start()
        collector.stop()
        collector.stop()
        self.assertIsNone(collector._thread)

    def test_publishes_empty_snapshot(self):
        """空快照也必须发布：清掉 shm 里的陈旧条目，否则实例下线后 Obs 一直读到旧数据。"""
        published = []

        def _snapshot():
            # 发布一轮后置位 stop_event，让 _run 执行完本轮即退出（确定性）。
            collector._stop_event.set()
            return {}

        collector = SchedulerMetricsCollector(_snapshot, published.append, interval=0.5)
        thread = threading.Thread(target=collector._run)
        thread.start()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(published, [{}])


if __name__ == "__main__":
    unittest.main()
