# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

# Live verification of the HPA autoscaling metrics on the full /metrics pipeline.
# Feeds raw engine Prometheus text through parse -> compute -> aggregate -> render.

from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.metrics.aggregation_engine import SemanticAggregationEngine
from motor.coordinator.metrics.metric_computer import MotorMetricComputer
from motor.coordinator.metrics.metrics_collector import MetricsCollector
import threading

ENGINE_TEXT = """# HELP vllm:num_requests_waiting Number of requests waiting to be processed.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{model_name="m"} 6.0
# HELP vllm:kv_cache_usage_perc KV cache usage percentage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{model_name="m"} 0.72
# HELP vllm:prompt_tokens_total Number of prompt tokens processed.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{model_name="m"} 80000.0
# HELP vllm:generation_tokens_total Number of generation tokens processed.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{model_name="m"} 30000.0
# HELP vllm:request_success_total Count of successfully processed requests.
# TYPE vllm:request_success_total counter
vllm:request_success_total{finished_reason="stop",model_name="m"} 120.0
# HELP vllm:request_prefill_time_seconds Histogram of time spent in prefill.
# TYPE vllm:request_prefill_time_seconds histogram
vllm:request_prefill_time_seconds_bucket{le="0.1",model_name="m"} 10.0
vllm:request_prefill_time_seconds_bucket{le="0.5",model_name="m"} 40.0
vllm:request_prefill_time_seconds_bucket{le="1.0",model_name="m"} 50.0
vllm:request_prefill_time_seconds_bucket{le="+Inf",model_name="m"} 50.0
vllm:request_prefill_time_seconds_count{model_name="m"} 50.0
vllm:request_prefill_time_seconds_sum{model_name="m"} 15.0
"""


ENGINE_TEXT_CYCLE2 = """# HELP vllm:num_requests_waiting Number of requests waiting to be processed.
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{model_name="m"} 6.0
# HELP vllm:kv_cache_usage_perc KV cache usage percentage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{model_name="m"} 0.72
# HELP vllm:prompt_tokens_total Number of prompt tokens processed.
# TYPE vllm:prompt_tokens_total counter
vllm:prompt_tokens_total{model_name="m"} 88000.0
# HELP vllm:generation_tokens_total Number of generation tokens processed.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{model_name="m"} 33000.0
# HELP vllm:request_success_total Count of successfully processed requests.
# TYPE vllm:request_success_total counter
vllm:request_success_total{finished_reason="stop",model_name="m"} 130.0
# HELP vllm:request_prefill_time_seconds Histogram of time spent in prefill.
# TYPE vllm:request_prefill_time_seconds histogram
vllm:request_prefill_time_seconds_bucket{le="0.1",model_name="m"} 11.0
vllm:request_prefill_time_seconds_bucket{le="0.5",model_name="m"} 44.0
vllm:request_prefill_time_seconds_bucket{le="1.0",model_name="m"} 55.0
vllm:request_prefill_time_seconds_bucket{le="+Inf",model_name="m"} 55.0
vllm:request_prefill_time_seconds_count{model_name="m"} 55.0
vllm:request_prefill_time_seconds_sum{model_name="m"} 16.5
"""


def make_collect(collector, role, engine_text=ENGINE_TEXT, job_name="job0"):
    metrics = collector._parse_metric_text(engine_text)
    return {
        "role": role,
        "job_name": job_name,
        "model_name": "m",
        "engine_type": "vllm",
        "endpoints": {0: {"metrics_str": engine_text, "metrics": metrics}},
    }


collector = MetricsCollector.__new__(MetricsCollector)
collector._initialized = True
collector._inactive_instance_metrics_aggregate = {}
collector._instance_metrics_cached = {}
collector._config_lock = threading.RLock()

config = CoordinatorConfig()
collector._prometheus_metrics_config = config.prometheus_metrics_config
collector._deploy_config = config.deploy_config
collector._infer_tls_config = config.infer_tls_config
collector._aggregation_engine = SemanticAggregationEngine()
collector._motor_computer = MotorMetricComputer()


def make_collects(engine_text):
    return {
        i: make_collect(
            collector,
            "prefill" if i <= 2 else "decode",
            engine_text,
            job_name=f"job{i}",
        )
        for i in range(1, 7)
    }


# The planner differentiates cumulative counters across collection cycles: the
# first cycle only establishes baselines, the second one (growing counters)
# calibrates capacity and produces utilization / replicas_required.
metrics_config = collector._prometheus_metrics_config
collects = None
for engine_text in (ENGINE_TEXT, ENGINE_TEXT_CYCLE2):
    collects = make_collects(engine_text)
    collector._motor_computer.compute_pre_aggregation(collects)
    collector._motor_computer.update_planner(collects, metrics_config)

full = collector._generate_full_metrics(collects)

WANT = [
    "motor:prefill_utilization",
    "motor:decode_utilization",
    "motor_prefill_utilization",
    "motor_decode_utilization",
    "motor:prefill_replicas_required",
    "motor:decode_replicas_required",
    "motor_prefill_replicas_required",
    "motor_decode_replicas_required",
    "motor:request_rate",
    "motor_request_rate",
    "motor:pd_ratio_current",
    "motor_pd_ratio_current",
    "motor:pd_ratio_suggested",
    "motor_pd_ratio_suggested",
    "vllm:request_prefill_time_seconds_p50",
    "vllm:request_prefill_time_seconds_p95",
    "vllm:request_prefill_time_seconds_p99",
    "vllm_request_prefill_time_seconds_p95",
]
print("=== full view: new metric families ===")
missing = []
for name in WANT:
    hit = any(line.startswith(name + " ") or line.startswith(name + "{") for line in full.splitlines())
    print(("OK   " if hit else "MISS ") + name)
    if not hit:
        missing.append(name)

print("=== sample lines ===")
for line in full.splitlines():
    if "pd_ratio" in line or "utilization" in line or "replicas_required" in line:
        if not line.startswith("#"):
            print(line)

assert "saturation" not in full, "removed saturation metrics must not be rendered"

role_view = collector._generate_role_metrics(collects)
print("=== role view keys ===", sorted(role_view.keys()))
assert not missing, f"missing: {missing}"
assert "motor:prefill_utilization" in role_view["prefill"]
assert "motor_prefill_utilization" in role_view["prefill"], "role view must carry the colon-free HPA alias too"
print("LIVE VERIFICATION PASSED")
