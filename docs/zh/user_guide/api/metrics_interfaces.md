# 指标接口

## 接口概述

MindIE Motor 的监控指标由 Coordinator 统一汇聚、聚合并提供，**唯一推荐获取途径是 Coordinator Observability 端口上的 `GET /metrics` 接口**。

指标采集链路如下：

1. Coordinator 的 `MetricsCollector` 后台线程按周期（`prometheus_metrics_config.reuse_time`，默认 `3` 秒）从每个 Engine 的 `/metrics` 接口拉取原始指标；
2. 解析 Prometheus 文本，按指标语义（sum / max / mean / 直方图合并 / 直通）跨实例、跨端点聚合，并计算 Motor 自定义指标；
3. 聚合结果按采集版本缓存，通过本接口对外提供。

>[!NOTE]说明
>
> - Controller 侧的 `/observability/metrics` 是已弃用的转发代理（转发到 Coordinator `/metrics`），仅在兼容场景保留，**新接入请直接使用本接口**。
> - 默认端口 `1027` 与 Controller 观测接口（`observability_api_port`，承载 `/observability/inventory` 等）的默认端口相同，但两者属于不同组件、不同服务，配置时不要混淆。

## 接口格式

请求类型：**GET**

> URL：`http(s)://{IP}:{Port}/metrics`

**IP**

