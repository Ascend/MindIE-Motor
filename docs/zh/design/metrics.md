# Metrics 可观测性指标设计文档

## 概述

MindIE Motor 的 Metrics 子系统负责从所有 vLLM 引擎 Pod 采集 Prometheus 格式的原始指标，按语义类型进行聚合，并在 Coordinator 侧注入 Motor 自有计算指标，最终以多种视图重新暴露给下游监控系统（Prometheus / OpenTelemetry Collector）。

核心设计目标：

- **语义感知聚合**：不再按指标名硬编码聚合方式，而是将每个指标归类到 `MetricSemantic`，由聚合引擎按语义分发到正确的聚合策略（Sum / Max / Mean / Histogram Merge / Passthrough）。
- **Motor 计算指标层**：将 Motor 自有计算指标（如 TPS、Worker 计数）统一注册到 `MotorMetricComputer`，通过声明式 `ComputedMetricDef` 驱动，两阶段注入，无需修改 `MetricsCollector`。
- **引擎重启无偏差**：DP 级 counter rate 指标使用 `(job_name, dp_rank)` 作为稳定标识，通过 baseline 偏移机制在引擎重启后继承上一轮 counter 值。
- **多视图输出**：同一份数据支持 `full / instance / role / dp / node` 五种聚合视图。

## 架构总览

### 模块拆分

```text
motor/coordinator/metrics/
├── metric_types.py        Metric / MetricType / AggregationScope / AggregationContext
├── metric_registry.py     MetricSemantic / MetricSemanticConfig / MetricRegistry
├── aggregation_engine.py  SemanticAggregationEngine（聚合策略 + 后处理）
├── metric_computer.py     ComputedMetricDef / MotorMetricComputer（Motor 计算指标）
└── metrics_collector.py   主编排器：采集 → 解析 → 计算 → 聚合 → 缓存 → 格式化
```

### 四层架构

```text
                        ┌──────────────────────────────────┐
                        │     metrics_collector.py         │
                        │  采集 / 解析 / 编排 / 缓存 / 格式化  │
                        └──────┬──────────────┬────────────┘
                               │              │
              ┌────────────────┼──────────────┼────────────────┐
              │                │              │                │
              ▼                ▼              ▼                ▼
   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
   │ metric_types │  │metric_registry│ │aggregation_  │  │   metric     │
   │   基础类型    │  │  语义注册表    │  │   engine     │  │  computer    │
   │              │  │              │  │  聚合引擎     │  │ Motor计算指标  │
   └──────────────┘  └──────────────┘  └──────────────┘  └──────────────┘
```

**Layer 1 — `metric_types.py`（基础类型）**

定义整个系统通用的数据结构：

| 类型 | 说明 |
|------|------|
| `Metric` | 通用指标表示：`name`、`help`、`type`、`label`（列表）、`value`（列表） |
| `MetricType` | Prometheus 标准类型：`GAUGE`、`COUNTER`、`HISTOGRAM`、`SUMMARY` |
| `AggregationScope` | 聚合范围：`INSTANCE`、`ROLE`、`NODE`、`SERVICE` |
| `AggregationContext` | 聚合上下文：scope + instance_roles + deploy_mode + ins_ids |

**Layer 2 — `metric_registry.py`（语义注册表）**

每个已知 vLLM 指标名映射到 `MetricSemantic`，驱动聚合策略：

| 语义类别 | 聚合方式 | 典型指标 |
|---------|---------|---------|
| `COUNTER` | Sum | `request_success_total` |
| `THROUGHPUT_COUNTER` | Sum | `prompt_tokens_total`、`generation_tokens_total` |
| `STATE_GAUGE` | Sum | `num_requests_running`、`process_open_fds` |
| `QUEUE_GAUGE` | Sum | `num_requests_waiting` |
| `CACHE_METRIC` | Mean | `kv_cache_usage_perc` |
| `HOTSPOT_RESOURCE_GAUGE` | Max | `kv_cache_usage_perc_max` |
| `METADATA_GAUGE` | Passthrough | `process_max_fds`、所有 `motor_*` 指标 |
| `HISTOGRAM_LATENCY` | Histogram Merge | `e2e_request_latency_seconds` 等 |
| `SLA_METRIC` | Histogram Merge + 分位数 | `time_to_first_token_seconds` 等 |
| `RATIO_NUMERATOR` / `RATIO_DENOMINATOR` | Sum | `prefix_cache_hits_total` / `queries_total` |

