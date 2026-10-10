# 动态分桶调度

## 特性介绍

**动态分桶是面向Decode的调度策略，按请求真实输入token数和桶内负载动态选择短桶或长桶，以降低长短请求混合场景下的TPOT并提升输出吞吐。**

动态分桶调度（`dynamic_bucket`）将Decode endpoint稳定地划分为短序列桶和长序列桶，
再综合请求输入token数与两个桶的实时负载选择目标桶，最后在桶内选择负载最低的endpoint。
该策略适用于长短请求混合、不同序列长度对TPOT影响明显的场景（Attention耗时随序列长度增长线性增长），目标是在保留长短序列隔离收益的同时，
允许负载差异较大时将请求动态迁移到另一个桶，避免固定分桶造成热点。
该算法旨在降低TPOT，提升Decode的输出吞吐。

该策略不依赖KV Conductor：

- 仅Decode角色支持动态分桶选择，可以支持跨机Decode节点。
- Prefill、Encode和Union角色由 `prefill_scheduler_type` 控制，不能配置 `dynamic_bucket`。
- 请求无法计算有效 token 数时回退到 `load_balance`；负载均衡也无法选择时再回退到轮询。

### 工作原理

一次Decode调度依次执行以下步骤：

1. 获取请求长度。优先复用请求上已有的token ID；否则使用模型 tokenizer 处理 `messages` 或
   `prompt`，并将结果缓存到请求中。
2. 将 endpoint 按 `(instance_id, endpoint_id)` 排序，并按照配置稳定划入短桶和长桶。
3. 分别计算短桶和长桶 endpoint 的平均 `active_tokens`。
4. 使用 `dynamic_bucket_border` 根据请求长度得到初选桶，再通过长度因子和负载因子判断是否切换到另一个桶。
5. 在目标桶内选择 `active_tokens` 最小的 endpoint；如果目标桶为空，则尝试另一个桶。
6. Worker将目标桶中的全部候选endpoint交给CAS分配路径。工作负载视图发生变化时，CAS慢路径会使用
   最新负载在同一桶内重新选择，避免使用过期快照造成热点。

成功分配后，请求输入token数作为本次请求的 `active_tokens` 增量；请求结束时释放实际提交的工作负载。
`get_request_token_length` 优先复用已有 token ID，否则调用 tokenizer 获取 token 数，供选桶和记账使用。
动态分桶按输入token数衡量Decode负载，与现有 `load_balance` 按请求体长度 `req_info.req_len` 记账的口径不同。

#### Endpoint 分桶规则

设可用endpoint总数为 `N`，短桶数量为 `N_short`：

- `dynamic_bucket_short_bucket_count` 和 `dynamic_bucket_long_bucket_count` 均为 `0` 时，
  `N_short = max(1, floor(N / 2))`。
- 两者均大于 `0` 时，按两者的比例计算短桶数量：

  ```text
  N_short = round(N × dynamic_bucket_short_bucket_count / (dynamic_bucket_short_bucket_count + dynamic_bucket_long_bucket_count))
  ```

- 仅配置 `dynamic_bucket_short_bucket_count` 时，将它作为期望的短桶 endpoint 数量。
- 仅配置 `dynamic_bucket_long_bucket_count` 时，使用 `N - dynamic_bucket_long_bucket_count` 作为期望的短桶 endpoint 数量。
- 当 `N >= 2` 时，最终结果会限制在 `[1, N - 1]`，保证两个桶均非空；只有一个 endpoint 时全部归入短桶。

排序和分桶不依赖请求，因此相同 endpoint 集合会得到稳定的分桶结果。

#### 目标桶算法

##### 基础定义

定义递减 Sigmoid 函数：

```text
sigmoid_down(x) = 1 / (1 + exp(x))
```

设：

- `L`：请求输入token数。
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

### 使用场景

**适用于长短请求混合且序列长度对TPOT影响明显的Decode负载。已在Atlas 800I A3超节点服务器、Qwen3.5-397B-A17B-W8A8、Decode启用CPU offload的长尾混合负载中验证收益；其他模型、负载和配置应通过业务压测确认。**

| 场景 | 适用条件 |
|------|----------|
| 长短请求混合 | 长短请求混合的Decode负载，请求输入长度分布跨度大 |
| 序列长度对TPOT影响明显 | 不同序列长度对TPOT影响明显，Attention耗时随序列长度线性增长 |
| 预期收益模型 | Qwen3-MoE、Qwen3.5-Qwen3.8 |
| 已验证无收益模型 | DeepSeek-V4、GLM-5.1（使用压缩KV、Lightning Indexer减少Attention计算量，减少了Attention阶段读取KV Cache的I/O量） |

### 约束与限制