- 使用 Kubernetes 部署时，IP 使用主机 IP 或域名。
- 在 Kubernetes 集群内，IP 使用 `Coordinator` 服务的 IP：
  - 取值来自 [`user_config.json`](../configuration/config_reference.md#motor_coordinator_config) 配置文件的 `coordinator_api_host` 配置项；
  - 未配置时使用环境变量 `POD_IP`；
  - 环境变量不存在或为空时，使用默认值 `127.0.0.1`。

**端口**

- 使用 Kubernetes 部署时，端口使用 `yaml` 文件中 `mindie-motor-coordinator-obs` 元数据定义的 `nodePort`，默认 `31017`。
- 在 Kubernetes 集群内，端口使用 `coordinator_obs_port` 配置项定义的端口，默认 `1027`。

**协议**

- 安全协议由 `mgmt_tls_config.enable_tls` 控制：为 `true` 时使用 `https`，否则使用 `http`。

## 请求参数

| 参数名 | 类型 | 必选 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `type` | string | 否 | `full` | 指标聚合视图，取值 `full` / `instance` / `role` / `dp` / `node`，详见[聚合视图](#聚合视图)。非法取值回退为 `full`。 |
| `role` | string | 否 | 无 | 仅当 `type=role` 时生效，过滤指定角色：`prefill` 或 `decode`。不传时返回所有角色的聚合结果。 |
| `format` | string | 否 | `prometheus` | 返回格式：`prometheus`（Prometheus 文本）或 `opentelemetry`（OpenTelemetry JSON）。非法取值返回 HTTP `400`。 |

**使用样例**

```bash
# 全量聚合指标（默认，行为与不带参数完全一致）
curl -X GET "http://{IP}:{Port}/metrics"

# 实例级指标（label 中注入 instance_id 和 role）
curl -X GET "http://{IP}:{Port}/metrics?type=instance"

# 所有角色的聚合指标（按角色拼接为单一文本）
curl -X GET "http://{IP}:{Port}/metrics?type=role"

# 仅 prefill / decode 角色的聚合指标
curl -X GET "http://{IP}:{Port}/metrics?type=role&role=prefill"
curl -X GET "http://{IP}:{Port}/metrics?type=role&role=decode"

# 按 DP 端点粒度输出（不聚合）
curl -X GET "http://{IP}:{Port}/metrics?type=dp"

# 按物理节点聚合
curl -X GET "http://{IP}:{Port}/metrics?type=node"

# 以 OpenTelemetry JSON 格式返回
curl -X GET "http://{IP}:{Port}/metrics?format=opentelemetry"
```

## 聚合视图

`type` 参数控制指标的聚合粒度，不同视图注入不同的维度标签：

| 取值 | 聚合粒度 | 注入标签 | 用途 |
|------|----------|----------|------|
| `full`（默认） | 全集群（SERVICE） | 无 | Prometheus 抓取，全局指标聚合为单一值 |
| `instance` | 实例级（INSTANCE） | `instance_id`、`role` | 区分不同实例的数据，实例间对比/排障 |
| `role` | 角色级（ROLE） | `role` | Prefill 与 Decode 角色对比 |
| `dp` | 不聚合，按端点原始输出 | `dp_rank`、`role`、`instance_id`、`pod_ip` | 单端点级排障 |
| `node` | 节点级（NODE） | `pod_ip`、`role` | 按物理节点监控 |

>[!NOTE]说明
>
> - `full` 视图适合直接接入 Prometheus 抓取；`role` 视图不指定 `role` 参数时，各角色独立聚合后拼接为单一文本，同一指标名会出现多次（各自带 `role` 标签），属正常现象。
> - 除 `dp` 视图外，其他视图都经过了语义化聚合（详见[数据加工说明](#数据加工说明)）。

## 响应示例（`format=prometheus`，默认）

响应为 Prometheus 文本格式，`Content-Type: text/plain`。

**响应示例（`type=full`）**

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

**响应示例（`type=instance`）**

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

**响应示例（`type=role&role=prefill`）**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{role="prefill",model_name="Qwen2.5-7B-Instruct"} 20.0
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{role="prefill",model_name="Qwen2.5-7B-Instruct"} 0.535
```

**响应示例（`type=dp`）**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{dp_rank="0",role="prefill",instance_id="0",pod_ip="192.168.1.10",model_name="Qwen2.5-7B-Instruct"} 4.0
vllm:num_requests_running{dp_rank="1",role="prefill",instance_id="0",pod_ip="192.168.1.11",model_name="Qwen2.5-7B-Instruct"} 8.0
```

**响应示例（`type=node`）**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{pod_ip="192.168.1.10",role="prefill",model_name="Qwen2.5-7B-Instruct"} 4.0
```

## 响应示例（`format=opentelemetry`）

响应为 OpenTelemetry 兼容的 JSON 结构，`Content-Type: application/json`。所有视图均支持该格式，数据内容与 Prometheus 格式一致，仅做结构转换。

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

## 返回指标说明

接口返回 vLLM 引擎指标（`vllm:` 前缀）与 Motor 自定义指标（`motor:` 前缀）的汇聚结果，主要指标如下：

### SLA 与时延指标（直方图）

聚合时合并各端点直方图桶，并计算分位数。带分位数配置的指标输出 `_p50` / `_p95` / `_p99` / `_mean` 后缀的分位数 gauge；无分位数配置的指标仅保留合并后的直方图桶。

| 指标名 | 说明 | 分位数 |
|--------|------|--------|
| `vllm:time_to_first_token_seconds` | 首 token 时延（TTFT），仅统计 Decode 侧 | p50 / p95 / p99 |
| `vllm:time_per_output_token_seconds` | 每输出 token 时延（TPOT），仅统计 Decode 侧 | p50 / p95 / p99 |
| `vllm:e2e_request_latency_seconds` | 请求端到端时延，仅统计 Decode 侧 | p50 / p95 / p99 |
| `vllm:request_queue_time_seconds` | 请求排队时延 | p50 / p95 / p99 |
| `vllm:request_prefill_time_seconds` | 请求 Prefill 阶段耗时，仅统计 Prefill 侧 | p50 / p95 / p99 |
| `vllm:request_decode_time_seconds` | 请求 Decode 阶段耗时 | 无 |
| `vllm:request_params_n` | 请求参数数量 | 无 |
| `vllm:request_params_max_tokens` | 请求最大 token 数 | 无 |

### 运行状态指标（Gauge）

| 指标名 | 类型 | 聚合方式 | 说明 |
|--------|------|----------|------|
| `vllm:num_requests_running` | Gauge | 求和 | 当前运行中的请求数 |
| `vllm:num_requests_waiting` | Gauge | 求和 | 当前排队等待的请求数 |
| `vllm:num_requests_swapped` | Gauge | 求和 | 当前被换出的请求数 |
| `vllm:num_requests_running_max` | Gauge | 取最大值 | 运行中请求数的热点（最大值）视图 |
| `vllm:kv_cache_usage_perc` | Gauge | 取均值 | KV Cache 总使用率 |
| `vllm:gpu_cache_usage_perc` | Gauge | 取均值 | GPU KV Cache 使用率 |
| `vllm:cpu_cache_usage_perc` | Gauge | 取均值 | CPU KV Cache 使用率 |
| `vllm:kv_cache_usage_perc_max` | Gauge | 取最大值 | KV Cache 使用率的热点（最大值）视图 |

### 计数指标（Counter）

>[!IMPORTANT]说明
>以下 Counter 均为 Engine 进程启动以来的**累计值**。跨 Engine、跨实例求和没有直接含义，计算速率时请在 Prometheus 侧使用 `rate()` / `irate()` 函数，不要对原始值做减法或求平均。

| 指标名 | 聚合方式 | 说明 |
|--------|----------|------|
| `vllm:prompt_tokens_total` | 求和 | 累计处理的 Prompt token 数 |
| `vllm:generation_tokens_total` | 求和 | 累计生成的 Output token 数 |
| `vllm:new_tokens_total` | 求和 | 累计生成的新 token 数 |
| `vllm:request_success_total` | 求和 | 累计成功处理的请求数 |
| `vllm:num_preemptions_total` | 求和 | 累计抢占次数 |
| `vllm:prefix_cache_hits_total` | 求和 | Prefix Cache 命中次数（派生命中率分子） |
| `vllm:prefix_cache_queries_total` | 求和 | Prefix Cache 查询次数（派生命中率分母） |
| `vllm:gpu_prefix_cache_hits_total` | 求和 | GPU Prefix Cache 命中次数 |
| `vllm:gpu_prefix_cache_queries_total` | 求和 | GPU Prefix Cache 查询次数 |

### 派生指标

由原始指标在聚合后加工得出：

| 指标名 | 说明 |
|--------|------|
| `vllm:prefix_cache_hit_rate` | Prefix Cache 命中率，由 `vllm:prefix_cache_hits_total / vllm:prefix_cache_queries_total` 计算，取值限制在 `[0, 1]` |
| `motor:prompt_tokens_per_second` | Prompt token 吞吐，由 `vllm:prompt_tokens_total` 计数器增量计算 |
| `motor:generation_tokens_per_second` | Output token 吞吐，由 `vllm:generation_tokens_total` 计数器增量计算 |
| `motor:active_prefill_workers` | 当前活跃 Prefill 实例数 |
| `motor:active_decode_workers` | 当前活跃 Decode 实例数 |
| `motor:inactive_prefill_workers` | 当前非活跃 Prefill 实例数（配置数 − 可用数） |
| `motor:inactive_decode_workers` | 当前非活跃 Decode 实例数（配置数 − 可用数） |
| `motor:request_rate` | 入口请求速率（requests/s），由 `vllm:request_success_total` 计数器增量计算 |
| `motor:prefill_utilization` | Prefill 需求/供给利用率（>1 表示供给不足），详见[容量规划指标](#容量规划指标) |
| `motor:decode_utilization` | Decode 需求/供给利用率（>1 表示供给不足），详见[容量规划指标](#容量规划指标) |
| `motor:prefill_replicas_required` | 容量规划推导的所需 Prefill 实例数，详见[容量规划指标](#容量规划指标) |
| `motor:decode_replicas_required` | 容量规划推导的所需 Decode 实例数（吞吐与 KV 双约束），详见[容量规划指标](#容量规划指标) |
| `motor:pd_ratio_current` | 当前 PD 配比：`active Prefill 实例数 / max(active Decode 实例数, 1)` |
| `motor:pd_ratio_suggested` | 建议 PD 配比（仅为建议信号，Motor 不执行任何调整），详见[容量规划指标](#容量规划指标) |

### 容量规划指标

以下指标面向 HPA 自动扩缩容与 PD 配比管理场景，均为 service/role 级派生 Gauge，
由 Coordinator 内置的容量规划器按采集周期刷新。

**理论依据**：Prefill 阶段为算力受限，单实例在充分负载下的输入 token 处理速率近似
恒定 `C_p`；Decode 阶段为显存带宽受限，单实例持续生成吞吐近似 `C_d`，KV cache
容量 `K`（块数 × block_size）是第二约束。排队论表明供给/需求 ≥ `1/ρ` 时等待有界
（ρ 为目标利用率），Little's law 给出 KV 驻留 token 需求
`D_kv = λ·W_kv·(L̄_in+L̄_out)`。

**计算口径**（每采集周期，counter delta + EMA 平滑）：

```text
需求侧（分角色聚合）：
  λ     = Δrequest_success_total / Δt              （完成请求口径）
  L̄_in  = Δprompt_tokens_total / Δrequests         （平均输入长度）
  L̄_out = Δgeneration_tokens_total / Δrequests     （平均输出长度）
  W_kv  = Δqueue_time/Δcount + Δdecode_time/Δcount （Decode 侧 KV 驻留时长）

供给侧（在线标定）：
  C_p = EMA(Δprompt_tokens_total / Δprefill_time_sum)
        （纯处理吞吐：vLLM 的 prefill_time 不含排队与空闲）
  C_d = 单实例 generation TPS 峰值学习：观测到更高值即上抬，否则每周期
        ×(1-capacity_decay)，但不跌破 floor = max(先验, 滑动窗口样本峰值)
        （窗口长度 capacity_window_cycles，默认 200 周期 ≈ 10 分钟）
  K   = vllm:cache_config_info label 中的 num_gpu_blocks × block_size

实例数与利用率推导（ρ、ρ_kv 可配）：
  N_p    = ⌈ λ·L̄_in / (C_p·ρ) ⌉
  N_d_tp = ⌈ λ·L̄_out / (C_d·ρ) ⌉
  N_d_kv = ⌈ λ·W_kv·(L̄_in+L̄_out) / (K·ρ_kv) ⌉
  N_d    = max(N_d_tp, N_d_kv)
  U      = 需求速率 / (当前实例数 × C)
```

**指标清单**：

| 指标名 | 语义 | 公式 |
|--------|------|------|
| `motor:prefill_utilization` | Prefill 需求/供给利用率 | `λ·L̄_in/(N_p·C_p)` |
| `motor:decode_utilization` | Decode 需求/供给利用率 | `λ·L̄_out/(N_d·C_d)` |
| `motor:prefill_replicas_required` | 所需 Prefill 实例数 | `⌈λ·L̄_in/(C_p·ρ)⌉` |
| `motor:decode_replicas_required` | Decode 所需实例数 | `max(N_d_tp, N_d_kv)` |
| `motor:prefill_capacity_tps` | 单实例 Prefill 容量估计（诊断） | `C_p` |
| `motor:decode_capacity_tps` | 单实例 Decode 容量估计（诊断） | `C_d` |
| `motor:prefill_demand_tps` | Prefill 需求速率 tokens/s（诊断） | `λ·L̄_in` |
| `motor:decode_demand_tps` | Decode 需求速率 tokens/s（诊断） | `λ·L̄_out` |
| `motor:kv_demand_tokens` | KV 驻留 token 需求（诊断） | `λ·W_kv·(L̄_in+L̄_out)` |
| `motor:capacity_calibrated` | 容量估计是否已标定（0/1，label `role`） | 先验 >0 或已有有效观测 |
| `motor:pd_ratio_current` | 当前 PD 配比 | `N_p_active/max(N_d_active, 1)` |
| `motor:pd_ratio_suggested` | 建议 PD 配比（建议信号） | `N_p/N_d`，EMA + 裁剪；未标定/无需求时回退现状比 |
| `motor:request_rate` | 入口请求速率 requests/s | `Δrequest_success/Δt` |

计算规则要点：

- **输出条件**：`utilization` 仅在容量估计 `C > 0` 且对应角色有 active 实例时输出；
  `replicas_required` 始终输出（容量未标定或无需求时取值为 `0`），Decode 侧取
  吞吐约束 `N_d_tp` 与 KV 约束 `N_d_kv` 的较大者；`kv_demand_tokens` 仅在 `K`
  已知时输出。`utilization` 取值 >1 表示供给不足，适合直接作为 HPA 的 target
  指标（target 即 ρ，推荐配置见[自动弹性扩缩容](../features/auto_scaling.md)）。
- **保守性（安全方向）**：`C_p` 与 `C_d` 均为真实容量的下界估计——并发 / chunked
  场景下各请求的 prefill_time 存在重叠使 `C_p` 系统性偏低，需求不足时则观测不到
  `C_d` 的真实峰值——因此 `utilization` 偏高、扩容倾向偏多，且自矫正：多扩后承接
  更多需求，可观测到更高的 TPS，容量估计随之收敛。
- **标定置信标志**：`motor:capacity_calibrated{role="prefill"|"decode"}` 为 `1`
  表示该角色已有有效在线观测或配置了容量先验，为 `0` 表示未标定（此时
  `replicas_required` 仍照常输出，下游可据此区分置信度）。
- **KV 信息缺失退化**：引擎未暴露 `vllm:cache_config_info`（或解析失败）时 KV
  约束自动跳过，`N_d` 仅由吞吐约束决定，并记录一次性 WARNING（K 恢复后可再次
  触发）。
- **PD 混合部署不输出**：容量规划指标仅按 prefill / decode 角色实例计算，PD 混合
  （hybrid / union）部署不输出上述 P/D 侧容量规划指标。
- **建议信号不执行**：Motor 只输出 `motor:pd_ratio_suggested` 信号，不会依据它修改
  任何实例数量或 K8s 资源，执行由外部控制器完成。

相关配置项（均位于 `prometheus_metrics_config.capacity_planning` 子配置段，均可选）：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `target_utilization` | `0.8` | 目标单实例利用率 ρ，实例数推导与 HPA target 的语义基准 |
| `kv_target_utilization` | `0.9` | KV 约束的目标利用率 ρ_kv |
| `ema_alpha` | `0.85` | 需求速率、平均长度、W_kv 与 `C_p` 的 EMA 平滑系数；样本本身是 3s 窗口聚合均值，无需重平滑，3 个采集周期内收敛 |
| `capacity_decay` | `0.005` | `C_d` 峰值每周期慢衰减系数 |
| `capacity_window_cycles` | `200` | `C_d` 衰减下限的滑动窗口长度（周期数，默认 ≈10 分钟） |
| `prefill_tps_capacity_prior` | `0`（无先验） | 单实例 Prefill 容量先验（tokens/s），`0` 表示无先验 |
| `decode_tps_capacity_prior` | `0`（无先验） | 单实例 Decode 容量先验（tokens/s），`0` 表示无先验 |
| `pd_ratio_smooth_alpha` | `0.3` | `motor:pd_ratio_suggested` 的 EMA 平滑系数，越小越平滑 |
| `pd_ratio_min` | `0.05` | 建议配比裁剪下界 |
| `pd_ratio_max` | `20.0` | 建议配比裁剪上界 |

### HPA 契约指标与无冒号别名

K8s External Metrics 命名习惯不含 `:`，因此下列 HPA 契约指标在 `/metrics` 输出（`prometheus` 与 `opentelemetry` 两种格式）中会**同时**以原始名和无冒号别名（`:` 替换为 `_`）各暴露一条序列，两者取值一致，别名序列的 HELP 文本带“(HPA alias)”后缀。HPA / External Metrics Adaptor 推荐使用别名接入。

| 原始指标名 | HPA 别名 |
|------------|----------|
| `motor:prefill_utilization` | `motor_prefill_utilization` |
| `motor:decode_utilization` | `motor_decode_utilization` |
| `motor:prefill_replicas_required` | `motor_prefill_replicas_required` |
| `motor:decode_replicas_required` | `motor_decode_replicas_required` |
| `motor:request_rate` | `motor_request_rate` |
| `motor:pd_ratio_current` | `motor_pd_ratio_current` |
| `motor:pd_ratio_suggested` | `motor_pd_ratio_suggested` |
| `vllm:request_prefill_time_seconds`（含 `_mean` / `_p50` / `_p95` / `_p99` 派生分位数） | `vllm_request_prefill_time_seconds`（及对应后缀形式，如 `vllm_request_prefill_time_seconds_p95`） |

>[!NOTE]说明
>
> - 仅上述契约清单（恰好 8 个原始指标）内的指标会生成别名，其他指标（如 `vllm:num_requests_running`）只以原始名输出；容量规划的诊断指标（`*_capacity_tps` / `*_demand_tps` / `motor:kv_demand_tokens` / `motor:capacity_calibrated`）不生成别名。
> - 别名只在输出渲染层追加，聚合语义、`type` / `role` 等查询参数行为均不受影响。

### 进程与运行时指标

Engine 进程自身的指标，跨 Engine 求和：

| 指标名 | 类型 |
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

### KV Store 指标（可选）

配置了 `kv_cache_store_config`（Mooncake / Memcache）时，额外返回 KV Store 汇总指标（单位已换算为 GB）：

| 指标名 | 说明 |
|--------|------|
| `kv_store_size` | KV Store 容量，label `layer=cpu\|ssd\|all`、`stat=usage\|total` |
| `kv_store_ratio` | KV Store 使用率（0~1），label `layer=cpu\|ssd\|all`、`stat=usage_rate` |
| `kv_store_keys` | KV Store 存储的 key 数量 |
| `kv_store_eviction` | KV Store 淘汰计数，label `stat=success\|attempts` |

>[!NOTE]说明
>
> - KV Store 指标**仅随 `type=full` 视图返回**：`instance` / `role` / `dp` / `node` 视图只输出引擎指标，不含 `kv_store_*` 系列。
> - 未在 Motor 语义注册表中的未知指标不会被丢弃，按 Prometheus 类型回退聚合：`histogram` 合并桶，`gauge` / `counter` 求和。
> - 指标名以 `motor:` 为前缀的是 Motor 在聚合过程中计算或注入的指标，其余为 Engine 原始指标。

## 数据加工说明

Coordinator 在 `/metrics` 端点内部完成全部数据加工，调用方直接获取最终格式的数据，无需二次加工。加工要点如下：

- **采集周期**：后台线程按 `prometheus_metrics_config.reuse_time`（默认 `3` 秒）采集全部 Engine 指标并重新聚合，视图按采集版本缓存。
- **聚合语义**：Counter、状态/队列 Gauge 求和；缓存类 Gauge 取均值；热点资源 Gauge 取最大值；直方图合并桶；元数据类指标直通。
- **内部标签清理**：Engine 内部标签 `engine="<id>"` 会被剥离，不对外暴露。
- **时间戳系列清理**：`_created` 结尾的时间戳系列（Prometheus 惯例）会被丢弃。
- **负值处理**：仅 Gauge 允许负值；Counter / 直方图出现负值视为损坏，丢弃该采样行。
- **空指标处理**：无采样值的指标族仍会输出，值为显式 `0`，避免抓取端丢失系列。
- **非活跃实例**：实例不可用后其 Gauge 归零平滑衰减而非消失；Counter 清零（累计历史由新实例通过基线偏移承接）。
- **角色过滤（role_scope）**：TTFT / TPOT 等指标在 PD 分离模式下仅统计 Decode 侧（Prefill 节点不产出 token），`vllm:request_prefill_time_seconds` 仅统计 Prefill 侧；Prefill 与 Decode 合设（PD 混合）时不受影响。
- **分位数计算**：带分位数配置的直方图指标在合并桶后计算 p50 / p95 / p99 及均值。

## 对接 Prometheus

将本接口配置为 Prometheus 抓取目标即可。以 Kubernetes nodePort 部署为例：

```yaml
scrape_configs:
  - job_name: "mindie-motor"
    static_configs:
      - targets: ["{主机IP}:31017"]
    metrics_path: "/metrics"
```

使用建议：

- 集群级监控使用默认的 `full` 视图抓取；按节点监控使用 `type=node`；排障时用 `type=instance` / `type=dp` 查看细分数据。
- Counter 指标（如 `vllm:prompt_tokens_total`）在 PromQL 中使用 `rate()` / `irate()` 计算速率，不要使用原始累计值直接相减。
- Engine 重启会导致 Counter 归零，聚合值出现回落，Prometheus 的 `rate()` 自带计数器重置检测，无需额外处理。

## 错误处理

| 场景 | 结果 |
|------|------|
| `format` 参数取值非法 | HTTP `400`，响应体为错误详情 |
| `type` 参数取值非法 | 回退为 `full` 视图，不报错 |
| 指标尚未采集到（服务刚启动） | 返回空文本，或仅包含值为 `0` 的指标族 |

## 已弃用接口

### 实例指标查询接口

> [!NOTE] 说明
> `GET /instance/metrics` 接口已弃用，请使用 `GET /metrics?type=instance` 代替。调用本接口将返回 HTTP 410 Gone。

**接口格式**

请求类型：**GET**
> URL：`http(s)://{IP}:{Port}/instance/metrics`

IP与端口参见[指标接口的IP/端口与配置](./README.md#指标接口的ip端口与配置)

**响应示例**

```text
# /instance/metrics is deprecated. Use GET /metrics?type=instance instead.
```
