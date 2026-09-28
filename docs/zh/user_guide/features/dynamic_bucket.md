# 动态分桶调度

## 功能介绍

动态分桶调度（`dynamic_bucket`）将 Decode endpoint 稳定地划分为短序列桶和长序列桶，
再综合请求输入 token 数与两个桶的实时负载选择目标桶，最后在桶内选择负载最低的 endpoint。
该策略适用于长短请求混合、不同序列长度对TPOT影响明显的场景（Attention耗时随序列长度增长线性增长），目标是在保留长短序列隔离收益的同时，
允许负载差异较大时将请求动态迁移到另一个桶，避免固定分桶造成热点。
该算法旨在降低TPOT，提升Decode的输出吞吐，预期收益的模型（Qwen3-MoE，Qwen3.5-Qwen3.8），已验证无收益的模型（DeepSeek-V4，GLM-5.1，使用压缩KV、Lightning Indexer减少了Attention的计算量，减少了Attention阶段读取 KV Cache 的 I/O量）

该策略不依赖 KV Conductor：

- 仅 Decode 角色支持动态分桶选择，可以支持跨机Decode节点。
- Prefill、Encode 和 Union 角色由 `prefill_scheduler_type` 控制，不能配置 `dynamic_bucket`。
- 请求无法计算有效 token 数时回退到 `load_balance`；负载均衡也无法选择时再回退到轮询。

## 调度流程

一次 Decode 调度依次执行以下步骤：

1. 获取请求长度。优先复用请求上已有的 token ID；否则使用模型 tokenizer 处理 `messages` 或
   `prompt`，并将结果缓存到请求中。
2. 将 endpoint 按 `(instance_id, endpoint_id)` 排序，并按照配置稳定划入短桶和长桶。
3. 分别计算短桶和长桶 endpoint 的平均 `active_tokens`。
4. 使用 `dynamic_bucket_border` 根据请求长度得到初选桶，再通过长度因子和负载因子判断是否切换到另一个桶。
5. 在目标桶内选择 `active_tokens` 最小的 endpoint；如果目标桶为空，则尝试另一个桶。
6. Worker 将目标桶中的全部候选 endpoint 交给 CAS 分配路径。工作负载视图发生变化时，CAS 慢路径会使用
   最新负载在同一桶内重新选择，避免使用过期快照造成热点。

成功分配后，请求输入 token 数作为本次请求的 `active_tokens` 增量；请求结束时释放实际提交的工作负载。
`get_request_token_length` 优先复用已有 token ID，否则调用 tokenizer 获取 token 数，供选桶和记账使用。
动态分桶按输入 token 数衡量 Decode 负载，与现有 `load_balance` 按请求体长度 `req_info.req_len` 记账的口径不同。

## Endpoint 分桶规则

设可用 endpoint 总数为 `N`，短桶数量为 `N_short`：

- `dynamic_bucket_short_bucket_count` 和 `dynamic_bucket_long_bucket_count` 均为 `0` 时，
  `N_short = max(1, floor(N / 2))`。
- 两者均大于 `0` 时，按两者的比例计算短桶数量：

  ```text
  N_short = round(N × short_count / (short_count + long_count))
  ```

- 仅配置 `short_count` 时，将它作为期望的短桶 endpoint 数量。
- 仅配置 `long_count` 时，使用 `N - long_count` 作为期望的短桶 endpoint 数量。
- 当 `N >= 2` 时，最终结果会限制在 `[1, N - 1]`，保证两个桶均非空；只有一个 endpoint 时全部归入短桶。

排序和分桶不依赖请求，因此相同 endpoint 集合会得到稳定的分桶结果。

## 目标桶算法

### 基础定义

定义递减 Sigmoid 函数：

```text
sigmoid_down(x) = 1 / (1 + exp(x))
```

设：

- `L`：请求输入 token 数。
- `B`：`dynamic_bucket_border`。
- `M_short`：`dynamic_bucket_short_median`。
- `M_long`：`dynamic_bucket_long_median`。
- `Q_short`、`Q_long`：短桶和长桶 endpoint 的平均 `active_tokens`。
- `alpha`：`dynamic_bucket_length_scale`。
- `beta`：`dynamic_bucket_load_scale`。

当 `L <= B` 时初选短桶，并计算：

```text
distance        = L
bucket_span     = M_short
length_factor   = sigmoid_down(alpha / M_short × (distance - M_short))
load_ratio      = Q_long / Q_short
load_factor     = sigmoid_down(beta × (load_ratio - 1 / beta))
```

