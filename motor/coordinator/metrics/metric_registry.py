# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Metric semantic taxonomy, configuration, and registry.

Defines:
  - MetricSemantic: enum driving aggregation strategy + post-processing
  - MetricRegistry: metric name → semantic config mapping for vLLM / process / coordinator metrics
  - RatioPair: derived ratio metric definition

Unknown metrics fall back to type-based defaults (gauge→sum, counter→sum, histogram→merge).
"""

from dataclasses import dataclass
from enum import Enum

from motor.common.resources.dispatch import DispatchPlan


# ---------------------------------------------------------------------------
# Semantic taxonomy
# ---------------------------------------------------------------------------


class MetricSemantic(Enum):
    """Semantic category — determines aggregation strategy and post-processing."""

    # sum across sources
    COUNTER = "counter"
    STATE_GAUGE = "state_gauge"
    QUEUE_GAUGE = "queue_gauge"
    THROUGHPUT_COUNTER = "throughput_counter"
    RATIO_NUMERATOR = "ratio_numerator"
    RATIO_DENOMINATOR = "ratio_denominator"

    # max across sources
    HOTSPOT_RESOURCE_GAUGE = "hotspot_resource_gauge"
    OCCUPANCY_METRIC = "occupancy_metric"

    # mean across sources
    RESOURCE_UTILIZATION_GAUGE = "resource_utilization_gauge"
    CACHE_METRIC = "cache_metric"

    # passthrough (pick first)
    METADATA_GAUGE = "metadata_gauge"

    # histogram merge + quantile post-processing
    HISTOGRAM_LATENCY = "histogram_latency"
    SLA_METRIC = "sla_metric"


@dataclass
class MetricSemanticConfig:
    """Configuration binding a metric name to its semantic, role scope, and post-processing hints."""

    semantic: MetricSemantic
    role_scope: str | None = None  # SERVICE-scope only: "prefill", "decode", or None
    metadata: dict = None  # e.g. {"quantiles": [0.5, 0.95, 0.99]}

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


@dataclass
class RatioPair:
    """Derived ratio metric: name = numerator_total / denominator_total."""

    name: str
    help: str
    numerator: str
    denominator: str


# ---------------------------------------------------------------------------
# Registry: metric name → semantic config
# ---------------------------------------------------------------------------

_VLLM_METRIC_REGISTRY: dict[str, MetricSemanticConfig] = {
    # -- SLA histograms → quantiles ------------------------------------------
    "vllm:time_to_first_token_seconds": MetricSemanticConfig(
        semantic=MetricSemantic.SLA_METRIC,
        role_scope="decode",
        metadata={"quantiles": [0.5, 0.95, 0.99]},
    ),
    "vllm:time_per_output_token_seconds": MetricSemanticConfig(
        semantic=MetricSemantic.SLA_METRIC,
        role_scope="decode",
        metadata={"quantiles": [0.5, 0.95, 0.99]},
    ),
    # -- Latency histograms → quantiles --------------------------------------
    "vllm:e2e_request_latency_seconds": MetricSemanticConfig(
        semantic=MetricSemantic.HISTOGRAM_LATENCY,
        role_scope="decode",
        metadata={"quantiles": [0.5, 0.95, 0.99]},
    ),
    "vllm:request_queue_time_seconds": MetricSemanticConfig(
        semantic=MetricSemantic.HISTOGRAM_LATENCY,
        metadata={"quantiles": [0.5, 0.95, 0.99]},
    ),
    # -- Other histograms (no quantile metadata → no quantile output) --------
    "vllm:request_prefill_time_seconds": MetricSemanticConfig(
        semantic=MetricSemantic.HISTOGRAM_LATENCY,
    ),
    "vllm:request_decode_time_seconds": MetricSemanticConfig(
        semantic=MetricSemantic.HISTOGRAM_LATENCY,
    ),
    "vllm:request_params_n": MetricSemanticConfig(
        semantic=MetricSemantic.HISTOGRAM_LATENCY,
    ),
    "vllm:request_params_max_tokens": MetricSemanticConfig(
        semantic=MetricSemantic.HISTOGRAM_LATENCY,
    ),
    # -- Queue depth ---------------------------------------------------------
    "vllm:num_requests_waiting": MetricSemanticConfig(
        semantic=MetricSemantic.QUEUE_GAUGE,
    ),
    # -- Running / swapped state ---------------------------------------------
    "vllm:num_requests_running": MetricSemanticConfig(
        semantic=MetricSemantic.STATE_GAUGE,
    ),
    "vllm:num_requests_swapped": MetricSemanticConfig(
        semantic=MetricSemantic.STATE_GAUGE,
    ),
    # -- KV cache usage ------------------------------------------------------
    "vllm:kv_cache_usage_perc": MetricSemanticConfig(
        semantic=MetricSemantic.CACHE_METRIC,
    ),
    "vllm:gpu_cache_usage_perc": MetricSemanticConfig(
        semantic=MetricSemantic.CACHE_METRIC,
    ),
    "vllm:cpu_cache_usage_perc": MetricSemanticConfig(
        semantic=MetricSemantic.CACHE_METRIC,
    ),
    # -- Prefix cache numerator/denominator (for hit rate derivation) ---------
    "vllm:prefix_cache_queries_total": MetricSemanticConfig(
        semantic=MetricSemantic.RATIO_DENOMINATOR,
    ),
    "vllm:prefix_cache_hits_total": MetricSemanticConfig(
        semantic=MetricSemantic.RATIO_NUMERATOR,
    ),
    "vllm:gpu_prefix_cache_queries_total": MetricSemanticConfig(
        semantic=MetricSemantic.RATIO_DENOMINATOR,
    ),
    "vllm:gpu_prefix_cache_hits_total": MetricSemanticConfig(
        semantic=MetricSemantic.RATIO_NUMERATOR,
    ),
    # -- Throughput counters -------------------------------------------------
    "vllm:prompt_tokens_total": MetricSemanticConfig(
        semantic=MetricSemantic.THROUGHPUT_COUNTER,
    ),
    "vllm:generation_tokens_total": MetricSemanticConfig(
        semantic=MetricSemantic.THROUGHPUT_COUNTER,
    ),
    "vllm:new_tokens_total": MetricSemanticConfig(
        semantic=MetricSemantic.THROUGHPUT_COUNTER,
    ),
    # -- Generic counters ----------------------------------------------------
    "vllm:request_success_total": MetricSemanticConfig(
        semantic=MetricSemantic.COUNTER,
    ),
    "vllm:num_preemptions_total": MetricSemanticConfig(
        semantic=MetricSemantic.COUNTER,
    ),
    # -- Hotspot resources ---------------------------------------------------
    "vllm:num_requests_running_max": MetricSemanticConfig(
        semantic=MetricSemantic.HOTSPOT_RESOURCE_GAUGE,
    ),
    "vllm:kv_cache_usage_perc_max": MetricSemanticConfig(
        semantic=MetricSemantic.HOTSPOT_RESOURCE_GAUGE,
    ),
    # -- Python / process ----------------------------------------------------
    "process_virtual_memory_bytes": MetricSemanticConfig(
        semantic=MetricSemantic.RESOURCE_UTILIZATION_GAUGE,
    ),
    "process_resident_memory_bytes": MetricSemanticConfig(
        semantic=MetricSemantic.RESOURCE_UTILIZATION_GAUGE,
    ),
    "process_open_fds": MetricSemanticConfig(
        semantic=MetricSemantic.STATE_GAUGE,
    ),
    "process_max_fds": MetricSemanticConfig(
        semantic=MetricSemantic.METADATA_GAUGE,
    ),
    "process_cpu_seconds_total": MetricSemanticConfig(
        semantic=MetricSemantic.COUNTER,
    ),
    "process_start_time_seconds": MetricSemanticConfig(
        semantic=MetricSemantic.METADATA_GAUGE,
    ),
    "python_info": MetricSemanticConfig(
        semantic=MetricSemantic.METADATA_GAUGE,
    ),
    "python_gc_objects_collected_total": MetricSemanticConfig(
        semantic=MetricSemantic.COUNTER,
    ),
    "python_gc_objects_uncollectable_total": MetricSemanticConfig(
        semantic=MetricSemantic.COUNTER,
    ),
    "python_gc_collections_total": MetricSemanticConfig(
        semantic=MetricSemantic.COUNTER,
    ),
    # -- Coordinator-side (already aggregated) --------------------------------
    "motor:active_prefill_workers": MetricSemanticConfig(
        semantic=MetricSemantic.METADATA_GAUGE,
    ),
    "motor:active_decode_workers": MetricSemanticConfig(
        semantic=MetricSemantic.METADATA_GAUGE,
    ),
    "motor:inactive_prefill_workers": MetricSemanticConfig(
        semantic=MetricSemantic.METADATA_GAUGE,
    ),
    "motor:inactive_decode_workers": MetricSemanticConfig(
        semantic=MetricSemantic.METADATA_GAUGE,
    ),
    # -- Coordinator-side (computed from counter deltas) -----------------------
    "motor:prompt_tokens_per_second": MetricSemanticConfig(
        semantic=MetricSemantic.METADATA_GAUGE,
    ),
    "motor:generation_tokens_per_second": MetricSemanticConfig(
        semantic=MetricSemantic.METADATA_GAUGE,
    ),
}

# Ratio pairs: derived during post-processing
_RATIO_PAIRS: list[RatioPair] = [
    RatioPair(
        name="vllm:prefix_cache_hit_rate",
        help="Prefix cache hit rate (cached tokens / queried tokens).",
        numerator="vllm:prefix_cache_hits_total",
        denominator="vllm:prefix_cache_queries_total",
    ),
]

_CREATED_SUFFIX = "_created"


class MetricRegistry:
    """Central registry for metric semantics."""

    @classmethod
    def get_semantic(cls, metric_name: str) -> MetricSemanticConfig | None:
        """Look up semantic config; returns None for unknown metrics
        (engine falls back by Prometheus type).
        """
        if metric_name.endswith(_CREATED_SUFFIX):
            return MetricSemanticConfig(semantic=MetricSemantic.METADATA_GAUGE)
        return _VLLM_METRIC_REGISTRY.get(metric_name)

    @classmethod
    def get_effective_role_scope(
        cls,
        metric_name: str,
        dispatch_capabilities: set[str] | None = None,
    ) -> str | None:
        """Get the effective role scope for a metric, considering connector capabilities.

        Handoff connectors expose meaningful TTFT on both P and D instances.
        Concurrent connectors use D's TTFT as the authoritative service value.
        """
        config = cls.get_semantic(metric_name)
        if config is None or config.role_scope is None:
            return None

        if (
            metric_name == "vllm:time_to_first_token_seconds"
            and dispatch_capabilities
            and DispatchPlan.PREFILL_HANDOFF_DECODE.value in dispatch_capabilities
        ):
            return None

        return config.role_scope

    @classmethod
    def is_created_metric(cls, metric_name: str) -> bool:
        return metric_name.endswith(_CREATED_SUFFIX)

    @classmethod
    def get_ratio_pairs(cls) -> list[RatioPair]:
        return list(_RATIO_PAIRS)

    @classmethod
    def register(cls, metric_name: str, config: MetricSemanticConfig) -> None:
        _VLLM_METRIC_REGISTRY[metric_name] = config

    @classmethod
    def register_ratio_pair(cls, ratio_pair: RatioPair) -> None:
        _RATIO_PAIRS.append(ratio_pair)
