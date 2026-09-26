# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
import math
import re
import time
import threading
from collections.abc import Callable
from typing import Any
import requests

from motor.common.resources.instance import Instance
from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.common.utils.net import format_address
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.api_client.native_engine_api_client import NativeEngineApiClient
from motor.coordinator.domain.probe import is_master_from_role_shm
from motor.coordinator.metrics.metric_types import (
    AggregationContext,
    AggregationScope,
    MOTOR_ENDPOINT_STATE_HELP,
    MOTOR_ENDPOINT_STATE_METRIC,
    Metric,
    MetricType,
)
from motor.coordinator.metrics.aggregation_engine import SemanticAggregationEngine
from motor.coordinator.metrics.hpa_contract import get_hpa_alias
from motor.coordinator.metrics.metric_registry import MetricRegistry
from motor.coordinator.metrics.metric_computer import MotorMetricComputer, get_inherited_metric_names

logger = get_logger(__name__)

_METRICS_FORMAT_PROMETHEUS = "prometheus"
_METRICS_FORMAT_OPENTELEMETRY = "opentelemetry"
_STANDBY_METRICS_TEXT = "# coordinator standby; metrics are served by the master\n"

_MOTOR_METRIC_PREFIX = "motor:"
# 引擎上报的 per-endpoint KV cache 使用率指标（collects 中已采集），
# 以 motor:endpoint_state 族的 stat="kv_usage" 样本输出（与 SchedulerServer 侧
# cnt/fresh_load/a_tokens/total_cnt 样本合并为同一指标族，避免重复 HELP/TYPE）。
_KV_USAGE_METRIC_NAME = "vllm:kv_cache_usage_perc"
_MOTOR_KV_USAGE_STAT = "kv_usage"

# 引擎上报的 per-endpoint 运行/等待请求数（collects 中已采集），
# 以 motor:endpoint_state 族的 stat="running" / stat="waiting" 样本输出。
_REQUESTS_RUNNING_METRIC_NAME = "vllm:num_requests_running"
_REQUESTS_WAITING_METRIC_NAME = "vllm:num_requests_waiting"
_MOTOR_RUNNING_STAT = "running"
_MOTOR_WAITING_STAT = "waiting"

# P 实例各节点 KV 命中率：命中率 = 累计命中长度 / 累计请求输入长度。
# 累计命中长度 = 本地 HBM 缓存命中（vllm:prefix_cache_hits_total）+ 外部缓存命中
# （vllm:external_prefix_cache_hits_total）；累计请求输入长度统一取
# vllm:prefix_cache_queries_total（queried tokens）。以 motor:endpoint_state
# 族的 stat="kv_hit_tokens" / stat="kv_input_tokens" / stat="kv_hit_rate" 样本输出。
_KV_HIT_TOKENS_METRIC_NAME = "vllm:prefix_cache_hits_total"
_KV_EXTERNAL_HIT_TOKENS_METRIC_NAME = "vllm:external_prefix_cache_hits_total"
_KV_INPUT_TOKENS_METRIC_NAME = "vllm:prefix_cache_queries_total"
_MOTOR_KV_HIT_TOKENS_STAT = "kv_hit_tokens"
_MOTOR_KV_INPUT_TOKENS_STAT = "kv_input_tokens"
_MOTOR_KV_HIT_RATE_STAT = "kv_hit_rate"
# KV 命中率统计窗口：仅统计最近 5 分钟的增量（命中/输入均为窗口内 delta），
# 而非引擎侧的历史累计值。窗口内最早采样点作为基线，超过窗口则重置基线。
_KV_HIT_WINDOW_SECONDS = 300


def _filter_motor_only_metrics(metrics_text: str) -> str:
    """Keep only metric families whose name starts with ``motor:``.

    Prometheus text is family-based: a ``# HELP <name> ...`` / ``# TYPE <name> ...``
    header opens a family, followed by its sample lines until the next header. Any
    family that does not start with the ``motor:`` prefix is dropped entirely.
    """
    if not metrics_text:
        return ""
    out_lines: list[str] = []
    keep_family = False
    for line in metrics_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("# HELP ") or stripped.startswith("# TYPE "):
            parts = stripped.split()
            name = parts[2] if len(parts) >= 3 else ""
            keep_family = name.startswith(_MOTOR_METRIC_PREFIX)
        if keep_family:
            out_lines.append(line)
    return "\n".join(out_lines)


# 端点粒度附加 motor: 族（sched metrics / kv usage / kv hit rate / running-waiting）
# 的视图排除表：这些视图只输出引擎聚合指标（与 kv_store_* 的视图语义一致）；
# 其余取值（full、motor、非法 type 回退 full）均追加附加族。
_EXTRA_METRIC_EXCLUDED_VIEWS = frozenset({"instance", "role", "dp", "node"})


# ============================ reusable endpoint metrics helpers ============================
# 供 kv_usage 采集复用：查询单个 PD endpoint 的 /metrics 文本，并解析 Prometheus 文本。
_ENGINE_LABEL_RE = re.compile(r'engine="\d+",')


def query_endpoint_metrics(endpoint, tls_config=None) -> str:
    """查询单个 PD endpoint 的 /metrics 文本（复用 NativeEngineApiClient 查询逻辑）。

    :param endpoint: Endpoint 对象（使用其 ip/business_port）
    :param tls_config: 推理通道 TLS 配置；缺省用进程级配置
    :returns: Prometheus 文本；失败返回空串。
    """
    return NativeEngineApiClient.query_metrics(
        format_address(endpoint.ip, endpoint.business_port),
        tls_config if tls_config is not None else CoordinatorConfig.from_json().infer_tls_config,
    )


def _parse_metric_help(metric: Metric, line: str) -> bool:
    parts = line.split()
    if len(parts) >= 4 and parts[0] == "#" and parts[1] == "HELP":
        metric.name = parts[2]
        metric.help = " ".join(parts[3:])
        return True
    logger.error("[Metrics] Parse metric help failed.")
    return False


def _parse_metric_type(metric: Metric, line: str) -> bool:
    parts = line.split()
    if len(parts) == 4 and parts[0] == "#" and parts[1] == "TYPE":
        try:
            metric.type = MetricType.from_string(parts[3])
            return True
        except KeyError:
            logger.error("[Metrics] Illegal metric type: %s", parts[3])
            return False
    logger.error("[Metrics] Parse metric type failed.")
    return False


def _parse_metric_body_block(metric: Metric, line: str) -> bool:
    # The value is always the last whitespace-separated token; rsplit once
    # so label values containing spaces (e.g. name="prepare input") survive.
    parts = line.rsplit(None, 1)
    if len(parts) != 2:
        return False

    try:
        value = float(parts[1])
    except ValueError:
        return False

    # Only gauges may legitimately be negative; a negative counter/histogram
    # is corrupt, so drop just this line rather than the whole metric text.
    if value < 0 and metric.type != MetricType.GAUGE:
        return False

    # Append label and value together so the parallel arrays stay aligned.
    label = _ENGINE_LABEL_RE.sub("", parts[0])
    metric.label.append(label)
    metric.value.append(value)
    return True