当 `L > B` 时初选长桶，并计算：

```text
distance        = L - B
bucket_span     = M_long - B
length_factor   = sigmoid_down(alpha / (M_long - B) × (distance - (M_long - B)))
load_ratio      = Q_short / Q_long
load_factor     = sigmoid_down(beta × (load_ratio - 1))
```

如果分母对应的初选桶负载为 `0`，实现会把 `load_ratio` 视为正无穷，使负载因子趋近于 `0`，
从而优先保留在当前空闲桶中。

最终决策为：

```text
length_factor + load_factor > 1  → 切换到另一个桶
length_factor + load_factor <= 1 → 保持初选桶
```

长度因子表达请求迁移到另一个桶的灵活程度：请求长度低于相应特征长度时该因子大于 `0.5`，
高于特征长度时小于 `0.5`。因此较短的请求在初选桶过载时更容易被迁移，而超过长桶特征长度的请求
更倾向于保留在长桶。负载因子表达初选桶相对另一个桶的拥塞程度。两者共同决定是否突破 `B`
给出的初始长短分类。

## 参数说明

参数均位于 `motor_coordinator_config.scheduler_config`，在配置加载时校验。长度与桶数量必须为整数，
不接受布尔值；缩放系数不接受布尔值、NaN 或无穷大。