| 维度 | 约束说明 |
|------|----------|
| 部署场景 | 仅Decode角色支持动态分桶选择，可支持跨机Decode节点；Prefill、Encode、Union角色由 `prefill_scheduler_type` 控制，不能配置 `dynamic_bucket`。 |
| 特性互斥 | Decode通过 `decode_scheduler_type` 选择一种调度策略；Prefill可独立配置。动态分桶本身不依赖KV Conductor，其他策略的依赖按其配置要求启用。 |
| 硬件 | 已验证环境为Atlas 800I A3超节点服务器；其他硬件环境的收益需结合目标业务压测确认。 |
| 引擎 | 仅对Decode角色启用动态分桶；熔断、引擎类型和路由能力过滤发生在分桶之前。 |
| 软件依赖 | Coordinator需能访问模型目录并加载与引擎一致的tokenizer，所有可能运行Coordinator的节点均需挂载该目录；无需启用kv-events-config或部署KV Conductor。 |
| 方案约束 | 以请求真实输入token数选桶和记账；至少有两个可用endpoint时才能保证长短桶均非空，只有一个endpoint时全部归入短桶。具体收益需结合请求长度分布、并发度和分桶参数验证。 |
| 其他限制 | <ul><li>无法复用有效token ID，且tokenizer未加载、模型目录不可访问或token化失败时，回退 `load_balance`。</li><li>目标桶无可用endpoint时，尝试另一个桶。</li><li>两个桶均无可用endpoint时，回退 `load_balance` 再轮询。</li><li>熔断、引擎类型和路由能力过滤发生在分桶之前。</li><li>endpoint数量变化后重新分桶。</li><li>当 `N >= 2` 时，短桶数量限制在 `[1, N - 1]`；仅有一个endpoint时，全部归入短桶。</li></ul> |

## 特性使用

### 使用样例

