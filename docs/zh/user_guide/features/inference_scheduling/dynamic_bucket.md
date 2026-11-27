# 动态分桶调度

## 特性介绍

**动态分桶按请求真实输入token数和桶内负载动态选择目标桶，支持独立配置Prefill / Union与Decode调度策略。Decode场景可用于优化长短请求混合负载的TPOT与输出吞吐；Prefill / Union的性能收益需另行验证。**

动态分桶调度（`dynamic_bucket`）将所选角色的**实例**稳定地划分为多个序列长度桶，同一实例下的全部 endpoint 跟随该实例所在桶。
桶由 `dynamic_bucket_boundaries` 定义，桶数等于边界数加一；默认边界为 `[32768]`，即
`0-32K` 与 `32K+` 两个桶，配置多个边界时可扩展为任意数量的桶，例如
`0-32K`、`32K-64K`、`64K-128K`、`128K+`。
调度时综合请求输入token数与相邻桶的实时负载选择目标桶，最后在桶内选择负载最低的endpoint。
该策略适用于长短请求混合、不同序列长度对TPOT影响明显的场景（Attention耗时随序列长度线性增长），目标是在保留长度隔离收益的同时，
允许负载差异较大时将请求动态迁移到相邻桶，避免固定分桶造成热点。
该算法旨在降低TPOT，提升Decode的输出吞吐。

该策略不依赖KV Conductor：

- Prefill / Union通过 `prefill_scheduler_type=dynamic_bucket` 启用；Decode通过 `decode_scheduler_type=dynamic_bucket` 独立启用。
- Encode沿用 `prefill_scheduler_type`，配置为 `dynamic_bucket` 时仍回退到 `load_balance`。
- 请求无法计算有效 token 数时回退到 `load_balance`；负载均衡也无法选择时再回退到轮询。

### 工作原理

一次动态分桶调度依次执行以下步骤：

1. 获取请求长度。优先复用请求上已有的token ID；否则使用模型 tokenizer 处理 `messages` 或
   `prompt`，并将结果缓存到请求中。
2. 将 instance 按 `instance_id` 排序，按配置把**整实例**划入一个桶；该实例下全部 endpoint 跟随同一桶，避免同一 MoE/DP 组跨长短桶。
3. 分别计算每个桶内 endpoint 的平均 `active_tokens`。
4. 根据桶边界得到初选桶，再通过长度因子和负载因子判断是否切换到相邻桶。
5. 在目标桶内选择 `active_tokens` 最小的 endpoint；如果目标桶为空，则尝试最近的桶。
6. Worker将目标桶中的全部候选endpoint交给CAS分配路径。工作负载视图发生变化时，CAS慢路径会使用
   最新负载在同一桶内重新选择，避免使用过期快照造成热点。

成功分配后，请求输入token数作为本次请求的 `active_tokens` 增量；请求结束时释放实际提交的工作负载。
`get_request_token_length` 优先复用已有 token ID，否则调用 tokenizer 获取 token 数，供选桶和记账使用。
动态分桶按输入token数衡量所选角色的负载，与现有 `load_balance` 按请求体长度 `req_info.req_len` 记账的口径不同。

#### Prefill / Union 的配置与调用链

`SchedulerConfig` 分别保存 Prefill / Union 与 Decode 的策略，`type_for_role` 为 Worker 解析当前角色。
只要任一侧启用动态分桶，配置加载器便从引擎配置中补齐模型路径，`TokenizerManager` 在启动时加载 tokenizer；
不要求启用 KV 事件或配置 KV Conductor 服务。

请求处理顺序为：Router 创建 `RequestInfo` → `AsyncSchedulerClient` 过滤当前角色的可用实例 →
`DynamicBucketPolicy` 按真实 token 数及负载选桶 → 共享内存 CAS 提交分配 → 请求结束释放负载。
Prefill / Union 复用现有的分桶算法与 CAS 账本，不增加跨 Worker 的可变共享状态；两侧共用分桶参数，
候选 endpoint 和负载按当前角色独立获取。Decode 的策略可保持 `load_balance` 或单独启用分桶。

Encode 继续走负载均衡，并沿用现有多模态工作负载估算与记账。token 数不可用时记录告警并回退负载均衡，之后仍可回退轮询；
实例过滤、熔断、同桶 CAS 冲突重选及结束释放继续遵循现有调度流程。默认配置不启用本特性。

