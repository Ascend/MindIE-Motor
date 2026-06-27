# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
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
from urllib import request as urllib_request
from urllib.error import URLError

from motor.common.resources.dispatch import DispatchPlan
from motor.common.resources.instance import Instance
from motor.common.logger import get_logger
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.api_client.engine_server_api_client import EngineServerApiClient
from motor.coordinator.metrics.metric_types import (
    AggregationContext,
    AggregationScope,
    Metric,
    MetricType,
)
from motor.coordinator.metrics.aggregation_engine import SemanticAggregationEngine
from motor.coordinator.metrics.metric_registry import MetricRegistry
from motor.coordinator.metrics.metric_computer import MotorMetricComputer, get_inherited_metric_names

logger = get_logger(__name__)

_METRICS_FORMAT_PROMETHEUS = "prometheus"
_METRICS_FORMAT_OPENTELEMETRY = "opentelemetry"

# Mooncake Master -> a few kv_pool_* families with labels (cpu/ssd/all, usage/total/rate).
_BYTES_PER_GB = 1024**3
_KVPOOL_METRIC_ALLOWLIST = frozenset(
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
_KVPOOL_FAMILY_HELP: dict[str, str] = {
    "kv_pool_size": "KV pool size in GB (layer=cpu|ssd|all, stat=usage|total)",
    "kv_pool_ratio": "KV pool used ratio 0-1 (layer=cpu|ssd|all, stat=usage_rate)",
    "kv_pool_keys": "KV pool number of stored keys",
    "kv_pool_eviction": "KV pool eviction counters (stat=success|attempts)",
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
        lines.append(f"# HELP {name} {_KVPOOL_FAMILY_HELP[name]}")
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
    _emit_labeled(lines, emitted_help, "kv_pool_size", usage_bytes / _BYTES_PER_GB, layer=layer, stat="usage")
    _emit_labeled(lines, emitted_help, "kv_pool_size", total_bytes / _BYTES_PER_GB, layer=layer, stat="total")


def _emit_layer_rate(
    lines: list[str],
    emitted_help: set[str],
    layer: str,
    usage_bytes: float,
    total_bytes: float,
) -> None:
    usage_rate = usage_bytes / total_bytes if total_bytes > 0 else 0.0
    _emit_labeled(lines, emitted_help, "kv_pool_ratio", usage_rate, layer=layer, stat="usage_rate")


def _filter_kvpool_metrics(raw: str) -> str:
    """Filter Mooncake Master metrics to labeled kv_pool_* families."""
    if not raw.strip():
        return ""
    values: dict[str, float] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        sample = _SAMPLE_VALUE_RE.match(stripped)
        if not sample:
            continue
        old = sample.group(1)
        if old not in _KVPOOL_METRIC_ALLOWLIST:
            continue
        values[old] = values.get(old, 0.0) + float(sample.group(2))

    out: list[str] = []
    emitted_help: set[str] = set()
    cpu_usage = values.get("master_allocated_bytes", 0.0)
    cpu_total = values.get("master_total_capacity_bytes", 0.0)
    ssd_usage = values.get("master_allocated_file_size_bytes", 0.0)
    ssd_total = values.get("master_total_file_capacity_bytes", 0.0)

    total_usage = cpu_usage + ssd_usage
    total_cap = cpu_total + ssd_total

    _emit_layer_size(out, emitted_help, "cpu", cpu_usage, cpu_total)
    _emit_layer_size(out, emitted_help, "ssd", ssd_usage, ssd_total)
    _emit_layer_size(out, emitted_help, "all", total_usage, total_cap)

    _emit_layer_rate(out, emitted_help, "cpu", cpu_usage, cpu_total)
    _emit_layer_rate(out, emitted_help, "ssd", ssd_usage, ssd_total)
    _emit_layer_rate(out, emitted_help, "all", total_usage, total_cap)

    _emit_labeled(out, emitted_help, "kv_pool_keys", values.get("master_key_count", 0.0))
    _emit_labeled(
        out,
        emitted_help,
        "kv_pool_eviction",
        values.get("master_successful_evictions_total", 0.0),
        stat="success",
    )
    _emit_labeled(
        out,
        emitted_help,
        "kv_pool_eviction",
        values.get("master_attempted_evictions_total", 0.0),
        stat="attempts",
    )

    if not out:
        return ""
    return "\n".join(out) + "\n"


class MetricsCollector(ThreadSafeSingleton):
    METRICS_KEY = "metrics"
    _ENGINE_LABEL_RE = re.compile(r'engine="\d+",')

    def __init__(self, config: CoordinatorConfig | None = None):
        if hasattr(self, "_initialized"):
            return

        self._config_lock = threading.RLock()
        if config is None:
            config = CoordinatorConfig()
        self._prometheus_metrics_config = config.prometheus_metrics_config
        self._deploy_config = config.deploy_config

        # Initial metrics state
        self._inactive_instance_metrics_aggregate: dict[str, list[Metric]] = {}
        self._instance_metrics_cached: dict[int, dict[str, list[Metric]]] = {}
        self._last_collects: dict[int, dict[str, Any]] = {}

        self._collects_version: int = 0
        self._caches: dict[str, Any] = {}
        self._cache_version: int = -1
        self._serialize_lock = threading.Lock()

        self._lock = threading.Lock()
        self._pool_metrics_text: str = ""
        self._stop_event = threading.Event()
        self._metrics_update_thread = None
        # Event loop for async get_all_instances (set from lifespan)
        self._loop = None
        # When set, use this to get scheduler (same view as scheduling); must be set in lifespan
        self._scheduler_provider: Callable[[], Any] | None = None

        self._aggregation_engine = SemanticAggregationEngine()
        self._motor_computer = MotorMetricComputer()

        self._initialized = True
        logger.info("MetricsCollector initialized.")

    def set_event_loop(self, loop):
        """Set the event loop for async calls from the metrics thread (call from lifespan)."""
        self._loop = loop

    def set_scheduler_provider(self, get_scheduler: Callable[[], Any]) -> None:
        """Use same instance view as scheduling: get_scheduler().get_all_instances() (call from lifespan)."""
        self._scheduler_provider = get_scheduler

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
        logger.info("MetricsCollector configuration updated")

    def get_metrics(
        self,
        metrics_type: str = "full",
        role: str | None = None,
        metrics_format: str = _METRICS_FORMAT_PROMETHEUS,
    ) -> str | dict[str, Any]:
        """
        Unified metrics retrieval with type and format selection.

        :param metrics_type: "full" (default), "instance", "role", "dp", or "node"
        :param role: when metrics_type is "role", filter to a specific role (e.g. "prefill", "decode")
        :param metrics_format: "prometheus" (default) or "opentelemetry"
        :returns: Prometheus text or OpenTelemetry JSON-compatible dict
        """
        normalized_format = self._normalize_metrics_format(metrics_format)
        metrics = self._get_prometheus_metrics(metrics_type, role)
        if normalized_format == _METRICS_FORMAT_OPENTELEMETRY:
            return self._format_opentelemetry(metrics)
        return metrics

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
                return "\n".join(self._caches["role"].values())
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
        pool_text = self._pool_metrics_text
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
        return result

    def _update_metrics_thread(self) -> None:
        while not self._stop_event.is_set():
            collects = self._collect_metrics()
            if collects is not None:
                with self._lock:
                    self._last_collects = collects
                    self._collects_version += 1
            self._fetch_pool_metrics()
            with self._config_lock:
                reuse_time = self._prometheus_metrics_config.reuse_time
            time.sleep(reuse_time)

    def _fetch_pool_metrics(self) -> None:
        with self._config_lock:
            cfg = self._prometheus_metrics_config
            enabled = cfg.pool_metrics_enable
            endpoint = cfg.pool_metrics_endpoint
        if not enabled or not endpoint:
            self._pool_metrics_text = ""
            return
        try:
            with urllib_request.urlopen(endpoint, timeout=5) as resp:
                status_code = getattr(resp, "status", 200)
                if status_code != 200:
                    logger.warning("Pool metrics fetch got HTTP %s from %s", status_code, endpoint)
                    self._pool_metrics_text = ""
                    return
                raw = resp.read().decode("utf-8", errors="replace")
                self._pool_metrics_text = _filter_kvpool_metrics(raw)
        except (URLError, TimeoutError, OSError) as e:
            logger.warning("Pool metrics fetch %s failed: %s", endpoint, e)
            self._pool_metrics_text = ""

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
                if not parsed_metric:
                    logger.error("[Metrics] Parse metric text failed for instance %s", instance_id)
                    return False
                pod_info[self.METRICS_KEY] = parsed_metric
        return True

    def _parse_metric_text(self, metrics_str: str) -> list[Metric]:
        lines = [ln for ln in metrics_str.splitlines() if ln.strip()]
        if not lines:
            return []

        metric_array: list[Metric] = []
        i, n = 0, len(lines)
        while i < n:
            metric = Metric()
            if not self._parse_metric_help(metric, lines[i]):
                return []
            i += 1
            if i >= n or not self._parse_metric_type(metric, lines[i]):
                return []
            i += 1
            while i < n and not lines[i].startswith("#"):
                if not self._parse_metric_body_block(metric, lines[i]):
                    return []
                i += 1
            metric_array.append(metric)
        return metric_array

    @staticmethod
    def _parse_metric_help(
        metric: Metric,
        line: str,
    ) -> bool:
        parts = line.split()
        if len(parts) >= 4 and parts[0] == "#" and parts[1] == "HELP":
            metric.name = parts[2]
            metric.help = " ".join(parts[3:])
            return True
        logger.error("[Metrics] Parse metric help failed.")
        return False

    @staticmethod
    def _parse_metric_type(
        metric: Metric,
        line: str,
    ) -> bool:
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

    @classmethod
    def _parse_metric_body_block(
        cls,
        metric: Metric,
        line: str,
    ) -> bool:
        parts = line.rsplit(None, 1)
        if len(parts) != 2:
            logger.error("[Metrics] Parse metric body failed.")
            return False

        label = cls._ENGINE_LABEL_RE.sub("", parts[0])
        metric.label.append(label)
        try:
            value = float(parts[1])
            if value < 0:
                logger.error("[Metrics] Illegal metric value: %s", parts[1])
                return False
            metric.value.append(value)
        except ValueError:
            logger.error("[Metrics] Illegal metric value: %s", parts[1])
            return False
        return True

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
                collect["dispatch_capabilities"] = list(ins_info.dispatch_capabilities or [])
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
            metrics_str = EngineServerApiClient.query_metrics(f"{en_info.ip}:{en_info.mgmt_port}")
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
        instance_dispatch_capabilities: dict[int, set[str]],
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
            instance_dispatch_capabilities=instance_dispatch_capabilities,
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
                if name == "vllm:time_to_first_token_seconds" and ctx.instance_dispatch_capabilities is not None:
                    handoff = DispatchPlan.PREFILL_HANDOFF_DECODE.value
                    entries = [
                        (ins_id, m)
                        for ins_id, m in entries
                        if (
                            ins_id == -1
                            or ctx.instance_roles.get(ins_id) in {"decode", "union", "both", "hybrid"}
                            or (
                                ctx.instance_roles.get(ins_id) == "prefill"
                                and handoff in ctx.instance_dispatch_capabilities.get(ins_id, set())
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
        instance_dispatch_capabilities = {
            ins_id: set(data.get("dispatch_capabilities", []))
            for ins_id, data in collects.items()
            if isinstance(data, dict)
        }
        aggregate = self._aggregation_engine.post_process(
            self._aggregate_metrics_all_instance(collects, instance_roles, instance_dispatch_capabilities)
        )
        with self._config_lock:
            deploy_config = self._deploy_config
        self._motor_computer.compute_post_aggregation(aggregate, collects, deploy_config)
        return self._format_prometheus(aggregate)

    def _format_prometheus(self, aggregate: list[Metric]) -> str:
        lines = []
        for item in aggregate:
            lines.append("# HELP {} {}".format(item.name, item.help))
            lines.append("# TYPE {} {}".format(item.name, item.type))
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
                lines.append("{} {}".format(label, vs))
        return "\n".join(lines)

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
    def _emit_metric_groups(name_to_meta: dict[str, dict[str, Any]]) -> str:
        out_lines: list[str] = []
        for name in sorted(name_to_meta.keys()):
            meta = name_to_meta[name]
            out_lines.append(f"# HELP {name} {meta['help']}")
            out_lines.append(f"# TYPE {name} {meta['type']}")
            meta["lines"].sort(key=lambda kv: (kv[0], kv[1]))
            out_lines.extend(line for _, line in meta["lines"])
        return "\n".join(out_lines)

    def _generate_dp_metrics(self, collects: dict[int, dict[str, Any]]) -> str:
        name_to_meta: dict[str, dict[str, Any]] = {}
        for instance_id, ins_collect in collects.items():
            if not isinstance(ins_collect, dict) or "endpoints" not in ins_collect:
                continue
            role = ins_collect.get("role", "")
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
                    for i, label_str in enumerate(metric.label):
                        new_label = self._prepend_dim_labels(label_str, dim_labels)
                        meta["lines"].append(
                            ((instance_id, ep_id), f"{new_label} {self._metric_value_str(metric.value[i])}")
                        )
        return self._emit_metric_groups(name_to_meta)

    def _generate_node_metrics(self, collects: dict[int, dict[str, Any]]) -> str:
        key_to_lists: dict[tuple[str, str], list[list[Metric]]] = {}
        for ins_collect in collects.values():
            if not isinstance(ins_collect, dict) or "endpoints" not in ins_collect:
                continue
            role = ins_collect.get("role", "")
            for pod_info in ins_collect["endpoints"].values():
                if self.METRICS_KEY not in pod_info:
                    continue
                pod_ip = pod_info.get("pod_ip", "")
                if not pod_ip:
                    continue
                key_to_lists.setdefault((pod_ip, role), []).append(pod_info[self.METRICS_KEY])
        node_ctx = AggregationContext(scope=AggregationScope.NODE)
        pod_aggregates = {
            key: self._aggregate_metrics(metrics_lists, ctx=node_ctx)
            for key, metrics_lists in key_to_lists.items()
            if metrics_lists
        }
        name_to_meta: dict[str, dict[str, Any]] = {}
        for (pod_ip, role), aggregate in pod_aggregates.items():
            dim_labels = f'pod_ip="{pod_ip}",role="{role}"'
            for metric in aggregate:
                meta = name_to_meta.setdefault(
                    metric.name,
                    {"help": metric.help, "type": metric.type, "lines": []},
                )
                for i, label_str in enumerate(metric.label):
                    new_label = self._prepend_dim_labels(label_str, dim_labels)
                    meta["lines"].append(((pod_ip, role), f"{new_label} {self._metric_value_str(metric.value[i])}"))
        return self._emit_metric_groups(name_to_meta)
