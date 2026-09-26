# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 license for more details.

"""Tests for KV cache usage provider (kv_usage) and the load-balance exclusion logic."""

import struct
import subprocess
import sys
import time
import unittest
from multiprocessing import shared_memory
from unittest.mock import patch

from motor.coordinator.scheduler.allocate_arbitration import ArbitrationContext, select_global_load_balance_candidate
from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy
from motor.coordinator.scheduler.runtime import kv_usage
from motor.coordinator.metrics.metric_types import Metric, MetricType
from motor.common.resources.instance import PDRole
from tests.coordinator.scheduler.conftest import (
    create_mock_instance,
    create_mock_endpoint,
    create_mock_workload,
)


def _make_collect(instance_id, endpoint_id, usage=None):
    """Build a MetricsCollector-style collects entry: per-endpoint parsed Metric lists."""
    metrics = []
    if usage is not None:
        metrics.append(
            Metric(
                name="vllm:kv_cache_usage_perc",
                help="KV cache usage.",
                type=MetricType.GAUGE,
                value=[usage],
            )
        )
    return {
        instance_id: {
            "role": "decode",
            "endpoints": {
                endpoint_id: {"metrics": metrics, "pod_ip": "10.0.0.1"},
            },
        }
    }


class TestKvUsageCache(unittest.TestCase):
    """Test the kv usage cache: extract from collects populates it, reads are cache-only."""

    def setUp(self):
        kv_usage.clear_kv_usage_cache()

    def tearDown(self):
        # 清理模块级缓存，避免脏数据影响后续用例（模块缓存是进程全局的）。
        kv_usage.clear_kv_usage_cache()

    def _make_instance(self, instance_id, ip="10.0.0.1", business_port="8081"):
        ep = create_mock_endpoint(endpoint_id=1)
        ep.ip = ip
        ep.business_port = business_port
        return create_mock_instance(
            instance_id=instance_id,
            endpoints={"pod1": {1: ep}},
        )

    def _single_endpoint(self, instance):
        return instance.get_all_endpoints()[0]

    def test_read_before_sync_returns_none(self):
        """Before any publish/sync, reads return None (no I/O on read path)."""
        instance = self._make_instance(1)
        self.assertIsNone(kv_usage.get_endpoint_kv_cache_usage(instance, self._single_endpoint(instance)))

    def test_extract_kv_usage_from_collects(self):
        """extract_kv_usage_from_collects pulls per-endpoint usage from collects."""
        collects = _make_collect(1, 1, usage=0.75)
        extracted = kv_usage.extract_kv_usage_from_collects(collects)
        self.assertEqual(extracted, {(1, 1): 0.75})

    def test_extract_missing_metric_returns_empty(self):
        """Collects without the kv usage family extract to an empty mapping."""
        collects = _make_collect(1, 1, usage=None)
        self.assertEqual(kv_usage.extract_kv_usage_from_collects(collects), {})

    def test_extract_invalid_input_returns_empty(self):
        """Non-dict / None collects degrade to an empty mapping."""
        self.assertEqual(kv_usage.extract_kv_usage_from_collects(None), {})
        self.assertEqual(kv_usage.extract_kv_usage_from_collects([]), {})

    def test_extract_per_endpoint(self):
        """Each endpoint is extracted under its own (instance_id, endpoint_id) key."""
        collects = {
            1: {
                "role": "decode",
                "endpoints": {
                    1: {
                        "metrics": [
                            Metric(
                                name="vllm:kv_cache_usage_perc",
                                help="KV cache usage.",
                                type=MetricType.GAUGE,
                                value=[0.4],
                            )
                        ],
                        "pod_ip": "10.0.0.1",
                    },
                    2: {
                        "metrics": [
                            Metric(
                                name="vllm:kv_cache_usage_perc",
                                help="KV cache usage.",
                                type=MetricType.GAUGE,
                                value=[0.9],
                            )
                        ],
                        "pod_ip": "10.0.0.2",
                    },
                },
            },
        }
        extracted = kv_usage.extract_kv_usage_from_collects(collects)
        self.assertEqual(extracted, {(1, 1): 0.4, (1, 2): 0.9})

    def test_update_and_snapshot_cache(self):
        """update_kv_usage_cache replaces the cache; snapshot returns a copy."""
        kv_usage.update_kv_usage_cache({(1, 1): 0.4, (2, 1): 0.8})
        snap = kv_usage.snapshot_kv_usage_cache()
        self.assertEqual(snap, {(1, 1): 0.4, (2, 1): 0.8})
        # Empty mapping must not wipe the cache (shm reader returns {} on unchanged snapshot).
        kv_usage.update_kv_usage_cache({})
        self.assertEqual(kv_usage.snapshot_kv_usage_cache(), {(1, 1): 0.4, (2, 1): 0.8})

    def test_on_demand_read_from_shm(self):
        """After MetricsCollector publishes a snapshot, reads pick it up immediately."""
        shm = TestKvUsageShm._make_shm()
        writer = kv_usage.KvUsageShmWriter(shm, max_entries=16)
        reader = kv_usage.KvUsageShmReader(writer.shm_name)
        reader.attach()
        try:
            writer.write_snapshot({(1, 1): 0.3, (2, 1): 0.7})
            kv_usage.set_kv_usage_reader(reader)
            instance = self._make_instance(1)
            ep = self._single_endpoint(instance)
            # 按需读取：无需定时同步线程，下一次读取即拿到 collector 最新发布的数据。
            self.assertAlmostEqual(kv_usage.get_endpoint_kv_cache_usage(instance, ep), 0.3)
            # 新快照发布后，读取自动刷新。
            writer.write_snapshot({(1, 1): 0.9, (2, 1): 0.7})
            self.assertAlmostEqual(kv_usage.get_endpoint_kv_cache_usage(instance, ep), 0.9)
        finally:
            kv_usage.set_kv_usage_reader(None)
            reader.detach()
            writer.release()
            shm.close()
            shm.unlink()

    def test_set_reader_none_keeps_cache(self):
        """Clearing the reader disables shm reads without wiping the local cache."""
        shm = TestKvUsageShm._make_shm()
        writer = kv_usage.KvUsageShmWriter(shm, max_entries=16)
        reader = kv_usage.KvUsageShmReader(writer.shm_name)
        reader.attach()
        try:
            writer.write_snapshot({(1, 1): 0.3})
            kv_usage.set_kv_usage_reader(reader)
            instance = self._make_instance(1)
            ep = self._single_endpoint(instance)
            self.assertAlmostEqual(kv_usage.get_endpoint_kv_cache_usage(instance, ep), 0.3)
            # 注销 reader 后：不再读 shm，缓存保持上一次的值。
            kv_usage.set_kv_usage_reader(None)
            self.assertAlmostEqual(kv_usage.get_endpoint_kv_cache_usage(instance, ep), 0.3)
        finally:
            kv_usage.set_kv_usage_reader(None)
            reader.detach()
            writer.release()
            shm.close()
            shm.unlink()

    def test_empty_snapshot_clears_cache(self):
        """A published empty snapshot clears stale entries instead of keeping them forever."""
        shm = TestKvUsageShm._make_shm()
        writer = kv_usage.KvUsageShmWriter(shm, max_entries=16)
        reader = kv_usage.KvUsageShmReader(writer.shm_name)
        reader.attach()
        try:
            writer.write_snapshot({(1, 1): 0.3})
            kv_usage.set_kv_usage_reader(reader)
            instance = self._make_instance(1)
            ep = self._single_endpoint(instance)
            self.assertAlmostEqual(kv_usage.get_endpoint_kv_cache_usage(instance, ep), 0.3)

            # 发布端清空（引擎重启 / 指标消失）：空快照必须清掉旧值。
            writer.write_snapshot({})
            self.assertIsNone(kv_usage.get_endpoint_kv_cache_usage(instance, ep))
            self.assertEqual(kv_usage.snapshot_kv_usage_cache(), {})
        finally:
            kv_usage.set_kv_usage_reader(None)
            reader.detach()
            writer.release()
            shm.close()
            shm.unlink()

    def test_cache_ttl_expires_stale_entries(self):
        """Entries not refreshed within the TTL report None (dead publisher fallback)."""
        shm = TestKvUsageShm._make_shm()
        writer = kv_usage.KvUsageShmWriter(shm, max_entries=16)
        reader = kv_usage.KvUsageShmReader(writer.shm_name)
        reader.attach()
        try:
            writer.write_snapshot({(1, 1): 0.3})
            kv_usage.set_kv_usage_reader(reader)
            instance = self._make_instance(1)
            ep = self._single_endpoint(instance)
            self.assertAlmostEqual(kv_usage.get_endpoint_kv_cache_usage(instance, ep), 0.3)

            # 发布端停止更新：时间推进超过 TTL 后条目过期。
            expired = time.monotonic() + kv_usage._kv_usage_cache_ttl + 1.0
            with patch("time.monotonic", return_value=expired):
                self.assertIsNone(kv_usage.get_endpoint_kv_cache_usage(instance, ep))
        finally:
            kv_usage.set_kv_usage_reader(None)
            reader.detach()
            writer.release()
            shm.close()
            shm.unlink()