1. 在 `user_config.json` 中，将 `motor_coordinator_config.scheduler_config.decode_scheduler_type` 设置为 `dynamic_bucket`，启用动态分桶调度，并配置以下分桶参数。

   根据需要调整同级的 `dynamic_bucket_*` 参数；长度和负载参数可先使用默认值，桶数量按集群容量设置。
   将 `motor_engine_prefill_config.engine_config.model` 设置为实际模型目录，其余部署配置保持原有设置。

   以下示例按 `2:6` 的比例划分短桶和长桶；当共有8个endpoint时，短桶和长桶分别包含2个和6个：

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

    参数均位于 motor_coordinator_config.scheduler_config，在配置加载时校验。长度与桶数量必须为整数，
    不接受布尔值；缩放系数不接受布尔值、NaN 或无穷大。

    | 参数 | 类型 | 默认值 | 必填 | 取值范围 | 说明 |
    |------|------|--------|------|----------|------|
    | decode_scheduler_type | string | `load_balance` | 否 | load_balance / round_robin / kv_cache_affinity / dynamic_bucket | 启用本特性时需显式设置为 `dynamic_bucket`。`prefill_scheduler_type` 不支持该取值；Decode配置为 `kv_cache_affinity` 时实际使用 `load_balance`。 |
    | dynamic_bucket_short_median | int | `16384` | 否 | > 0 | 短桶特征长度（token），是短桶长度因子的 Sigmoid 中点。它不是运行时统计得到的中位数。 |
    | dynamic_bucket_long_median | int | `98304` | 否 | > dynamic_bucket_border | 长桶特征长度（token）。`long_median - border` 是长桶长度因子的归一化跨度。 |
    | dynamic_bucket_border | int | `32768` | 否 | > 0 且 < dynamic_bucket_long_median | 初始长短分类边界（token）。`L <= border` 初选短桶，否则初选长桶。 |
    | dynamic_bucket_length_scale | float | `1.0` | 否 | > 0 的有限值 | 长度因子缩放系数。值越大，特征长度附近的 Sigmoid 变化越陡，可通过业务压测调整。 |
    | dynamic_bucket_load_scale | float | `4.0` | 否 | > 0 的有限值 | 负载因子缩放系数。该值还令短桶初选路径的负载比中点为 `1 / load_scale`，不是普通的线性权重。 |
    | dynamic_bucket_short_bucket_count | int | `0` | 否 | >= 0 | 短桶 endpoint 数量或比例分子。详见 [Endpoint 分桶规则](#endpoint-分桶规则)。 |
    | dynamic_bucket_long_bucket_count | int | `0` | 否 | >= 0 | 长桶 endpoint 数量或比例分子。与短桶数量同时为正时，两者表示分桶比例。 |

2. 部署服务。

   部署要求：

   - Coordinator 需要能够访问 `engine_config.model` 指向的模型目录，以加载与引擎一致的 tokenizer。
   - 部署时应将模型目录挂载到 Coordinator，并确保所有可能运行 Coordinator 的节点均可读取该目录。
   - 无需启用 `kv-events-config` 或部署 KV Conductor。

   K8s 部署示例：

   ```bash
   cd examples/deployer

   # PD 分离
   python deploy.py --config_dir ../infer_engines/vllm
   ```

### 验证特性

**可通过Coordinator输出的动态分桶选择日志核对请求长度、目标桶、所选endpoint和候选负载；请求无法获得有效token数时会回退到负载均衡。**

将 `motor_coordinator_config.logging_config.log_level` 设置为 `DEBUG` 并使配置生效。动态分桶选择日志为DEBUG级别，默认INFO级别不会输出。

部署完成后，向服务发送推理请求。以下示例中的 `<ip>`、`<port>` 和 `model` 值需替换为实际服务地址、端口和模型名称：

```bash
curl -X POST "http://<ip>:<port>/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3.5",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "who are you?"}
    ],
    "max_tokens": 100,
    "stream": true
  }'
```

通过查看Coordinator日志验证动态分桶调度是否生效：

```bash
kubectl logs <coordinator-pod> | grep "Dynamic bucket selected"
```

预期输出：

```text
Dynamic bucket selected req_length=... target_bucket=... selected=...-... loads=...
```

## 调优建议

建议先使用默认长度和负载参数，只按集群容量设置桶数量；获得真实请求长度分布和桶负载数据后，再调整
`short_median`、`long_median`、`border`、`length_scale` 和 `load_scale`。

以下结果来自Atlas 800I A3超节点服务器环境下两组配置的有效轮次中位数，仅代表该模型、数据分布和部署配置下的
调优结论，不作为其他模型或硬件环境的通用默认值。

### 实验模型与服务配置

- 模型：`Qwen3.5-397B-A17B-W8A8`。
- 部署：1P1D，Decode使用DP16 / TP1，共16个endpoint，并开启CPU offload。
- 压测：两组均使用1000条长尾混合请求，并发384，请求率配置400 req/s；每组内部使用相同数据集及匹配的运行配置比较DB与LB。

| 参数 | 配置 A | 配置 B |
|---|---:|---:|
| burstiness | 1.0 | 0 |
| 短桶 / 长桶配置比例 | 12:4 | 11:5 |
| 分桶边界（token） | 65,536 | 65,536 |
| 短请求代表长度（token） | 32,768 | 32,768 |
| 长请求代表长度（token） | 196,608 | 98,304 |
| length_scale | 1 | 1 |
| load_scale | 3.5 | 2.5 |
| DB / LB 有效轮数 | 3 / 3 | 2 / 2 |

短桶 / 长桶配置比例对应 `dynamic_bucket_short_bucket_count` 与 `dynamic_bucket_long_bucket_count`。
两者均为正时表示比例，实际 endpoint 数量由可用 Decode endpoint 总数按前述分桶规则换算。
本次共 16 个 Decode endpoint，因此配置 A 实际划分为短桶 12 个、长桶 4 个，配置 B 为短桶 11 个、长桶 5 个。

该负载中91%的输入小于16K token，3%达到或超过64K token，其中2%达到或超过100,000 token。
本次两组实验统计的最大输入长度为134,691 token，目标输出长度范围为256～4,096 token。
每轮实际总输出均为962,527 token，保证对比相同的输出工作量。

### 两组测试配置

上表列出了配置A与配置B的分桶参数。以下为配置B的调度配置示例，其压测burstiness为0；
如使用配置A，应将长桶特征长度改为196608、负载系数改为3.5、短桶和长桶配置改为12和4，并将压测burstiness设为1.0。

```json
{
  "motor_coordinator_config": {
    "scheduler_config": {
      "prefill_scheduler_type": "load_balance",
      "decode_scheduler_type": "dynamic_bucket",
      "dynamic_bucket_short_median": 32768,
      "dynamic_bucket_long_median": 98304,
      "dynamic_bucket_border": 65536,
      "dynamic_bucket_length_scale": 1.0,
      "dynamic_bucket_load_scale": 2.5,
      "dynamic_bucket_short_bucket_count": 11,
      "dynamic_bucket_long_bucket_count": 5
    }
  }
}
```

### 两组测试中位数

下表各项指标均取有效轮次的中位数。其中，TPOT P90 为各轮 P90 的中位数；峰均比为各轮 D 请求长度负载峰均比均值的中位数。

| 指标 | 配置 A：LB → DB | 相对变化 | 配置 B：LB → DB | 相对变化 |
|---|---|---:|---|---:|
| 输出吞吐（token/s） | 1466.17 → 1497.06 | **提升 2.11%** | 1455.57 → 1503.39 | **提升 3.29%** |
| TPOT Mean（ms） | 82.80 → 81.80 | **降低 1.21%** | 82.60 → 80.75 | **降低 2.24%** |
| TPOT P90（ms） | 90.00 → 89.50 | **降低 0.56%** | 89.60 → 88.15 | **降低 1.62%** |
| D 请求长度负载峰均比 | 1.4748 → 1.3857 | 降低 6.04% | 1.4773 → 1.5102 | 升高 2.22% |

吞吐提升按 `DB / LB − 1` 计算，指标降低比例按 `1 − DB / LB` 计算。

在上述 Atlas 800I A3超节点服务器、Decode CPU offload、长尾混合负载配置下，DB 的输出吞吐和 TPOT P90 均得到改善。
负载峰均比的改善并不一致。本节以外的另行验证中，关闭 offload 的部分配置也观察到正收益，
相关对比数据未在本节列出，不纳入上述两组实验的结论。其他参数组合中存在吞吐负收益，
实际使用时仍需针对目标负载、并发和分桶参数进行验证。
