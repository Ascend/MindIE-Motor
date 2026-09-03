# Metrics & Observability — Architecture & Implementation

## 4-Layer Design

``` text
motor/coordinator/metrics/
├── metric_types.py          # Foundation: Metric, MetricType, AggregationScope, AggregationContext
├── metric_registry.py       # Semantic taxonomy: ~50+ metric names → MetricSemantic + role_scope
├── aggregation_engine.py    # Semantic-aware aggregation (sum/max/mean/histogram-merge) + post-processing
├── metric_computer.py       # Coordinator-side metric computation (MotorMetricComputer)
└── metrics_collector.py     # Orchestrator: collect → parse → aggregate → cache → serve
```

### Layer 1 — `metric_types.py`

The canonical `Metric` dataclass:

```python
@dataclass
class Metric:
    name: str           # Prometheus metric name (e.g. "num_requests_running")
    help: str           # HELP text
    type: MetricType    # GAUGE, COUNTER, HISTOGRAM, SUMMARY, NONE (default)
    label: list[str]    # Label strings — parallel array with value
    value: list[float]  # Sample values — parallel array with label
```

The `label`/`value` fields are **parallel arrays** (one entry per sample line), not
a key-value dict. `MetricType` also carries a `NONE` member used as the default.

`AggregationScope` enum controls merge granularity:

- `INSTANCE` — merge endpoints within one instance
- `ROLE` — merge by role (prefill, decode)
- `NODE` — merge by physical node (pod_ip)
- `SERVICE` — cluster-wide merge

### Layer 2 — `metric_registry.py`

Defines the `MetricSemantic` enum (lines 31-55) driving aggregation strategy:

- **Sum-based**: `COUNTER`, `STATE_GAUGE`, `QUEUE_GAUGE`, `THROUGHPUT_COUNTER`, `RATIO_NUMERATOR`, `RATIO_DENOMINATOR`
- **Max-based**: `HOTSPOT_RESOURCE_GAUGE`, `OCCUPANCY_METRIC`
- **Mean-based**: `RESOURCE_UTILIZATION_GAUGE`, `CACHE_METRIC`
- **Passthrough**: `METADATA_GAUGE` (pick first, no merge)
- **Histogram**: `HISTOGRAM_LATENCY`, `SLA_METRIC` (merge buckets, compute quantiles)
- **Unknown**: fall back by Prometheus type (histogram → merge, gauge/counter → sum)

Maps vLLM metric names to `MetricSemantic` + optional `role_scope` + optional `metadata`. Also supports derived `RatioPair` metrics (e.g., `prefix_cache_hit_rate = hits / queries`). Unknown metrics auto-fallback by Prometheus type.

### Layer 3 — `aggregation_engine.py`

`SemanticAggregationEngine` has two phases:

**Phase 1 — `aggregate(metric_name, metric_list)`:**
Dispatches to the correct reduce strategy per semantic:

- `COUNTER` → `sum(values)`
- `HOTSPOT_RESOURCE` → `max(values)`
- `CACHE_METRIC` → `mean(values)`
- `HISTOGRAM_LATENCY` → merge buckets (sum matching `le` labels)
- `METADATA` → `values[0]` (passthrough)

**Phase 2 — `post_process(aggregate)`:**

- Drops `_created` timestamp metrics (Prometheus convention)
- Computes quantile gauges (p50/p95/p99 + mean) from merged histogram buckets
- Derives ratio metrics (e.g., `prefix_cache_hit_rate = hits / queries`, result clamped to [0,1])
- No negative-value filtering — that is the parser's job, and only for non-GAUGE types

### Layer 4 — `metrics_collector.py`

Singleton (`ThreadSafeSingleton`) with a background daemon thread:

``` text
Periodic collect loop (every N seconds):

  1. HTTP GET /metrics from every native engine endpoint (`NativeEngineApiClient`)
  2. Parse Prometheus exposition format → list[Metric]

     - Manual parser (not prometheus_client library)
     - Strips internal engine="<id>" label
     - Rejects negative values only for non-GAUGE types (negative gauges are legal and preserved)
     - Regex: _PROM_SAMPLE_RE + _PROM_LABEL_RE

  3. Cache raw collects with monotonic version counter
  4. On-demand aggregation in 5 views (cached per version for efficiency)

Metrics endpoint:
  GET /metrics?type=full|instance|role|dp|node&format=prometheus|opentelemetry
  → aggregate by scope → post_process → append coordinator metrics → format
  (Prometheus text, or OpenTelemetry JSON-compatible dict when format=opentelemetry)
```

**Mooncake KV store metrics** — Filtered through `_KVSTORE_METRIC_ALLOWLIST` (the `master_*` families). Converted to GB-sized gauges with `layer=cpu|ssd|all` and `stat=usage|total|usage_rate` labels, plus `kv_store_keys` and `kv_store_eviction` families.

**KV store metrics fetch endpoint** — `_fetch_kv_store_metrics()` builds `http://<kv_store_service>:<port>/metrics`; when `kv_store_metrics_port` is unset/0 the auto default is **50090 for both mooncake and memcache** (mooncake must be launched with `--metrics_port` — see `mooncake.sh` / `all_combine_in_single_container.sh`, env override `MOONCAKE_METRICS_PORT`, default 50090). Explicit override order: `kv_cache_store_config.metrics_port` → env `KV_STORE_METRICS_PORT` → auto default.

**Memcache KV store metrics** — `_filter_memcache_metrics()` passes through `motor:memcache_`-prefixed metrics (renamed from `memcache_`) and emits the same `kv_store_*` summary families from capacity / keys / eviction data.

**Inactive instance handling** (`_clear_inactive_metrics()`):