def parse_metric_text(metrics_str: str) -> list[Metric] | None:
    """Parse Prometheus text into Metric families (模块级，供 kv_usage 等复用).

    Returns:
        list[Metric]: parse completed; may be empty when every family was
            empty or dropped by per-line resilience.
        None: structural failure (bad HELP/TYPE layout); caller must fail.
    """
    lines = [ln for ln in metrics_str.splitlines() if ln.strip()]
    if not lines:
        return []

    metric_array: list[Metric] = []
    i, n = 0, len(lines)
    while i < n:
        metric = Metric()
        if not _parse_metric_help(metric, lines[i]):
            return None
        i += 1
        if i >= n or not _parse_metric_type(metric, lines[i]):
            return None
        i += 1
        sample_total = 0
        sample_failed = 0
        while i < n and not lines[i].startswith("#"):
            # A single bad body line is skipped so the rest of this instance's
            # metrics still parse; the per-family summary below records the
            # impact without taking down the full metrics text.
            sample_total += 1
            if not _parse_metric_body_block(metric, lines[i]):
                sample_failed += 1
            i += 1
        if not metric.value:
            if sample_total != 0:
                # Had samples but every line failed: drop the family instead of
                # emitting empty label/value arrays to downstream consumers.
                logger.error(
                    "[Metrics] Drop metric %s: all %d sample line(s) failed to parse",
                    metric.name,
                    sample_total,
                )
                continue
            # sample_total == 0: HELP/TYPE only (idle / just-started). Keep the
            # family with empty samples; serializers emit an explicit 0.
        elif sample_failed:
            # One WARNING per family only; per-line detail is omitted to avoid
            # spam when the same metric keeps emitting bad values each scrape.
            logger.warning(
                "[Metrics] Metric %s: skipped %d of %d bad sample line(s)",
                metric.name,
                sample_failed,
                sample_total,
            )
        metric_array.append(metric)
    return metric_array


