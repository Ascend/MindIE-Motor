# Metrics Interfaces

## Interface Overview

The monitoring metrics of MindIE Motor are uniformly collected, aggregated, and provided by the Coordinator. **The only recommended way to obtain them is the `GET /metrics` interface on the Coordinator Observability port**.

The metric collection chain is as follows:

1. The `MetricsCollector` background thread of the Coordinator periodically (according to `prometheus_metrics_config.reuse_time`, default `3` seconds) pulls raw metrics from the `/metrics` interface of each Engine;

2. It parses the Prometheus text, aggregates across instances and endpoints according to metric semantics (sum/max/mean/histogram merging/pass-through), and computes Motor custom metrics;

3. The aggregation results are cached by collection version and provided externally through this interface.

>[!NOTE]NOTE
>
> - The `/observability/metrics` on the Controller side is a deprecated forwarding proxy (forwarding to the Coordinator `/metrics`), retained only for compatibility scenarios. **For new integrations, use this interface directly**.
> - The default port `1027` is the same as the default port of the Controller observation interface (`observability_api_port`, which hosts `/observability/inventory` and others), but the two belong to different components and different services. Do not confuse them during configuration.

## Interface Format

Request type: **GET**

> URL: `http(s)://{IP}:{Port}/metrics`

**IP**

- When deployed with Kubernetes, the IP uses the host IP or domain name.