#### 实例分桶规则

请求长度按 `dynamic_bucket_boundaries` 划分桶区间；实例（而非单个 endpoint）再划入这些桶：

- 边界必须为严格递增的正整数，表示每个有界桶的**闭区间上界**。例如
  `[32768, 65536, 131072]` 划分为 `[0, 32768]`、`(32768, 65536]`、`(65536, 131072]`
  和 `(131072, +∞)` 四个桶；最后一个桶没有上界。只配置一个元素（如默认的 `[32768]`）时即为两个桶。
- `dynamic_bucket_counts` 与桶一一对应，长度必须等于桶数。所有元素均为 `0` 或未配置时按桶数均分，
  余数优先分配给靠后的桶（两桶场景即长桶，保持原方案的 `短桶 = ⌊N/2⌋`）；
  当元素之和恰好等于实例数 `N` 且每个元素都大于 `0` 时按精确数量分配；否则按总实例数计算比例配额，
  向下取整后按小数余数从大到小分配剩余实例（相同余数优先给靠后的桶）。仅在出现空桶时，从可让出实例的桶补足下限。
  `N` 不小于桶数时保证每个桶至少一个实例；`N` 小于桶数时按桶顺序填满前 `N` 个桶，
  其余桶为空，由就近回退兜底。同一实例的全部 endpoint 必须落在同一桶。
- `dynamic_bucket_medians` 与桶一一对应，是各桶用于长度因子的特征长度（可省略）。省略时有界桶取区间中点，
  最后一个桶取 `下界 + 2 × 前一桶宽度`。默认单一边界 `[32768]` 时两个特征长度为
  `[16384, 98304]`，与原两桶方案一致。配置值必须大于对应桶下界且严格递增。
- instance 按 `instance_id` 排序后，按解析出的桶数量连续切分；切分单位是实例而不是 endpoint，因此相同实例集合会得到稳定的分桶结果。

排序和分桶不依赖请求，因此相同实例集合和配置会得到稳定的分桶结果。

#### 目标桶算法

设：

- `i`：按 `dynamic_bucket_boundaries` 得到的初选桶。
- `B_i`：桶 `i` 的下界；第一桶下界为 `0`，其余为前一个边界。
- `M_i`：桶 `i` 的特征长度（`dynamic_bucket_medians` 或默认推导值）。
- `L`、`alpha_i`、`beta_j`：请求长度、桶 `i` 的 `dynamic_bucket_length_scales[i]`、
  相邻桶 `j` 的 `dynamic_bucket_load_scales[j]`。
- `Q_i`：桶 `i` 内 endpoint 的平均 `active_tokens`；桶为空时视为不可用。

长度因子只由初选桶决定，请求长度在桶内越靠近上界（或越超过特征长度），因子越低：

```text
length_factor = sigmoid_down(alpha_i × ((L - B_i) / (M_i - B_i) - 1))
```

随后只评估与初选桶相邻的两个桶（`i - 1` 和 `i + 1`），不会跨桶跳转。对每个非空相邻桶 `j`：

```text
load_ratio   = Q_j / Q_i
load_factor  = sigmoid_down(beta_j × (load_ratio - 1 / beta_j))   # j 更长
load_factor  = sigmoid_down(beta_j × (load_ratio - 1))            # j 更短
score        = length_factor + load_factor
```

`alpha_i` 与 `beta_j` 均按目标桶取值：长度因子使用初选桶 `i` 的缩放系数，负载因子使用相邻桶 `j` 的缩放系数。

取 `score` 最大的相邻桶；仅当 `score > 1` 时切换，否则保留初选桶。若初选桶为空，
则按桶距离选择最近的非空桶；若相邻桶为空，则不会为切换目标。

分桶是双向的软隔离，并不为短请求预留 Prefill 容量。向较短桶切换时，等负载的负载因子为 `0.5`；
若请求长度低于初选桶 median，长度因子大于 `0.5`，因此等负载时也可能向短桶切换。
例如边界 32K / 96K、median 16K / 64K / 144K、每桶一个实例且负载均为 96K 时，
48K 请求可进入短桶，随后 4K 请求也可能在该实例上排队。实例隔离不消除这种 Prefill 算力竞争。
若业务要求短请求 P95/P99 TTFT，应对双向跨桶与限制向短桶溢出的策略开展同镜像、同前缀缓存初态、
同到达序列的对照，同时记录实际落桶分布、短请求尾时延和整体吞吐；当前实验未量化该差异。

