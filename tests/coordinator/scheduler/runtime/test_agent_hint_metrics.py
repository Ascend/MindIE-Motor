# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for the cross-process KVC metrics shm bridge."""

import uuid

from motor.coordinator.scheduler.runtime.agent_hint_metrics import (
    COUNTER_KEYS,
    KvcMetricsShmReader,
    KvcMetricsShmWriter,
    kvc_metrics_to_motor_metrics,
)


def _shm_name() -> str:
    return "motor_kvc_test_%s" % uuid.uuid4().hex[:12]


def test_writer_reader_roundtrip():
    name = _shm_name()
    writer = KvcMetricsShmWriter(name)
    reader = KvcMetricsShmReader(name)
    try:
        writer.update({"evict_total": 3, "offload_total": 2, "prefetch_total": 1})
        assert reader.attach() is True
        stats = reader.read()
        assert stats is not None
        assert stats["evict_total"] == 3
        assert stats["offload_total"] == 2
        assert stats["prefetch_total"] == 1
        assert stats["edit_fail"] == 0
    finally:
        reader.detach()
        writer.close()


def test_reader_missing_shm_returns_false():
    reader = KvcMetricsShmReader(_shm_name())
    assert reader.attach() is False
    assert reader.read() is None


def test_conversion_emits_every_counter_as_prometheus_counter():
    stats = {key: index for index, key in enumerate(COUNTER_KEYS)}
    metrics = kvc_metrics_to_motor_metrics(stats)

    assert len(metrics) == len(COUNTER_KEYS)
    names = {metric.name for metric in metrics}
    assert "motor:kvc_evict_total" in names
    assert "motor:kvc_offload_total" in names
    assert "motor:kvc_prefetch_total" in names
    assert "motor:kvc_edit_fail_total" in names
    assert all(str(metric.type) == "counter" for metric in metrics)
    assert all(metric.value for metric in metrics)


def test_conversion_labels_include_metric_name_for_prometheus_samples():
    stats = {"offload_total": 3}
    metrics = kvc_metrics_to_motor_metrics(stats, worker_index=2)

    offload = next(metric for metric in metrics if metric.name == "motor:kvc_offload_total")
    assert offload.label == ['motor:kvc_offload_total{worker="2"}']


def test_central_scope_labels_include_metric_name_for_prometheus_samples():
    metrics = kvc_metrics_to_motor_metrics({"offload_total": 3}, scope="central")

    offload = next(metric for metric in metrics if metric.name == "motor:kvc_offload_total")
    assert offload.label == ['motor:kvc_offload_total{scope="central"}']