class TestKvUsageShm(unittest.TestCase):
    """Test the kv usage shared-memory writer/reader round-trip."""

    @staticmethod
    def _make_shm():
        # macOS POSIX shm 名上限 31 字符，测试名保持短前缀 + 短随机后缀。
        name = f"tku_{time.monotonic_ns() % 1_000_000_000}"
        size = kv_usage.kv_usage_shm_total_size(16)
        shm = shared_memory.SharedMemory(name=name, create=True, size=size)
        return shm

    def test_writer_reader_round_trip(self):
        """Writer publishes a snapshot and the reader recovers it."""
        shm = self._make_shm()
        writer = kv_usage.KvUsageShmWriter(shm, max_entries=16)
        reader = None
        try:
            writer.write_snapshot({(1, 1): 0.3, (2, 1): 0.7, (3, 2): 0.9})

            reader = kv_usage.KvUsageShmReader(writer.shm_name)
            reader.attach()
            usages = reader.read_snapshot()
            self.assertEqual(usages, {(1, 1): 0.3, (2, 1): 0.7, (3, 2): 0.9})
            # Second read with an unchanged sequence returns {} (no new snapshot).
            self.assertEqual(reader.read_snapshot(), {})
        finally:
            if reader:
                reader.detach()
            writer.release()
            shm.close()
            shm.unlink()

    def test_writer_reader_update(self):
        """A newer snapshot is picked up by an already-attached reader."""
        shm = self._make_shm()
        writer = kv_usage.KvUsageShmWriter(shm, max_entries=16)
        reader = None
        try:
            writer.write_snapshot({(1, 1): 0.3})

            reader = kv_usage.KvUsageShmReader(writer.shm_name)
            reader.attach()
            self.assertEqual(reader.read_snapshot(), {(1, 1): 0.3})
            writer.write_snapshot({(1, 1): 0.3, (2, 1): 0.6})
            self.assertEqual(reader.read_snapshot(), {(1, 1): 0.3, (2, 1): 0.6})
        finally:
            if reader:
                reader.detach()
            writer.release()
            shm.close()
            shm.unlink()

    def test_reader_missing_shm_returns_empty(self):
        """A reader that cannot attach degrades to an empty snapshot."""
        reader = kv_usage.KvUsageShmReader("no_such_shm_name")
        self.assertEqual(reader.read_snapshot(), {})
        self.assertEqual(reader.shm_name, "no_such_shm_name")

    def test_publisher_writes_collects_to_shm(self):
        """KvUsageShmPublisher extracts kv usage from collects and writes it to the shm."""
        shm = self._make_shm()
        writer = kv_usage.KvUsageShmWriter(shm, max_entries=16)
        reader = None
        publisher = None
        try:
            # 一次 collects 含两个实例的 per-endpoint 指标（全量快照语义）。
            collects = {
                1: {
                    "role": "decode",
                    "endpoints": {
                        1: {
                            "metrics": [
                                Metric(
                                    name="vllm:kv_cache_usage_perc",
                                    help="KV cache usage.",
                                    type=MetricType.GAUGE,
                                    value=[0.55],
                                )
                            ],
                            "pod_ip": "10.0.0.1",
                        },
                    },
                },
                2: {
                    "role": "decode",
                    "endpoints": {
                        1: {
                            "metrics": [
                                Metric(
                                    name="vllm:kv_cache_usage_perc",
                                    help="KV cache usage.",
                                    type=MetricType.GAUGE,
                                    value=[0.85],
                                )
                            ],
                            "pod_ip": "10.0.0.2",
                        },
                    },
                },
            }
            publisher = kv_usage.KvUsageShmPublisher(lambda: writer.shm_name, max_entries=16)
            publisher.publish(collects)

            reader = kv_usage.KvUsageShmReader(writer.shm_name)
            reader.attach()
            usages = reader.read_snapshot()
            self.assertEqual(usages, {(1, 1): 0.55, (2, 1): 0.85})
        finally:
            if publisher:
                publisher._detach()
            if reader:
                reader.detach()
            writer.release()
            shm.close()
            shm.unlink()

    def test_publisher_no_shm_name_is_noop(self):
        """Without a shm name the publisher is a no-op (e.g. scheduler not ready)."""
        publisher = kv_usage.KvUsageShmPublisher(lambda: None, max_entries=16)
        publisher.publish(_make_collect(1, 1, usage=0.5))  # must not raise

    def test_reader_clamps_usage_and_drops_non_finite(self):
        """Out-of-range usage is clamped to [0, 1]; non-finite (torn) doubles are dropped."""
        shm = self._make_shm()
        writer = kv_usage.KvUsageShmWriter(shm, max_entries=16)
        reader = None
        try:
            # writer 不做 clamp（发布端信任采集值），由 reader 侧统一收敛。
            writer.write_snapshot({(1, 1): 1.5, (2, 1): -0.5})

            reader = kv_usage.KvUsageShmReader(writer.shm_name)
            reader.attach()
            self.assertEqual(reader.read_snapshot(), {(1, 1): 1.0, (2, 1): 0.0})

            # NaN 条目直接丢弃：changed=True 但数据为空（该 endpoint 无有效值）。
            writer.write_snapshot({(1, 1): float("nan")})
            changed, data = reader.read_snapshot_ex()
            self.assertTrue(changed)
            self.assertEqual(data, {})
        finally:
            if reader:
                reader.detach()
            writer.release()
            shm.close()
            shm.unlink()

    def test_reader_rejects_torn_header_and_recovers(self):
        """A torn header (entry_count > max_entries) is rejected without consuming the sequence."""
        shm = self._make_shm()
        writer = kv_usage.KvUsageShmWriter(shm, max_entries=16)
        reader = None
        try:
            writer.write_snapshot({(1, 1): 0.3})
            reader = kv_usage.KvUsageShmReader(writer.shm_name)
            reader.attach()
            self.assertEqual(reader.read_snapshot(), {(1, 1): 0.3})

            # 模拟撕裂 header：entry_count 超过 max_entries，序列号为新的偶数。
            # 直接用 shm.buf（临时 memoryview，语句结束即释放，避免占用导出导致 close 失败）。
            struct.pack_into(
                kv_usage._KV_USAGE_HEADER_FMT,
                shm.buf,
                0,
                kv_usage._KV_USAGE_MAGIC,
                kv_usage._KV_USAGE_SCHEMA_VERSION,
                0,
                100,  # 新的偶数序列号
                999,  # entry_count > max_entries=16 -> 撕裂
                16,
                0,
            )
            self.assertEqual(reader.read_snapshot(), {})
            self.assertEqual(reader.read_snapshot(), {})
            # 序列号未被消费：写者发布有效快照后仍能读到。
            writer.write_snapshot({(2, 1): 0.6})
            self.assertEqual(reader.read_snapshot(), {(2, 1): 0.6})
        finally:
            if reader:
                reader.detach()
            writer.release()
            shm.close()
            shm.unlink()

    def test_reader_rejects_odd_sequence(self):
        """An odd (mid-write) sequence is rejected like before the fix."""
        shm = self._make_shm()
        writer = kv_usage.KvUsageShmWriter(shm, max_entries=16)
        reader = None
        try:
            writer.write_snapshot({(1, 1): 0.3})
            reader = kv_usage.KvUsageShmReader(writer.shm_name)
            reader.attach()
            self.assertEqual(reader.read_snapshot(), {(1, 1): 0.3})

            # 模拟写者 mid-write：奇数序列号。
            struct.pack_into(
                kv_usage._KV_USAGE_HEADER_FMT,
                shm.buf,
                0,
                kv_usage._KV_USAGE_MAGIC,
                kv_usage._KV_USAGE_SCHEMA_VERSION,
                0,
                101,  # odd
                1,
                16,
                0,
            )
            self.assertEqual(reader.read_snapshot(), {})
        finally:
            if reader:
                reader.detach()
            writer.release()
            shm.close()
            shm.unlink()

    @unittest.skipIf(sys.platform == "win32", "POSIX shared memory only")
    def test_attached_reader_process_exit_does_not_unlink_segment(self):
        """Reader 进程退出（未 detach，模拟 worker 崩溃）不得 unlink SchedulerServer 拥有的段。

        CPython 把 create=False 的 attach 也注册进本进程 resource_tracker，退出时
        tracker 会 unlink 同名段；kv_usage attach 后必须 unregister（参见
        kv_usage._untrack_shared_memory）。
        """
        shm = self._make_shm()
        name = shm.name.lstrip("/")
        try:
            child = (
                "from motor.coordinator.scheduler.runtime import kv_usage;"
                f"r = kv_usage.KvUsageShmReader('{name}');"
                "r.attach()"
            )
            proc = subprocess.run(
                [sys.executable, "-c", child], capture_output=True, text=True, timeout=30, check=False
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("leaked shared_memory", proc.stderr)
            # 段仍可 attach：未被子进程退出时 unlink。
            probe = shared_memory.SharedMemory(name=name, create=False)
            kv_usage._untrack_shared_memory(probe)  # 探测 attach 不得污染本进程 tracker
            probe.close()
        finally:
            shm.close()
            shm.unlink()

    @unittest.skipIf(sys.platform == "win32", "POSIX shared memory only")
    def test_publisher_process_exit_does_not_unlink_segment(self):
        """Publisher（Obs 进程）退出同样不得 unlink SchedulerServer 拥有的段。"""
        shm = self._make_shm()
        name = shm.name.lstrip("/")
        try:
            child = (
                "from motor.coordinator.scheduler.runtime import kv_usage;"
                f"p = kv_usage.KvUsageShmPublisher(lambda: '{name}');"
                "p.publish(None)"
            )
            proc = subprocess.run(
                [sys.executable, "-c", child], capture_output=True, text=True, timeout=30, check=False
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("leaked shared_memory", proc.stderr)
            probe = shared_memory.SharedMemory(name=name, create=False)
            kv_usage._untrack_shared_memory(probe)
            probe.close()
        finally:
            shm.close()
            shm.unlink()


class TestKvUsageScoringPenalty(unittest.TestCase):
    """Test that KV cache usage folds into the endpoint score as a penalty.

    Penalty = kv_usage * instance_workload_score / endpoint_count, so a heavily used
    instance is ranked later instead of being hard-excluded. Missing usage data keeps
    the plain load-balance score.
    """

    def _make_scored(self, gathered_tokens=10.0):
        ep1 = create_mock_endpoint(endpoint_id=1, workload=create_mock_workload(active_tokens=5.0))
        ep2 = create_mock_endpoint(endpoint_id=2, workload=create_mock_workload(active_tokens=10.0))
        ep3 = create_mock_endpoint(endpoint_id=3, workload=create_mock_workload(active_tokens=1.0))
        inst1 = create_mock_instance(
            instance_id=1,
            endpoints={"pod1": {1: ep1}},
            gathered_workload=create_mock_workload(active_tokens=gathered_tokens),
        )
        inst2 = create_mock_instance(
            instance_id=2,
            endpoints={"pod2": {2: ep2}},
            gathered_workload=create_mock_workload(active_tokens=gathered_tokens),
        )
        inst3 = create_mock_instance(
            instance_id=3,
            endpoints={"pod3": {3: ep3}},
            gathered_workload=create_mock_workload(active_tokens=gathered_tokens),
        )
        return [inst1, inst2, inst3]

    def _usage_side_effect(self, usage_map):
        def _get(instance, endpoint):
            return usage_map.get((instance.id, endpoint.id))

        return _get

    def test_penalty_equals_kv_usage_times_instance_score_over_endpoint_count(self):
        """The penalty term is exactly kv_usage * instance_score / endpoint_count."""
        inst1, _, _ = self._make_scored(gathered_tokens=10.0)
        ep = inst1.get_all_endpoints()[0]
        base = LoadBalancePolicy.calculate_endpoint_score(inst1, ep, role=PDRole.ROLE_D)
        usage = 0.5
        scored = LoadBalancePolicy._apply_kv_usage_penalty(
            base,
            inst1,
            ep,
            role=PDRole.ROLE_D,
            kv_usage_provider=self._usage_side_effect({(1, 1): usage}),
        )
        # endpoint_count == 1, instance workload score == 10.0 -> penalty = 0.5 * 10 / 1
        self.assertAlmostEqual(scored, base + usage * 10.0 / 1)

    def test_penalty_normalized_by_endpoint_count(self):
        """A larger instance shares the KV pressure across more endpoints."""
        ep1 = create_mock_endpoint(endpoint_id=1, workload=create_mock_workload(active_tokens=5.0))
        ep2 = create_mock_endpoint(endpoint_id=2, workload=create_mock_workload(active_tokens=5.0))
        inst = create_mock_instance(
            instance_id=1,
            endpoints={"pod1": {1: ep1, 2: ep2}},
            gathered_workload=create_mock_workload(active_tokens=20.0),
        )
        base = LoadBalancePolicy.calculate_endpoint_score(inst, ep1, role=PDRole.ROLE_D)
        scored = LoadBalancePolicy._apply_kv_usage_penalty(
            base,
            inst,
            ep1,
            role=PDRole.ROLE_D,
            kv_usage_provider=self._usage_side_effect({(1, 1): 0.5}),
        )
        # 2 endpoints -> per-endpoint penalty = 0.5 * 20 / 2
        self.assertAlmostEqual(scored, base + 0.5 * 20.0 / 2)

    def test_high_kv_usage_ranked_later_among_equal_workloads(self):
        """With equal workload, the higher-usage endpoint is ranked later, not excluded."""
        inst1, inst2, inst3 = self._make_scored()
        usage_map = {(1, 1): 0.9, (2, 2): 0.3, (3, 3): 0.1}
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            [inst1, inst2, inst3],
            role=PDRole.ROLE_D,
            top_k=1,
            exclude_highest_kv_usage=True,
            kv_usage_provider=self._usage_side_effect(usage_map),
        )
        self.assertEqual(len(candidates), 1)
        # inst3 has the lowest workload score (1.0) and the lowest usage -> best pick.
        self.assertEqual(candidates[0].instance.id, 3)

    def test_penalty_can_override_workload_ordering(self):
        """A heavy KV usage penalty can outweigh a slightly better workload score."""
        inst1, inst2, _ = self._make_scored(gathered_tokens=20.0)
        # inst1: lowest workload (5.0) but nearly full KV cache; inst2: higher workload (10.0),
        # near-empty KV cache. The penalty flips the pick to inst2.
        usage_map = {(1, 1): 0.9, (2, 2): 0.05, (3, 3): 0.1}
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            [inst1, inst2],
            role=PDRole.ROLE_D,
            top_k=1,
            exclude_highest_kv_usage=True,
            kv_usage_provider=self._usage_side_effect(usage_map),
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].instance.id, 2)

    def test_no_penalty_when_usage_unavailable(self):
        """When no usage data is available, selection falls back to plain scoring."""
        inst1, inst2, inst3 = self._make_scored()
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            [inst1, inst2, inst3],
            role=PDRole.ROLE_D,
            top_k=1,
            exclude_highest_kv_usage=True,
            kv_usage_provider=self._usage_side_effect({}),
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].instance.id, 3)

    def test_no_penalty_without_provider(self):
        """Without a provider, plain load-balance ordering is kept."""
        inst1, inst2, inst3 = self._make_scored()
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            [inst1, inst2, inst3],
            role=PDRole.ROLE_D,
            top_k=1,
            exclude_highest_kv_usage=True,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].instance.id, 3)

    def test_no_penalty_when_flag_off(self):
        """With the flag off, KV usage is ignored even when a provider is given."""
        inst1, inst2, inst3 = self._make_scored()
        usage_map = {(1, 1): 0.9, (2, 2): 0.3, (3, 3): 0.5}
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            [inst1, inst2, inst3],
            role=PDRole.ROLE_D,
            top_k=1,
            exclude_highest_kv_usage=False,
            kv_usage_provider=self._usage_side_effect(usage_map),
        )
        # inst3 has the lowest workload score and is picked regardless of usage.
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].instance.id, 3)

    def test_single_candidate_selected_with_high_usage(self):
        """A single candidate is still selected, even with high usage."""
        inst1, _, _ = self._make_scored()
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            [inst1],
            role=PDRole.ROLE_D,
            top_k=1,
            exclude_highest_kv_usage=True,
            kv_usage_provider=self._usage_side_effect({(1, 1): 0.95}),
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].instance.id, 1)

    def test_provider_exception_degrades_to_plain_scoring(self):
        """An exception from the provider must not break selection."""
        inst1, inst2, inst3 = self._make_scored()

        def _boom(instance, endpoint):
            raise RuntimeError("metrics endpoint down")

        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            [inst1, inst2, inst3],
            role=PDRole.ROLE_D,
            top_k=1,
            exclude_highest_kv_usage=True,
            kv_usage_provider=_boom,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].instance.id, 3)

    def test_top_k_respected_with_penalty(self):
        """Penalty ranking still honors top_k over the remaining pool."""
        inst1, inst2, inst3 = self._make_scored()
        usage_map = {(1, 1): 0.9, (2, 2): 0.3, (3, 3): 0.5}
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            [inst1, inst2, inst3],
            role=PDRole.ROLE_D,
            top_k=2,
            exclude_highest_kv_usage=True,
            kv_usage_provider=self._usage_side_effect(usage_map),
        )
        ids = {c.instance.id for c in candidates}
        # inst1 carries the heaviest usage penalty and is ranked last.
        self.assertNotIn(1, ids)
        self.assertEqual(ids, {2, 3})