| 参数 | 类型 | 默认值 | 约束与含义 |
|------|------|--------|------------|
| `decode_scheduler_type` | string | `load_balance` | 设置为 `dynamic_bucket` 时为 Decode 启用动态分桶调度。`prefill_scheduler_type` 不支持该取值。 |
| `dynamic_bucket_short_median` | int | `16384` | 短桶特征长度（token），是短桶长度因子的 Sigmoid 中点。必须大于 `0`。它不是运行时统计得到的中位数。 |
| `dynamic_bucket_long_median` | int | `98304` | 长桶特征长度（token）。`long_median - border` 是长桶长度因子的归一化跨度，必须大于 `dynamic_bucket_border`。 |
| `dynamic_bucket_border` | int | `32768` | 初始长短分类边界（token），必须大于 `0`。`L <= border` 初选短桶，否则初选长桶。 |
| `dynamic_bucket_length_scale` | float | `1.0` | 长度因子缩放系数，必须是大于 `0` 的有限数值。值越大，特征长度附近的 Sigmoid 变化越陡，可通过业务压测调整。 |
| `dynamic_bucket_load_scale` | float | `4.0` | 负载因子缩放系数，必须是大于 `0` 的有限数值。该值还令短桶初选路径的负载比中点为 `1 / load_scale`，不是普通的线性权重。 |
| `dynamic_bucket_short_bucket_count` | int | `0` | 短桶 endpoint 数量或比例分子，必须不小于 `0`。详见 [Endpoint 分桶规则](#endpoint-分桶规则)。 |
| `dynamic_bucket_long_bucket_count` | int | `0` | 长桶 endpoint 数量或比例分子，必须不小于 `0`。与短桶数量同时为正时，两者表示分桶比例。 |

建议先使用默认长度和负载参数，只按集群容量设置桶数量；获得真实请求长度分布和桶负载数据后，再调整
`short_median`、`long_median`、`border`、`length_scale` 和 `load_scale`。

## 配置示例

以下示例按 `2:6` 的比例划分短桶和长桶；当共有 8 个 endpoint 时，短桶和长桶分别包含 2 个和 6 个：

```json
{
  "motor_coordinator_config": {
    "scheduler_config": {
      "prefill_scheduler_type": "load_balance",
      "decode_scheduler_type": "dynamic_bucket",
      "dynamic_bucket_short_median": 16384,
      "dynamic_bucket_long_median": 98304,
      "dynamic_bucket_border": 32768,
      "dynamic_bucket_length_scale": 1.0,
      "dynamic_bucket_load_scale": 4.0,
      "dynamic_bucket_short_bucket_count": 2,
      "dynamic_bucket_long_bucket_count": 6
    }
  },
  "motor_engine_prefill_config": {
    "engine_config": {
      "model": "/mnt/weight/your-model"
    }
  }
}
```

Coordinator 需要能够访问 `engine_config.model` 指向的模型目录，以加载与引擎一致的 tokenizer。
部署时应将模型目录挂载到 Coordinator，并确保所有可能运行 Coordinator 的节点均可读取该目录。
无需启用 `kv-events-config` 或部署 KV Conductor。

## 910C 实验配置与结果

以下结果来自 Ascend 910C 环境下的三轮重复测试中位数，仅代表该模型、数据分布和部署配置下的
调优结论，不作为其他模型或硬件环境的通用默认值。

### 实验模型与服务配置

- 模型：`Qwen3.5-397B-A17B-w8a8`，Ascend 量化。
- Prefill 使用 Mooncake KV producer，Decode 使用 Mooncake KV consumer。
- Decode 使用 recompute scheduler 和 `FULL_DECODE_ONLY` CUDAGraph，关闭 Prefix Caching。

| 组件 | 并行配置 | `max-model-len` | `max-num-batched-tokens` | `max-num-seqs` | `gpu-memory-utilization` |
|------|----------|----------------:|-------------------------:|---------------:|-------------------------:|
| Prefill | DP16 / TP1 | 140000 | 8192 | 32 | 0.90 |
| Decode | DP16 / TP1 | 140000 | 120 | 32 | 0.96 |

压测使用 1000 条长尾混合请求，其中输入长度不小于 64K token 的请求占 3%，不小于 100K token
的请求占 2%；实测平均输入长度约 9686 token，最大输入长度为 134689 token。AISBench 的并发数为
256，request rate 为 400，最大输出长度为 4096，`temperature=0`，并设置 `ignore_eos=true`。

### 本环境推荐配置

```json
{
  "motor_coordinator_config": {
    "scheduler_config": {
      "prefill_scheduler_type": "load_balance",
      "decode_scheduler_type": "dynamic_bucket",
      "dynamic_bucket_short_median": 32768,
      "dynamic_bucket_long_median": 196608,
      "dynamic_bucket_border": 65536,
      "dynamic_bucket_length_scale": 1.0,
      "dynamic_bucket_load_scale": 2.5,
      "dynamic_bucket_short_bucket_count": 11,
      "dynamic_bucket_long_bucket_count": 5
    }
  }
}
```

### 三轮测试中位数

Load Balance 与 Dynamic Bucket 使用相同数据集、服务配置和压测参数，六轮测试均为
`1000/1000` 请求成功。

| 指标 | Load Balance | Dynamic Bucket | Dynamic Bucket 相对变化 |
|------|-------------:|---------------:|------------------------:|
| 测试时长 | 635208.3 ms | 598251.2 ms | -5.82% |
| 输出吞吐 | 1398.5712 token/s | 1493.6803 token/s | +6.80% |
| 请求吞吐 | 1.5743 req/s | 1.6715 req/s | +6.17% |
| TTFT P90 | 69295.9 ms | 63785.5 ms | -7.95% |
| TPOT Mean | 78.4 ms/token | 78.1 ms/token | -0.38% |
| TPOT P90 | 85.3 ms/token | 85.6 ms/token | +0.35% |
| TPOT P99 | 100.5 ms/token | 98.3 ms/token | -2.19% |
| `request_len` 峰均比平均值 | 1.51844 | 1.49386 | -1.62% |
| `request_len` 峰均比最大值 | 2.45478 | 2.11415 | -13.88% |
| 全局平均 batch | 148.9143 | 150.5883 | +1.12% |
| 每 DP 平均 batch | 9.3071 | 9.4118 | +1.12% |

该配置在本环境中改善了输出吞吐、请求吞吐、长尾 TTFT、TPOT Mean/P99 和 Decode 负载均衡；
但 TPOT P90 比 Load Balance 高 0.3 ms/token，因此不能表述为所有指标均优于 Load Balance。

## 降级与边界行为

- Tokenizer 未加载、模型目录不可访问或请求无法生成有效 token ID：回退到 `load_balance`。
- 目标桶无可用 endpoint：尝试另一个桶。
- 两个桶均无可用 endpoint：回退到 `load_balance`，仍失败则回退到轮询。
- Prefill、Encode 和 Union 请求：不执行动态分桶，使用 `prefill_scheduler_type` 指定的策略。
- 熔断、引擎类型和路由能力过滤发生在分桶之前；被过滤的实例不会进入候选桶。
- endpoint 数量变化后，会根据新的稳定排序重新计算分桶；显式数量最终仍受 `[1, N - 1]` 限制。

发生动态分桶选择时，Coordinator 会输出包含请求长度、目标桶、所选 endpoint 和各候选负载的日志：

```text
Dynamic bucket selected req_length=... target_bucket=... selected=...-... loads=...
```