### 使用场景

适用于长短请求混合、序列长度对时延影响明显的负载。Decode 侧重 TPOT 与输出吞吐；Prefill / Union 侧重 TTFT。
Prefill 已在 Atlas 800I A2、DeepSeek-V4-Flash、3P1D 的长短混合数据集上对照负载均衡（见下文实验）；
Decode 曾在 Atlas 800I A3、Qwen3.5-397B-A17B-W8A8、CPU offload 的长尾混合负载上验证 TPOT。
其他模型、负载和配置应通过业务压测确认。

| 场景 | 适用条件 |
|------|----------|
| 长短请求混合 | 请求输入长度分布跨度大，短请求与长 Prefill 争用同一批实例 |
| 序列长度对时延影响明显 | Attention 耗时随序列长度增长；Prefill 看 TTFT，Decode 看 TPOT |
| Prefill 已见表内收益 | Atlas 800I A2、DeepSeek-V4-Flash、实例级三桶（下文实验） |
| Decode 预期收益模型 | Qwen3-MoE、Qwen3.5 |
| Decode 未见 TPOT 收益 | DeepSeek-V4、GLM-5.1（压缩 KV / Lightning Indexer，Attention 读 KV 的 I/O 更少） |

### 约束与限制

| 维度 | 约束说明 |
|------|----------|
| 部署场景 | Prefill / Union与Decode可独立启用动态分桶，可支持跨机节点；Encode不使用分桶选择。 |
| 特性互斥 | Decode通过 `decode_scheduler_type` 选择一种调度策略；Prefill可独立配置。动态分桶本身不依赖KV Conductor，其他策略的依赖按其配置要求启用。 |
| 硬件 | Prefill 对照实验环境为 Atlas 800I A2；Decode 既有验证环境为 Atlas 800I A3 超节点。其他硬件的收益需结合目标业务压测确认。 |
| 引擎 | 对Prefill / Union与Decode角色启用动态分桶；熔断、引擎类型和路由能力过滤发生在分桶之前。 |
| 软件依赖 | Coordinator需能访问模型目录并加载与引擎一致的tokenizer，所有可能运行Coordinator的节点均需挂载该目录；无需启用kv-events-config或部署KV Conductor。 |
| 方案约束 | 以请求真实输入token数选桶和记账。**隔离粒度是该角色的实例数，不是卡数或 DP rank 数**：同一实例下的全部 endpoint 进入同一桶。实例数不小于桶数时保证每个桶非空，少于桶数时按桶顺序填充并由就近回退兜底。单实例多 endpoint（如 1P1D、Decode DP16）无法形成多桶隔离。具体收益需结合请求长度分布、并发度和分桶参数验证。 |
| 其他限制 | <ul><li>无法复用有效token ID，且tokenizer未加载、模型目录不可访问或token化失败时，回退 `load_balance`。</li><li>目标桶无可用endpoint时，按桶距离回退到最近的非空桶。</li><li>所有桶均无可用endpoint时，回退 `load_balance` 再轮询。</li><li>熔断、引擎类型和路由能力过滤发生在分桶之前。</li><li>实例集合变化后重新分桶。</li><li>实例数 `N` 小于桶数时，按桶顺序填满前 `N` 个桶，其余桶为空并由就近回退兜底。</li></ul> |

## 特性使用

### 使用样例

