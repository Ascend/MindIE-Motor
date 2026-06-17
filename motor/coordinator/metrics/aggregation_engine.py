# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Semantic-aware aggregation engine.

Replaces the hardcoded name check in MetricsCollector._aggregate_single_metric()
with semantic-driven dispatch. Adds post-processing for quantile computation and
ratio derivation.
"""

import re
from collections.abc import Callable

from motor.coordinator.metrics.metric_types import Metric, MetricType
from motor.coordinator.metrics.metric_registry import (
    MetricRegistry,
    MetricSemantic,
)

_BUCKET_LE_RE = re.compile(r'le="([^"]*)"')
_DEFAULT_QUANTILES = [0.5, 0.95, 0.99]


class SemanticAggregationEngine:
    """
    Semantic-aware metric aggregation and post-processing.

    Usage:
      1. engine.aggregate(name, metric_list) replaces _aggregate_single_metric
      2. engine.post_process(aggregate) after all metrics are aggregated
    """

    # Semantic → (reduce_fn, initial_value)
    _REDUCE_MAP: dict[MetricSemantic, tuple[Callable[[float, float], float], float]] = {
        MetricSemantic.COUNTER: (lambda a, b: a + b, 0.0),
        MetricSemantic.STATE_GAUGE: (lambda a, b: a + b, 0.0),
        MetricSemantic.QUEUE_GAUGE: (lambda a, b: a + b, 0.0),
        MetricSemantic.THROUGHPUT_COUNTER: (lambda a, b: a + b, 0.0),
        MetricSemantic.RATIO_NUMERATOR: (lambda a, b: a + b, 0.0),
        MetricSemantic.RATIO_DENOMINATOR: (lambda a, b: a + b, 0.0),
        MetricSemantic.HOTSPOT_RESOURCE_GAUGE: (lambda a, b: a if a > b else b, float("-inf")),
        MetricSemantic.OCCUPANCY_METRIC: (lambda a, b: a if a > b else b, float("-inf")),
    }

    # Semantics whose histograms get quantile post-processing
    _QUANTILE_SEMANTICS = frozenset({MetricSemantic.HISTOGRAM_LATENCY, MetricSemantic.SLA_METRIC})

    def aggregate(self, metric_name: str, metric_list: list[Metric]) -> Metric:
        """Aggregate a list of same-name Metrics using semantic-aware strategy."""
        if not metric_list:
            return Metric()

        first = metric_list[0]
        config = MetricRegistry.get_semantic(metric_name)

        if config is None:
            # Unknown metric — fall back by Prometheus type
            if first.type == MetricType.HISTOGRAM:
                return self._aggregate_histogram_merge(metric_list)
            return self._aggregate_reduce(metric_list, lambda a, b: a + b, 0.0)

        semantic = config.semantic

        if semantic in (MetricSemantic.RESOURCE_UTILIZATION_GAUGE, MetricSemantic.CACHE_METRIC):
            return self._mean_from_reduce(metric_list)

        if semantic in self._QUANTILE_SEMANTICS:
            return self._aggregate_histogram_merge(metric_list)

        if semantic == MetricSemantic.METADATA_GAUGE:
            return metric_list[0].copy()

        reduce_spec = self._REDUCE_MAP.get(semantic)
        if reduce_spec is not None:
            return self._aggregate_reduce(metric_list, *reduce_spec)

        # Fallback by Prometheus type:
        if first.type == MetricType.HISTOGRAM:
            return self._aggregate_histogram_merge(metric_list)
        # counter / gauge / summary → sum
        return self._aggregate_reduce(metric_list, lambda a, b: a + b, 0.0)

    # ------------------------------------------------------------------
    # Aggregation strategies
    # ------------------------------------------------------------------

    @staticmethod
    def _aggregate_reduce(
        metric_list: list[Metric],
        reduce_fn: Callable[[float, float], float],
        initial: float,
    ) -> Metric:
        """Generic label-wise reduction (sum, max, etc.)."""
        first = metric_list[0]
        result: dict[str, float] = {}
        for metric in metric_list:
            for i, label in enumerate(metric.label):
                result[label] = reduce_fn(result.get(label, initial), metric.value[i])
        return Metric(
            name=first.name,
            help=first.help,
            type=first.type,
            label=list(result.keys()),
            value=list(result.values()),
        )

    @staticmethod
    def _mean_from_reduce(metric_list: list[Metric]) -> Metric:
        """Arithmetic mean: sum all then divide by count."""
        result = SemanticAggregationEngine._aggregate_reduce(metric_list, lambda a, b: a + b, 0.0)
        n = len(metric_list)
        if n > 1:
            for i in range(len(result.value)):
                result.value[i] /= n
        return result

    @staticmethod
    def _aggregate_histogram_merge(metric_list: list[Metric]) -> Metric:
        """Merge histogram buckets by summing per-le counts, counts, and sums."""
        first = metric_list[0]
        buckets: dict[str, float] = {}
        counts: dict[str, float] = {}
        sums: dict[str, float] = {}
        bucket_order: list[str] = []
        count_order: list[str] = []
        sum_order: list[str] = []

        for metric in metric_list:
            for i, label in enumerate(metric.label):
                if "_bucket{" in label:
                    buckets[label] = buckets.get(label, 0.0) + metric.value[i]
                    if label not in bucket_order:
                        bucket_order.append(label)
                elif "_count{" in label or label.endswith("_count"):
                    counts[label] = counts.get(label, 0.0) + metric.value[i]
                    if label not in count_order:
                        count_order.append(label)
                elif "_sum{" in label or label.endswith("_sum"):
                    sums[label] = sums.get(label, 0.0) + metric.value[i]
                    if label not in sum_order:
                        sum_order.append(label)

        def _le_val(label: str) -> float:
            m = _BUCKET_LE_RE.search(label)
            if not m:
                return float("inf")
            s = m.group(1)
            return float("inf") if s == "+Inf" else float(s)

        bucket_order.sort(key=_le_val)
        new_labels = bucket_order + count_order + sum_order
        new_values = [buckets.get(l, counts.get(l, sums.get(l, 0.0))) for l in new_labels]

        return Metric(
            name=first.name,
            help=first.help,
            type=first.type,
            label=new_labels,
            value=new_values,
        )

    # ------------------------------------------------------------------
    # Post-processing
    # ------------------------------------------------------------------

    def post_process(self, aggregate: list[Metric]) -> list[Metric]:
        """
        Post-process aggregated metrics:
        1. Drop _created timestamp gauges
        2. Compute quantile gauges and mean from histogram metrics
        3. Derive ratio metrics (e.g. cache hit rate)
        """
        result: list[Metric] = []
        metric_map: dict[str, Metric] = {}

        for metric in aggregate:
            if MetricRegistry.is_created_metric(metric.name):
                continue
            result.append(metric)
            metric_map[metric.name] = metric

            config = MetricRegistry.get_semantic(metric.name)
            if config is not None and config.semantic in self._QUANTILE_SEMANTICS:
                result.extend(self._compute_histogram_quantiles(metric, config.metadata))

        for rp in MetricRegistry.get_ratio_pairs():
            ratio = self._derive_ratio(rp, metric_map)
            if ratio is not None:
                result.append(ratio)

        return result

    # ------------------------------------------------------------------
    # Quantile computation
    # ------------------------------------------------------------------

    @staticmethod
    def _histogram_label_group_key(label: str, metric_name: str) -> str:
        """Extract the non-suffix label group key for a histogram label."""
        rest = label[len(metric_name) :]
        for suffix in ("_bucket", "_count", "_sum"):
            if rest.startswith(suffix):
                rest = rest[len(suffix) :]
                break
        rest = re.sub(r'\ble="[^"]*",?\s*', "", rest)
        rest = re.sub(r'\{\s*\}', "", rest.strip())
        rest = re.sub(r',\s*}', "}", rest).replace("{,", "{")
        return rest

    @classmethod
    def _parse_histogram_buckets(cls, metric: Metric) -> dict[str, list[tuple[float, float]]] | None:
        """Parse histogram Metric into per-label-set bucket lists keyed by non-le label group."""
        if metric.type != MetricType.HISTOGRAM:
            return None

        groups: dict[str, list[tuple[float, float]]] = {}
        for label, value in zip(metric.label, metric.value):
            if "_bucket" not in label:
                continue
            m = _BUCKET_LE_RE.search(label)
            if not m:
                continue
            le_str = m.group(1)
            upper = float("inf") if le_str == "+Inf" else float(le_str)
            key = cls._histogram_label_group_key(label, metric.name)
            groups.setdefault(key, []).append((upper, value))

        for buckets in groups.values():
            buckets.sort(key=lambda x: x[0])

        return groups if groups else None

    @classmethod
    def _histogram_quantile(cls, q: float, buckets: list[tuple[float, float]]) -> float:
        """Prometheus histogram_quantile: linear interpolation from bucket counts."""
        if not buckets or q < 0 or q > 1:
            return float("nan")
        total = buckets[-1][1]
        if total <= 0:
            return float("nan")

        target = q * total
        prev_bound, prev_count = 0.0, 0.0
        for upper, cum in buckets:
            if cum < target:
                prev_bound, prev_count = upper, cum
                continue
            if upper == float("inf"):
                return prev_bound
            if cum == prev_count:
                return upper
            fraction = (target - prev_count) / (cum - prev_count)
            return prev_bound + fraction * (upper - prev_bound)
        return float("nan")

    def _compute_histogram_quantiles(self, metric: Metric, metadata: dict) -> list[Metric]:
        """Compute quantile and mean gauges from a merged histogram."""
        quantiles = metadata.get("quantiles", _DEFAULT_QUANTILES)
        buckets_by_base = self._parse_histogram_buckets(metric)
        if not buckets_by_base:
            return []

        # Extract _sum and _count per label group for mean computation
        sum_by_group: dict[str, float] = {}
        count_by_group: dict[str, float] = {}
        for label, value in zip(metric.label, metric.value):
            key = self._histogram_label_group_key(label, metric.name)
            if "_sum" in label:
                sum_by_group[key] = value
            elif "_count" in label:
                count_by_group[key] = value

        result: list[Metric] = []
        for base_label, buckets in buckets_by_base.items():
            # Mean from merged histogram _sum / _count
            total_sum = sum_by_group.get(base_label, 0.0)
            total_count = count_by_group.get(base_label, 0.0)
            mean_val = total_sum / total_count if total_count > 0 else float("nan")
            mean_name = f"{metric.name}_mean"
            if base_label and "{" in base_label:
                mean_label = mean_name + base_label
            else:
                mean_label = mean_name
            result.append(
                Metric(
                    name=mean_name,
                    help=f"Computed mean from {metric.name}",
                    type=MetricType.GAUGE,
                    label=[mean_label],
                    value=[mean_val],
                )
            )
            # Quantiles
            for q in quantiles:
                q_value = self._histogram_quantile(q, buckets)
                q_name = f"{metric.name}_p{int(q * 100)}"
                if base_label and "{" in base_label:
                    q_label = q_name + base_label.replace("{", '{quantile="' + str(q) + '",')
                else:
                    q_label = f'{q_name}{{quantile="{q}"}}'
                result.append(
                    Metric(
                        name=q_name,
                        help=f"Computed p{int(q * 100)} quantile from {metric.name}",
                        type=MetricType.GAUGE,
                        label=[q_label],
                        value=[q_value],
                    )
                )
        return result

    # ------------------------------------------------------------------
    # Ratio derivation
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_ratio(ratio_pair, metric_map: dict[str, Metric]) -> Metric | None:
        """Derive ratio = numerator_sum / denominator_sum, clamped to [0, 1]."""
        num_m = metric_map.get(ratio_pair.numerator)
        den_m = metric_map.get(ratio_pair.denominator)
        if not num_m or not den_m:
            return None
        num_total = sum(num_m.value)
        den_total = sum(den_m.value)
        ratio = 0.0 if den_total == 0 else min(num_total / den_total, 1.0)
        return Metric(
            name=ratio_pair.name,
            help=ratio_pair.help,
            type=MetricType.GAUGE,
            label=[ratio_pair.name],
            value=[ratio],
        )