部分指标带有 `role_scope`（如 TTFT 为 `decode` 范围），在 SERVICE 级聚合时按角色过滤。

**Layer 3 — `aggregation_engine.py`（聚合引擎）**

`SemanticAggregationEngine` 两阶段处理：

1. **聚合阶段** (`aggregate`)：按语义分发到正确的聚合策略：
   - Sum: `lambda a, b: a + b`（Counter、State Gauge、Queue Gauge、Throughput Counter）
   - Max: `lambda a, b: max(a, b)`（Hotspot Resource、Occupancy）
   - Mean: Sum → 除以源数量（Cache Metric、Resource Utilization）
   - Passthrough: 取第一个值（Metadata Gauge）
   - Histogram Merge: 合并 bucket、累加 `_count` / `_sum`，按 le 排序
2. **后处理阶段** (`post_process`)：
   - 去除 `_created` 时间戳
   - 从合并后的直方图计算分位数（p50 / p95 / p99）和均值
   - 派生比率指标（如 `prefix_cache_hit_rate = hits / queries`）

**Layer 4 — `metrics_collector.py`（主编排器）**

后台 daemon 线程周期性执行采集 → 聚合 → 服务流水线：

```text
各 Engine /metrics 端点 (HTTP)
  → _fetch_instance_metrics()        拉取原始 Prometheus 文本
  → _parse_metric_text()             解析为 list[Metric]
  → _motor_computer.compute_pre_aggregation()
  │   └── 注入 DP 级计算指标（如 TPS）到 endpoint metrics 列表
  → 存入 _last_collects

get_metrics("full")
  → _aggregate_collects_by_instance()   INSTANCE 级聚合
  → _aggregate_metrics_all_instance()   SERVICE 级聚合（含 role 过滤）
  → post_process()                      直方图分位数 + 比率派生
  → _motor_computer.compute_post_aggregation()
  │   └── 追加 Service 级计算指标（如 worker 计数）
  → _format_prometheus()              输出 Prometheus 文本
```

### 五种指标视图

| 视图 | `type=` 参数 | 聚合范围 | 注入标签 | 用途 |
|------|-------------|---------|---------|------|
| `full` | 默认 | SERVICE（集群级） | — | Prometheus 抓取 |
| `instance` | `instance` | INSTANCE | `instance_id`、`role` | 单实例排障 |
| `role` | `role` | ROLE | `role` | Prefill vs Decode 对比 |
| `dp` | `dp` | 无聚合（per-endpoint） | `dp_rank`、`role`、`instance_id`、`pod_ip` | 细粒度排障 |
| `node` | `node` | NODE | `pod_ip`、`role` | 节点级监控 |

## Motor 计算指标设计

### 设计动机

原先 `MetricsCollector` 中有两处 Motor 自有指标的插入逻辑：

1. `_append_coordinator_metrics()` — Service 级，在聚合后追加 worker 计数
2. 新需求：DP 级 counter rate 指标（TPS），需在聚合前注入 endpoint metrics 列表

为避免逻辑散落，抽象出统一的 **Motor 计算指标层**（`MotorMetricComputer`），集中管理所有 Motor 自有计算指标的注册与计算。

### ComputedMetricDef — 计算指标定义