- GAUGE values: zeroed out and accumulated so they decay to zero (not vanish)
- Inherited counters (e.g. `vllm:prompt_tokens_total`, `vllm:generation_tokens_total`): zeroed — their cumulative history is carried forward by the new instance via the baseline offset in `MotorMetricComputer`
- Other COUNTER / HISTOGRAM values: unchanged

### Coordinator-Side Metrics (`metric_computer.py`)

`MotorMetricComputer` computes metrics not present in vLLM's `/metrics`, in two phases:

- **Pre-aggregation (DP-level counter rates)**: `motor:prompt_tokens_per_second`, `motor:generation_tokens_per_second` — computed from `vllm:prompt_tokens_total` / `vllm:generation_tokens_total` counter deltas with a per-(job, dp_rank) baseline offset; the injected TPS gauges then flow through normal aggregation
- **Post-aggregation (service-level worker counts)**: `motor:active_prefill_workers`, `motor:active_decode_workers`, `motor:inactive_prefill_workers`, `motor:inactive_decode_workers` — instance counts per role (inactive = configured count − available count)
- Inherited metric names are tracked via `get_inherited_metric_names()`

## Five Metric Views

| View | AggregationScope | Label Injection | Use Case |
|------|-----------------|-----------------|----------|
| `full` (default) | SERVICE | — | Prometheus scraping (cluster-wide) |
| `instance` | INSTANCE | `instance_id`, `role` | Per-instance debugging |
| `role` | ROLE | `role` | Prefill vs decode comparison |
| `dp` | none (raw per-endpoint) | `dp_rank`, `role`, `instance_id`, `pod_ip` | Per-endpoint debugging |
| `node` | NODE | `pod_ip`, `role` | Per-node monitoring |

## vLLM Metrics: Critical Knowledge

**All vLLM Counter metrics are cumulative since engine process start.** They never reset between `/metrics` scrapes. This follows standard Prometheus Counter semantics.

**MindIE Motor does NOT compute deltas or rates for vLLM's own counters.** Raw cumulative values are aggregated via label-wise sum across engines. Rate computation (`rate()`, `irate()`) is the downstream Prometheus server's responsibility via PromQL. The one exception: `MotorMetricComputer` derives `motor:prompt_tokens_per_second` / `motor:generation_tokens_per_second` from counter deltas.

### Implications for Development

- Counters across engines with different uptimes produce **skewed aggregates** — always use `rate()` in PromQL, never raw values.
- **Engine restart → counter reset to 0** → aggregate sum drops → no continuity correction exists. The Prometheus server handles this via counter reset detection in `rate()`.
- **Inactive instance GAUGE metrics** are zeroed in `_clear_inactive_metrics()` so they decay smoothly rather than vanishing; inherited counters are zeroed too (their history lives on in the new instance's baseline).
- **TTFT metric is decode-scoped** in PD mode because prefill nodes don't produce tokens — `role_scope` in the metric registry handles this filtering.

### Key Metric Categories

| Category | Metrics | Type | Aggregation |
|----------|---------|------|-------------|
| SLA | `time_to_first_token_seconds`, `time_per_output_token_seconds` | Histogram | merge buckets, compute p50/p95/p99 |
| Latency | `e2e_request_latency_seconds`, `request_queue_time_seconds`, `request_prefill_time_seconds`, `request_decode_time_seconds` | Histogram | merge buckets |
| Queue/State | `num_requests_running`, `num_requests_waiting`, `num_requests_swapped` | Gauge | sum |
| Cache | `kv_cache_usage_perc`, `gpu_cache_usage_perc`, `cpu_cache_usage_perc` | Gauge | mean |
| Throughput | `prompt_tokens_total`, `generation_tokens_total` | Counter | sum |
| Prefix Cache | `prefix_cache_hits_total`, `prefix_cache_queries_total` | Counter | sum → derived ratio |
| Process | memory RSS, open fds, CPU %, GC counts | Gauge/Counter | varies |

## Prometheus Text Parser Implementation

The parser is manual (regex-based, not using the `prometheus_client` library) to avoid dependency weight and control label handling precisely:

- `_PROM_SAMPLE_RE`: matches `metric_name{labels} value` lines
- `_PROM_LABEL_RE`: extracts `key="value"` pairs from label strings
- Internal `engine="<id>"` label is stripped (it's a vLLM internal detail)
- Negative values are rejected **only for non-GAUGE metrics** (a negative counter/histogram is corrupt, so just that sample line is dropped); negative gauges are legal and preserved
- If vLLM adds new label conventions, update `_parse_metric_text()` in `metrics_collector.py`

## Development Rules

- **Adding a new vLLM metric to aggregate**: register in `metric_registry.py` with the correct `MetricSemantic` and optional `role_scope`. Unknown metrics automatically fall back to type-based defaults.
- **New aggregation strategy**: add to `MetricSemantic` enum, add reduce function to `_REDUCE_MAP` in `aggregation_engine.py`, handle post-processing in `post_process()`.
- **New metric view**: add `AggregationScope` if needed, add branch in `get_metrics()` following the existing pattern (aggregate → post_process → format).
- **New coordinator-side metric**: add computation in `MotorMetricComputer`, append formatting in `_append_coordinator_metrics()`.
- **Thread safety**: `MetricsCollector` uses `_lock` for write-side protection (collect + aggregate) and `_serialize_lock` for cache access.
- **Port**: Observability is on `coordinator_obs_port` (default 1027), NOT the management port. Controller and ccae_reporter must connect to the obs port.

## Reference

- vLLM metrics design doc: `https://github.com/vllm-project/vllm/blob/main/docs/design/metrics.md`
- vLLM production metrics: `https://docs.vllm.ai/en/latest/usage/metrics/`