1. 在 `user_config.json` 中，将 `motor_coordinator_config.scheduler_config.decode_scheduler_type` 设置为 `dynamic_bucket`，启用Decode动态分桶调度，并配置以下分桶参数。Prefill / Union场景改为设置同级的 `prefill_scheduler_type=dynamic_bucket`；两者可单独或同时启用，并共用同一组分桶参数。

   根据需要调整同级的 `dynamic_bucket_*` 参数；桶边界、特征长度和缩放参数可先使用默认值，桶数量按集群容量设置。
   将 `motor_engine_prefill_config.engine_config.model` 设置为实际模型目录，其余部署配置保持原有设置。

   以下示例使用单一边界 `[32768]` 划分短桶和长桶，并通过 `dynamic_bucket_counts` 按 `2:6` 的比例分配；
   当该角色共有 8 个实例时，短桶和长桶分别包含 2 个和 6 个实例（每个实例的全部 endpoint 跟随所在桶）：

   ```json
   {
     "motor_coordinator_config": {
       "scheduler_config": {
         "prefill_scheduler_type": "load_balance",
         "decode_scheduler_type": "dynamic_bucket",
         "dynamic_bucket_boundaries": [32768],
         "dynamic_bucket_medians": [16384, 98304],
         "dynamic_bucket_counts": [2, 6],
         "dynamic_bucket_length_scales": [1.0, 1.0],
         "dynamic_bucket_load_scales": [4.0, 4.0]
       }
     },
     "motor_engine_prefill_config": {
       "engine_config": {
         "model": "/mnt/weight/your-model"
       }
     }
   }
   ```

    参数均位于 motor_coordinator_config.scheduler_config，在配置加载时校验。边界、特征长度与桶数量必须为整数，
    不接受布尔值；缩放系数必须是与桶数等长的浮点列表（省略时广播默认值），不接受布尔值、NaN 或无穷大。

    | 参数 | 类型 | 默认值 | 必填 | 取值范围 | 说明 |
    |------|------|--------|------|----------|------|
    | prefill_scheduler_type | string | `load_balance` | 否 | load_balance / round_robin / kv_cache_affinity / dynamic_bucket | 设置为 `dynamic_bucket` 时为Prefill / Union启用分桶，Encode回退为 `load_balance`。 |
    | decode_scheduler_type | string | `load_balance` | 否 | load_balance / round_robin / kv_cache_affinity / dynamic_bucket | 启用本特性时需显式设置为 `dynamic_bucket`。仅启用Prefill / Union分桶时可保持本字段为 `load_balance`；Decode配置为 `kv_cache_affinity` 时实际使用 `load_balance`。 |
    | dynamic_bucket_length_scales | list[float] | `[]` | 否 | 省略或为空时全部取 `1.0`；否则长度等于桶数，每项为 > 0 的有限值 | 各桶长度因子缩放系数。第 `i` 项控制桶 `i` 的 Sigmoid 陡峭度；值越大，特征长度附近变化越陡。 |
    | dynamic_bucket_load_scales | list[float] | `[]` | 否 | 省略或为空时全部取 `4.0`；否则长度等于桶数，每项为 > 0 的有限值 | 各桶负载因子缩放系数。切换到桶 `j` 时使用第 `j` 项，并令向更长桶切换的负载比中点为 `1 / dynamic_bucket_load_scales[j]`。 |
    | dynamic_bucket_boundaries | list[int] | `[32768]` | 否 | 不能为空；严格递增的正整数列表 | 桶上界（token）。长度加一即为桶数；每个边界属于较小的桶，最后一个桶无上界。默认 `[32768]` 表示 `0-32K` 与 `32K+` 两个桶。 |
    | dynamic_bucket_medians | list[int] | `[]` | 否 | 长度等于桶数；每项大于对应桶下界且严格递增 | 各桶特征长度（token），用于长度因子。省略时有界桶取区间中点，最后一个桶取 `下界 + 2 × 前一桶宽度`；默认单边界时为 `[16384, 98304]`。 |
    | dynamic_bucket_counts | list[int] | `[]` | 否 | 长度等于桶数；每项 >= 0 | 各桶实例数量或比例权重。总和等于实例总数时按精确数量分配，否则按比例分配；全为 `0` 或省略时均分，余数优先给靠后的桶。同一实例的全部 endpoint 跟随该实例所在桶。 |

   旧双桶标量字段（`dynamic_bucket_border`、`dynamic_bucket_short_median`、`dynamic_bucket_long_median`、`dynamic_bucket_length_scale`、`dynamic_bucket_load_scale`、`dynamic_bucket_short_bucket_count`、`dynamic_bucket_long_bucket_count`）**已废弃**，存量配置仍可加载：Coordinator 读取时迁移为上表列表字段并打印 warning。新旧混用时以列表字段为准，旧字段被忽略。对照关系见[配置参考](../../configuration/config_reference.md#scheduler_config-动态分桶旧字段兼容)。

   多桶示例：将实例划入 `0-32K`、`32K-64K`、`64K-128K`、`128K+` 四个桶，8 个实例时每桶 2 个。
   示例省略 `dynamic_bucket_medians`，由配置加载自动推导；如需控制各桶长度因子的中点，可显式配置 4 个特征长度。

   ```json
   {
     "motor_coordinator_config": {
       "scheduler_config": {
         "prefill_scheduler_type": "load_balance",
         "decode_scheduler_type": "dynamic_bucket",
         "dynamic_bucket_boundaries": [32768, 65536, 131072],
         "dynamic_bucket_counts": [2, 2, 2, 2],
         "dynamic_bucket_length_scales": [1.0, 1.0, 1.0, 1.0],
         "dynamic_bucket_load_scales": [4.0, 4.0, 4.0, 4.0]
       }
     },
     "motor_engine_prefill_config": {
       "engine_config": {
         "model": "/mnt/weight/your-model"
       }
     }
   }
   ```

2. 部署服务。

   部署要求：

   - Coordinator 需要能够访问 `engine_config.model` 指向的模型目录，以加载与引擎一致的 tokenizer。
   - 部署时应将模型目录挂载到 Coordinator，并确保所有可能运行 Coordinator 的节点均可读取该目录。
   - 无需启用 `kv-events-config` 或部署 KV Conductor。

   K8s 部署示例：

   ```bash
   cd examples/deployer

   # PD 分离
   python deploy.py --config_dir ../../infer_engines/vllm
   ```

   若命令提示安全预检未通过，请参见[部署前安全预检](../../deployment/k8s/README.md#deployment-security-precheck)完成安全配置。仅在隔离测试环境需要沿用现有配置时，可在原命令末尾追加 `--skip-check`。

### 验证特性

可通过Coordinator输出的动态分桶选择日志核对请求长度、目标桶、所选endpoint和候选负载；请求无法获得有效token数时会回退到负载均衡。

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

```output
Dynamic bucket selected req_length=... target_bucket=... selected=...-... loads=...
```

## 调优建议

建议先使用默认桶边界和缩放参数，只按集群容量设置桶数量；获得真实请求长度分布和桶负载数据后，再调整
`dynamic_bucket_boundaries`、`dynamic_bucket_medians`、`dynamic_bucket_counts`、
`dynamic_bucket_length_scales` 和 `dynamic_bucket_load_scales`。

长短隔离的粒度是该角色的**实例数**，不是卡数或 DP rank 数。要让短/中/长桶同时非空，对应角色的实例数必须不少于桶数；
单实例多 endpoint（例如 1P1D、Decode DP16）时，该实例的全部 endpoint 进入同一桶，`dynamic_bucket_counts` 无法把同一
MoE/DP 组拆到不同桶。

多桶布局建议从业务请求长度分布的 2～4 个主要分位区间开始，先配置 `dynamic_bucket_boundaries` 和
`dynamic_bucket_counts`，让每个桶至少有 1 个实例；运行后观察各桶负载和回退日志，再决定是否增加桶数、
调整边界或显式配置 `dynamic_bucket_medians`。桶数越多，每个桶可用的实例越少，跨桶负载均衡能力越弱，
应结合集群规模和目标并发验证。

以下为 Atlas 800I A2、DeepSeek-V4-Flash、3P1D 上对同一数据集的三轮闭环对照，仅代表该模型、拓扑和数据分布下的调优结论，不作为其他环境的通用默认值。
两桶一轮按当时的 **endpoint** 粒度切分；三桶一轮按当前 **实例** 粒度切分（3 个 Prefill 实例各占一桶）。不要把两桶的 endpoint 计数抄到当前实现上。

### 数据集长度分布

对照所用为自建长短混合负载：3 条 prefix warmup + 2000 条业务请求，请求带共享前缀。直方图与分位数均按业务请求的 `prompt_tokens`（不含 warmup）。
客户端闭环循环读取该负载直至墙钟结束。

| 统计 | prompt_tokens | uncached_prompt_tokens |
|------|--------------:|-----------------------:|
| 条数 | 2000 | 2000 |
| min | 81 | 28 |
| p50 | 44847 | 2889 |
| p90 | 122410 | 38541 |
| p99 | 196691 | 139049 |
| max | 199592 | 196691 |
| 均值 | 51731 | 12704 |
| 合计 | 1.035×10⁸ | 2.541×10⁷ |

按三桶边界 `[32768, 98304]` 划分的请求数与 token 质量：

| 区间 | 条数 | 条数占比 | token 合计 | token 占比 |
|------|-----:|---------:|-----------:|-----------:|
| `(0, 32768]` | 856 | 42.80% | 6.19×10⁶ | 5.98% |
| `(32768, 98304]` | 795 | 39.75% | 5.13×10⁷ | 49.54% |
| `(98304, +∞)` | 349 | 17.45% | 4.60×10⁷ | 44.48% |

短请求条数不少，但 token 质量集中在中长桶：若把短桶上界收到 4096，短桶只占 **0.59%** 的 prompt token（563 条 / 28.15% 请求），其余几乎全部涌进长桶。

`prompt_tokens` 直方图（业务 2000 条；`#` 个数按该区间条数相对最大柱 407 条线性缩放）：

```text
区间                条数    条数占比   token占比
(0, 1024]            356    17.80%     0.17%  #####################
(1024, 2048]         113     5.65%     0.16%  #######
(2048, 4096]          94     4.70%     0.26%  ######
(4096, 8192]          32     1.60%     0.20%  ##
(8192, 16384]         74     3.70%     0.90%  ####
(16384, 32768]       187     9.35%     4.30%  ###########
(32768, 49152]       199     9.95%     7.88%  ############
(49152, 65536]       189     9.45%    10.46%  ###########
(65536, 98304]       407    20.35%    31.20%  ########################
(98304, 131072]      180     9.00%    19.47%  ###########
(131072, 196608]     140     7.00%    19.49%  ########
(196608, +∞)          29     1.45%     5.52%  ##
```

### 实验环境

- 硬件：Atlas 800I A2，4 节点。
- 模型：DeepSeek-V4-Flash-W4A8。
- 拓扑：3P1D；每个 Prefill / Decode 实例 1 个 Pod、8 张 NPU（Decode 为 DP8 / TP1）。
- 负载：上述自建长短混合请求（见上表）。
- 客户端：闭环 `concurrency=8`，`duration=7200` 秒，`max_tokens` 上限 64，接口 `/v1/completions`。
- TTFT：客户端收到第一条 SSE `data:` 的时间，单位秒。分位数与压测脚本一致：`sorted[round(p/100*(N-1))]`，不插值。
- Prefill token/s：成功请求的 `prompt_tokens` 之和除以墙钟秒数（输入侧吞吐，不是 Decode 输出吞吐）。

| 轮次 | 调度 | 分桶粒度 | 墙钟（秒） | 成功请求 |
|------|------|----------|----------:|---------:|
| 负载均衡 | Prefill / Decode 均为 `load_balance` | — | 7225 | 5772 |
| 两桶 | Prefill `dynamic_bucket`，Decode `load_balance` | endpoint（旧实现） | 7214 | 5063 |
| 三桶 | Prefill `dynamic_bucket`，Decode `load_balance` | 实例（当前实现） | 7212 | 8955 |

三桶墙钟取结果目录创建到 `records.jsonl` 落盘的间隔（约 2 小时，与客户端 7200 秒窗口一致）。

### 两桶（endpoint）配置

旧双桶标量；1 个 Prefill endpoint 进短桶、2 个进长桶。`4096` 是短桶 **median**，不是边界。边界为 48K，因此 ≤4K 只是短桶里更靠前的一段。

```json
{
  "prefill_scheduler_type": "dynamic_bucket",
  "decode_scheduler_type": "load_balance",
  "dynamic_bucket_border": 49152,
  "dynamic_bucket_short_median": 4096,
  "dynamic_bucket_long_median": 51200,
  "dynamic_bucket_length_scale": 2.0,
  "dynamic_bucket_load_scale": 4.0,
  "dynamic_bucket_short_bucket_count": 1,
  "dynamic_bucket_long_bucket_count": 2
}
```

### 三桶（实例）配置

3 个 Prefill 实例按 `counts=[1, 1, 1]` 各进一桶。同一实例的全部 endpoint 跟随该实例。

```json
{
  "prefill_scheduler_type": "dynamic_bucket",
  "decode_scheduler_type": "load_balance",
  "dynamic_bucket_boundaries": [32768, 98304],
  "dynamic_bucket_medians": [16384, 65536, 147456],
  "dynamic_bucket_counts": [1, 1, 1],
  "dynamic_bucket_length_scales": [2.0, 1.0, 1.0],
  "dynamic_bucket_load_scales": [2.0, 4.0, 4.0]
}
```

### 总体结果

相对变化相对负载均衡：吞吐 / QPS 按 `新 / LB − 1`，时延按 `1 − 新 / LB`（正值表示变快）。

| 指标 | 负载均衡 | 两桶 endpoint | 相对 LB | 三桶实例 | 相对 LB |
|------|--------:|-------------:|--------:|--------:|--------:|
| QPS | 0.799 | 0.702 | 降低 12.1% | 1.242 | **提升 55.4%** |
| Prefill token/s | 41446 | 36584 | 降低 11.7% | 64605 | **提升 55.9%** |
| TTFT mean（s） | 8.277 | 9.672 | 升高 16.9% | 5.401 | **降低 34.7%** |
| TTFT p50（s） | 5.188 | 5.297 | 升高 2.1% | 3.515 | **降低 32.2%** |
| TTFT p90（s） | 19.249 | 21.779 | 升高 13.1% | 11.873 | **降低 38.3%** |
| TTFT p99（s） | 41.958 | 53.541 | 升高 27.6% | 25.869 | **降低 38.3%** |
| e2e p50（s） | 6.957 | 7.085 | 升高 1.8% | 4.554 | **降低 34.5%** |

### 按输入长度的 TTFT

分组按请求 `prompt_tokens`。`≤4K` 对应两桶短请求隔离目标；其余三行对齐三桶边界。

| 长度 | 负载均衡 mean / p50 / p90 | 两桶 endpoint mean / p50 / p90 | 三桶实例 mean / p50 / p90 |
|------|---------------------------|--------------------------------|---------------------------|
| 全部 | 8.277 / 5.188 / 19.249 | 9.672 / 5.297 / 21.779 | 5.401 / 3.515 / 11.873 |
| `≤4096` | 5.136 / 2.629 / 12.744 | 3.229 / 1.779 / 7.345 | 3.120 / 2.004 / 7.288 |
| `(0, 32768]` | 5.686 / 3.353 / 13.219 | 4.093 / 2.818 / 8.494 | **3.668 / 2.643 / 8.166** |
| `(32768, 98304]` | 8.712 / 5.975 / 18.170 | 10.968 / 6.954 / 22.447 | **5.110 / 3.695 / 10.408** |
| `(98304, +∞)` | 13.631 / 8.233 / 30.107 | 20.231 / 13.037 / 37.165 | **10.257 / 6.529 / 22.344** |

### 读数

- **两桶、短 median=4096、endpoint 切分**：`≤4K` 的 TTFT mean / p50 / p90 相对负载均衡分别降低 37.1% / 32.3% / 42.4%，短请求隔离有效。该轮初选边界为 48K；按输入长度初选，短桶覆盖 1055/2000 条请求（52.75%），约占输入 token 的 13.9%。
  约 0.59% 仅对应 `≤4K` 子集，不是整个短桶占比。中长组 TTFT 和整体 QPS 都差于负载均衡
  （全部 mean 升高 16.9%，QPS 降低 12.1%）；缺少实际落桶统计，不能据初选分布推断负载切换后的桶负载或吞吐下降原因。
- **三桶、边界 32K / 96K、实例切分**：三个 Prefill 实例按长度分到短 / 中 / 长桶后，本轮 **各长度组** 的 TTFT mean 和 p90 都低于负载均衡，QPS 与 Prefill token/s 约提升 55%。`≤4K` 的 p50（2.004 s）略高于两桶（1.779 s），因为短桶上界放到了 32K，极短请求仍与最长 32K 的 Prefill 共桶。
- 三轮顺序执行，镜像与分桶粒度不同（两桶为旧 endpoint 实现，三桶为当前实例实现），前缀缓存初态也不保证相同，不能把表中差值全部归因于调度策略。换模型、并发或 P 实例数后需要重测。

当前实现下要复现三桶隔离，Prefill 实例数必须不少于 3；1P 多 DP 不会把同一实例拆进多个桶。两桶那组的 `short_bucket_count=1 / long_bucket_count=2` 是 endpoint 计数，不能当作现在的 `dynamic_bucket_counts`。