- Within the Kubernetes cluster, the IP uses the IP of the `Coordinator` service:

  - The value comes from the `coordinator_api_host` configuration item in the [`user_config.json`](../configuration/config_reference.md#motor_coordinator_config) configuration file;

  - When not configured, the environment variable `POD_IP` is used;

  - When the environment variable does not exist or is empty, the default value `127.0.0.1` is used.

**Port**

- When deployed with Kubernetes, the port uses the `nodePort` defined in the `mindie-motor-coordinator-obs` metadata of the `yaml` file, which defaults to `31017`.

- Within a Kubernetes cluster, the port uses the port defined by the `coordinator_obs_port` configuration item, which defaults to `1027`.

**Protocol**

- The security protocol is controlled by `mgmt_tls_config.enable_tls`: when it is `true`, `https` is used; otherwise, `http` is used.

## Request Parameters

| Parameter Name | Type | Required | Default Value | Description |
|--------|------|------|--------|------|
| `type` | string | No | `full` | Metric aggregation view. Value: `full` / `instance` / `role` / `dp` / `node`. For details, see [Aggregation View](#aggregation-view). Invalid values fall back to `full`. |
| `role` | string | No | None | Takes effect only when `type=role`. Filters the specified role: `prefill` or `decode`. If not specified, returns the aggregation result of all roles. |
| `format` | string | No | `prometheus` | Return format: `prometheus` (Prometheus text) or `opentelemetry` (OpenTelemetry JSON). Invalid values return HTTP `400`. |

**Usage Examples**

```bash
# Full aggregation metrics (default, identical to the behavior without parameters)
curl -X GET "http://{IP}:{Port}/metrics"

# Instance-level metrics (inject instance_id and role into labels)
curl -X GET "http://{IP}:{Port}/metrics?type=instance"

# Aggregation metrics of all roles (concatenated into a single text by role)
curl -X GET "http://{IP}:{Port}/metrics?type=role"

# Aggregation metrics of only the prefill/decode role
curl -X GET "http://{IP}:{Port}/metrics?type=role&role=prefill"
curl -X GET "http://{IP}:{Port}/metrics?type=role&role=decode"

# Output at DP endpoint granularity (no aggregation)
curl -X GET "http://{IP}:{Port}/metrics?type=dp"

# Aggregate by physical node
curl -X GET "http://{IP}:{Port}/metrics?type=node"

# Return in OpenTelemetry JSON format
curl -X GET "http://{IP}:{Port}/metrics?format=opentelemetry"
```

## Aggregation View

The `type` parameter controls the aggregation granularity of metrics. Different views inject different dimension labels:

| Value | Aggregation Granularity | Injected Tag | Purpose |
|------|----------|----------|------|
| `full` (default) | Cluster-wide (SERVICE) | None | Prometheus scraping, aggregating global metrics into a single value |
| `instance` | Instance-level (INSTANCE) | `instance_id`, `role` | Distinguishing data of different instances for cross-instance comparison and troubleshooting |
| `role` | Role-level (ROLE) | `role` | Comparing Prefill and Decode roles |
| `dp` | No aggregation, raw output per endpoint | `dp_rank`, `role`, `instance_id`, `pod_ip` | Single-endpoint-level troubleshooting |
| `node` | Node-level (NODE) | `pod_ip`, `role` | Monitoring by physical node |

>[!NOTE]NOTE
>
> - The `full` view is suitable for direct Prometheus scraping. When the `role` view does not specify the `role` parameter, each role is aggregated independently and then concatenated into a single text, so the same metric name appears multiple times (each with its own `role` label), which is normal.
> - Except for the `dp` view, all other views undergo semantic aggregation (for details, see [Data Processing Description](#data-processing-description)).

## Response Example (`format=prometheus`, default)

The response is in Prometheus text format, with `Content-Type: text/plain`.

**Response example (`type=full`)**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{model_name="Qwen2.5-7B-Instruct"} 26.0
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{model_name="Qwen2.5-7B-Instruct"} 0.585
# HELP motor:active_prefill_workers Number of active prefill workers.
# TYPE motor:active_prefill_workers gauge
motor:active_prefill_workers 2.0
```

**Response example (`type=instance`)**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{instance_id="0",role="prefill",model_name="Qwen2.5-7B-Instruct"} 12.0
vllm:num_requests_running{instance_id="1",role="prefill",model_name="Qwen2.5-7B-Instruct"} 8.0
vllm:num_requests_running{instance_id="2",role="decode",model_name="Qwen2.5-7B-Instruct"} 6.0
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{instance_id="0",role="prefill",model_name="Qwen2.5-7B-Instruct"} 0.62
vllm:kv_cache_usage_perc{instance_id="2",role="decode",model_name="Qwen2.5-7B-Instruct"} 0.72
```

**Response example (`type=role&role=prefill`)**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{role="prefill",model_name="Qwen2.5-7B-Instruct"} 20.0
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{role="prefill",model_name="Qwen2.5-7B-Instruct"} 0.535
```

**Response example (`type=dp`)**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{dp_rank="0",role="prefill",instance_id="0",pod_ip="192.168.1.10",model_name="Qwen2.5-7B-Instruct"} 4.0
vllm:num_requests_running{dp_rank="1",role="prefill",instance_id="0",pod_ip="192.168.1.11",model_name="Qwen2.5-7B-Instruct"} 8.0
```

**Response example (`type=node`)**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{pod_ip="192.168.1.10",role="prefill",model_name="Qwen2.5-7B-Instruct"} 4.0
```

## Response Example (`format=opentelemetry`)

The response is an OpenTelemetry-compatible JSON structure with `Content-Type: application/json`. All views support this format, and the data content is consistent with the Prometheus format, with only structural conversion applied.

```JSON
{
  "resourceMetrics": [
    {
      "resource": { "attributes": [] },
      "scopeMetrics": [
        {
          "scope": { "name": "motor.coordinator.metrics" },
          "metrics": [
            {
              "name": "vllm:num_requests_running",
              "description": "Number of requests in model execution batches.",
              "unit": "",
              "type": "gauge",
              "dataPoints": [
                {
                  "sampleName": "vllm:num_requests_running",
                  "attributes": [
                    { "key": "model_name", "value": { "stringValue": "Qwen2.5-7B-Instruct" } }
                  ],
                  "asDouble": 26.0
                }
              ]
            }
          ]
        }
      ]
    }
  ]
}
```

## Returned Metric Description

The interface returns the aggregated results of vLLM engine metrics (with the `vllm:` prefix) and Motor custom metrics (with the `motor:` prefix). The main metrics are as follows:

### SLA and Latency Metrics (Histogram)

During aggregation, the histogram buckets of each endpoint are merged and quantiles are calculated. Metrics with quantile configuration output quantile gauges with the `_p50` / `_p95` / `_p99` / `_mean` suffixes; metrics without quantile configuration retain only the merged histogram buckets.

| Metric Name | Description | Quantile |
|--------|------|--------|
| `vllm:time_to_first_token_seconds` | Time to first token (TTFT), statistics only on the Decode side | p50 / p95 / p99 |
| `vllm:time_per_output_token_seconds` | Time per output token (TPOT), statistics only on the Decode side | p50 / p95 / p99 |
| `vllm:e2e_request_latency_seconds` | End-to-end request latency, statistics only on the Decode side | p50 / p95 / p99 |
| `vllm:request_queue_time_seconds` | Request queue latency | p50 / p95 / p99 |
| `vllm:request_prefill_time_seconds` | Time spent in the Prefill phase of a request, statistics only on the Prefill side | p50 / p95 / p99 |
| `vllm:request_decode_time_seconds` | Time spent in the Decode phase of a request | None |
| `vllm:request_params_n` | Number of request parameters | None |
| `vllm:request_params_max_tokens` | Maximum number of tokens for a request | None |

### Running Status Metrics (Gauge)

| Metric Name | Type | Aggregation Method | Description |
|--------|------|----------|------|
| `vllm:num_requests_running` | Gauge | Sum | Number of requests currently running |
| `vllm:num_requests_waiting` | Gauge | Sum | Number of requests currently waiting in the queue |
| `vllm:num_requests_swapped` | Gauge | Sum | Number of requests currently swapped out |
| `vllm:num_requests_running_max` | Gauge | Maximum value | Hotspot (maximum value) view of the number of running requests |
| `vllm:kv_cache_usage_perc` | Gauge | Average value | Total KV Cache usage rate |
| `vllm:gpu_cache_usage_perc` | Gauge | Average value | GPU KV Cache usage rate |
| `vllm:cpu_cache_usage_perc` | Gauge | Average value | CPU KV Cache usage rate |
| `vllm:kv_cache_usage_perc_max` | Gauge | Maximum value | Hotspot (maximum value) view of the KV Cache usage rate |

### Counter Metrics

>[!IMPORTANT]IMPORTANT
>The following Counters are all **cumulative values** since the Engine process started. Summing across Engines or instances has no direct meaning. When calculating rates, use the `rate()` / `irate()` functions on the Prometheus side. Do not subtract or average the raw values.

| Metric Name | Aggregation Method | Description |
|--------|----------|------|
| `vllm:prompt_tokens_total` | Sum | Cumulative number of Prompt tokens processed |
| `vllm:generation_tokens_total` | Sum | Cumulative number of Output tokens generated |
| `vllm:new_tokens_total` | Sum | Cumulative number of new tokens generated |
| `vllm:request_success_total` | Sum | Cumulative number of successfully processed requests |
| `vllm:num_preemptions_total` | Sum | Cumulative number of preemptions |
| `vllm:prefix_cache_hits_total` | Sum | Prefix Cache hit count (numerator of the derived hit rate) |
| `vllm:prefix_cache_queries_total` | Sum | Prefix Cache query count (denominator of the derived hit rate) |
| `vllm:gpu_prefix_cache_hits_total` | Sum | GPU Prefix Cache hit count |
| `vllm:gpu_prefix_cache_queries_total` | Sum | GPU Prefix Cache query count |

### Derived Metrics

Derived from the raw metrics after aggregation:

| Metric Name | Description |
|--------|------|
| `vllm:prefix_cache_hit_rate` | Prefix Cache hit rate, calculated as `vllm:prefix_cache_hits_total / vllm:prefix_cache_queries_total`, with the value limited to `[0, 1]` |
| `motor:prompt_tokens_per_second` | Prompt token throughput, calculated from the increment of the `vllm:prompt_tokens_total` counter |
| `motor:generation_tokens_per_second` | Output token throughput, calculated from the increment of the `vllm:generation_tokens_total` counter |
| `motor:active_prefill_workers` | Current number of active Prefill instances |
| `motor:active_decode_workers` | Current number of active Decode instances |
| `motor:inactive_prefill_workers` | Current number of inactive Prefill instances (configured count − available count) |
| `motor:inactive_decode_workers` | Current number of inactive Decode instances (configured count − available count) |
| `motor:request_rate` | Ingress request rate (requests/s), calculated from the increment of the `vllm:request_success_total` counter |
| `motor:prefill_utilization` | Prefill demand/supply utilization (>1 indicates insufficient supply). For details, see [Capacity Planning Metrics](#capacity-planning-metrics) |
| `motor:decode_utilization` | Decode demand/supply utilization (>1 indicates insufficient supply). For details, see [Capacity Planning Metrics](#capacity-planning-metrics) |
| `motor:prefill_replicas_required` | Number of Prefill instances required as derived from capacity planning. For details, see [Capacity Planning Metrics](#capacity-planning-metrics) |
| `motor:decode_replicas_required` | Number of Decode instances required as derived from capacity planning (dual constraints of throughput and KV). For details, see [Capacity Planning Metrics](#capacity-planning-metrics) |
| `motor:pd_ratio_current` | Current PD ratio: `number of active Prefill instances / max(number of active Decode instances, 1)` |
| `motor:pd_ratio_suggested` | Suggested PD ratio (a suggestion signal only; Motor does not perform any adjustment). For details, see [Capacity Planning Metrics](#capacity-planning-metrics) |

### Capacity Planning Metrics

The following metrics are intended for HPA automatic scaling and PD ratio management scenarios. They are all service/role-level derived Gauges, and are refreshed by the capacity planner built into the Coordinator at each collection period.

**Theoretical basis**: The Prefill phase is compute-bound, and the input token processing rate of a single instance under sufficient load is approximately constant at `C_p`; the Decode phase is memory-bandwidth-bound, and the continuous generation throughput of a single instance is approximately `C_d`, with the KV cache capacity `K` (number of blocks × block_size) as the second constraint. Queueing theory shows that the wait is bounded when supply/demand ≥ `1/ρ` (ρ is the target utilization), and Little's law gives the KV resident token demand `D_kv = λ·W_kv·(L̄_in+L̄_out)`.

**Calculation basis** (for each collection period, counter delta + EMA smoothing):

```text
Demand side (aggregated by role):
  λ     = Δrequest_success_total / Δt              (completed request basis)
  L̄_in  = Δprompt_tokens_total / Δrequests         (average input length)
  L̄_out = Δgeneration_tokens_total / Δrequests     (average output length)
  W_kv  = Δqueue_time/Δcount + Δdecode_time/Δcount (KV residency time on the Decode side)

Supply side (online calibration):
  C_p = EMA(Δprompt_tokens_total / Δprefill_time_sum)
        (pure processing throughput: prefill_time in vLLM excludes queueing and idle time)
  C_d = single-instance generation TPS peak learning: the value is raised when a higher value
        is observed; otherwise it is multiplied by (1-capacity_decay) per period, but never
        falls below floor = max(prior, sliding-window sample peak)
        (window length capacity_window_cycles, default 200 cycles ≈ 10 minutes)
  K   = num_gpu_blocks in the vllm:cache_config_info label × block_size

Instance count and utilization derivation (ρ and ρ_kv are configurable):
  N_p    = ⌈ λ·L̄_in / (C_p·ρ) ⌉
  N_d_tp = ⌈ λ·L̄_out / (C_d·ρ) ⌉
  N_d_kv = ⌈ λ·W_kv·(L̄_in+L̄_out) / (K·ρ_kv) ⌉
  N_d    = max(N_d_tp, N_d_kv)
  U      = demand rate / (current number of instances × C)
```

**Metric list**:

| Metric Name | Meaning | Formula |
|--------|------|------|
| `motor:prefill_utilization` | Prefill demand/supply utilization | `λ·L̄_in/(N_p·C_p)` |
| `motor:decode_utilization` | Decode demand/supply utilization | `λ·L̄_out/(N_d·C_d)` |
| `motor:prefill_replicas_required` | Number of Prefill instances required | `⌈λ·L̄_in/(C_p·ρ)⌉` |
| `motor:decode_replicas_required` | Number of Decode instances required | `max(N_d_tp, N_d_kv)` |
| `motor:prefill_capacity_tps` | Single-instance Prefill capacity estimate (diagnostic) | `C_p` |
| `motor:decode_capacity_tps` | Single-instance Decode capacity estimate (diagnostic) | `C_d` |
| `motor:prefill_demand_tps` | Prefill demand rate in tokens/s (diagnostic) | `λ·L̄_in` |
| `motor:decode_demand_tps` | Decode demand rate in tokens/s (diagnostic) | `λ·L̄_out` |
| `motor:kv_demand_tokens` | KV resident token demand (diagnostic) | `λ·W_kv·(L̄_in+L̄_out)` |
| `motor:capacity_calibrated` | Whether the capacity estimate has been calibrated (0/1, label `role`) | Prior >0 or a valid observation already exists |
| `motor:pd_ratio_current` | Current PD ratio | `N_p_active/max(N_d_active, 1)` |
| `motor:pd_ratio_suggested` | Suggested PD ratio (suggestion signal) | `N_p/N_d`, EMA + clipping; falls back to the current ratio when not calibrated or when there is no demand |
| `motor:request_rate` | Ingress request rate in requests/s | `Δrequest_success/Δt` |

Key points of the calculation rules:

- **Output conditions**: `utilization` is output only when the capacity estimate `C > 0` and the corresponding role has active instances; `replicas_required` is always output (its value is `0` when the capacity is not calibrated or there is no demand), and the Decode side takes the larger of the throughput constraint `N_d_tp` and the KV constraint `N_d_kv`; `kv_demand_tokens` is output only when `K` is known. A `utilization` value >1 indicates insufficient supply, so it can be used directly as the target metric of the HPA (the target is ρ; for the recommended configuration, see [Automatic Elastic Scaling](../features/auto_scaling.md)).

- **Conservativeness (safe direction)**: Both `C_p` and `C_d` are lower-bound estimates of the real capacity — in concurrent / chunked scenarios, the prefill_time of requests overlaps, which makes `C_p` systematically low, and when the demand is insufficient, the real peak of `C_d` cannot be observed — so `utilization` is on the high side and the scaling-out tendency is on the high side, and it is self-correcting: after scaling out more, more demand is accepted, a higher TPS can be observed, and the capacity estimate converges accordingly.

- **Calibration confidence flag**: `motor:capacity_calibrated{role="prefill"|"decode"}` being `1` indicates that the role already has a valid online observation or a capacity prior configured, and `0` indicates that it is not calibrated (in this case, `replicas_required` is still output as usual, and downstream can distinguish the confidence level accordingly).

- **Degradation when KV information is missing**: When the engine does not expose `vllm:cache_config_info` (or parsing fails), the KV constraint is skipped automatically, `N_d` is determined only by the throughput constraint, and a one-time WARNING is recorded (it can be triggered again after K is restored).

- **Not output in PD hybrid deployment**: Capacity planning metrics are calculated only for prefill / decode role instances, and the P/D side capacity planning metrics described above are not output for PD hybrid (hybrid / union) deployment.

- **Suggestion signal not executed**: Motor only outputs the `motor:pd_ratio_suggested` signal and does not modify any instance count or K8s resource based on it; the execution is performed by an external controller.

Related configuration items (all located in the `prometheus_metrics_config.capacity_planning` sub-configuration section, and all optional):

| Configuration Item | Default Value | Description |
|--------|--------|------|
| `target_utilization` | `0.8` | Target single-instance utilization ρ, the semantic basis for instance count derivation and the HPA target |
| `kv_target_utilization` | `0.9` | Target utilization ρ_kv of the KV constraint |
| `ema_alpha` | `0.85` | EMA smoothing coefficient for the demand rate, average lengths, W_kv, and `C_p`; the samples themselves are aggregated means over 3s windows and do not need to be smoothed again, so they converge within 3 collection periods |
| `capacity_decay` | `0.005` | Slow decay coefficient of the `C_d` peak per period |
| `capacity_window_cycles` | `200` | Length of the sliding window (in periods) for the `C_d` decay floor (default ≈10 minutes) |
| `prefill_tps_capacity_prior` | `0` (no prior) | Capacity prior of a single Prefill instance (tokens/s); `0` indicates no prior |
| `decode_tps_capacity_prior` | `0` (no prior) | Capacity prior of a single Decode instance (tokens/s); `0` indicates no prior |
| `pd_ratio_smooth_alpha` | `0.3` | EMA smoothing coefficient of `motor:pd_ratio_suggested`; the smaller the value, the smoother the result |
| `pd_ratio_min` | `0.05` | Lower clipping bound of the suggested ratio |
| `pd_ratio_max` | `20.0` | Upper clipping bound of the suggested ratio |

### HPA Contract Metrics and Aliases Without Colons

The naming convention of K8s External Metrics does not contain `:`, so the following HPA contract metrics are **both** exposed in the `/metrics` output (in both the `prometheus` and `opentelemetry` formats) as one series under the original name and one series under the alias without a colon (`:` replaced with `_`). The two values are consistent, and the HELP text of the alias series carries the "(HPA alias)" suffix. It is recommended that the HPA / External Metrics Adaptor use the aliases.

| Original Metric Name | HPA Alias |
|------------|----------|
| `motor:prefill_utilization` | `motor_prefill_utilization` |
| `motor:decode_utilization` | `motor_decode_utilization` |
| `motor:prefill_replicas_required` | `motor_prefill_replicas_required` |
| `motor:decode_replicas_required` | `motor_decode_replicas_required` |
| `motor:request_rate` | `motor_request_rate` |
| `motor:pd_ratio_current` | `motor_pd_ratio_current` |
| `motor:pd_ratio_suggested` | `motor_pd_ratio_suggested` |
| `vllm:request_prefill_time_seconds` (including the `_mean` / `_p50` / `_p95` / `_p99` derived quantiles) | `vllm_request_prefill_time_seconds` (and the corresponding suffix forms, such as `vllm_request_prefill_time_seconds_p95`) |

>[!NOTE]NOTE
>
> - Aliases are generated only for the metrics in the contract list above (exactly 8 original metrics); other metrics (such as `vllm:num_requests_running`) are output only under their original names, and the capacity planning diagnostic metrics (`*_capacity_tps` / `*_demand_tps` / `motor:kv_demand_tokens` / `motor:capacity_calibrated`) do not generate aliases.
> - Aliases are appended only at the output rendering layer, and the aggregation semantics and the behavior of query parameters such as `type` / `role` are not affected.

### Process and Runtime Metrics

Metrics of the Engine process itself, summed across Engines:

| Metric Name | Type |
|--------|------|
| `process_virtual_memory_bytes` | Gauge |
| `process_resident_memory_bytes` | Gauge |
| `process_open_fds` | Gauge |
| `process_max_fds` | Gauge |
| `process_cpu_seconds_total` | Counter |
| `process_start_time_seconds` | Gauge |
| `python_info` | Gauge |
| `python_gc_objects_collected_total` | Counter |
| `python_gc_objects_uncollectable_total` | Counter |
| `python_gc_collections_total` | Counter |

### (Optional) KV Store Metrics

When `kv_cache_store_config` (Mooncake/Memcache) is configured, KV Store summary metrics are additionally returned (units converted to GB):

| Metric Name | Description |
|--------|------|
| `kv_store_size` | KV Store capacity, labels `layer=cpu\|ssd\|all`, `stat=usage\|total` |
| `kv_store_ratio` | KV Store usage rate (0~1), labels `layer=cpu\|ssd\|all`, `stat=usage_rate` |
| `kv_store_keys` | Number of keys stored in the KV Store |
| `kv_store_eviction` | KV Store eviction count, label `stat=success\|attempts` |

>[!NOTE]NOTE
>
> - KV Store metrics are **returned only with the `type=full` view**: the `instance` / `role` / `dp` / `node` views output only engine metrics and do not include the `kv_store_*` series.
> - The endpoint-granularity scheduling state metric `motor:endpoint_state` (containing the `request_count` / `fresh_load` / `active_tokens` / `total_cnt` / `kv_usage` / `kv_hit_tokens` / `kv_input_tokens` / `kv_hit_rate` / `running` / `waiting` samples) is the same as the KV Store metrics: it is **returned only with the `type=full` view (including the `type=motor` view)**, and the `instance` / `role` / `dp` / `node` views do not contain this family.
>   - `request_count` (stat=`request_count`) is the number of requests **currently being processed** by the endpoint (an in-flight instantaneous value that is incremented by 1 when an Infer Worker allocates a request and decremented by 1 when the request is released); scraping 0 in the gap between request processes is normal.
>   - `total_cnt` (stat=`total_cnt`) is the number of requests **cumulatively allocated** by the endpoint (it only increases and is incremented by 1 when an Infer Worker allocates a request); when the endpoint is removed from the membership table and then added again, it accumulates from 0 again.
>   - Both are written directly into the workload shared memory (schema 5) by the Infer Worker, which requires Mgmt and all Infer Workers to be built from the same version.
> - Unknown metrics that are not in the Motor semantic registry are not discarded; they are aggregated by fallback according to the Prometheus type: `histogram` merges buckets, and `gauge` / `counter` are summed.
> - Metrics whose names are prefixed with `motor:` are metrics computed or injected by Motor during aggregation; the rest are raw Engine metrics.

## Data Processing Description

The Coordinator performs all data processing internally at the `/metrics` endpoint, so callers directly obtain data in the final format without any secondary processing. The processing details are as follows:

- **Collection period**: A background thread collects all Engine metrics and re-aggregates them according to `prometheus_metrics_config.reuse_time` (default `3` seconds), and views are cached by collection version.

- **Aggregation semantics**: Counter and status/queue Gauge metrics are summed; cache-type Gauge metrics use the average value; hot-spot resource Gauge metrics use the maximum value; histograms merge buckets; metadata-type metrics pass through directly.

- **Internal label cleanup**: The Engine internal label `engine="<id>"` is stripped and not exposed externally.

- **Timestamp series cleanup**: Timestamp series ending with `_created` (a Prometheus convention) are discarded.

- **Negative value handling**: Only Gauge metrics allow negative values; negative values in Counter or histogram metrics are treated as corrupted, and the corresponding sample line is discarded.

- **Empty metric handling**: Metric families with no sample values are still output with an explicit value of `0`, to prevent the scraping side from losing series.

- **Inactive instances**: After an instance becomes unavailable, its Gauge metrics decay smoothly to zero rather than disappearing; Counter metrics are reset to zero (the accumulated history is carried over by the new instance through baseline offset).

- **Role filtering (role_scope)**: Metrics such as TTFT and TPOT are statistics only for the Decode side in PD disaggregation mode (Prefill nodes do not produce tokens), and `vllm:request_prefill_time_seconds` is statistics only for the Prefill side; they are unaffected when Prefill and Decode are co-located (PD hybrid).

- **Quantile calculation**: Histogram metrics with quantile configuration calculate p50/p95/p99 and the average value after merging buckets.

## Integrating with Prometheus

Configure this interface as a Prometheus scrape target. The following uses a Kubernetes nodePort deployment as an example:

```yaml
scrape_configs:
  - job_name: "mindie-motor"
    static_configs:
      - targets: ["{host IP}:31017"]
    metrics_path: "/metrics"
```

Usage recommendations:

- For cluster-level monitoring, scrape using the default `full` view; for per-node monitoring, use `type=node`; for troubleshooting, use `type=instance` / `type=dp` to view detailed data.

- For Counter metrics (such as `vllm:prompt_tokens_total`), use `rate()`/`irate()` in PromQL to calculate the rate. Do not directly subtract raw cumulative values.

- An Engine restart causes Counters to reset to zero, resulting in a drop in aggregated values. Prometheus's `rate()` includes built-in counter reset detection, so no additional handling is required.

## Error Handling

| Scenario | Result |
|------|------|
| The value of the `format` parameter is invalid. | HTTP `400`, with the response body containing error details. |
| The value of the `type` parameter is invalid. | Falls back to the `full` view without reporting an error. |
| Metrics have not been collected yet (the service has just started). | Returns empty text, or only metric families with a value of `0`. |

## Deprecated Interface

### Instance Metric Query Interface

> [!NOTE]NOTE
> The `GET /instance/metrics` interface is deprecated. Use `GET /metrics?type=instance` instead. Calling this interface returns HTTP 410 Gone.

**Interface Format**

Request type: **GET**
> URL: `http(s)://{IP}:{Port}/instance/metrics`

For the IP and port, see [IP/Port and Configuration of the Metrics Interface](./README.md#ipport-and-configuration-of-the-metrics-interface)

**Response Example**

```text
# /instance/metrics is deprecated. Use GET /metrics?type=instance instead.
```