```python
@dataclass
class ComputedMetricDef:
    name: str                        # 指标名，如 "motor_generation_tokens_per_second"
    help: str                        # HELP 文本
    phase: str                       # "pre_aggregation"（DP 级）| "post_aggregation"（Service 级）
    compute_type: str                # "counter_rate" | "worker_count" | 未来扩展
    source_counters: list[str]       # 来源 vLLM counter 名（counter_rate 类型必填）
    role_filter: list[str] | None    # 可选：限定角色，如 ["decode"] 或 None（全部）
```

### MotorMetricComputer — 计算引擎

```python
class MotorMetricComputer:
    def __init__(self):
        self._dp_state: dict[tuple[str, int, str], dict] = {}  # DP 级 counter rate 状态

    def compute_pre_aggregation(self, collects: dict) -> None:
        """DP 级指标：注入到各 endpoint 的 metrics 列表中。"""

    def compute_post_aggregation(self, aggregate: list[Metric],
                                  collects: dict, deploy_config) -> None:
        """Service 级指标：追加到 aggregate 列表。"""
```

两阶段注入：

1. **`pre_aggregation`**（在 `_collect_metrics()` 中 `_parse_metrics()` 之后调用）：DP 级指标注入各 endpoint 的 `metrics` 列表，随后自然流经现有聚合流水线（DP → Instance → Service）。
2. **`post_aggregation`**（在 `_generate_full_metrics()` 中聚合完成后调用）：Service 级指标直接追加到最终 aggregate 列表。

### 内置计算指标注册表

```python
_MOTOR_COMPUTED_METRICS: list[ComputedMetricDef] = [
    # -- DP 级: counter rate → tokens-per-second --
    ComputedMetricDef(
        name="motor_prompt_tokens_per_second",
        help="Prompt tokens per second computed from vllm:prompt_tokens_total counter deltas",
        phase="pre_aggregation",
        compute_type="counter_rate",
        source_counters=["vllm:prompt_tokens_total"],
        role_filter=None,
    ),
    ComputedMetricDef(
        name="motor_generation_tokens_per_second",
        help="Generation tokens per second computed from vllm:generation_tokens_total counter deltas",
        phase="pre_aggregation",
        compute_type="counter_rate",
        source_counters=["vllm:generation_tokens_total"],
        role_filter=None,
    ),
    # -- Service 级: worker 计数 --
    ComputedMetricDef(
        name="motor_active_prefill_workers",
        phase="post_aggregation",
        compute_type="worker_count",
        role_filter=["prefill"],
    ),
    # ... 其余省略，共 6 个
]
```

### Counter Rate 重启容错设计

vLLM 的 Counter 指标（如 `generation_tokens_total`）是自引擎进程启动后的累计值，不会因 `/metrics` 请求而重置。引擎重启时 Counter 归零，如果直接做 `delta(counter) / dt` 计算，重启窗口会算出负速率。

Motor 使用 `(job_name, dp_rank)` 作为稳定标识（`job_name` 跨重启不变，`instance_id` 重启后变化），通过 baseline 偏移机制保证 effective counter 连续：

```text
时间线：
  T0: job=decoder-0, dp_rank=0, ins_id=5, raw_counter=10000
      baseline=0, effective=10000, TPS=0（首次，无历史）

  T1: job=decoder-0, dp_rank=0, ins_id=5, raw_counter=11000
      effective=11000, dt=T1-T0, TPS=1000/dt ✓

  [引擎重启]
  T2: job=decoder-0, dp_rank=0, ins_id=12, raw_counter=50
      检测 restart（ins_id 12 ≠ 5）
      baseline = 11000（继承上次 effective）
      effective = 50 + 11000 = 11050
      dt = T2-T1, TPS = (11050-11000)/dt ≈ 0（重启间隙无产出）✓

  T3: job=decoder-0, dp_rank=0, ins_id=12, raw_counter=500
      effective = 500 + 11000 = 11500
      dt = T3-T2, TPS = (11500-11050)/dt = 450/dt ✓
```

重启检测双重判断：

- `instance_id` 发生变化
- `raw_counter` 显著下降（drop > 10%）