class TestAuthoritativePathKvUsagePenalty(unittest.TestCase):
    """Test that the authoritative (CAS re-rank) path applies the same D-role KV penalty.

    select_global_load_balance_candidate must mirror the fast path
    (_select_endpoint_candidates_by_load_balance): when the context carries a
    kv_usage_provider, D-role scoring gains the KV usage penalty; without a provider
    plain load-balance scoring is preserved.
    """

    def _make_scored(self, gathered_tokens=10.0):
        ep1 = create_mock_endpoint(endpoint_id=1, workload=create_mock_workload(active_tokens=5.0))
        ep2 = create_mock_endpoint(endpoint_id=2, workload=create_mock_workload(active_tokens=10.0))
        ep3 = create_mock_endpoint(endpoint_id=3, workload=create_mock_workload(active_tokens=1.0))
        inst1 = create_mock_instance(
            instance_id=1,
            endpoints={"pod1": {1: ep1}},
            gathered_workload=create_mock_workload(active_tokens=gathered_tokens),
        )
        inst2 = create_mock_instance(
            instance_id=2,
            endpoints={"pod2": {2: ep2}},
            gathered_workload=create_mock_workload(active_tokens=gathered_tokens),
        )
        inst3 = create_mock_instance(
            instance_id=3,
            endpoints={"pod3": {3: ep3}},
            gathered_workload=create_mock_workload(active_tokens=gathered_tokens),
        )
        return [inst1, inst2, inst3]

    @staticmethod
    def _make_ctx(instances, kv_usage_provider=None):
        return ArbitrationContext(
            get_available_instances=lambda role: {inst.id: inst for inst in instances},
            is_instance_circuit_open=lambda instance_id: False,
            endpoint_instance_score_weight=0.0,
            is_load_balance_scheduler=True,
            kv_usage_provider=kv_usage_provider,
        )

    @staticmethod
    def _usage_provider(usage_map):
        return lambda instance, endpoint: usage_map.get((instance.id, endpoint.id))

    def test_authoritative_d_role_applies_kv_usage_penalty(self):
        """With a provider, the D-role re-rank flips the pick away from the KV-heavy node."""
        instances = self._make_scored(gathered_tokens=20.0)[:2]
        # inst1: lowest workload (5.0) but nearly full KV; inst2: higher workload (10.0),
        # near-empty KV. Penalty flips the pick: inst1 -> 5+0.9*20=23, inst2 -> 10+0.05*20=11.
        usage_map = {(1, 1): 0.9, (2, 2): 0.05}
        ctx = self._make_ctx(instances, kv_usage_provider=self._usage_provider(usage_map))

        selected = select_global_load_balance_candidate(ctx, PDRole.ROLE_D)
        self.assertIsNotNone(selected)
        self.assertEqual(selected[0].id, 2)

    def test_authoritative_without_provider_keeps_plain_scoring(self):
        """Without a provider, the re-rank keeps plain load-balance scoring."""
        instances = self._make_scored()
        ctx = self._make_ctx(instances)

        selected = select_global_load_balance_candidate(ctx, PDRole.ROLE_D)
        self.assertIsNotNone(selected)
        self.assertEqual(selected[0].id, 3)

    def test_authoritative_p_role_ignores_kv_usage_penalty(self):
        """P-role re-rank never applies the penalty (fast-path gating is D-only)."""
        instances = self._make_scored(gathered_tokens=20.0)
        usage_map = {(1, 1): 0.9, (2, 2): 0.05, (3, 3): 0.1}
        ctx = self._make_ctx(instances, kv_usage_provider=self._usage_provider(usage_map))

        selected = select_global_load_balance_candidate(ctx, PDRole.ROLE_P)
        self.assertIsNotNone(selected)
        # Plain scoring: inst3 has the lowest workload (1.0).
        self.assertEqual(selected[0].id, 3)

    def test_authoritative_matches_fast_path_selection(self):
        """Fast path and authoritative path rank identically for D instances."""
        instances = self._make_scored(gathered_tokens=20.0)
        usage_map = {(1, 1): 0.9, (2, 2): 0.05, (3, 3): 0.1}
        provider = self._usage_provider(usage_map)

        fast = LoadBalancePolicy.select_endpoint_candidates_from_list(
            instances,
            role=PDRole.ROLE_D,
            top_k=1,
            instance_score_weight=0.0,
            exclude_highest_kv_usage=True,
            kv_usage_provider=provider,
        )
        ctx = self._make_ctx(instances, kv_usage_provider=provider)
        authoritative = select_global_load_balance_candidate(ctx, PDRole.ROLE_D)

        self.assertIsNotNone(fast)
        self.assertIsNotNone(authoritative)
        self.assertEqual(fast[0].instance.id, authoritative[0].id)
        self.assertEqual(fast[0].endpoint.id, authoritative[1].id)


if __name__ == "__main__":
    unittest.main()