# Mooncake Master -> a few kv_store_* families with labels (cpu/ssd/all, usage/total/rate).
_BYTES_PER_GB = 1024**3
_KVSTORE_METRIC_ALLOWLIST = frozenset(
    {
        "master_allocated_bytes",
        "master_total_capacity_bytes",
        "master_allocated_file_size_bytes",
        "master_total_file_capacity_bytes",
        "master_key_count",
        "master_successful_evictions_total",
        "master_attempted_evictions_total",
    }
)
_KVSTORE_FAMILY_HELP: dict[str, str] = {
    "kv_store_size": "KV store size in GB (layer=cpu|ssd|all, stat=usage|total)",
    "kv_store_ratio": "KV store used ratio 0-1 (layer=cpu|ssd|all, stat=usage_rate)",
    "kv_store_keys": "KV store number of stored keys",
    "kv_store_eviction": "KV store eviction counters (stat=success|attempts)",
}
_SAMPLE_VALUE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{.*\})?\s+([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_PROM_SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)$")
_PROM_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


def _emit_labeled(
    lines: list[str],
    emitted_help: set[str],
    name: str,
    value: float,
    **labels: str,
) -> None:
    if value < 0:
        return
    if name not in emitted_help:
        lines.append(f"# HELP {name} {_KVSTORE_FAMILY_HELP[name]}")
        lines.append(f"# TYPE {name} gauge")
        emitted_help.add(name)
    if labels:
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        lines.append(f"{name}{{{label_str}}} {value}")
    else:
        lines.append(f"{name} {value}")


def _emit_layer_size(
    lines: list[str],
    emitted_help: set[str],
    layer: str,
    usage_bytes: float,
    total_bytes: float,
) -> None:
    _emit_labeled(lines, emitted_help, "kv_store_size", usage_bytes / _BYTES_PER_GB, layer=layer, stat="usage")
    _emit_labeled(lines, emitted_help, "kv_store_size", total_bytes / _BYTES_PER_GB, layer=layer, stat="total")


def _emit_layer_rate(
    lines: list[str],
    emitted_help: set[str],
    layer: str,
    usage_bytes: float,
    total_bytes: float,
) -> None:
    usage_rate = usage_bytes / total_bytes if total_bytes > 0 else 0.0
    _emit_labeled(lines, emitted_help, "kv_store_ratio", usage_rate, layer=layer, stat="usage_rate")


def _filter_kvstore_metrics(raw: str, backend: str = "") -> str:
    """Filter KV store metrics to labeled kv_store_* families.

    Dispatches to the right parser based on *backend*.
    """
    if not raw.strip():
        return ""
    if backend == "memcache":
        return _filter_memcache_metrics(raw)
    return _filter_mooncake_metrics(raw)


def _emit_kv_store_prometheus(
    cpu_usage: float,
    cpu_total: float,
    ssd_usage: float,
    ssd_total: float,
    keys: float,
    eviction_success: float,
    eviction_attempts: float,
) -> str:
    """Emit kv_store_* Prometheus text from pre-parsed backend values."""
    out: list[str] = []
    emitted_help: set[str] = set()
    _emit_layer_size(out, emitted_help, "cpu", cpu_usage, cpu_total)
    _emit_layer_size(out, emitted_help, "ssd", ssd_usage, ssd_total)
    _emit_layer_size(out, emitted_help, "all", cpu_usage + ssd_usage, cpu_total + ssd_total)
    _emit_layer_rate(out, emitted_help, "cpu", cpu_usage, cpu_total)
    _emit_layer_rate(out, emitted_help, "ssd", ssd_usage, ssd_total)
    _emit_layer_rate(out, emitted_help, "all", cpu_usage + ssd_usage, cpu_total + ssd_total)
    _emit_labeled(out, emitted_help, "kv_store_keys", keys)
    _emit_labeled(out, emitted_help, "kv_store_eviction", eviction_success, stat="success")
    _emit_labeled(out, emitted_help, "kv_store_eviction", eviction_attempts, stat="attempts")
    if not out:
        return ""
    return "\n".join(out) + "\n"


def _filter_memcache_metrics(raw: str) -> str:
    """Pass through memcache metrics with ``motor:memcache_`` prefix.

    Also emits the summary ``kv_store_*`` families from capacity / keys /
    eviction data.
    """
    labels_re = re.compile(r'\{(.+)\}')
    pass_through: list[str] = []
    usage: dict[str, float] = {}
    total: dict[str, float] = {}
    keys: float = 0.0
    evictions: float = 0.0

    for line in raw.splitlines():
        stripped = line.strip()
        # Pass through HELP / TYPE lines with renamed prefix
        if stripped.startswith("# ") and "memcache_" in stripped:
            pass_through.append(stripped.replace("memcache_", "motor:memcache_"))
            continue
        if not stripped or stripped.startswith("#"):
            continue
        sample = _SAMPLE_VALUE_RE.match(stripped)
        if not sample:
            continue
        name = sample.group(1)
        val = float(sample.group(2))
        label_str = labels_re.search(stripped)
        labels = {}
        if label_str:
            labels = dict(_PROM_LABEL_RE.findall(label_str.group(1)))

        # Aggregate for kv_store_* summary
        if name == "memcache_total_capacity_bytes":
            total[labels.get("medium", "dram")] = total.get(labels.get("medium", "dram"), 0.0) + val
        elif name == "memcache_allocated_bytes":
            usage[labels.get("medium", "dram")] = usage.get(labels.get("medium", "dram"), 0.0) + val
        elif name == "memcache_stored_keys":
            keys += val
        elif name == "memcache_evict_operations_total":
            evictions += val

        # Pass through: rename memcache_ → motor:memcache_
        new_name = name.replace("memcache_", "motor:memcache_", 1)
        pass_through.append(stripped.replace(name, new_name, 1))

    # Summary metrics + pass-through all in one output
    out = _emit_kv_store_prometheus(
        usage.get("dram", 0.0),
        total.get("dram", 0.0),
        0.0,
        0.0,
        keys,
        evictions,
        0.0,
    )
    if pass_through:
        out += "\n".join(pass_through) + "\n"
    return out


def _filter_mooncake_metrics(raw: str) -> str:
    """Parse Mooncake Master metrics (``master_*`` with allowlist)."""
    values: dict[str, float] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        sample = _SAMPLE_VALUE_RE.match(stripped)
        if not sample:
            continue
        old = sample.group(1)
        if old not in _KVSTORE_METRIC_ALLOWLIST:
            continue
        values[old] = values.get(old, 0.0) + float(sample.group(2))

    return _emit_kv_store_prometheus(
        values.get("master_allocated_bytes", 0.0),
        values.get("master_total_capacity_bytes", 0.0),
        values.get("master_allocated_file_size_bytes", 0.0),
        values.get("master_total_file_capacity_bytes", 0.0),
        values.get("master_key_count", 0.0),
        values.get("master_successful_evictions_total", 0.0),
        values.get("master_attempted_evictions_total", 0.0),
    )


class MetricsCollector(ThreadSafeSingleton):
    METRICS_KEY = "metrics"

    def __init__(self, config: CoordinatorConfig | None = None):
        if hasattr(self, "_initialized"):
            return

        self._config_lock = threading.RLock()
        if config is None:
            config = CoordinatorConfig()
        self._prometheus_metrics_config = config.prometheus_metrics_config
        self._deploy_config = config.deploy_config
        self._infer_tls_config = config.infer_tls_config
        self._enable_master_standby = config.standby_config.enable_master_standby

        # Initial metrics state
        self._inactive_instance_metrics_aggregate: dict[str, list[Metric]] = {}
        self._instance_metrics_cached: dict[int, dict[str, list[Metric]]] = {}
        self._last_collects: dict[int, dict[str, Any]] = {}
        # KV 命中率 5 分钟窗口状态：key=(instance_id, endpoint_id)，
        # value=按时间升序的采样点列表 [(ts, hit_tokens, input_tokens), ...]。
        self._kv_hit_window: dict[tuple[int, int], list[tuple[float, float, float]]] = {}

        self._collects_version: int = 0
        self._caches: dict[str, Any] = {}
        self._cache_version: int = -1
        self._serialize_lock = threading.Lock()

        self._lock = threading.Lock()
        self._kv_store_metrics_text: str = ""
        self._stop_event = threading.Event()
        self._metrics_update_thread = None
        # Event loop for async get_all_instances (set from lifespan)
        self._loop = None
        # When set, use this to get scheduler (same view as scheduling); must be set in lifespan
        self._scheduler_provider: Callable[[], Any] | None = None
        # Optional kv-usage publisher callback: called with the latest collects after each
        # collect cycle, so the kv_usage cache can be fed from MetricsCollector results
        # instead of scraping engine metrics again (registered by the Obs lifespan).
        self._kv_usage_publisher: Callable[[dict | None], None] | None = None
        # Optional scheduler-metrics provider: called when /metrics is generated, returning
        # extra ``motor:*`` Metric objects (e.g. per-endpoint cnt/fresh_load/a_tokens read
        # from the SchedulerServer's shared memory). Registered by the Obs lifespan.
        self._sched_metrics_provider: Callable[[], list[Metric]] | None = None

        self._aggregation_engine = SemanticAggregationEngine()
        self._motor_computer = MotorMetricComputer()
        self._kv_store_disabled_logged = False
        self._rl = RateLimitedLogger(logger)

        self._initialized = True
        logger.info("MetricsCollector initialized.")

    def set_event_loop(self, loop):
        """Set the event loop for async calls from the metrics thread (call from lifespan)."""
        self._loop = loop

    def set_scheduler_provider(self, get_scheduler: Callable[[], Any]) -> None:
        """Use same instance view as scheduling: get_scheduler().get_all_instances() (call from lifespan)."""
        self._scheduler_provider = get_scheduler

    def set_kv_usage_publisher(self, publisher: Callable[[dict | None], None] | None) -> None:
        """Register a callback that receives each collect cycle's ``collects`` (Obs lifespan).

        Used by kv_usage to publish per-endpoint KV cache usage from the already-scraped
        metrics, avoiding a second engine scrape. Pass ``None`` to disable.
        """
        self._kv_usage_publisher = publisher

    def set_sched_metrics_provider(self, provider: Callable[[], list[Metric]] | None) -> None:
        """Register a callback returning extra ``motor:*`` Metric objects (Obs lifespan).

        Used to expose SchedulerServer-side scheduling metrics (per-endpoint cnt /
        fresh_load / a_tokens) on /metrics without engine scraping. Pass ``None`` to disable.
        """
        self._sched_metrics_provider = provider

    def start(self) -> None:
        """Start update metrics thread."""
        if self._stop_event.is_set():
            self._stop_event.clear()
        self._metrics_update_thread = threading.Thread(
            target=self._update_metrics_thread, daemon=True, name="MetricsUpdate"
        )
        self._metrics_update_thread.start()
        logger.info("MetricsCollector started.")

    def stop(self) -> None:
        """Stop update metrics thread."""
        self._stop_event.set()
        if self._metrics_update_thread and self._metrics_update_thread.is_alive():
            self._metrics_update_thread.join()
        logger.info("MetricsCollector stopped.")

    def update_config(self, config: CoordinatorConfig) -> None:
        """Update configuration for the metrics collector"""
        with self._config_lock:
            self._prometheus_metrics_config = config.prometheus_metrics_config
            self._deploy_config = config.deploy_config
            self._infer_tls_config = config.infer_tls_config
            self._enable_master_standby = config.standby_config.enable_master_standby
        logger.info("MetricsCollector configuration updated")

    def get_metrics(
        self,
        metrics_type: str = "full",
        role: str | None = None,
        metrics_format: str = _METRICS_FORMAT_PROMETHEUS,
    ) -> str | dict[str, Any]:
        """
        Unified metrics retrieval with type and format selection.

        :param metrics_type: "full" (default), "instance", "role", "dp", "node",
            or "motor" (only Motor's own computed metrics, i.e. ``motor:`` families)
        :param role: when metrics_type is "role", filter to a specific role (e.g. "prefill", "decode")
        :param metrics_format: "prometheus" (default) or "opentelemetry"
        :returns: Prometheus text or OpenTelemetry JSON-compatible dict
        """
        normalized_format = self._normalize_metrics_format(metrics_format)
        if not self._should_serve_metrics():
            if normalized_format == _METRICS_FORMAT_OPENTELEMETRY:
                return self._format_opentelemetry("")
            return _STANDBY_METRICS_TEXT
        metrics = self._get_prometheus_metrics(metrics_type, role)
        # 收集 SchedulerServer 侧调度指标（motor:endpoint_state 的 cnt/fresh_load/a_tokens/total_cnt 样本）
        # 与已采集的 per-endpoint KV usage（同族的 stat="kv_usage" 样本）。
        # 视图过滤：这些端点粒度的附加 motor: 族只随 full（含非法 type 回退）与 motor
        # 视图输出，instance/role/dp/node 视图保持引擎聚合指标语义（与 kv_store_* 一致）。
        extra_metrics: list[Metric] = []
        if metrics_type not in _EXTRA_METRIC_EXCLUDED_VIEWS:
            provider = self._sched_metrics_provider
            if provider is not None:
                try:
                    extra = provider()
                    if extra:
                        extra_metrics.extend(extra)
                except Exception as e:
                    logger.warning("[Metrics] sched metrics provider failed: %s", e)
            kv_usage_metrics = self._build_kv_usage_motor_metrics()
            if kv_usage_metrics:
                extra_metrics.extend(kv_usage_metrics)
            kv_hit_rate_metrics = self._build_kv_hit_rate_motor_metrics()
            if kv_hit_rate_metrics:
                extra_metrics.extend(kv_hit_rate_metrics)
            running_waiting_metrics = self._build_running_waiting_motor_metrics()
            if running_waiting_metrics:
                extra_metrics.extend(running_waiting_metrics)
            if extra_metrics:
                # 合并同名族（label/value 拼接），保证每个族只有一份 HELP/TYPE。
                extra_text = self._format_prometheus(self._merge_same_name_metrics(extra_metrics))
                if extra_text:
                    metrics = (metrics + "\n" + extra_text) if metrics else extra_text
        # GET /metrics?type=motor 时，只返回 motor 自身指标（motor: 前缀族）。
        if metrics_type == "motor":
            metrics = _filter_motor_only_metrics(metrics)
        if normalized_format == _METRICS_FORMAT_OPENTELEMETRY:
            return self._format_opentelemetry(metrics)
        return metrics

    @staticmethod
    def _merge_same_name_metrics(metrics_list: list[Metric]) -> list[Metric]:
        """合并同名 Metric 族（label/value 拼接），避免 Prometheus 文本重复 HELP/TYPE。

        例如 SchedulerServer 侧的 ``motor:endpoint_state``（request_count/fresh_load/
        active_tokens/total_cnt）与本进程已采集的 kv usage 样本（stat="kv_usage"）合并为一个族。
        """
        merged: dict[str, Metric] = {}
        order: list[str] = []
        for item in metrics_list:
            if item.name not in merged:
                merged[item.name] = Metric(
                    name=item.name,
                    help=item.help,
                    type=item.type,
                    label=list(item.label or []),
                    value=list(item.value or []),
                )
                order.append(item.name)
            else:
                target = merged[item.name]
                target.label.extend(item.label or [])
                target.value.extend(item.value or [])
        return [merged[name] for name in order]

    def _build_kv_usage_motor_metrics(self) -> list[Metric]:
        """把已采集的 per-endpoint ``vllm:kv_cache_usage_perc`` 转成 ``motor:endpoint_state`` 的 ``stat="kv_usage"`` 样本。

        Data comes from the latest collect cycle (``_last_collects``), the same
        source the kv usage publisher writes to the scheduler shm — no extra engine
        scrape and no scheduler round-trip. Emits one labelled sample per endpoint
        with ``instance_id`` / ``endpoint_id`` / ``role`` / ``stat="kv_usage"`` labels.
        """
        with self._lock:
            collects = self._last_collects
        labels: list[str] = []
        values: list[float] = []
        if isinstance(collects, dict):
            for ins_id, ins_data in collects.items():
                if not isinstance(ins_data, dict):
                    continue
                role = ins_data.get("role", "")
                endpoints = ins_data.get("endpoints") or {}
                for ep_id, pod_info in endpoints.items():
                    if not isinstance(pod_info, dict):
                        continue
                    for metric in pod_info.get(self.METRICS_KEY) or []:
                        if getattr(metric, "name", None) != _KV_USAGE_METRIC_NAME:
                            continue
                        metric_values = getattr(metric, "value", None) or []
                        if not metric_values:
                            continue
                        usage = max(metric_values)
                        base = f'instance_id="{ins_id}",endpoint_id="{ep_id}",role="{role}"'
                        labels.append(f'{MOTOR_ENDPOINT_STATE_METRIC}{{{base},stat="{_MOTOR_KV_USAGE_STAT}"}}')
                        values.append(usage)
        if not labels:
            return []
        return [
            Metric(
                name=MOTOR_ENDPOINT_STATE_METRIC,
                help=MOTOR_ENDPOINT_STATE_HELP,
                type=MetricType.GAUGE,
                label=labels,
                value=values,
            )
        ]

    def _build_running_waiting_motor_metrics(self) -> list[Metric]:
        """把已采集的 per-endpoint ``vllm:num_requests_running`` / ``vllm:num_requests_waiting``
        转成 ``motor:endpoint_state`` 的 ``stat="running"`` / ``stat="waiting"`` 样本。

        Data comes from the latest collect cycle (``_last_collects``), the same
        source the kv usage samples use — no extra engine scrape and no scheduler
        round-trip. Emits two labelled samples per endpoint with ``instance_id`` /
        ``endpoint_id`` / ``role`` / ``stat`` labels; missing metrics are skipped.
        """
        with self._lock:
            collects = self._last_collects
        labels: list[str] = []
        values: list[float] = []
        if isinstance(collects, dict):
            for ins_id, ins_data in collects.items():
                if not isinstance(ins_data, dict):
                    continue
                role = ins_data.get("role", "")
                endpoints = ins_data.get("endpoints") or {}
                for ep_id, pod_info in endpoints.items():
                    if not isinstance(pod_info, dict):
                        continue
                    running = self._sum_metric_values(pod_info, _REQUESTS_RUNNING_METRIC_NAME)
                    waiting = self._sum_metric_values(pod_info, _REQUESTS_WAITING_METRIC_NAME)
                    if running is None and waiting is None:
                        continue
                    base = f'instance_id="{ins_id}",endpoint_id="{ep_id}",role="{role}"'
                    if running is not None:
                        labels.append(f'{MOTOR_ENDPOINT_STATE_METRIC}{{{base},stat="{_MOTOR_RUNNING_STAT}"}}')
                        values.append(running)
                    if waiting is not None:
                        labels.append(f'{MOTOR_ENDPOINT_STATE_METRIC}{{{base},stat="{_MOTOR_WAITING_STAT}"}}')
                        values.append(waiting)
        if not labels:
            return []
        return [
            Metric(
                name=MOTOR_ENDPOINT_STATE_METRIC,
                help=MOTOR_ENDPOINT_STATE_HELP,
                type=MetricType.GAUGE,
                label=labels,
                value=values,
            )
        ]

    def _build_kv_hit_rate_motor_metrics(self) -> list[Metric]:
        """把已采集的 per-endpoint prefix cache 计数器转成 P 实例各节点的 KV 命中率样本。

        命中率统计**最近 ``_KV_HIT_WINDOW_SECONDS`` 秒（5 分钟）窗口**内的增量，
        而非引擎侧的历史累计值：

          - 命中增量 = 本地 HBM 缓存命中（``vllm:prefix_cache_hits_total``）
            + 外部缓存命中（``vllm:external_prefix_cache_hits_total``）在窗口内的差值
          - 输入增量 = ``vllm:prefix_cache_queries_total``（queried tokens，统一分母）在窗口内的差值
          - 命中率 = 命中增量 / 输入增量

        窗口内最早采样点作为基线，超出窗口的采样点被丢弃（滑动窗口）；引擎重启
        导致计数回退时窗口被清空、从当前值重新累计。仅对 role="prefill"（P 实例）
        的节点输出，每个节点生成三个样本：``stat="kv_hit_tokens"``、
        ``stat="kv_input_tokens"`` 与派生出的 ``stat="kv_hit_rate"``，与
        SchedulerServer 侧的同族样本合并。
        """
        with self._lock:
            collects = self._last_collects
        labels: list[str] = []
        values: list[float] = []
        now = time.time()
        if isinstance(collects, dict):
            for ins_id, ins_data in collects.items():
                if not isinstance(ins_data, dict):
                    continue
                # 命中率仅对 P 实例（prefill 角色）节点统计。
                role = ins_data.get("role", "")
                if role != "prefill":
                    continue
                endpoints = ins_data.get("endpoints") or {}
                for ep_id, pod_info in endpoints.items():
                    if not isinstance(pod_info, dict):
                        continue
                    local_hit = self._sum_metric_values(pod_info, _KV_HIT_TOKENS_METRIC_NAME) or 0.0
                    external_hit = self._sum_metric_values(pod_info, _KV_EXTERNAL_HIT_TOKENS_METRIC_NAME) or 0.0
                    input_tokens = self._sum_metric_values(pod_info, _KV_INPUT_TOKENS_METRIC_NAME)
                    if input_tokens is None:
                        continue
                    # 累计命中长度 = 本地 HBM 命中 + 外部缓存命中，分母统一为累计请求输入长度。
                    hit_tokens = local_hit + external_hit
                    with self._lock:
                        hit_delta, input_delta = self._update_kv_hit_window(
                            (ins_id, ep_id), now, hit_tokens, input_tokens
                        )
                    hit_rate = hit_delta / input_delta if input_delta > 0 else 0.0
                    base = f'instance_id="{ins_id}",endpoint_id="{ep_id}",role="{role}"'
                    labels.append(f'{MOTOR_ENDPOINT_STATE_METRIC}{{{base},stat="{_MOTOR_KV_HIT_TOKENS_STAT}"}}')
                    values.append(hit_delta)
                    labels.append(f'{MOTOR_ENDPOINT_STATE_METRIC}{{{base},stat="{_MOTOR_KV_INPUT_TOKENS_STAT}"}}')
                    values.append(input_delta)
                    labels.append(f'{MOTOR_ENDPOINT_STATE_METRIC}{{{base},stat="{_MOTOR_KV_HIT_RATE_STAT}"}}')
                    values.append(hit_rate)
        if not labels:
            return []
        return [
            Metric(
                name=MOTOR_ENDPOINT_STATE_METRIC,
                help=MOTOR_ENDPOINT_STATE_HELP,
                type=MetricType.GAUGE,
                label=labels,
                value=values,
            )
        ]

    def _update_kv_hit_window(
        self,
        key: tuple[int, int],
        now: float,
        hit_tokens: float,
        input_tokens: float,
    ) -> tuple[float, float]:
        """维护 (instance_id, endpoint_id) 的 5 分钟滑动窗口，返回窗口内增量。

        ``(hit_delta, input_delta)`` = 当前采样值与窗口内最早采样点之差。窗口内
        仅保留最近 ``_KV_HIT_WINDOW_SECONDS`` 秒的采样点；引擎重启（计数回退，
        当前值小于窗口内最近值）时清空窗口并从当前值重新累计。

        调用方须持有 ``self._lock``。
        """
        cutoff = now - _KV_HIT_WINDOW_SECONDS
        history = self._kv_hit_window.get(key, [])
        # 丢弃过期采样点，仅保留窗口内的。
        history = [(ts, h, i) for ts, h, i in history if ts >= cutoff]
        # 引擎重启检测：当前计数小于窗口内最近一次采样值（counter 重置）。
        if history and (hit_tokens < history[-1][1] or input_tokens < history[-1][2]):
            history = []
        history.append((now, hit_tokens, input_tokens))
        self._kv_hit_window[key] = history
        if len(history) < 2:
            # 首个采样点无基线，窗口增量暂为 0。
            return 0.0, 0.0
        _base_ts, base_hit, base_input = history[0]
        hit_delta = max(0.0, hit_tokens - base_hit)
        input_delta = max(0.0, input_tokens - base_input)
        return hit_delta, input_delta

    @staticmethod
    def _sum_metric_values(pod_info: dict, name: str) -> float | None:
        """Sum all label values of metric *name* in a pod's metrics, or None if absent."""
        total: float | None = None
        for metric in pod_info.get(MetricsCollector.METRICS_KEY) or []:
            if getattr(metric, "name", None) != name:
                continue
            metric_values = getattr(metric, "value", None) or []
            if metric_values:
                total = (total or 0.0) + float(sum(metric_values))
        return total

    def _get_prometheus_metrics(
        self,
        metrics_type: str = "full",
        role: str | None = None,
    ) -> str:
        with self._lock:
            version = self._collects_version
            collects = self._last_collects
        with self._serialize_lock:
            if self._cache_version != version:
                self._caches = {}
                self._cache_version = version
            if metrics_type == "instance":
                if "instance" not in self._caches:
                    self._caches["instance"] = self._generate_instance_metrics(collects)
                return self._caches["instance"]
            if metrics_type == "role":
                if "role" not in self._caches:
                    self._caches["role"] = self._generate_role_metrics(collects)
                if role:
                    return self._caches["role"].get(role, "")
                parts = [text.rstrip("\n") for text in self._caches["role"].values() if text]
                return ("\n".join(parts) + "\n") if parts else ""
            if metrics_type == "dp":
                if "dp" not in self._caches:
                    self._caches["dp"] = self._generate_dp_metrics(collects)
                return self._caches["dp"]
            if metrics_type == "node":
                if "node" not in self._caches:
                    self._caches["node"] = self._generate_node_metrics(collects)
                return self._caches["node"]
            if "full" not in self._caches:
                self._caches["full"] = self._generate_full_metrics(collects)
            metrics = self._caches["full"]
        pool_text = self._kv_store_metrics_text
        if pool_text:
            if metrics and not metrics.endswith("\n"):
                metrics += "\n"
            metrics += pool_text
        return metrics

    @staticmethod
    def _normalize_metrics_format(metrics_format: str | None) -> str:
        fmt = (metrics_format or _METRICS_FORMAT_PROMETHEUS).strip().lower()
        if fmt in ("", "prom", _METRICS_FORMAT_PROMETHEUS):
            return _METRICS_FORMAT_PROMETHEUS
        if fmt in ("otel", _METRICS_FORMAT_OPENTELEMETRY):
            return _METRICS_FORMAT_OPENTELEMETRY
        raise ValueError(f"Unsupported metrics format: {metrics_format}")

    @staticmethod
    def _parse_prometheus_labels(label_text: str | None) -> list[dict[str, dict[str, str]]]:
        if not label_text:
            return []
        return [{"key": key, "value": {"stringValue": value}} for key, value in _PROM_LABEL_RE.findall(label_text)]

    @classmethod
    def _format_opentelemetry(cls, prometheus_text: str) -> dict[str, Any]:
        metrics: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        current_metric = ""
        for raw_line in prometheus_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("# HELP "):
                parts = line.split(maxsplit=3)
                if len(parts) < 4:
                    continue
                current_metric = parts[2]
                if current_metric not in metrics:
                    metrics[current_metric] = {
                        "name": current_metric,
                        "description": parts[3],
                        "unit": "",
                        "type": "",
                        "dataPoints": [],
                    }
                    order.append(current_metric)
                else:
                    metrics[current_metric]["description"] = metrics[current_metric].get("description") or parts[3]
                continue
            if line.startswith("# TYPE "):
                parts = line.split(maxsplit=3)
                if len(parts) != 4:
                    continue
                current_metric = parts[2]
                if current_metric not in metrics:
                    metrics[current_metric] = {
                        "name": current_metric,
                        "description": "",
                        "unit": "",
                        "type": parts[3],
                        "dataPoints": [],
                    }
                    order.append(current_metric)
                else:
                    metrics[current_metric]["type"] = parts[3]
                continue
            sample = _PROM_SAMPLE_RE.match(line)
            if not sample:
                continue
            sample_name, label_text, value = sample.groups()
            metric_name = current_metric if current_metric else sample_name
            if metric_name not in metrics:
                metrics[metric_name] = {
                    "name": metric_name,
                    "description": "",
                    "unit": "",
                    "type": "",
                    "dataPoints": [],
                }
                order.append(metric_name)
            metrics[metric_name]["dataPoints"].append(
                {
                    "sampleName": sample_name,
                    "attributes": cls._parse_prometheus_labels(label_text),
                    "asDouble": value,
                }
            )

        return {
            "resourceMetrics": [
                {
                    "resource": {"attributes": []},
                    "scopeMetrics": [
                        {
                            "scope": {"name": "motor.coordinator.metrics"},
                            "metrics": [metrics[name] for name in order],
                        }
                    ],
                }
            ]
        }

    @classmethod
    def _inject_labels(
        cls,
        metric: Metric,
        **labels: str,
    ) -> Metric:
        extra = ",".join(f'{k}="{v}"' for k, v in labels.items())
        result = metric.copy()
        result.label = [
            lbl.replace("{", "{" + extra + ",") if "{" in lbl else lbl + "{" + extra + "}" for lbl in metric.label
        ]
        return result

    def _generate_instance_metrics(
        self,
        collects: dict[int, dict[str, Any]],
    ) -> str:
        instance_metrics = self._aggregate_collects_by_instance(collects)
        if not instance_metrics:
            return ""
        all_metrics: list[Metric] = []
        for ins_id, metrics_list in instance_metrics.items():
            role = collects.get(ins_id, {}).get("role", "unknown")
            for m in metrics_list:
                all_metrics.append(self._inject_labels(m, instance_id=str(ins_id), role=role))
        return self._format_prometheus(all_metrics)

    def _generate_role_metrics(
        self,
        collects: dict[int, dict[str, Any]],
    ) -> dict[str, str]:
        instance_metrics = self._aggregate_collects_by_instance(collects)
        role_groups: dict[str, list[list[Metric]]] = {}
        for ins_id, metrics_list in instance_metrics.items():
            role = collects.get(ins_id, {}).get("role", "unknown")
            role_groups.setdefault(role, []).append(metrics_list)

        result: dict[str, str] = {}
        ctx = AggregationContext(scope=AggregationScope.ROLE)
        for role, metrics_lists in role_groups.items():
            # Include inactive metrics for this role so counters survive restarts
            inactive_for_role = self._inactive_instance_metrics_aggregate.get(role, [])
            if inactive_for_role:
                metrics_lists = list(metrics_lists) + [inactive_for_role]
            aggregated = self._aggregation_engine.post_process(self._aggregate_metrics(metrics_lists, ctx=ctx))
            if aggregated:
                labeled = [self._inject_labels(m, role=role) for m in aggregated]
                result[role] = self._format_prometheus(labeled)
        # Append the role's HPA utilization metric (from the CapacityPlanner's
        # latest output; the colon-free alias is added by the rendering layer).
        for role in list(result.keys()):
            utilization_metrics = self._motor_computer.compute_role_utilization(collects, role)
            if utilization_metrics:
                labeled = [self._inject_labels(m, role=role) for m in utilization_metrics]
                result[role] += self._format_prometheus(labeled)
        return result

    def _should_serve_metrics(self) -> bool:
        """Standby coordinators must not republish the same engine metrics as the master."""
        with self._config_lock:
            enabled = self._enable_master_standby
        if not enabled:
            return True
        return is_master_from_role_shm()

    def _clear_collected_metrics(self) -> None:
        with self._lock:
            if self._last_collects or self._kv_store_metrics_text or self._instance_metrics_cached:
                self._last_collects = {}
                self._kv_store_metrics_text = ""
                self._instance_metrics_cached = {}
                self._inactive_instance_metrics_aggregate = {}
                self._collects_version += 1

    def _update_metrics_thread(self) -> None:
        logger.info("Metrics update thread started")
        while not self._stop_event.is_set():
            if not self._should_serve_metrics():
                self._clear_collected_metrics()
                with self._config_lock:
                    reuse_time = self._prometheus_metrics_config.reuse_time
                time.sleep(reuse_time)
                continue
            collects = self._collect_metrics()
            if collects is not None:
                with self._lock:
                    self._last_collects = collects
                    self._collects_version += 1
                # 复用本线程已抓取的结果：把 kv_cache_usage_perc 发布给 kv_usage，
                # 避免调度侧再单独查询引擎 /metrics。
                publisher = self._kv_usage_publisher
                if publisher is not None:
                    try:
                        publisher(collects)
                    except Exception as e:
                        logger.warning("[Metrics] kv usage publisher failed: %s", e)
            self._fetch_kv_store_metrics()
            with self._config_lock:
                reuse_time = self._prometheus_metrics_config.reuse_time
            time.sleep(reuse_time)

    def _fetch_kv_store_metrics(self) -> None:
        """Fetch KV store pool metrics from the configured backend.

        Enabled automatically when ``kv_cache_store_config`` is present in
        ``user_config.json``.  Env vars serve as overrides for values that
        only the deployer can provide (e.g. service hostname).
        """
        with self._config_lock:
            cfg = self._prometheus_metrics_config

        # --- determine endpoint ---
        # 1. Explicit override
        endpoint = cfg.kv_store_metrics_endpoint
        # 2. Auto-construct from config (env overrides deployer-only values)
        if not endpoint and cfg.kv_store_backend:
            master = cfg.kv_store_service
            if master:
                port = str(cfg.kv_store_metrics_port)
                if not port or port == "0":
                    port = "50090"
                endpoint = f"http://{format_address(master, port)}/metrics"

        # --- determine enabled ---
        enabled = cfg.enable_kv_store_metrics or bool(cfg.kv_store_backend)

        if not enabled:
            self._kv_store_metrics_text = ""
            # Log once when KV store metrics are not configured (no kv_cache_store_config)
            if not self._kv_store_disabled_logged:
                self._kv_store_disabled_logged = True
                logger.info(
                    "KV store metrics not enabled: backend=%r, explicit_enable=%s",
                    cfg.kv_store_backend,
                    cfg.enable_kv_store_metrics,
                )
            return
        if not endpoint:
            self._rl.error_window(
                "kv_store_metrics.no_endpoint",
                "KV store metrics enabled but endpoint is empty (service unreachable)",
                window_sec=120,
                level="WARNING",
            )
            self._kv_store_metrics_text = ""
            return
        logger.debug("Fetching KV store metrics from %s", endpoint)
        try:
            resp = requests.get(endpoint, timeout=15)
            if resp.status_code != 200:
                self._rl.error_window(
                    "kv_store_metrics.http_error",
                    f"KV store metrics HTTP {resp.status_code} from {endpoint}",
                    window_sec=60,
                    level="WARNING",
                )
                self._kv_store_metrics_text = ""
                return
            self._kv_store_metrics_text = _filter_kvstore_metrics(resp.text, cfg.kv_store_backend)
        except requests.RequestException as e:
            self._rl.error_window(
                "kv_store_metrics.fetch_failed",
                f"KV store metrics fetch {endpoint} failed: {e}",
                window_sec=60,
                level="WARNING",
            )
            self._kv_store_metrics_text = ""

    def _collect_metrics(self) -> dict[int, dict[str, Any]] | None:
        available_instances, unavailable_instances = self._get_available_instances()
        self._clear_inactive_metrics(unavailable_instances)

        # Step 1: get instances/endpoints info and get all endpoints metrics text.
        collects = self._fetch_instance_metrics(available_instances)

        # Step 2: parse metrics text to format data for all instances/endpoints.
        if not self._parse_metrics(collects):
            logger.error("[Metrics] Parse vllm server metrics failed.")
            return None

        # Step 3: compute Motor-specific DP-level metrics (e.g. TPS) and
        # inject them into each endpoint's metrics list.
        self._motor_computer.compute_pre_aggregation(collects)

        # Step 4: feed the CapacityPlanner on the collection cycle so its
        # signals advance even when no full view is scraped.  The full /
        # role views only render the planner's cached output.  A planner
        # failure must not kill the collection loop: log and keep the last
        # cached output.
        with self._config_lock:
            metrics_config = self._prometheus_metrics_config
        try:
            self._motor_computer.update_planner(collects, metrics_config)
        except Exception as e:
            logger.error("[Metrics] CapacityPlanner update failed, keeping last output: %s", e)

        return collects

    def _aggregate_collects_by_instance(
        self,
        collects: dict[int, dict[str, Any]],
    ) -> dict[int, list[Metric]]:
        """Non-destructively aggregate endpoints per instance from raw collects."""
        ctx = AggregationContext(scope=AggregationScope.INSTANCE)
        instance_metrics: dict[int, list[Metric]] = {}
        for ins_id, ins_data in collects.items():
            if not isinstance(ins_data, dict) or "endpoints" not in ins_data:
                continue
            aggr_input = []
            for pod_info in ins_data["endpoints"].values():
                if self.METRICS_KEY in pod_info:
                    aggr_input.append(pod_info[self.METRICS_KEY])
            if aggr_input:
                instance_metrics[ins_id] = self._aggregate_metrics(aggr_input, ctx=ctx)
        return instance_metrics

    def _get_available_instances(self) -> tuple[dict[int, Instance], dict[int, Instance]]:
        loop = self._loop
        if loop is None or self._scheduler_provider is None:
            return {}, {}
        try:
            future = asyncio.run_coroutine_threadsafe(self._scheduler_provider().get_all_instances(), loop)
            return future.result(timeout=10)
        except Exception as e:
            logger.warning("[Metrics] get_all_instances failed: %s", e)
            return {}, {}

    def _clear_inactive_metrics(self, unavailable_pool: dict[int, Instance]) -> None:
        # 1. get instance list to clear
        clear_ins_list = []
        for ins_id in unavailable_pool.keys():
            if ins_id in self._instance_metrics_cached:
                clear_ins_list.append(ins_id)

        if not clear_ins_list:
            return

        # 2. group clearing instances by role
        inherited_names = get_inherited_metric_names()
        role_ins_groups: dict[str, list[list[Metric]]] = {}
        for ins_id in clear_ins_list:
            role = unavailable_pool[ins_id].role
            metrics = self._instance_metrics_cached[ins_id][self.METRICS_KEY]
            aggr_input_single = []
            for metric in metrics:
                m = self._copy_metric_zero_gauge(metric)
                # Zero out inherited effective counters — their values are
                # already carried forward by new instances via baseline offset.
                if metric.name in inherited_names:
                    m = metric.copy()
                    m.value = [0.0] * len(m.value)
                aggr_input_single.append(m)
            role_ins_groups.setdefault(role, []).append(aggr_input_single)

        # 3. per-role: aggregate clearing metrics with existing inactive history
        for role, metric_lists in role_ins_groups.items():
            aggr_input = list(metric_lists)
            # add existing inactive aggregate for this role
            existing = self._inactive_instance_metrics_aggregate.get(role, [])
            if existing:
                aggr_input.append(existing)
            self._inactive_instance_metrics_aggregate[role] = self._aggregate_metrics(aggr_input)

        # 4. remove ins_id from cache
        for ins_id in clear_ins_list:
            del self._instance_metrics_cached[ins_id]

    def _parse_metrics(self, collects: dict[int, dict[str, dict[int, dict[str, str]]]]) -> bool:
        if not isinstance(collects, dict):
            logger.error("[Metrics] Invalid collects type, expected dict.")
            return False
        if not collects:
            return True

        for instance_id, inst_data in collects.items():
            if not isinstance(inst_data, dict) or not inst_data:
                logger.error("[Metrics] Invalid instance entry for instance %s", instance_id)
                return False
            pods = inst_data.get("endpoints")
            if not pods:
                logger.error("[Metrics] Missing 'endpoints' in instance %s", instance_id)
                return False

            for pod_info in pods.values():
                metrics_str = pod_info.get("metrics_str")
                if not metrics_str:
                    logger.error("[Metrics] Missing 'metrics_str' for endpoint in instance %s", instance_id)
                    return False
                parsed_metric = self._parse_metric_text(metrics_str)
                # None = structural parse failure; [] = parsed OK but no valid samples.
                if parsed_metric is None:
                    logger.error("[Metrics] Parse metric text failed for instance %s", instance_id)
                    return False
                pod_info[self.METRICS_KEY] = parsed_metric
        return True

    def _parse_metric_text(self, metrics_str: str) -> list[Metric] | None:
        """Parse Prometheus text into Metric families.

        Delegates to the module-level ``parse_metric_text`` so the same parser is
        shared with the kv_usage collector.

        Returns:
            list[Metric]: parse completed; may be empty when every family was
                empty or dropped by per-line resilience.
            None: structural failure (bad HELP/TYPE layout); caller must fail.
        """
        return parse_metric_text(metrics_str)

    def _fetch_instance_metrics(
        self,
        available_instances: dict[int, Instance],
    ) -> dict[int, dict[str, dict[int, str]]]:
        """Get instances/endpoints info and get all endpoints metrics text.

        :param available_instances: alive instances
        :returns:
            for example:
            {
                instance_id0: {
                    "endpoints": {
                        endpoint_id0: {
                            "metrics_str": "xxx"
                        },
                        endpoint_id1: ...
                    }
                },
                instance_id1: ...
            }
        """
        collects = {}
        for ins_info in available_instances.values():
            collect = self._fetch_endpoint_metrics(ins_info)
            if collect:
                collect["role"] = ins_info.role
                collect["job_name"] = ins_info.job_name
                collect["model_name"] = ins_info.model_name
                collect["engine_type"] = ins_info.engine_type
                collects[ins_info.id] = collect

        return collects

    def _fetch_endpoint_metrics(self, ins_info: Instance) -> dict[str, dict[int, str]]:
        """Get all endpoints metrics text in single instance.

        :param ins_info:
        :returns: if any failed, return {}
            for example:
            {
                "endpoints": {
                    endpoint_id0: {
                        "metrics_str": "xxx"
                    },
                    endpoint_id1: ...
                }
            }
        """
        collect = {"endpoints": {}}

        for en_info in ins_info.get_all_endpoints():
            metrics_str = query_endpoint_metrics(en_info, self._infer_tls_config)
            if not metrics_str:
                return {}
            collect["endpoints"][en_info.id] = {
                "metrics_str": metrics_str,
                "pod_ip": en_info.ip,
            }

        return collect

    def _aggregate_metrics_all_instance(
        self,
        collects: dict[int, dict[str, Any]],
        instance_roles: dict[int, str],
        instance_engine_types: dict[int, str],
    ) -> list[Metric]:
        """Aggreagte metrics of all instances."""

        if not self._instance_metrics_cached:
            return []

        aggr_input = []
        # 1. add cache data to input data
        for ins_id, ins_info in self._instance_metrics_cached.items():
            aggr_input_single = []
            for metric in ins_info[self.METRICS_KEY]:
                aggr_input_single.append(self._copy_metric_zero_gauge(metric) if ins_id not in collects else metric)
            aggr_input.append(aggr_input_single)

        # 2. add history metrics from all roles to input data
        aggr_input_single = []
        for role_metrics in self._inactive_instance_metrics_aggregate.values():
            aggr_input_single.extend(role_metrics)
        aggr_input.append(aggr_input_single)

        # 3. service-scope aggregate with role filtering
        ins_ids = list(self._instance_metrics_cached.keys()) + [-1]
        ctx = AggregationContext(
            scope=AggregationScope.SERVICE,
            ins_ids=ins_ids,
            instance_roles=instance_roles,
            instance_engine_types=instance_engine_types,
        )
        return self._aggregate_metrics(aggr_input, ctx=ctx)

    @staticmethod
    def _copy_metric_zero_gauge(metric: Metric) -> Metric:
        """Copy metric; if gauge, zero out values (inactive instances contribute 0)."""
        if metric.type != MetricType.GAUGE:
            return metric
        zeroed = metric.copy()
        zeroed.value = [0.0] * len(zeroed.value)
        return zeroed

    def _aggregate_metrics(
        self,
        metrics_list: list[list[Metric]],
        ctx: AggregationContext | None = None,
    ) -> list[Metric]:
        """Aggregate metrics from multiple sources.

        Role-scope filtering runs only when ctx.scope is SERVICE.
        """
        ins_ids = ctx.ins_ids if ctx else None
        aggr_input: dict[str, list[tuple[int, Metric]]] = {}
        for idx, metrics in enumerate(metrics_list):
            ins_id = ins_ids[idx] if ins_ids and idx < len(ins_ids) else -1
            for metric in metrics:
                if metric.name not in aggr_input:
                    aggr_input[metric.name] = []
                aggr_input[metric.name].append((ins_id, metric))

        result: list[Metric] = []
        for name, entries in aggr_input.items():
            if ctx is not None and ctx.scope == AggregationScope.SERVICE and ctx.instance_roles is not None:
                if name == "vllm:time_to_first_token_seconds" and ctx.instance_engine_types is not None:
                    entries = [
                        (ins_id, m)
                        for ins_id, m in entries
                        if (
                            ins_id == -1
                            or ctx.instance_roles.get(ins_id) in {"decode", "union", "both", "hybrid"}
                            or (
                                ctx.instance_roles.get(ins_id) == "prefill"
                                and ctx.instance_engine_types.get(ins_id, "").strip().lower() == "vllm"
                            )
                        )
                    ]
                    if not entries:
                        continue
                else:
                    role_scope = MetricRegistry.get_effective_role_scope(name)
                    if role_scope:
                        entries = [
                            (ins_id, m)
                            for ins_id, m in entries
                            if (
                                ins_id == -1
                                or ctx.instance_roles.get(ins_id) == role_scope
                                or (
                                    role_scope == "decode"
                                    and ctx.instance_roles.get(ins_id) in {"union", "both", "hybrid"}
                                )
                            )
                        ]
                        if not entries:
                            continue
            metric_list = [m for _, m in entries]
            result.append(self._aggregate_single_metric(metric_list))
        return result

    def _aggregate_single_metric(self, metric_list: list[Metric]) -> Metric:
        return self._aggregation_engine.aggregate(metric_list[0].name, metric_list)

    def _generate_full_metrics(
        self,
        collects: dict[int, dict[str, Any]],
    ) -> str:
        instance_metrics = self._aggregate_collects_by_instance(collects)
        if not instance_metrics:
            return ""
        for ins_id, metrics_list in instance_metrics.items():
            self._instance_metrics_cached[ins_id] = {self.METRICS_KEY: metrics_list}
        instance_roles = {ins_id: data.get("role", "") for ins_id, data in collects.items() if isinstance(data, dict)}
        instance_engine_types = {
            ins_id: str(data.get("engine_type", "")) for ins_id, data in collects.items() if isinstance(data, dict)
        }
        aggregate = self._aggregation_engine.post_process(
            self._aggregate_metrics_all_instance(collects, instance_roles, instance_engine_types)
        )
        with self._config_lock:
            deploy_config = self._deploy_config
            metrics_config = self._prometheus_metrics_config
        self._motor_computer.compute_post_aggregation(aggregate, collects, deploy_config, metrics_config)
        return self._format_prometheus(aggregate)

    def _format_prometheus(self, aggregate: list[Metric]) -> str:
        lines = []
        for item in aggregate:
            lines.append("# HELP {} {}".format(item.name, item.help))
            lines.append("# TYPE {} {}".format(item.name, item.type))
            sample_lines = self._format_metric_samples(item)
            lines.extend(sample_lines)
            alias = get_hpa_alias(item.name)
            if alias is not None:
                # HPA contract: expose the same family a second time under a
                # colon-free name for K8s External Metrics consumers.
                lines.append("# HELP {} {} (HPA alias)".format(alias, item.help))
                lines.append("# TYPE {} {}".format(alias, item.type))
                for sample in sample_lines:
                    lines.append(alias + sample[len(item.name) :])
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    @staticmethod
    def _format_metric_samples(item: Metric) -> list[str]:
        """Render the sample lines of one metric family (labels carry the name)."""
        if not item.label or not item.value:
            # Keep zero-valued / empty-sample families visible on :1027/metrics.
            return ["{} 0".format(item.name)]
        sample_lines = []
        for i, label in enumerate(item.label):
            v = item.value[i]
            if math.isnan(v):
                vs = "Nan"
            elif v == float("inf"):
                vs = "+Inf"
            elif v == float("-inf"):
                vs = "-Inf"
            else:
                vs = str(v)
            sample_lines.append("{} {}".format(label, vs))
        return sample_lines

    @staticmethod
    def _prepend_dim_labels(
        label_str: str,
        dim_labels: str,
    ) -> str:
        if "{" not in label_str:
            return f"{label_str}{{{dim_labels}}}"
        name_part, rest = label_str.split("{", 1)
        if rest == "}":
            return f"{name_part}{{{dim_labels}}}"
        return f"{name_part}{{{dim_labels},{rest}"

    @staticmethod
    def _metric_value_str(value: float) -> str:
        if math.isnan(value):
            return "Nan"
        if value == float("inf"):
            return "+Inf"
        if value == float("-inf"):
            return "-Inf"
        return str(value)

    @staticmethod
    def _escape_prometheus_label_value(value: str) -> str:
        """Escape a label value for Prometheus text exposition format."""
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    @staticmethod
    def _empty_family_base_label(name: str, model_name: str) -> str:
        """Base label for an empty (HELP/TYPE-only) family.

        Engine omits the sample line until the series is first written, so the
        synthesized zero line has no engine-side labels. Carry the Motor-known
        model_name so the output matches sibling endpoints that did emit a
        sample (which carry model_name); never invent other engine labels.
        """
        if model_name:
            escaped = MetricsCollector._escape_prometheus_label_value(model_name)
            return f'{name}{{model_name="{escaped}"}}'
        return name

    @staticmethod
    def _emit_metric_groups(name_to_meta: dict[str, dict[str, Any]]) -> str:
        out_lines: list[str] = []
        for name in sorted(name_to_meta.keys()):
            meta = name_to_meta[name]
            out_lines.append(f"# HELP {name} {meta['help']}")
            out_lines.append(f"# TYPE {name} {meta['type']}")
            if not meta["lines"]:
                # No sample lines (idle / empty family): still expose an explicit 0.
                out_lines.append(f"{name} 0")
            else:
                meta["lines"].sort(key=lambda kv: (kv[0], kv[1]))
                out_lines.extend(line for _, line in meta["lines"])
        if not out_lines:
            return ""
        return "\n".join(out_lines) + "\n"

    def _generate_dp_metrics(self, collects: dict[int, dict[str, Any]]) -> str:
        name_to_meta: dict[str, dict[str, Any]] = {}
        for instance_id, ins_collect in collects.items():
            if not isinstance(ins_collect, dict) or "endpoints" not in ins_collect:
                continue
            role = ins_collect.get("role", "")
            model_name = ins_collect.get("model_name", "")
            for ep_id, pod_info in ins_collect["endpoints"].items():
                if self.METRICS_KEY not in pod_info:
                    continue
                pod_ip = pod_info.get("pod_ip", "")
                dim_labels = f'dp_rank="{ep_id}",role="{role}",instance_id="{instance_id}",pod_ip="{pod_ip}"'
                for metric in pod_info[self.METRICS_KEY]:
                    meta = name_to_meta.setdefault(
                        metric.name,
                        {"help": metric.help, "type": metric.type, "lines": []},
                    )
                    if not metric.label or not metric.value:
                        base = self._empty_family_base_label(metric.name, model_name)
                        new_label = self._prepend_dim_labels(base, dim_labels)
                        meta["lines"].append(((instance_id, ep_id), f"{new_label} {self._metric_value_str(0.0)}"))
                        continue
                    for i, label_str in enumerate(metric.label):
                        new_label = self._prepend_dim_labels(label_str, dim_labels)
                        meta["lines"].append(
                            ((instance_id, ep_id), f"{new_label} {self._metric_value_str(metric.value[i])}")
                        )
        return self._emit_metric_groups(name_to_meta)

    def _generate_node_metrics(self, collects: dict[int, dict[str, Any]]) -> str:
        key_to_lists: dict[tuple[str, str], list[list[Metric]]] = {}
        key_to_model: dict[tuple[str, str], str] = {}
        for ins_collect in collects.values():
            if not isinstance(ins_collect, dict) or "endpoints" not in ins_collect:
                continue
            role = ins_collect.get("role", "")
            model_name = ins_collect.get("model_name", "")
            for pod_info in ins_collect["endpoints"].values():
                if self.METRICS_KEY not in pod_info:
                    continue
                pod_ip = pod_info.get("pod_ip", "")
                if not pod_ip:
                    continue
                key_to_lists.setdefault((pod_ip, role), []).append(pod_info[self.METRICS_KEY])
                key_to_model.setdefault((pod_ip, role), model_name)
        node_ctx = AggregationContext(scope=AggregationScope.NODE)
        pod_aggregates = {
            key: self._aggregate_metrics(metrics_lists, ctx=node_ctx)
            for key, metrics_lists in key_to_lists.items()
            if metrics_lists
        }
        name_to_meta: dict[str, dict[str, Any]] = {}
        for (pod_ip, role), aggregate in pod_aggregates.items():
            dim_labels = f'pod_ip="{pod_ip}",role="{role}"'
            model_name = key_to_model.get((pod_ip, role), "")
            for metric in aggregate:
                meta = name_to_meta.setdefault(
                    metric.name,
                    {"help": metric.help, "type": metric.type, "lines": []},
                )
                if not metric.label or not metric.value:
                    base = self._empty_family_base_label(metric.name, model_name)
                    new_label = self._prepend_dim_labels(base, dim_labels)
                    meta["lines"].append(((pod_ip, role), f"{new_label} {self._metric_value_str(0.0)}"))
                    continue
                for i, label_str in enumerate(metric.label):
                    new_label = self._prepend_dim_labels(label_str, dim_labels)
                    meta["lines"].append(((pod_ip, role), f"{new_label} {self._metric_value_str(metric.value[i])}"))
        return self._emit_metric_groups(name_to_meta)