## 如何新增一个自定义的 Motor 计算指标

### 场景一：新增 DP 级 counter rate 指标

例如，新增一个 `motor_new_tokens_per_second` 指标，基于 `vllm:new_tokens_total` 计算速率。

**Step 1：在 `_MOTOR_COMPUTED_METRICS` 注册表中添加定义**

编辑 `motor/coordinator/metrics/metric_computer.py`，在 `_MOTOR_COMPUTED_METRICS` 列表中添加：

```python
ComputedMetricDef(
    name="motor_new_tokens_per_second",
    help="New tokens per second computed from vllm:new_tokens_total counter deltas",
    phase="pre_aggregation",
    compute_type="counter_rate",
    source_counters=["vllm:new_tokens_total"],
    role_filter=None,
),
```

**Step 2：在 `metric_registry.py` 中注册语义**

编辑 `motor/coordinator/metrics/metric_registry.py`，在 `_VLLM_METRIC_REGISTRY` 末尾添加：

```python
"motor_new_tokens_per_second": MetricSemanticConfig(
    semantic=MetricSemantic.METADATA_GAUGE,
),
```

**完成**。`compute_type="counter_rate"` 已在 `MotorMetricComputer._compute_counter_rates()` 中实现，无需编写额外代码。TPS 指标会自动注入到各 endpoint 的 metrics 列表，并通过聚合流水线在所有视图中输出。

### 场景二：新增 Service 级聚合指标

例如，新增一个 `motor_decode_queue_depth` 指标，统计 decode 实例的总排队请求数。

**Step 1：在 `_MOTOR_COMPUTED_METRICS` 注册表中添加定义**

```python
ComputedMetricDef(
    name="motor_decode_queue_depth",
    help="Total requests waiting in decode queue",
    phase="post_aggregation",
    compute_type="gauge_sum_by_role",
    source_counters=["vllm:num_requests_waiting"],
    role_filter=["decode"],
),
```

**Step 2：在 `MotorMetricComputer` 中添加计算方法**

编辑 `motor/coordinator/metrics/metric_computer.py`，在 `MotorMetricComputer` 类中添加：

```python
def _compute_gauge_sum_by_role(
    self,
    aggregate: list[Metric],
    collects: dict[int, dict[str, Any]],
    defn: ComputedMetricDef,
) -> None:
    """按角色过滤后求和指定 gauge 指标。"""
    total = 0.0
    for ins_data in collects.values():
        role = ins_data.get("role", "")
        if defn.role_filter and role not in defn.role_filter:
            continue
        for pod_info in ins_data.get("endpoints", {}).values():
            for m in pod_info.get("metrics", []):
                if m.name in defn.source_counters:
                    total += sum(m.value)
    aggregate.append(Metric(
        name=defn.name,
        help=defn.help,
        type=MetricType.GAUGE,
        label=[defn.name],
        value=[total],
    ))
```

**Step 3：在 `compute_post_aggregation` 中注册新的 compute_type**

```python
def compute_post_aggregation(self, aggregate, collects, deploy_config):
    for defn in _get_defs_by_phase("post_aggregation"):
        if defn.compute_type == "worker_count":
            self._compute_worker_counts(aggregate, collects, deploy_config)
        elif defn.compute_type == "gauge_sum_by_role":       # 新增
            self._compute_gauge_sum_by_role(aggregate, collects, defn)
```

**Step 4：注册语义**（同场景一 Step 2）。

### 场景三：新增全新的 compute_type

如果现有 `compute_type`（`counter_rate` / `worker_count`）无法满足需求，可以添加全新的计算类型：

1. 在 `_MOTOR_COMPUTED_METRICS` 中定义新的 `ComputedMetricDef`，使用新的 `compute_type` 值。
2. 在 `MotorMetricComputer` 中实现对应的 `_compute_xxx()` 方法。
3. 在 `compute_pre_aggregation` 或 `compute_post_aggregation` 中添加对新的 `compute_type` 的分发。
4. 在 `metric_registry.py` 中注册语义。

