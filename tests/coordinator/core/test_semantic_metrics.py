# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Unit tests for semantic metrics modules:
  - metric_registry.py
  - aggregation_engine.py
  - Integration with metrics_collector.py
"""

import math
from unittest.mock import patch, MagicMock

from motor.common.resources.dispatch import DispatchPlan
from motor.coordinator.metrics.metric_types import (
    AggregationContext,
    AggregationScope,
    Metric,
    MetricType,
)
from motor.coordinator.metrics.metric_registry import (
    MetricSemantic,
    MetricSemanticConfig,
    RatioPair,
    MetricRegistry,
)
from motor.coordinator.metrics.aggregation_engine import SemanticAggregationEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metric(name, mtype, labels, values, help_str=""):
    """Shorthand to create a Metric."""
    return Metric(name=name, help=help_str or name, type=mtype, label=labels, value=values)


def _check_metric_equal(a: Metric, b: Metric) -> bool:
    """Compare two Metric objects for equality."""
    if a.name != b.name or a.type != b.type:
        return False
    if a.label != b.label:
        return False
    if len(a.value) != len(b.value):
        return False
    for i, (av, bv) in enumerate(zip(a.value, b.value)):
        if math.isnan(av) and math.isnan(bv):
            continue
        if abs(av - bv) > 0.001:
            return False
    return True


# ---------------------------------------------------------------------------
# metric_semantic tests
# ---------------------------------------------------------------------------


class TestMetricSemantic:
    def test_semantic_enum_values(self):
        assert MetricSemantic.COUNTER.value == "counter"
        assert MetricSemantic.HISTOGRAM_LATENCY.value == "histogram_latency"
        assert MetricSemantic.QUEUE_GAUGE.value == "queue_gauge"
        assert MetricSemantic.SLA_METRIC.value == "sla_metric"

    def test_semantic_config_defaults(self):
        config = MetricSemanticConfig(semantic=MetricSemantic.COUNTER)
        assert config.semantic == MetricSemantic.COUNTER
        assert config.metadata == {}

    def test_semantic_config_with_metadata(self):
        config = MetricSemanticConfig(
            semantic=MetricSemantic.SLA_METRIC,
            metadata={"quantiles": [0.5, 0.99]},
        )
        assert config.metadata["quantiles"] == [0.5, 0.99]

    def test_ratio_pair(self):
        rp = RatioPair(
            name="vllm:cache_hit_rate",
            help="Cache hit rate",
            numerator="vllm:cache_hits_total",
            denominator="vllm:cache_queries_total",
        )
        assert rp.name == "vllm:cache_hit_rate"
        assert rp.numerator == "vllm:cache_hits_total"


# ---------------------------------------------------------------------------
# metric_registry tests
# ---------------------------------------------------------------------------


class TestMetricRegistry:
    def test_known_metric_returns_config(self):
        config = MetricRegistry.get_semantic("vllm:num_requests_running")
        assert config.semantic == MetricSemantic.STATE_GAUGE

    def test_kv_cache_usage_is_cache_metric(self):
        config = MetricRegistry.get_semantic("vllm:kv_cache_usage_perc")
        assert config.semantic == MetricSemantic.CACHE_METRIC

    def test_histogram_latency_metrics(self):
        for name in [
            "vllm:e2e_request_latency_seconds",
            "vllm:request_queue_time_seconds",
        ]:
            config = MetricRegistry.get_semantic(name)
            assert config.semantic == MetricSemantic.HISTOGRAM_LATENCY

    def test_sla_metrics_are_sla_semantic(self):
        ttft = MetricRegistry.get_semantic("vllm:time_to_first_token_seconds")
        assert ttft.semantic == MetricSemantic.SLA_METRIC

        tpot = MetricRegistry.get_semantic("vllm:time_per_output_token_seconds")
        assert tpot.semantic == MetricSemantic.SLA_METRIC

    def test_prefix_cache_ratio_numerator(self):
        config = MetricRegistry.get_semantic("vllm:prefix_cache_queries_total")
        assert config.semantic == MetricSemantic.RATIO_DENOMINATOR

        config = MetricRegistry.get_semantic("vllm:prefix_cache_hits_total")
        assert config.semantic == MetricSemantic.RATIO_NUMERATOR

    def test_created_metric_is_metadata(self):
        config = MetricRegistry.get_semantic("vllm:request_success_created")
        assert config.semantic == MetricSemantic.METADATA_GAUGE

    def test_is_created_metric(self):
        assert MetricRegistry.is_created_metric("vllm:foo_created")
        assert not MetricRegistry.is_created_metric("vllm:foo_total")
        assert not MetricRegistry.is_created_metric("vllm:foo")

    def test_unknown_metric_returns_none(self):
        config = MetricRegistry.get_semantic("some_unknown_metric")
        assert config is None

    def test_ratio_pairs(self):
        pairs = MetricRegistry.get_ratio_pairs()
        names = [rp.name for rp in pairs]
        assert "vllm:prefix_cache_hit_rate" in names

    def test_runtime_register(self):
        MetricRegistry.register(
            "test:custom_metric", MetricSemanticConfig(semantic=MetricSemantic.HOTSPOT_RESOURCE_GAUGE)
        )
        config = MetricRegistry.get_semantic("test:custom_metric")
        assert config.semantic == MetricSemantic.HOTSPOT_RESOURCE_GAUGE

    def test_coordinator_metrics_are_metadata(self):
        for name in [
            "motor:active_prefill_workers",
            "motor:active_decode_workers",
            "motor:inactive_prefill_workers",
            "motor:inactive_decode_workers",
        ]:
            config = MetricRegistry.get_semantic(name)
            assert config.semantic == MetricSemantic.METADATA_GAUGE


# ---------------------------------------------------------------------------
# aggregation_engine — individual aggregation strategies
# ---------------------------------------------------------------------------


class TestAggregationStrategies:
    # pylint: disable=attribute-defined-outside-init
    def setup_method(self):
        self.engine = SemanticAggregationEngine()

    def test_aggregate_sum_counter(self):
        m1 = _make_metric("test:counter", MetricType.COUNTER, ["a", "b"], [1.0, 2.0])
        m2 = _make_metric("test:counter", MetricType.COUNTER, ["a", "b"], [3.0, 4.0])
        result = self.engine.aggregate("test:counter", [m1, m2])
        assert result.name == "test:counter"
        assert result.type == MetricType.COUNTER
        assert result.value == [4.0, 6.0]

    def test_aggregate_sum_state_gauge(self):
        m1 = _make_metric(
            "vllm:num_requests_running", MetricType.GAUGE, ['vllm:num_requests_running{model="m"}'], [5.0]
        )
        m2 = _make_metric(
            "vllm:num_requests_running", MetricType.GAUGE, ['vllm:num_requests_running{model="m"}'], [3.0]
        )
        result = self.engine.aggregate("vllm:num_requests_running", [m1, m2])
        assert result.value == [8.0]

    def test_aggregate_weighted_mean_cache(self):
        m1 = _make_metric("vllm:kv_cache_usage_perc", MetricType.GAUGE, ['vllm:kv_cache_usage_perc{model="m"}'], [0.5])
        m2 = _make_metric("vllm:kv_cache_usage_perc", MetricType.GAUGE, ['vllm:kv_cache_usage_perc{model="m"}'], [0.9])
        result = self.engine.aggregate("vllm:kv_cache_usage_perc", [m1, m2])
        assert result.value == [0.7]  # (0.5 + 0.9) / 2

    def test_aggregate_max(self):
        MetricRegistry.register("test:max_gauge", MetricSemanticConfig(semantic=MetricSemantic.HOTSPOT_RESOURCE_GAUGE))
        m1 = _make_metric("test:max_gauge", MetricType.GAUGE, ["a", "b"], [1.0, 5.0])
        m2 = _make_metric("test:max_gauge", MetricType.GAUGE, ["a", "b"], [3.0, 2.0])
        result = self.engine.aggregate("test:max_gauge", [m1, m2])
        assert result.value == [3.0, 5.0]

    def test_aggregate_passthrough_metadata(self):
        m1 = _make_metric("python_info", MetricType.GAUGE, ['python_info{version="3.11"}'], [1.0])
        m2 = _make_metric("python_info", MetricType.GAUGE, ['python_info{version="3.11"}'], [1.0])
        result = self.engine.aggregate("python_info", [m1, m2])
        # Passthrough returns first metric unchanged
        assert result.value == [1.0]

    def test_aggregate_histogram_merge(self):
        m1 = _make_metric(
            "test:latency",
            MetricType.HISTOGRAM,
            [
                'test:latency_bucket{le="0.5"}',
                'test:latency_bucket{le="1.0"}',
                'test:latency_bucket{le="+Inf"}',
                'test:latency_count',
                'test:latency_sum',
            ],
            [3.0, 5.0, 5.0, 5.0, 2.5],
        )
        m2 = _make_metric(
            "test:latency",
            MetricType.HISTOGRAM,
            [
                'test:latency_bucket{le="0.5"}',
                'test:latency_bucket{le="1.0"}',
                'test:latency_bucket{le="+Inf"}',
                'test:latency_count',
                'test:latency_sum',
            ],
            [2.0, 4.0, 4.0, 4.0, 3.0],
        )
        result = self.engine.aggregate("test:latency", [m1, m2])
        assert result.type == MetricType.HISTOGRAM
        # Buckets should be summed
        # Labels sorted by le: 0.5, 1.0, +Inf, then count, then sum
        assert "le=\"0.5\"" in result.label[0]
        assert "le=\"1.0\"" in result.label[1]
        assert "le=\"+Inf\"" in result.label[2]
        assert result.value[0] == 5.0  # 3 + 2
        assert result.value[1] == 9.0  # 5 + 4
        assert result.value[2] == 9.0  # 5 + 4 (+Inf = total)
        assert result.value[3] == 9.0  # count: 5 + 4
        assert result.value[4] == 5.5  # sum: 2.5 + 3.0

    def test_aggregate_histogram_merge_preserves_label_order(self):
        """Histogram with labels in different orders across instances should still merge correctly."""
        m1 = _make_metric(
            "test:latency",
            MetricType.HISTOGRAM,
            [
                'test:latency_bucket{le="1.0"}',
                'test:latency_bucket{le="0.5"}',
                'test:latency_bucket{le="+Inf"}',
                'test:latency_count',
                'test:latency_sum',
            ],
            [5.0, 3.0, 5.0, 5.0, 2.5],
        )
        m2 = _make_metric(
            "test:latency",
            MetricType.HISTOGRAM,
            [
                'test:latency_bucket{le="0.5"}',
                'test:latency_bucket{le="+Inf"}',
                'test:latency_bucket{le="1.0"}',
                'test:latency_sum',
                'test:latency_count',
            ],
            [2.0, 4.0, 4.0, 3.0, 4.0],
        )
        result = self.engine.aggregate("test:latency", [m1, m2])
        # Buckets must be sorted by le value: 0.5, 1.0, +Inf
        assert "le=\"0.5\"" in result.label[0]
        assert "le=\"1.0\"" in result.label[1]
        assert "le=\"+Inf\"" in result.label[2]
        assert result.value[0] == 5.0  # 3 + 2
        assert result.value[1] == 9.0  # 5 + 4
        assert result.value[2] == 9.0  # 5 + 4

    def test_aggregate_histogram_with_role_labels(self):
        """Histogram with role-specific labels in bucket labels."""
        m1 = _make_metric(
            "vllm:ttft",
            MetricType.HISTOGRAM,
            [
                'vllm:ttft_bucket{le="0.05",model="m"}',
                'vllm:ttft_bucket{le="0.1",model="m"}',
                'vllm:ttft_bucket{le="+Inf",model="m"}',
                'vllm:ttft_count{model="m"}',
                'vllm:ttft_sum{model="m"}',
            ],
            [10.0, 20.0, 20.0, 20.0, 1.5],
        )
        m2 = _make_metric(
            "vllm:ttft",
            MetricType.HISTOGRAM,
            [
                'vllm:ttft_bucket{le="0.05",model="m"}',
                'vllm:ttft_bucket{le="0.1",model="m"}',
                'vllm:ttft_bucket{le="+Inf",model="m"}',
                'vllm:ttft_count{model="m"}',
                'vllm:ttft_sum{model="m"}',
            ],
            [5.0, 15.0, 15.0, 15.0, 0.8],
        )
        result = self.engine.aggregate("vllm:ttft", [m1, m2])
        assert result.value[0] == 15.0  # 10 + 5
        assert result.value[1] == 35.0  # 20 + 15
        assert result.value[2] == 35.0  # 20 + 15 (+Inf)
        assert result.value[3] == 35.0  # count
        assert result.value[4] == 2.3  # sum

    def test_aggregate_empty_list_returns_empty(self):
        result = self.engine.aggregate("test:empty", [])
        assert result.name == ""


# ---------------------------------------------------------------------------
# aggregation_engine — post-processing
# ---------------------------------------------------------------------------


class TestPostProcessing:
    # pylint: disable=attribute-defined-outside-init
    def setup_method(self):
        self.engine = SemanticAggregationEngine()

    def test_drops_created_metrics(self):
        aggregate = [
            _make_metric(
                "vllm:request_success_total", MetricType.COUNTER, ['vllm:request_success_total{reason="stop"}'], [5.0]
            ),
            _make_metric("vllm:request_success_created", MetricType.GAUGE, ['vllm:request_success_created'], [1.7e09]),
        ]
        result = self.engine.post_process(aggregate)
        names = [m.name for m in result]
        assert "vllm:request_success_total" in names
        assert "vllm:request_success_created" not in names

    def test_computes_histogram_quantiles(self):
        MetricRegistry.register("vllm:latency", MetricSemanticConfig(semantic=MetricSemantic.HISTOGRAM_LATENCY))
        # Simple histogram: 2 observations at 0.3 and 0.7
        hist = _make_metric(
            "vllm:latency",
            MetricType.HISTOGRAM,
            [
                'vllm:latency_bucket{le="0.25"}',
                'vllm:latency_bucket{le="0.5"}',
                'vllm:latency_bucket{le="0.75"}',
                'vllm:latency_bucket{le="1.0"}',
                'vllm:latency_bucket{le="+Inf"}',
                'vllm:latency_count',
                'vllm:latency_sum',
            ],
            [0.0, 1.0, 2.0, 3.0, 3.0, 3.0, 1.8],
        )
        result = self.engine.post_process([hist])
        quantile_names = [m.name for m in result if "_p" in m.name and m.name.startswith("vllm:latency_p")]
        assert len(quantile_names) >= 3  # p50, p95, p99

        # Find p50 metric
        p50 = next((m for m in result if m.name == "vllm:latency_p50"), None)
        assert p50 is not None
        assert p50.type == MetricType.GAUGE
        # p50: 3 obs at ~0.2, ~0.5, ~0.9; target 1.5 in le=0.75 bucket → ≈0.625
        assert 0.55 <= p50.value[0] <= 0.7

    def test_histogram_quantile_exact(self):
        """Test quantile computation with known values."""
        engine = self.engine
        buckets = [(0.5, 2.0), (1.0, 4.0), (2.0, 6.0), (float("inf"), 8.0)]
        # total count = 8
        # p50: target = 4.0, falls exactly at le=1.0 bucket (cum=4)
        p50 = engine._histogram_quantile(0.5, buckets)
        assert p50 == 1.0
        # p25: target = 2.0, falls exactly at le=0.5 bucket (cum=2)
        p25 = engine._histogram_quantile(0.25, buckets)
        assert p25 == 0.5
        # p75: target = 6.0, falls exactly at le=2.0 bucket (cum=6)
        p75 = engine._histogram_quantile(0.75, buckets)
        assert p75 == 2.0

    def test_histogram_quantile_interpolation(self):
        """Test quantile with linear interpolation."""
        engine = self.engine
        buckets = [(1.0, 3.0), (5.0, 5.0), (float("inf"), 5.0)]
        # total = 5, p50: target = 2.5, falls in first bucket
        # prev_bound=0, prev_count=0, upper=1.0, cum=3.0
        # fraction = (2.5-0)/(3-0) = 0.833
        # result = 0 + 0.833 * (1.0-0) = 0.833
        p50 = engine._histogram_quantile(0.5, buckets)
        assert abs(p50 - 0.833) < 0.01

    def test_derives_ratio_metric(self):
        """Test that ratio metric is derived when both numerator and denominator are present."""
        num = _make_metric(
            "vllm:prefix_cache_hits_total", MetricType.COUNTER, ['vllm:prefix_cache_hits_total{model="m"}'], [30.0]
        )
        den = _make_metric(
            "vllm:prefix_cache_queries_total",
            MetricType.COUNTER,
            ['vllm:prefix_cache_queries_total{model="m"}'],
            [100.0],
        )
        other = _make_metric(
            "vllm:num_requests_running", MetricType.GAUGE, ['vllm:num_requests_running{model="m"}'], [5.0]
        )
        result = self.engine.post_process([num, den, other])
        ratio = next((m for m in result if m.name == "vllm:prefix_cache_hit_rate"), None)
        assert ratio is not None
        assert ratio.type == MetricType.GAUGE
        assert abs(ratio.value[0] - 0.3) < 0.001

    def test_ratio_metric_handles_zero_denominator(self):
        num = _make_metric(
            "vllm:prefix_cache_hits_total", MetricType.COUNTER, ['vllm:prefix_cache_hits_total{model="m"}'], [0.0]
        )
        den = _make_metric(
            "vllm:prefix_cache_queries_total", MetricType.COUNTER, ['vllm:prefix_cache_queries_total{model="m"}'], [0.0]
        )
        result = self.engine.post_process([num, den])
        ratio = next((m for m in result if m.name == "vllm:prefix_cache_hit_rate"), None)
        assert ratio is not None
        assert ratio.value[0] == 0.0

    def test_preserves_non_created_metrics(self):
        aggregate = [
            _make_metric("vllm:counter", MetricType.COUNTER, ["a"], [1.0]),
            _make_metric("vllm:gauge", MetricType.GAUGE, ["b"], [2.0]),
        ]
        result = self.engine.post_process(aggregate)
        assert len(result) == 2

    def test_post_process_no_histogram_no_ratio(self):
        """post_process should return original metrics if no special handling applies."""
        aggregate = [
            _make_metric("vllm:counter", MetricType.COUNTER, ["a"], [1.0]),
        ]
        result = self.engine.post_process(aggregate)
        assert len(result) == 1
        assert result[0].name == "vllm:counter"


# ---------------------------------------------------------------------------
# Histogram quantile edge cases
# ---------------------------------------------------------------------------


class TestHistogramQuantile:
    def test_empty_buckets(self):
        result = SemanticAggregationEngine._histogram_quantile(0.5, [])
        assert math.isnan(result)

    def test_zero_total_count(self):
        buckets = [(1.0, 0.0), (float("inf"), 0.0)]
        result = SemanticAggregationEngine._histogram_quantile(0.5, buckets)
        assert math.isnan(result)

    def test_q_out_of_range(self):
        buckets = [(1.0, 1.0), (float("inf"), 1.0)]
        assert math.isnan(SemanticAggregationEngine._histogram_quantile(-0.1, buckets))
        assert math.isnan(SemanticAggregationEngine._histogram_quantile(1.1, buckets))

    def test_single_observation(self):
        buckets = [(0.5, 1.0), (float("inf"), 1.0)]
        # One observation at ≤0.5, so quantile should be 0.5 (or lower bound)
        p50 = SemanticAggregationEngine._histogram_quantile(0.5, buckets)
        # target=0.5, first bucket cum=1.0 >= 0.5
        # prev_bound=0, prev_count=0, upper=0.5, cum=1.0
        # fraction = (0.5-0)/(1-0) = 0.5
        # result = 0 + 0.5 * 0.5 = 0.25
        assert abs(p50 - 0.25) < 0.01

    def test_all_inf_bucket(self):
        """When +Inf is the only bucket with count, quantile returns prev_bound (0)."""
        buckets = [(0.5, 0.0), (1.0, 0.0), (float("inf"), 5.0)]
        p50 = SemanticAggregationEngine._histogram_quantile(0.5, buckets)
        # target=2.5, first two buckets have cum=0, +Inf has cum=5
        # Enters +Inf bucket -> returns prev_bound = 1.0
        assert p50 == 1.0


# ---------------------------------------------------------------------------
# Integration: MetricsCollector._aggregate_single_metric delegates to engine
# ---------------------------------------------------------------------------


class TestMetricsCollectorIntegration:
    """Verify that MetricsCollector._aggregate_single_metric uses the engine."""

    @patch("threading.Thread.start", MagicMock())
    def test_aggregate_single_metric_delegates(self):
        from motor.coordinator.metrics.metrics_collector import MetricsCollector
        from motor.config.coordinator import CoordinatorConfig

        collector = MetricsCollector(CoordinatorConfig())

        m1 = _make_metric(
            "vllm:num_requests_running", MetricType.GAUGE, ['vllm:num_requests_running{model="m"}'], [3.0]
        )
        m2 = _make_metric(
            "vllm:num_requests_running", MetricType.GAUGE, ['vllm:num_requests_running{model="m"}'], [7.0]
        )
        result = collector._aggregate_single_metric([m1, m2])
        # Should use semantic sum (STATE_GAUGE)
        assert result.value == [10.0]

    @patch("threading.Thread.start", MagicMock())
    def test_aggregate_single_metric_kv_cache_uses_mean(self):
        from motor.coordinator.metrics.metrics_collector import MetricsCollector
        from motor.config.coordinator import CoordinatorConfig

        collector = MetricsCollector(CoordinatorConfig())

        m1 = _make_metric("vllm:kv_cache_usage_perc", MetricType.GAUGE, ['vllm:kv_cache_usage_perc{model="m"}'], [0.2])
        m2 = _make_metric("vllm:kv_cache_usage_perc", MetricType.GAUGE, ['vllm:kv_cache_usage_perc{model="m"}'], [0.8])
        result = collector._aggregate_single_metric([m1, m2])
        # Should use semantic weighted_mean (CACHE_METRIC)
        assert result.value == [0.5]

    @patch("threading.Thread.start", MagicMock())
    def test_aggregate_metrics_all_instance_with_cache(self):
        """Verify _aggregate_metrics_all_instance aggregates from cache and includes _created metrics."""
        from motor.coordinator.metrics.metrics_collector import MetricsCollector
        from motor.config.coordinator import CoordinatorConfig

        collector = MetricsCollector(CoordinatorConfig())

        # Create a simple collected state where:
        # - one instance has a counter and a _created metric
        counter = _make_metric(
            "vllm:request_success_total", MetricType.COUNTER, ['vllm:request_success_total{reason="stop"}'], [5.0]
        )
        created = _make_metric(
            "vllm:request_success_created", MetricType.GAUGE, ['vllm:request_success_created'], [1.7e09]
        )

        collector._instance_metrics_cached = {
            0: {"metrics": [counter, created]},
        }

        collector._inactive_instance_metrics_aggregate = {}

        # Call _aggregate_metrics_all_instance with empty collects (uses cache)
        result = collector._aggregate_metrics_all_instance({}, {}, {})
        assert any(m.name == "vllm:request_success_total" for m in result)
        assert any(m.name == "vllm:request_success_created" for m in result)


# ---------------------------------------------------------------------------
# Role scope — deploy_mode-aware filtering
# ---------------------------------------------------------------------------


def _make_ttft_histogram(count: float, sum_val: float) -> Metric:
    return _make_metric(
        "vllm:time_to_first_token_seconds",
        MetricType.HISTOGRAM,
        [
            'vllm:time_to_first_token_seconds_bucket{le="1.0"}',
            'vllm:time_to_first_token_seconds_bucket{le="+Inf"}',
            "vllm:time_to_first_token_seconds_count",
            "vllm:time_to_first_token_seconds_sum",
        ],
        [count, count, count, sum_val],
    )


def _apply_scope_filter(
    name: str,
    entries: list[tuple[int, Metric]],
    ctx: AggregationContext | None,
) -> list[tuple[int, Metric]]:
    """Mirror MetricsCollector._aggregate_metrics SERVICE-scope filtering."""
    if ctx is not None and ctx.scope == AggregationScope.SERVICE and ctx.instance_roles is not None:
        if name == "vllm:time_to_first_token_seconds" and ctx.instance_dispatch_capabilities is not None:
            handoff = DispatchPlan.PREFILL_HANDOFF_DECODE.value
            entries = [
                (ins_id, metric)
                for ins_id, metric in entries
                if (
                    ins_id == -1
                    or ctx.instance_roles.get(ins_id) in {"decode", "union", "both", "hybrid"}
                    or (
                        ctx.instance_roles.get(ins_id) == "prefill"
                        and handoff in ctx.instance_dispatch_capabilities.get(ins_id, set())
                    )
                )
            ]
        else:
            role_scope = MetricRegistry.get_effective_role_scope(name)
            if role_scope:
                entries = [
                    (ins_id, metric)
                    for ins_id, metric in entries
                    if ins_id == -1 or ctx.instance_roles.get(ins_id) == role_scope
                ]
    return entries


class TestAggregationScope:
    # pylint: disable=attribute-defined-outside-init
    def setup_method(self):
        self.engine = SemanticAggregationEngine()

    @patch.object(MetricRegistry, "get_effective_role_scope", return_value="decode")
    def test_service_scope_filters_ttft_to_decode(self, _mock_scope):
        p_hist = _make_ttft_histogram(10.0, 5.0)
        d_hist = _make_ttft_histogram(20.0, 8.0)
        ctx = AggregationContext(
            scope=AggregationScope.SERVICE,
            instance_roles={1: "prefill", 2: "decode"},
            instance_dispatch_capabilities={},
        )
        entries = _apply_scope_filter("vllm:time_to_first_token_seconds", [(1, p_hist), (2, d_hist)], ctx)
        assert len(entries) == 1
        ttft = self.engine.aggregate("vllm:time_to_first_token_seconds", [m for _, m in entries])
        assert ttft.value[-1] == 8.0

    def test_service_scope_includes_only_handoff_prefill_ttft(self):
        p_concurrent = _make_ttft_histogram(10.0, 5.0)
        p_handoff = _make_ttft_histogram(20.0, 15.0)
        d_hist = _make_ttft_histogram(30.0, 9.0)
        ctx = AggregationContext(
            scope=AggregationScope.SERVICE,
            instance_roles={1: "prefill", 2: "prefill", 3: "decode"},
            instance_dispatch_capabilities={
                1: {DispatchPlan.CONCURRENT_ENGINE_SYNC.value},
                2: {DispatchPlan.PREFILL_HANDOFF_DECODE.value},
                3: {DispatchPlan.PREFILL_HANDOFF_DECODE.value},
            },
        )

        entries = _apply_scope_filter(
            "vllm:time_to_first_token_seconds",
            [(1, p_concurrent), (2, p_handoff), (3, d_hist)],
            ctx,
        )

        assert [ins_id for ins_id, _ in entries] == [2, 3]

    def test_role_scope_keeps_prefill_ttft(self):
        p1 = _make_ttft_histogram(10.0, 5.0)
        p2 = _make_ttft_histogram(20.0, 15.0)
        ctx = AggregationContext(scope=AggregationScope.ROLE)
        entries = _apply_scope_filter("vllm:time_to_first_token_seconds", [(1, p1), (2, p2)], ctx)
        assert len(entries) == 2
        ttft = self.engine.aggregate("vllm:time_to_first_token_seconds", [m for _, m in entries])
        assert ttft.value[-1] == 20.0

    def test_instance_scope_no_role_filter(self):
        p_hist = _make_ttft_histogram(10.0, 5.0)
        ctx = AggregationContext(
            scope=AggregationScope.INSTANCE,
            instance_roles={1: "prefill"},
        )
        entries = _apply_scope_filter("vllm:time_to_first_token_seconds", [(1, p_hist)], ctx)
        assert len(entries) == 1

    def test_node_scope_no_role_filter(self):
        p_hist = _make_ttft_histogram(10.0, 5.0)
        d_hist = _make_ttft_histogram(20.0, 8.0)
        ctx = AggregationContext(scope=AggregationScope.NODE)
        entries = _apply_scope_filter("vllm:time_to_first_token_seconds", [(1, p_hist), (2, d_hist)], ctx)
        assert len(entries) == 2


class TestRoleScope:
    def test_ttft_has_decode_role_scope(self):
        config = MetricRegistry.get_semantic("vllm:time_to_first_token_seconds")
        assert config.role_scope == "decode"

    def test_tpot_has_decode_role_scope(self):
        config = MetricRegistry.get_semantic("vllm:time_per_output_token_seconds")
        assert config.role_scope == "decode"

    def test_e2e_latency_has_decode_role_scope(self):
        config = MetricRegistry.get_semantic("vllm:e2e_request_latency_seconds")
        assert config.role_scope == "decode"

    def test_other_metrics_have_no_role_scope(self):
        config = MetricRegistry.get_semantic("vllm:num_requests_running")
        assert config.role_scope is None

    def test_ttft_effective_role_scope_default(self):
        scope = MetricRegistry.get_effective_role_scope("vllm:time_to_first_token_seconds")
        assert scope == "decode"

    def test_ttft_effective_role_scope_handoff_connector(self):
        scope = MetricRegistry.get_effective_role_scope(
            "vllm:time_to_first_token_seconds",
            {DispatchPlan.PREFILL_HANDOFF_DECODE.value},
        )
        assert scope is None  # No filtering

    def test_effective_role_scope_unknown_metric(self):
        scope = MetricRegistry.get_effective_role_scope("some_unknown_metric")
        assert scope is None


# ---------------------------------------------------------------------------
# Histogram mean gauge
# ---------------------------------------------------------------------------


class TestHistogramMean:
    # pylint: disable=attribute-defined-outside-init
    def setup_method(self):
        self.engine = SemanticAggregationEngine()

    def test_mean_gauge_emitted(self):
        MetricRegistry.register("vllm:test_latency", MetricSemanticConfig(semantic=MetricSemantic.HISTOGRAM_LATENCY))
        # 3 observations: 0.2, 0.4, 0.9 → sum=1.5, count=3, mean=0.5
        hist = _make_metric(
            "vllm:test_latency",
            MetricType.HISTOGRAM,
            [
                'vllm:test_latency_bucket{le="0.25"}',
                'vllm:test_latency_bucket{le="0.5"}',
                'vllm:test_latency_bucket{le="1.0"}',
                'vllm:test_latency_bucket{le="+Inf"}',
                'vllm:test_latency_count',
                'vllm:test_latency_sum',
            ],
            [1.0, 2.0, 3.0, 3.0, 3.0, 1.5],
        )
        result = self.engine.post_process([hist])
        mean = next((m for m in result if m.name == "vllm:test_latency_mean"), None)
        assert mean is not None
        assert mean.type == MetricType.GAUGE
        assert abs(mean.value[0] - 0.5) < 0.001

    def test_mean_gauge_zero_count_returns_nan(self):
        MetricRegistry.register("vllm:empty_latency", MetricSemanticConfig(semantic=MetricSemantic.HISTOGRAM_LATENCY))
        hist = _make_metric(
            "vllm:empty_latency",
            MetricType.HISTOGRAM,
            [
                'vllm:empty_latency_bucket{le="+Inf"}',
                'vllm:empty_latency_count',
                'vllm:empty_latency_sum',
            ],
            [0.0, 0.0, 0.0],
        )
        result = self.engine.post_process([hist])
        mean = next((m for m in result if m.name == "vllm:empty_latency_mean"), None)
        assert mean is not None
        assert math.isnan(mean.value[0])

    def test_mean_gauge_with_labels(self):
        MetricRegistry.register("vllm:labeled_latency", MetricSemanticConfig(semantic=MetricSemantic.HISTOGRAM_LATENCY))
        hist = _make_metric(
            "vllm:labeled_latency",
            MetricType.HISTOGRAM,
            [
                'vllm:labeled_latency_bucket{le="0.5",model="x"}',
                'vllm:labeled_latency_bucket{le="+Inf",model="x"}',
                'vllm:labeled_latency_count{model="x"}',
                'vllm:labeled_latency_sum{model="x"}',
            ],
            [5.0, 5.0, 5.0, 10.0],
        )
        result = self.engine.post_process([hist])
        mean = next((m for m in result if m.name == "vllm:labeled_latency_mean"), None)
        assert mean is not None
        # sum=10.0, count=5.0 → mean=2.0
        assert abs(mean.value[0] - 2.0) < 0.001
        # Label should include the model label
        assert 'model="x"' in mean.label[0]

    def test_no_mean_for_histogram_without_quantile_semantic(self):
        """Histogram without quantile/metadata config should not produce quantile or mean."""
        # "vllm:request_prefill_time_seconds" is registered as HISTOGRAM_LATENCY
        # but has no quantiles metadata → no quantile output, but should have mean
        MetricRegistry.register("vllm:prefill_time", MetricSemanticConfig(semantic=MetricSemantic.HISTOGRAM_LATENCY))
        hist = _make_metric(
            "vllm:prefill_time",
            MetricType.HISTOGRAM,
            [
                'vllm:prefill_time_bucket{le="+Inf"}',
                'vllm:prefill_time_count',
                'vllm:prefill_time_sum',
            ],
            [1.0, 1.0, 5.0],
        )
        result = self.engine.post_process([hist])
        # Mean should still be emitted (for any HISTOGRAM_LATENCY or SLA_METRIC)
        mean = next((m for m in result if m.name == "vllm:prefill_time_mean"), None)
        assert mean is not None
        assert abs(mean.value[0] - 5.0) < 0.001

    def test_unknown_histogram_passthrough(self):
        """Unknown histogram metric should be merged but not produce extra gauges."""
        hist = _make_metric(
            "unknown:hist",
            MetricType.HISTOGRAM,
            [
                'unknown:hist_bucket{le="+Inf"}',
                'unknown:hist_count',
                'unknown:hist_sum',
            ],
            [1.0, 1.0, 5.0],
        )
        result = self.engine.post_process([hist])
        # Should pass through the original histogram unchanged
        # (no quantile or mean since not registered)
        names = [m.name for m in result]
        assert "unknown:hist_p50" not in names
        assert "unknown:hist_mean" not in names
