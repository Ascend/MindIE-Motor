# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Unit tests for the HPA autoscaling metrics:
  - CapacityPlanner pipeline metrics (metric_computer): motor:*_utilization,
    motor:*_replicas_required, motor:*_capacity_tps, motor:*_demand_tps,
    motor:kv_demand_tokens, motor:capacity_calibrated
  - motor:request_rate (counter_rate registration)
  - motor:pd_ratio_current / motor:pd_ratio_suggested (planner-raw driven)
  - vllm:request_prefill_time_seconds quantiles (registry + aggregation engine)
  - colon-free HPA aliases (hpa_contract + prometheus rendering)
"""

import pytest
from types import SimpleNamespace
from unittest.mock import patch

from motor.coordinator.metrics.aggregation_engine import SemanticAggregationEngine
from motor.coordinator.metrics.hpa_contract import get_hpa_alias
from motor.coordinator.metrics.metric_computer import (
    MotorMetricComputer,
    _parse_kv_tokens_per_instance,
)
from motor.coordinator.metrics.metric_registry import MetricRegistry, MetricSemantic
from motor.coordinator.metrics.metric_types import Metric, MetricType
from motor.coordinator.metrics.metrics_collector import MetricsCollector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gauge(name, value, label=None):
    return Metric(
        name=name,
        help=name,
        type=MetricType.GAUGE,
        label=[label or name],
        value=[value],
    )


def _counter(name, value):
    return Metric(name=name, help=name, type=MetricType.COUNTER, label=[name], value=[value])


def _make_config(**overrides):
    """Minimal stand-in for PrometheusMetricsConfig.

    Defaults match production, except ``ema_alpha`` pinned at 0.3 (production
    default 0.85) to keep the EMA 数值链 of these mechanism tests stable.
    """
    capacity_planning = {
        "target_utilization": 0.8,
        "kv_target_utilization": 0.9,
        "ema_alpha": 0.3,
        "capacity_decay": 0.005,
        "prefill_tps_capacity_prior": 5000.0,
        "decode_tps_capacity_prior": 2000.0,
        "pd_ratio_smooth_alpha": 0.3,
        "pd_ratio_min": 0.05,
        "pd_ratio_max": 20.0,
    }
    capacity_planning.update(overrides)
    return SimpleNamespace(capacity_planning=SimpleNamespace(**capacity_planning))


def _make_collects(instances):
    """instances: list of (ins_id, instance_dict)."""
    return dict(instances)


def _run_post_aggregation(collects, config=None, computer=None):
    computer = computer or MotorMetricComputer()
    aggregate = []
    deploy_config = SimpleNamespace(p_instances_num=1, d_instances_num=1)
    computer.compute_post_aggregation(aggregate, collects, deploy_config, config or _make_config())
    return computer, {m.name: m for m in aggregate}


def _histogram_metric(name, buckets, count, total_sum):
    labels = [f'{name}_bucket{{le="{le}"}}' for le, _ in buckets]
    values = [v for _, v in buckets]
    labels += [f"{name}_count", f"{name}_sum"]
    values += [count, total_sum]
    return Metric(name=name, help=name, type=MetricType.HISTOGRAM, label=labels, value=values)


def _planner_instance(
    role,
    req_total,
    tokens_total,
    ins_id,
    prefill_time=None,
    queue_time=None,
    decode_time=None,
    cache_config_label=None,
):
    """One instance carrying the cumulative counters the planner consumes.

    prefill_time / queue_time / decode_time are (sum, count) pairs rendered as
    histogram metrics (``_sum`` / ``_count`` label rows).
    """
    tokens_name = "vllm:prompt_tokens_total" if role == "prefill" else "vllm:generation_tokens_total"
    metrics = [
        _counter("vllm:request_success_total", req_total),
        _counter(tokens_name, tokens_total),
    ]
    if prefill_time is not None:
        metrics.append(
            _histogram_metric(
                "vllm:request_prefill_time_seconds",
                buckets=[("1.0", prefill_time[1]), ("+Inf", prefill_time[1])],
                count=prefill_time[1],
                total_sum=prefill_time[0],
            )
        )
    if queue_time is not None:
        metrics.append(
            _histogram_metric(
                "vllm:request_queue_time_seconds",
                buckets=[("1.0", queue_time[1]), ("+Inf", queue_time[1])],
                count=queue_time[1],
                total_sum=queue_time[0],
            )
        )
    if decode_time is not None:
        metrics.append(
            _histogram_metric(
                "vllm:request_decode_time_seconds",
                buckets=[("1.0", decode_time[1]), ("+Inf", decode_time[1])],
                count=decode_time[1],
                total_sum=decode_time[0],
            )
        )
    if cache_config_label is not None:
        metrics.append(_gauge("vllm:cache_config_info", 1.0, label=cache_config_label))
    return {
        "role": role,
        "job_name": f"{role}-{ins_id}",
        "endpoints": {0: {"metrics": metrics}},
    }


_CACHE_CONFIG_LABEL = (
    'vllm:cache_config_info{block_size="128",enable_prefix_caching="True",num_gpu_blocks="1000",engine="0"}'
)


def _two_cycle_collects():
    """Two consecutive collects snapshots with growing cumulative counters."""
    cycle1 = _make_collects(
        [
            (
                1,
                _planner_instance("prefill", 1000.0, 500000.0, 1, prefill_time=(200.0, 1000.0)),
            ),
            (
                2,
                _planner_instance(
                    "decode",
                    1000.0,
                    300000.0,
                    2,
                    queue_time=(50.0, 1000.0),
                    decode_time=(100.0, 1000.0),
                    cache_config_label=_CACHE_CONFIG_LABEL,
                ),
            ),
        ]
    )
    cycle2 = _make_collects(
        [
            (
                1,
                _planner_instance("prefill", 1010.0, 510000.0, 1, prefill_time=(204.0, 1010.0)),
            ),
            (
                2,
                _planner_instance(
                    "decode",
                    1010.0,
                    306000.0,
                    2,
                    queue_time=(50.5, 1010.0),
                    decode_time=(101.0, 1010.0),
                    cache_config_label=_CACHE_CONFIG_LABEL,
                ),
            ),
        ]
    )
    return [cycle1, cycle2]


def _run_cycles(computer, collects_cycles, config):
    """Run the collector flow per cycle: pre-aggregation → planner feed → post."""
    out = {}
    for collects in collects_cycles:
        computer.compute_pre_aggregation(collects)
        computer.update_planner(collects, config)
        computer, out = _run_post_aggregation(collects, config, computer)
    return out


# ---------------------------------------------------------------------------
# Capacity planner pipeline
# ---------------------------------------------------------------------------


class TestPlannerPipeline:
    def test_full_pipeline_emits_planner_metrics(self):
        """collects → compute_post_aggregation → aggregate 含新指标。"""
        computer = MotorMetricComputer()
        cfg = _make_config()
        out = _run_cycles(computer, _two_cycle_collects(), cfg)
        names = set(out)
        assert "motor:prefill_utilization" in names
        assert "motor:decode_utilization" in names
        assert "motor:prefill_replicas_required" in names
        assert "motor:decode_replicas_required" in names
        assert "motor:prefill_capacity_tps" in names
        assert "motor:decode_capacity_tps" in names
        assert "motor:prefill_demand_tps" in names
        assert "motor:decode_demand_tps" in names
        assert "motor:kv_demand_tokens" in names
        assert "motor:capacity_calibrated" in names
        assert "motor:prefill_saturation" not in names
        assert "motor:decode_saturation" not in names

    def test_cache_config_info_kv_parsing(self):
        """vllm:cache_config_info label 中的 num_gpu_blocks/block_size 被解析为 K。"""
        label = _CACHE_CONFIG_LABEL + " 1"
        assert _parse_kv_tokens_per_instance(label) == 128000.0
        assert _parse_kv_tokens_per_instance("vllm:cache_config_info 1") == 0.0

    def test_capacity_calibrated_labeled_by_role(self):
        computer = MotorMetricComputer()
        out = _run_cycles(computer, _two_cycle_collects(), _make_config())
        calibrated = out["motor:capacity_calibrated"]
        labels = set(calibrated.label)
        assert 'motor:capacity_calibrated{role="prefill"}' in labels
        assert 'motor:capacity_calibrated{role="decode"}' in labels
        assert calibrated.value == [1.0, 1.0]

    def test_role_absent_emits_no_role_specific_keys(self):
        """decode 从未出现且无 prior → decode 角色键不出现；replicas 始终输出。"""
        computer = MotorMetricComputer()
        cfg = _make_config(prefill_tps_capacity_prior=0.0, decode_tps_capacity_prior=0.0)
        cycles = [
            _make_collects(
                [
                    (
                        1,
                        _planner_instance("prefill", req, tokens, 1, prefill_time=(req * 0.2, req)),
                    )
                ]
            )
            for req, tokens in [(1000.0, 500000.0), (1010.0, 510000.0)]
        ]
        out = _run_cycles(computer, cycles, cfg)
        names = set(out)
        assert "motor:prefill_demand_tps" in names
        assert "motor:decode_demand_tps" not in names
        assert "motor:decode_capacity_tps" not in names
        assert "motor:decode_utilization" not in names
        assert "motor:kv_demand_tokens" not in names  # K unknown (no cache_config_info)
        assert "motor:prefill_replicas_required" in names
        assert "motor:decode_replicas_required" in names

    def test_kv_info_missing_warns_once_and_kv_constraint_skipped(self):
        """decode 有实例但无 cache_config_info → 恰好记一次 WARNING，
        decode_replicas_required 仍按吞吐约束输出；K 恢复后标志位重置。
        """
        computer = MotorMetricComputer()
        cfg = _make_config()
        without_k = [
            _make_collects(
                [
                    (
                        2,
                        _planner_instance(
                            "decode",
                            req,
                            tokens,
                            2,
                            queue_time=(req * 0.05, req),
                            decode_time=(req * 0.1, req),
                        ),
                    )
                ]
            )
            for req, tokens in [(1000.0, 300000.0), (1010.0, 306000.0)]
        ]
        with_k = _make_collects(
            [
                (
                    2,
                    _planner_instance(
                        "decode",
                        1020.0,
                        312000.0,
                        2,
                        queue_time=(51.0, 1020.0),
                        decode_time=(102.0, 1020.0),
                        cache_config_label=_CACHE_CONFIG_LABEL,
                    ),
                )
            ]
        )
        with patch("motor.coordinator.metrics.metric_computer.logger") as mock_logger:
            # 两个采集周期都缺 K：WARNING 恰好一次
            out = _run_cycles(computer, without_k, cfg)
            assert mock_logger.warning.call_count == 1
            # K 变为可用：不记 WARNING，且标志位重置
            _run_cycles(computer, [with_k], cfg)
            assert mock_logger.warning.call_count == 1
            # K 再次缺失：重新记一次
            _run_cycles(computer, without_k[:1], cfg)
            assert mock_logger.warning.call_count == 2
        # KV 约束跳过：kv_demand_tokens 不出现，replicas 仍按吞吐约束输出
        assert "motor:kv_demand_tokens" not in out
        assert "motor:decode_replicas_required" in out
        assert out["motor:decode_replicas_required"].value[0] >= 1.0

    def test_histogram_rows_pass_through_and_planner_holds_on_restart(self):
        """histogram _sum/_count 行直通（不做 baseline 修正）；重启/缩容造成的
        负 delta 由 planner 的 hold-on-bad-delta 语义吸收（capacity 保持 prior）。
        """
        computer = MotorMetricComputer()
        cfg = _make_config()
        cycle1 = _make_collects(
            [
                (
                    1,
                    _planner_instance("prefill", 100.0, 5000.0, 1, prefill_time=(100.0, 10.0)),
                )
            ]
        )
        # same job_name, new instance with reset counters → engine restart
        cycle2 = _make_collects(
            [
                (
                    2,
                    _planner_instance("prefill", 10.0, 500.0, 1, prefill_time=(10.0, 2.0)),
                )
            ]
        )
        for collects in (cycle1, cycle2):
            computer.compute_pre_aggregation(collects)
            computer.update_planner(collects, cfg)
        histogram = cycle2[2]["endpoints"][0]["metrics"][2]
        row = {lbl.split("{")[0]: v for lbl, v in zip(histogram.label, histogram.value)}
        # raw values pass through untouched (no baseline carry-over)
        assert row["vllm:request_prefill_time_seconds_sum"] == pytest.approx(10.0)
        assert row["vllm:request_prefill_time_seconds_count"] == pytest.approx(2.0)
        # negative deltas are clamped by the planner: capacity holds the prior
        _, out = _run_post_aggregation(cycle2, cfg, computer)
        assert out["motor:prefill_capacity_tps"].value == [5000.0]

    def test_planner_rebuilt_when_capacity_planning_config_changes(self):
        computer = MotorMetricComputer()
        collects = _two_cycle_collects()
        _run_cycles(computer, collects[:1], _make_config(prefill_tps_capacity_prior=5000.0))
        out = _run_cycles(computer, collects[1:], _make_config(prefill_tps_capacity_prior=9000.0))
        # rebuilt planner: capacity restarts from the new prior, no stale EMA blend
        assert out["motor:prefill_capacity_tps"].value == [9000.0]


# ---------------------------------------------------------------------------
# PD ratio
# ---------------------------------------------------------------------------


class TestPdRatio:
    def test_current_ratio(self):
        collects = _make_collects(
            [(100 + i, {"role": "prefill", "endpoints": {0: {"metrics": []}}}) for i in range(2)]
            + [(200 + i, {"role": "decode", "endpoints": {0: {"metrics": []}}}) for i in range(4)]
        )
        _, out = _run_post_aggregation(collects)
        assert out["motor:pd_ratio_current"].value == [0.5]

    def test_current_ratio_no_decode(self):
        collects = _make_collects([(100 + i, {"role": "prefill", "endpoints": {0: {"metrics": []}}}) for i in range(3)])
        _, out = _run_post_aggregation(collects)
        assert out["motor:pd_ratio_current"].value == [3.0]

    def test_suggested_uses_planner_raw_smoothed_and_clamped(self):
        """pd_ratio_suggested = EMA(clamp(planner pd_ratio_required_raw))。"""
        computer = MotorMetricComputer()
        cfg = _make_config()
        cycle1, cycle2 = _two_cycle_collects()
        # prefill 需求暴涨、decode 正常增长；sticky-K 语义下需从 cycle1 起就
        # 不暴露 cache_config_info，KV 约束才真正缺席（否则 N_d 被暴涨的
        # L̄_in 经 KV 约束主导，raw 反而趋 0）。
        # 此时 dt 在 N_p/N_d 中相消：N_d = ceil(1/0.8) = 2，raw = N_p/2 ≥ 20。
        cycle2[1]["endpoints"][0]["metrics"][1].value = [500000.0 + 1e12]
        cycle1[2]["endpoints"][0]["metrics"].pop()
        cycle2[2]["endpoints"][0]["metrics"].pop()
        out = _run_cycles(computer, [cycle1, cycle2], cfg)
        # cycle1: decode_required = 0（无需求样本）→ raw 键缺省 → 回退现状比 1.0，
        #         EMA 初始化 1.0
        # cycle2: raw ≥ 20 → clamp 20 → smoothed = 1.0 + 0.3 * (20 - 1.0) = 6.7
        suggested = out["motor:pd_ratio_suggested"].value[0]
        assert suggested == pytest.approx(6.7)
        assert out["motor:pd_ratio_current"].value == [1.0]

    def test_suggested_first_cycle_falls_back_to_current_ratio(self):
        """首轮 planner raw 键缺省（无需求样本）→ suggested 回退现状比。"""
        computer = MotorMetricComputer()
        cycle1 = _two_cycle_collects()[0]
        out = _run_cycles(computer, [cycle1], _make_config())
        assert out["motor:pd_ratio_suggested"].value == [1.0]

    def test_planner_rebuild_resets_pd_ratio_ema(self):
        """capacity_planning config 变化 → planner 重建 → suggested EMA 不串旧值。"""
        computer = MotorMetricComputer()
        cfg = _make_config()
        cycle1, cycle2 = _two_cycle_collects()
        # prefill 需求暴涨、decode 正常增长；sticky-K 语义下需从 cycle1 起就不
        # 暴露 cache_config_info（两个 cycle 都去掉）才能跳过 KV 约束
        # → raw 超上限 20 → smoothed 走到 6.7
        cycle2[1]["endpoints"][0]["metrics"][1].value = [500000.0 + 1e12]
        cycle1[2]["endpoints"][0]["metrics"].pop()
        cycle2[2]["endpoints"][0]["metrics"].pop()
        out = _run_cycles(computer, [cycle1, cycle2], cfg)
        assert out["motor:pd_ratio_suggested"].value[0] == pytest.approx(6.7)
        # config 变化 → planner 重建 + EMA 重置；重建后首周期 raw 键缺省 → 回退现状比 1.0。
        # 若 EMA 未重置，结果会是 6.7 + 0.3*(1.0-6.7) ≈ 3.99。
        cycle3 = _two_cycle_collects()[0]
        out = _run_cycles(computer, [cycle3], _make_config(prefill_tps_capacity_prior=9000.0))
        assert out["motor:pd_ratio_suggested"].value == [1.0]


# ---------------------------------------------------------------------------
# Request rate registration
# ---------------------------------------------------------------------------


class TestRequestRate:
    def test_counter_rate_injected_per_endpoint(self):
        computer = MotorMetricComputer()
        collects = _make_collects(
            [
                (
                    1,
                    {
                        "role": "decode",
                        "job_name": "job0",
                        "endpoints": {
                            0: {
                                "metrics": [
                                    Metric(
                                        name="vllm:request_success_total",
                                        help="",
                                        type=MetricType.COUNTER,
                                        label=["vllm:request_success_total"],
                                        value=[100.0],
                                    ),
                                ]
                            }
                        },
                    },
                ),
            ]
        )
        # first pass establishes baseline
        computer.compute_pre_aggregation(collects)
        metrics = collects[1]["endpoints"][0]["metrics"]
        assert any(m.name == "motor:request_rate" for m in metrics)

    def test_request_rate_registered_as_metadata_gauge(self):
        config = MetricRegistry.get_semantic("motor:request_rate")
        assert config is not None
        assert config.semantic == MetricSemantic.METADATA_GAUGE


# ---------------------------------------------------------------------------
# Prefill quantiles
# ---------------------------------------------------------------------------


class TestPrefillQuantiles:
    def test_registry_has_quantiles_and_prefill_scope(self):
        config = MetricRegistry.get_semantic("vllm:request_prefill_time_seconds")
        assert config.metadata["quantiles"] == [0.5, 0.95, 0.99]
        assert config.role_scope == "prefill"

    def test_quantiles_computed_from_histogram(self):
        engine = SemanticAggregationEngine()
        histogram = _histogram_metric(
            "vllm:request_prefill_time_seconds",
            buckets=[("0.1", 10), ("0.5", 40), ("1.0", 50), ("+Inf", 50)],
            count=50,
            total_sum=15.0,
        )
        result = engine.post_process([histogram])
        names = {m.name for m in result}
        assert "vllm:request_prefill_time_seconds_p50" in names
        assert "vllm:request_prefill_time_seconds_p95" in names
        assert "vllm:request_prefill_time_seconds_p99" in names
        assert "vllm:request_prefill_time_seconds_mean" in names


# ---------------------------------------------------------------------------
# HPA aliases
# ---------------------------------------------------------------------------


class TestHpaAlias:
    def test_alias_for_contract_metrics(self):
        assert get_hpa_alias("motor:prefill_utilization") == "motor_prefill_utilization"
        assert get_hpa_alias("motor:decode_utilization") == "motor_decode_utilization"
        assert get_hpa_alias("motor:prefill_replicas_required") == "motor_prefill_replicas_required"
        assert get_hpa_alias("motor:decode_replicas_required") == "motor_decode_replicas_required"
        assert get_hpa_alias("motor:request_rate") == "motor_request_rate"
        assert get_hpa_alias("motor:pd_ratio_current") == "motor_pd_ratio_current"
        assert get_hpa_alias("motor:pd_ratio_suggested") == "motor_pd_ratio_suggested"
        assert get_hpa_alias("vllm:request_prefill_time_seconds") == "vllm_request_prefill_time_seconds"

    def test_alias_for_prefill_quantile_gauges(self):
        assert get_hpa_alias("vllm:request_prefill_time_seconds_p95") == "vllm_request_prefill_time_seconds_p95"
        assert get_hpa_alias("vllm:request_prefill_time_seconds_mean") == "vllm_request_prefill_time_seconds_mean"

    def test_no_alias_for_non_contract_metrics(self):
        assert get_hpa_alias("vllm:num_requests_running") is None
        assert get_hpa_alias("motor:active_prefill_workers") is None
        assert get_hpa_alias("motor:prompt_tokens_per_second") is None

    def test_no_alias_for_removed_saturation_metrics(self):
        assert get_hpa_alias("motor:prefill_saturation") is None
        assert get_hpa_alias("motor:decode_saturation") is None

    def test_no_alias_for_diagnostic_metrics(self):
        assert get_hpa_alias("motor:prefill_capacity_tps") is None
        assert get_hpa_alias("motor:decode_capacity_tps") is None
        assert get_hpa_alias("motor:prefill_demand_tps") is None
        assert get_hpa_alias("motor:decode_demand_tps") is None
        assert get_hpa_alias("motor:kv_demand_tokens") is None
        assert get_hpa_alias("motor:capacity_calibrated") is None

    def _format(self, aggregate):
        collector = MetricsCollector.__new__(MetricsCollector)
        return collector._format_prometheus(aggregate)

    def test_alias_emitted_with_same_value(self):
        text = self._format([_gauge("motor:prefill_utilization", 0.8)])
        assert "motor:prefill_utilization 0.8" in text
        assert "motor_prefill_utilization 0.8" in text
        assert "# HELP motor_prefill_utilization motor:prefill_utilization (HPA alias)" in text

    def test_alias_carries_labels(self):
        metric = Metric(
            name="vllm:request_prefill_time_seconds_p95",
            help="p95",
            type=MetricType.GAUGE,
            label=['vllm:request_prefill_time_seconds_p95{quantile="0.95"}'],
            value=[0.42],
        )
        text = self._format([metric])
        assert 'vllm_request_prefill_time_seconds_p95{quantile="0.95"} 0.42' in text

    def test_non_contract_metric_has_no_alias_series(self):
        text = self._format([_gauge("vllm:num_requests_running", 3.0)])
        assert "vllm:num_requests_running 3.0" in text
        assert "vllm_num_requests_running" not in text

    def test_alias_flows_into_opentelemetry_format(self):
        collector = MetricsCollector.__new__(MetricsCollector)
        text = collector._format_prometheus([_gauge("motor:decode_utilization", 1.5)])
        otel = collector._format_opentelemetry(text)
        names = {m["name"] for m in otel["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
        assert "motor:decode_utilization" in names
        assert "motor_decode_utilization" in names


# ---------------------------------------------------------------------------
# Role-view utilization
# ---------------------------------------------------------------------------


class TestRoleViewUtilization:
    def test_compute_role_utilization_per_role(self):
        computer = MotorMetricComputer()
        collects_cycles = _two_cycle_collects()
        _run_cycles(computer, collects_cycles, _make_config())
        prefill = computer.compute_role_utilization(collects_cycles[-1], "prefill")
        decode = computer.compute_role_utilization(collects_cycles[-1], "decode")
        assert [m.name for m in prefill] == ["motor:prefill_utilization"]
        assert prefill[0].value[0] > 0.0
        assert [m.name for m in decode] == ["motor:decode_utilization"]
        assert decode[0].value[0] > 0.0

    def test_compute_role_utilization_unknown_role(self):
        computer = MotorMetricComputer()
        collects = _two_cycle_collects()[0]
        assert computer.compute_role_utilization(collects, "union") == []

    def test_compute_role_utilization_empty_before_planner_update(self):
        computer = MotorMetricComputer()
        collects = _two_cycle_collects()[0]
        assert computer.compute_role_utilization(collects, "prefill") == []

    def test_role_view_utilization_updates_without_full_view(self):
        """只抓 role 视图（不生成 full 视图）时 utilization 仍随采集推进更新。"""
        computer = MotorMetricComputer()
        cfg = _make_config()
        collects = None
        for collects in _two_cycle_collects():
            # 模拟两次 _collect_metrics：pre-aggregation + planner 喂数，
            # 全程不调用 compute_post_aggregation（full 视图从未生成）
            computer.compute_pre_aggregation(collects)
            computer.update_planner(collects, cfg)
        prefill = computer.compute_role_utilization(collects, "prefill")
        decode = computer.compute_role_utilization(collects, "decode")
        assert [m.name for m in prefill] == ["motor:prefill_utilization"]
        assert prefill[0].value[0] > 0.0
        assert [m.name for m in decode] == ["motor:decode_utilization"]
        assert decode[0].value[0] > 0.0


class TestRoleViewRace:
    def test_role_utilization_survives_output_rebind_between_reads(self):
        """采集线程在 contains 与取值之间整体替换 _planner_output 时不得抛 KeyError。"""

        class RacyDict(dict):
            """Simulates the collection thread rebinding _planner_output mid-method."""

            def __init__(self, *args, computer, **kwargs):
                super().__init__(*args, **kwargs)
                self._computer = computer

            def __contains__(self, key):
                # 模拟配置热更新重建 planner：先 _planner_output = {} 再重算
                self._computer._planner_output = {}
                return super().__contains__(key)

        computer = MotorMetricComputer()
        computer._planner_output = RacyDict({"prefill_utilization": 0.7}, computer=computer)
        result = computer.compute_role_utilization({}, "prefill")
        assert len(result) == 1
        assert result[0].value == [0.7]