**核心原则**：所有变更集中于 `metric_computer.py` 和 `metric_registry.py`，`MetricsCollector` 本身无需修改。

## vLLM 指标参考

vLLM 引擎通过 `/metrics` 端点暴露 Prometheus 兼容指标，使用 `vllm:` 前缀。指标分为两大类：

### 服务级指标（Gauge / Counter）

| 指标 | 类型 | 说明 |
|------|------|------|
| `vllm:num_requests_running` | Gauge | 当前运行的请求数 |
| `vllm:num_requests_waiting` | Gauge | 等待调度的请求数 |
| `vllm:kv_cache_usage_perc` | Gauge | KV cache 使用率（0–1） |
| `vllm:gpu_cache_usage_perc` | Gauge | GPU cache 使用率 |
| `vllm:prefix_cache_queries_total` | Counter | 前缀缓存查询次数（累计） |
| `vllm:prefix_cache_hits_total` | Counter | 前缀缓存命中次数（累计） |
| `vllm:prompt_tokens_total` | Counter | prompt token 总数（累计） |
| `vllm:generation_tokens_total` | Counter | 生成 token 总数（累计） |
| `vllm:request_success_total` | Counter | 完成的请求数（按 `finished_reason` 标签） |

### 请求级指标（Histogram）

| 指标 | 说明 |
|------|------|
| `vllm:time_to_first_token_seconds` | 首 token 延迟（TTFT） |
| `vllm:time_per_output_token_seconds` | 跨 token 延迟（TPOT） |
| `vllm:e2e_request_latency_seconds` | 端到端请求延迟 |
| `vllm:request_prefill_time_seconds` | prefill 耗时 |
| `vllm:request_decode_time_seconds` | decode 耗时 |
| `vllm:request_queue_time_seconds` | 排队时间 |
| `vllm:request_prompt_tokens` | 输入 token 数分布 |
| `vllm:request_generation_tokens` | 生成 token 数分布 |

### 关键注意事项

- **所有 Counter 指标是引擎进程启动后的累计值**，不会因 `/metrics` 请求而重置。Counter 归零的唯一时机是引擎进程重启。
- MindIE Motor **不做 delta / rate 计算**（Motor 计算指标除外），原始累计值直接透传。速率计算应由下游 Prometheus 通过 `rate()` PromQL 完成。
- vLLM 设计文档：`https://github.com/vllm-project/vllm/blob/main/docs/design/metrics.md`
- vLLM 用户文档：`https://docs.vllm.ai/en/latest/usage/metrics/`

## 数据流总图

```text
                     ┌──────────────────────────────┐
                     │        Prometheus            │
                     │    scrape /metrics:1027      │
                     └──────────────┬───────────────┘
                                    │
                     ┌──────────────▼───────────────┐
                     │   ObservabilityServer        │
                     │   GET /metrics?type=full|... │
                     └──────────────┬───────────────┘
                                    │
                     ┌──────────────▼───────────────┐
                     │     MetricsCollector         │
                     │  ┌─────────────────────────┐ │
                     │  │  _update_metrics_thread │ │
                     │  │  (daemon, 每 reuse_time)│ │
                     │  └───────────┬─────────────┘ │
                     │              │               │
                     │  _collect_metrics()          │
                     │  ├─ _fetch_instance_metrics  │──► Engine /metrics (HTTP)
                     │  ├─ _parse_metrics           │
                     │  └─ computer.pre_aggregation │──► 注入 DP 级 TPS
                     │              │               │
                     │  get_metrics("full")         │
                     │  ├─ INSTANCE agg (求和)       │
                     │  ├─ SERVICE agg (role 过滤)   │
                     │  ├─ post_process (分位数)     │
                     │  └─ computer.post_aggregation│──► 追加 Worker 计数
                     └──────────────────────────────┘
```
