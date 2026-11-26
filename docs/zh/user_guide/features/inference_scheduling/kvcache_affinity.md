# KV Cache亲和性调度

## 特性介绍

KV Cache 亲和性调度结合缓存前缀命中和当前负载，为请求选择推理 endpoint。KV Conductor 维护缓存索引，Coordinator 使用索引结果估计剩余 prefill 工作量并选择推理 endpoint。收益主要来自复用已有 KV、减少重复 prefill 计算；具体吞吐和时延收益取决于请求分布、缓存介质和负载。

- **KV Conductor**：订阅事件，维护 HBM 前缀树及 CPU/Disk 连续边索引，返回各 DP 的互斥命中块数。
- **Coordinator**：获取请求 token IDs，对命中块按介质加权，结合当前负载计算调度分数。
- **引擎及 KV Cache Store**：负责实际 KV 复用和数据传输。KV Conductor 只维护索引，不搬运 KV 数据。

亲和调度适用于 PD 分离的 Prefill（P）角色和 PD 混部的 Union（U）角色。
通过 `prefill_scheduler_type` 为 P/U 开启 `kv_cache_affinity`；Decode（D）通过
`decode_scheduler_type` 选择负载均衡或轮询。两个配置项默认均为 `load_balance`。
未显式配置 `render_config.enable` 时，vLLM 部署开启 `kv_cache_affinity` 会同时默认启用 Render，
以复用一致的 prompt token ID；如需关闭，可显式设置 `render_config.enable=false`。

| 子策略 | 初次选点行为 |
|--------|--------------|
| `unified`（默认） | 对所有可用候选融合加权缓存收益与负载，选择综合成本最低的endpoint。 |
| `load_gated` | 先保留负载最低的 N 个 endpoint，再按加权前缀匹配长度择优，并列时优先低负载。 |

两种策略均支持 `hit_rate_threshold`，配置方法见[参数说明](#note001)。调度评分、回退及并发分配的实现原理见 [KV Conductor 设计](../../../design/kv_conductor.md#coordinator-调度集成)。

## 特性使用

### 环境准备

- 先完成[PD分离部署](../../deployment/README.md#k8s-pd-disaggregation)或[PD混部部署](../../deployment/k8s/pd_aggregation_deployment.md)，确认基础推理正常。不部署 Controller / Node Manager 时，使用 [Coordinator独立部署](../../deployment/standalone.md)。
- 本文配置示例面向 vLLM；SGLang 的基线及缓存开关见[PD分离服务部署（基于SGLang推理引擎）](../../deployment/README.md#k8s-pd-disaggregation-sglang)。
- 引擎需要发布可用的 KV 事件，并具备对应的缓存复用能力。P/U 的事件端口需从 KV Conductor 所在网络可达。
- Coordinator 必须能得到与引擎一致的 token IDs。默认从引擎配置推导 `model_path`，本地 tokenizer 需要可访问对应模型目录；Render 产生的 token IDs 可复用，配置见 [Coordinator组件说明](../../../developer_guide/components/coordinator.md)。Tokenizer、chat template、工具调用模板不一致都会影响前缀匹配。
- 镜像中须能启动 KV Conductor，见下方组件检查方法。

### 使用样例

<a id="构建"></a>

1. 在实际运行镜像内确认 KV Conductor 组件是否可用。

    ```bash
    python -m motor.kv_conductor --help
    ```

    正常输出启动参数说明即可。若组件缺失，按 [KV Conductor 构建与安装](../../../../../motor/kv_conductor/README.md#快速开始)准备包含该组件的镜像。

2. 配置user_config.json文件。

     - 以下配置参数是合并到已有配置的增量，不能作为完整部署配置直接使用。
     - `block_size: 128` 假设主注意力组事件粒度为 128；混合 KV 模型需按[实际事件粒度](#deepseek-v4)修改。
     - 下面的 PD 分离、PD 混部示例开启 **HBM 亲和**；CPU/Disk 需在此基础上继续配置[池化亲和配置](#pool-affinity)。

    在已有 `user_config.json` 中逐字段合并增量，保留模型、镜像、资源及基础传输配置。

    **PD 分离配置**

    基线文件：[`model_configs/vllm/user_config.json`](../../../../../model_configs/vllm/user_config.json)。

    ```json
    {
      "motor_coordinator_config": {
        "scheduler_config": {
          "prefill_scheduler_type": "kv_cache_affinity"
        }
      },
      "motor_engine_prefill_config": {
        "engine_config": {
          "kv-events-config": {
            "publisher": "zmq",
            "enable_kv_cache_events": true,
            "endpoint": "tcp://*:5557",
            "topic": "kv-events"
          }
        }
      },
      "kv_conductor_config": {
        "block_size": 128,
        "http_server_port": 13333
      }
    }
    ```

    `kv_transfer_config` 属于 PD 分离基础传输配置，沿用已有部署设置。

    **PD混部配置**

    基线文件：[`model_configs/vllm/pd_hybrid/user_config.json`](../../../../../model_configs/vllm/pd_hybrid/user_config.json)。将事件配置放在 `motor_engine_union_config`。

    ```json
    {
      "motor_coordinator_config": {
        "scheduler_config": {
          "prefill_scheduler_type": "kv_cache_affinity"
        }
      },
      "motor_engine_union_config": {
        "engine_config": {
          "kv-events-config": {
            "publisher": "zmq",
            "enable_kv_cache_events": true,
            "endpoint": "tcp://*:5557",
            "topic": "kv-events"
          }
        }
      },
      "kv_conductor_config": {
        "block_size": 128,
        "http_server_port": 13333
      }
    }
    ```

    <a id="note001"></a>
    **参数说明**

    以下以本文的 **K8s Deployer + vLLM** 部署为例，配置合并到已有 `user_config.json`。`scheduler_config` 位于 `motor_coordinator_config` 下，`kv_conductor_config` 写在顶层。

    使用 LoRA 或 `cache_salt` 身份隔离时，在同一个顶层对象中加入以下字段：

    ```json
    {
      "kv_conductor_config": {
        "http_server_port": 13333,
        "identity_mode": "v1"
      }
    }
    ```

    `identity_mode` 可选 `legacy` 或 `v1`，未填写时保持 `legacy`。身份隔离不依赖 group 编号，
    不需要配置主组，也不自动选组。Coordinator 与 Conductor 应使用同一份配置；切换模式后重新启动
    Conductor，以重建内存索引。`v1` 查询只有收到 `X-KV-Identity-Protocol: 1` 才采纳缓存结果。

    本版本支持已核对的普通文本事件；HBM 仍校验受支持的注意力类型
    （FullAttention、MlaAttention、SinkFullAttention）、身份信息与对应 worker 注册一致的事件 block_size。
    注册和查询的 block_size 一致性由既有部署与调用方保证，身份模式不建立全局布局绑定。
    CPU offload 不携带 group 时，仅适用于已确认的单注意力组模型。
    多组/混合注意力的这类 CPU 事件、缺少完整 tokens 或完整 engine hash 的格式不在支持范围。
    多实例继续沿用现有索引对一致 engine hash 的要求：所有引擎使用相同哈希算法，并在启动环境中
    显式设置相同的 `PYTHONHASHSEED`（例如 `0`）；Motor 继承环境，但不会自动设置此值。
    LoRA 名称必须与实际加载名称一致，同名适配器须对应相同权重；更新权重时使用新名称。

    K8s 的独立 Deployment、InferServiceSet、RoleBasedGroup（RBG）及 Slurm 部署会将身份模式传入
    `KV_CONDUCTOR_IDENTITY_MODE`。启动脚本仅在启用 `v1` 时追加 `--identity-mode v1`；
    默认或显式配置 `legacy` 时沿用原启动参数。直接启动 `v1` 时使用：

    ```bash
    python -m motor.kv_conductor --identity-mode v1
    ```

    **表 1** 必须配置或核对的参数

    | 参数 | 说明 |
    |--------|----------|
    | scheduler_config.prefill_scheduler_type | 显式设为 `kv_cache_affinity`，为 P/U 开启亲和调度。 |
    | P/U 的 engine_config.kv-events-config | 按示例设置 `publisher: zmq`、`enable_kv_cache_events: true` 和事件地址；PD 分离配置在 Prefill 段，PD 混部配置在 Union 段。 |
    | kv_conductor_config.http_server_port | 顶层显式填写，例如 `13333`；Deployer 依靠此项启用 KV Conductor，不能只依赖运行时默认值。 |
    | kv_conductor_config.block_size | 必须核对主注意力组实际事件粒度，建议像示例一样显式填写。未填写时会从引擎 `block-size` 推导，缺省为 `128`；混合 KV 模型按[实际事件粒度](#deepseek-v4)覆盖。 |

    >[!NOTE] 说明
    >事件端口需从 KV Conductor 所在网络可达；多 DP 时需核对基础事件端口加 DP 秩后的各端口。

    **表 2** 无需额外配置的参数

    | 参数 | 说明 |
    |--------|----------|
    | scheduler_config.kv_affinity | 可省略整个对象，先使用默认 `unified` 策略与权重。<br>默认 `hit_rate_threshold=0`，不设置额外命中率门槛，`w_disk=0` 不计 Disk 亲和收益；命中验证通过后再按[调优建议](#调优建议)调整。 |
    | scheduler_config.decode_scheduler_type<br>kv_conductor_config.query_encoding | 新配置可分别沿用默认 `load_balance`、`msgpack`。 |
    | conductor_service<br>model_path<br>KV Conductor 侧 endpoint | 本文部署流程会注入服务地址，并从 P/U 引擎配置读取模型路径和事件地址，通常无需重复填写；Coordinator 仍须能访问该模型目录。 |

    **表 3** 特定场景再配置

    | 场景 | 说明 |
    |------|----------|
    | 独立部署或外接 KV Conductor | 配置可访问的 `conductor_service` 与实际 HTTP 端口；无法从引擎配置读取时，补充 `model_path` 和 `endpoint`。 |
    | 使用 SGLang | 将 `kv_conductor_config.engine_type` 设为 `sglang`，并按 [PD分离服务部署（基于SGLang推理引擎）](../../deployment/README.md#k8s-pd-disaggregation-sglang)配置引擎。 |
    | 使用 CPU/Disk 池化 | 按[池化亲和配置](#pool-affinity)接通两侧事件，并配置对应后端的订阅地址。 |
    | 需要重注册或历史事件回放 | 周期补注册设置正数 `re_register_interval_sec`（默认关闭）；历史回放配置引擎 `kv-events-config.replay_endpoint`。启用前确认[恢复能力边界](../../../design/kv_conductor.md#registration-lifecycle)。 |
    | 对接仅支持 JSON 的旧 KV Conductor | 将 `kv_conductor_config.query_encoding` 设为 `json`，见[查询异常排查](#coordinator无法查询kv-conductor)。 |

    完整参数定义见[配置参考](../../configuration/config_reference.md#motor_coordinator_config)，自动推导及旧配置迁移见[自动推导与兼容](../../configuration/config_reference.md#kv_conductor_config-自动推导与兼容)。

    <a id="model-block-size"></a>
    **表 4** 常见模型的 `kv_conductor_config.block_size` 参考

    >[!NOTE] 说明
    >Conductor 只对齐**主注意力组**（`mla_attention` / `full_attention`）的 `BlockStored.block_size`。表中值为 Motor 典配（`model_configs/vllm/models/`）在对应 vLLM-Ascend 版本下的推导结果；升级引擎、修改 DCP、开启 `--kv-cache-dtype mxfp8` 或切换混合 KV 对齐方式后，必须按表下规则重新核对。

    | 模型 / 部署形态 | 引擎 `--block-size` | 推荐 `block_size` | 说明 |
    |------|------|------|------|
    | DeepSeek-V3.1 | 128（未显式配置时） | 128 | 标准 MLA，主注意力组即 128。 |
    | DeepSeek-V4-Flash（A2/A3/A5 典配） | 32 | 128 | 主注意力组为 C4：`4 × 32 = 128`；C128 组事件粒度为 `128 × 32`，与注册值不一致会被丢弃，属预期。 |
    | DeepSeek-V4-Pro A3 | 128 | 512 | 主注意力组为 C4：`4 × 128 = 512`。 |
    | DeepSeek-V4-Pro A5 | 32 | 128 | 同 V4-Flash。 |
    | DeepSeek-V4-Pro A2 | 未固定（典配 Prefill 未显式配置，按镜像 vLLM 默认） | 先固定引擎值，再按 `4 ×` 配置 | 按当前 vLLM 默认 64 推得 256；不同镜像默认值可能不同，建议先在引擎显式固定 `--block-size`。 |
    | DeepSeek-V4.1-Flash（A2/A3/A5） | 128 | 128 | spec 直接使用 `cache_config.block_size`，`tokens_per_state` 不参与放大。 |
    | GLM-5 / 5.1 / 5.2 / 5.3 | 128（未显式配置时） | 128 | 标准 MLA。 |
    | GLM-5.3-Flash（GLM-Next 混合稀疏+线性） | 由 hybrid 对齐决定 | 按引擎实际事件粒度 | 混合 attention 会把 block size 抬高，不能沿用 128；以引擎日志 `Setting attention block size to N tokens` 和 trace 为准。 |
    | Qwen3.8-Flash-Next（GDN+QSA 混合） | 由 hybrid 对齐决定 | 按引擎实际事件粒度 | 典配启用 `mamba-cache-mode=align`；官方说明 block size 会被放大，不能沿用 128。 |
    | Qwen3-235B | 128（未显式配置时） | 128 | 标准 GQA。 |
    | 其他混合 KV / 新模型 | 按引擎 | 按 trace 或引擎日志确认 | 规则见下。 |

    **换算与核对规则**

    - Conductor 对齐的是事件字段，不是 `hash_block_size`：`hash_block_size` 取 `prefix_match_unit` 或「prefix-cacheable group 的 gcd」；`BlockStored.block_size` 取「主注意力组 block size × DCP」。常规配置下两者相等，配置 `prefix_match_unit` 或存在更细粒度 group 时可能不同，此时以事件字段为准。
    - DeepSeek-V4（非 V4.1）主组为 C4，`block_size = 4 × 引擎 --block-size`；C128 组事件为 `128 × 引擎 --block-size`（主组的 32 倍），会因 `block_size_mismatch` 被丢弃，不影响主组索引。
    - DeepSeek-V4.1 的 `tokens_per_state` 只改变物理页压缩，不改变事件 token 粒度。
    - 仅 DCP（`--decode-context-parallel-size N`）放大：所有介质与事件粒度按 `N` 倍放大；PCP（`--prefill-context-parallel-size`）不参与。
    - A5 开启 `--kv-cache-dtype mxfp8` 时，`refresh_block_size` 会把块大小对齐到 512 的倍数（未配置时默认 512），Conductor 同步配置。
    - 混合 attention + linear/Mamba 模型（GLM-5.3-Flash、Qwen3.8-Flash-Next 等）的有效块大小由 hybrid cache manager 对 mamba 页对齐后决定，可能远大于 `--block-size`；不要直接沿用 128。
    - 未配置 `kv_conductor_config.block_size` 时，Motor 从引擎 `block-size` 推导（缺省 128），对混合模型可能不正确，建议显式配置。

    **确认方法**：在引擎日志中检索 `Setting attention block size to`、`kv cache group sizes`、`kv lcm block sizes`；在 Conductor 侧短时设置 `RUST_LOG=info,kv_conductor=trace`，确认 `event_parsed` 中主注意力组的 `block_size` 与 `spec_kind`，并确认没有持续的 `block_size_mismatch`。
3. 部署服务。

    ```bash
    cd examples/deployer
    # PD 分离
    python deploy.py --config_dir ../../model_configs/vllm

    # PD 混部使用其独立配置目录
    python deploy.py --config_dir ../../model_configs/vllm/pd_hybrid
    ```

    部署后按[验证特性](#验证特性)检查注册、缓存命中和实际选点。

### 验证特性

以下命令在能访问服务的环境执行，替换命名空间和地址为实际值：

```bash
NAMESPACE=mindie-motor
CONDUCTOR_URL=http://kv-conductor:13333
kubectl -n "$NAMESPACE" get pods,services -o wide
curl --fail "$CONDUCTOR_URL/health"
curl --fail "$CONDUCTOR_URL/workers"
```

按下面顺序验收：

1. **注册**：`/workers` 中应包含每个目标 P/U DP；池化场景还应有相应 pool。Coordinator 实际日志包括 `HBM DP registered`、`Pool registered`、`YuanRong node pool registered`；KV Conductor 日志为 `worker registered`。
2. **事件**：发送推理请求后检查事件接收及解析，确认没有持续的 `block_size_mismatch`、订阅连接或解析错误。CPU/Disk 命中还需要引擎 offload 与后端存储确认两阶段事件。
3. **命中**：重复发送同一模型、同一模板且前缀超过一个 block 的请求，等待缓存事件可见后，检查 `conductor query response` 中各 DP 的覆盖。不要用不足一个 block 的请求验收命中。
4. **最终选点**：关联请求的 `scheduled` 日志，检查 `policy`、`matched`、`hbm/cpu/disk`、`instance/endpoint` 和 `repicked`。`policy=load_balance` 时结合门槛及回退原因解释结果。
5. **性能与恢复**：命中检查通过后，再用相同负载对比吞吐及 TTFT/尾延迟；需要恢复能力时，在测试环境单独验证 KV Conductor 重启后的各 DP / pool 注册和命中恢复。

<a id="pool-affinity"></a>

### 进阶配置：池化亲和（CPU/Disk）

先按所选后端的 [KV池化部署指导](kv_cache_store/README.md)完成存储池和 Store Connector 配置，确认 KV 能写入、读取，再叠加本节配置。开启 `kv_cache_store_config` 后，还需同时接通**引擎 offload 事件**和**存储后端确认事件**，KV Conductor 才能建立 CPU/Disk 索引。保留上面的 HBM 事件配置和亲和调度开关。

#### MemCache：在 vLLM PD 分离部署上开启 CPU 亲和

1. 按 [MemCache 的 KV events 配置](kv_cache_store/memcache.md#kv-events-affinity)开启 MetaService 广播。当前启动脚本中的 `kv_events_enable` 等配置默认被注释，需要按该指导启用，设置事件端口、模型标识和块粒度，并重启 kv_store。所用 MemCache 版本须支持这些事件配置项。
2. 检查引擎能发布 Store Connector 的 offload 事件。使用 MultiConnector 时，核对所用版本是否已支持子 Connector 事件转发；缺失时按上述 MemCache 指导应用对应补丁。
3. 在已有 `user_config.json` 顶层 `kv_conductor_config` 中合并以下增量：

    ```json
    {
      "kv_conductor_config": {
        "store_backend": "Memcache",
        "pool_endpoint": "tcp://*:5557"
      }
    }
    ```

    `5557` 是示例广播端口，必须与 MetaService 的 `kv_events_endpoint` 和 Service 暴露端口一致。这里的 `*` 由 K8s 注入的 `KVS_MASTER_SERVICE` 替换；外接存储池时填写 KV Conductor 可访问的实际域名或 IP。`pool_endpoint` 填事件广播地址，存储服务的读写端口不能用于此项。
4. 核对 MetaService 的 `kv_events_model_name` / `kv_events_block_size` 与 pool 注册使用的 `kv_conductor_config.model_path` / `block_size` 一致，同时确认 pool 与目标 P/U 注册的模型标识一致。后端事件携带的节点 IP 需能关联到目标 P/U 节点；MemCache 的 `backend_id` 按后端部署指导自动注入。
5. 按[使用样例](#使用样例)应用Motor配置，再按下方[池化验收](#pool-affinity-validation)检查CPU的默认权重 `w_cpu=1.0`，无需额外配置评分开关。

#### Mooncake / YuanRong：已有事件源时的 Motor 配置

以下列出 Motor 的接入配置；应用前须确认后端和引擎已提供与 KV Conductor 兼容的两侧事件。各后端事件发布服务的安装、启动方式以实际使用版本为准。

| 后端 | 合并到顶层 `kv_conductor_config` 的配置 | 部署前需确认 |
|------|-----------------------------------------|--------------|
| Mooncake | `store_backend: "Mooncake"`；`pool_endpoint` 填中心 pool 的实际 ZMQ 事件广播地址 | [Mooncake 部署文档](kv_cache_store/mooncake.md)当前主要覆盖存储服务，尚未给出完整的事件发布开启步骤；需先确认事件发布能力、地址及模型/块粒度 |
| YuanRong | `store_backend: "YuanRong"`；`cpu_endpoint` / `disk_endpoint` 分别填 `tcp://*:<对应介质事件端口>` | 节点级事件地址使用节点 IP 和基础端口，不加 DP 秩。显式配置实际事件地址，避免回退到 HBM 端口；[后端部署文档](kv_cache_store/yuanrong.md)仍待补全 |

以上配置用于订阅已有事件源，不会替后端开启广播。接入及事件格式的详细约定见[KV Conductor 设计](../../../design/kv_conductor.md#后端适配抽象)。

#### Disk 参与评分时的可选配置

只有实际启用 Disk 存储、具备 Disk 事件并验证查询命中后，才需要调整 `w_disk`。默认 `w_disk=0`，Disk 命中不计入亲和评分；需要计入时，在 `motor_coordinator_config.scheduler_config` 下合并，例如：

```json
{
  "motor_coordinator_config": {
    "scheduler_config": {
      "kv_affinity": {
        "w_disk": 1.0
      }
    }
  }
}
```

`1.0` 仅用于演示启用 Disk 评分，实际值需结合读取成本和性能验证调整。修改权重不会开启 Disk 存储或补齐缺失的事件。

<a id="pool-affinity-validation"></a>

#### 池化验收

1. 按[验证特性](#验证特性)检查 `/workers`：除 P/U HBM DP 外，MemCache/Mooncake 还应有对应中心 pool，YuanRong 应有节点池。注册成功后继续确认引擎 offload 与后端存储确认事件均已到达。
2. 构造 **HBM 未覆盖、但 CPU 或 Disk 仍保留目标前缀**的请求，在 `conductor query response` 中检查目标 DP 的 `cpu_blocks` / `disk_blocks`。返回覆盖按 HBM → CPU → Disk 互斥计算，完全由 HBM 命中时，CPU/Disk 为零不能用于判定池化失败。
3. 关联最终 `scheduled` 日志，检查实际 `policy`、`matched`、`hbm/cpu/disk` 和选中的 endpoint。结合介质权重、命中率门槛及负载判断结果；索引命中仍需通过引擎日志或指标确认实际 KV 复用。

## 调优建议

| 目标 | 调整方向与边界 |
|------|----------------|
| 起步配置 | 使用默认 `unified` 和默认权重，先确认索引、分词和注册正确。 |
| 增强负载影响 | 提高 `load_weight`，同时观察缓存复用、吞吐及尾延迟。 |
| 增强亲和偏好 | 降低 `load_weight`；0 使综合评分忽略负载，可能聚集热点，不能保证吞吐最优。 |
| 低命中时回退 | 设置正 `hit_rate_threshold`；按加权命中率选择门槛，并验证等于阈值的边界。 |
| 限制初选负载范围 | 使用 `load_gated` 和合适的 N，同时考虑其并发重选行为。 |
| 区分缓存介质 | 调整介质权重；默认 `w_disk=0`，Disk 覆盖仍会出现在原始查询结果中。 |

## 常见问题

<a id="deepseek-v4"></a>

### DeepSeek V4与混合KV Cache模型的block_size不匹配

**问题描述**

`kv_conductor_config.block_size` 与主注意力组事件粒度不一致时，收到的 `BlockStored` 事件会以 `block_size_mismatch` 原因被丢弃，导致缓存索引不完整或请求无法获得亲和命中。

**解决步骤**

1. 先查[表 4](#model-block-size)中是否已有对应模型的推荐值；表中未覆盖时，短时启用 KV Conductor 的 `RUST_LOG=info,kv_conductor=trace`，查看 `event_parsed` 日志中的 `block_size` 和 `spec_kind`。
2. 以主注意力组（如 `mla_attention` / `full_attention`）的事件粒度为准，确认实际 `block_size`；它不一定等于引擎 `--block-size`。混合 attention + linear/Mamba 模型还会被 hybrid cache manager 对齐放大，不能只按 `--block-size` 推算。
3. 按实际主组事件粒度修改配置。例如实际粒度为 512 时，在已有配置中合并以下增量；512 不是所有部署的固定值。

    ```json
    {
      "kv_conductor_config": {
        "block_size": 512
      }
    }
    ```

4. 按[使用样例](#使用样例)应用配置后，按[验证特性](#验证特性)检查事件接收和亲和命中。
5. DCP、PCP、`--kv-cache-dtype mxfp8` 或引擎版本变化后，重新核对事件粒度：DCP 与 mxfp8 会改变主组粒度，PCP 不改变；不能只依据引擎页大小自动推导。

### 服务正常但始终没有亲和命中

**问题描述**

基础推理请求能够完成，但重复发送长前缀请求后，仍没有观察到缓存命中或亲和调度。

**解决步骤**

1. 核对 `prefill_scheduler_type` 是否为 `kv_cache_affinity`、P/U 事件发布是否开启，以及 `kv_conductor_config.conductor_service` 是否指向可访问的 KV Conductor。
2. 检查 Coordinator 是否能加载模型目录中的 tokenizer、模板是否与引擎一致，并确认请求的实际 token 数不少于一个 `block_size`。
3. 在 `/workers` 中确认目标实例及 DP 已注册，核对事件端口、主注意力组事件粒度，以及查询中的模型标识与注册值是否一致。
4. 若 KV Conductor 已返回原始命中但实际 `policy=load_balance`，检查介质权重和 `hit_rate_threshold`。默认 `w_disk=0`，门槛判断要求加权命中率严格大于阈值。

若基础推理本身或 P/D 传输异常，先按对应的[vLLM PD 部署](../../deployment/README.md#k8s-pd-disaggregation)或[SGLang PD 部署](../../deployment/README.md#k8s-pd-disaggregation-sglang)指导排查。对于使用 `kv_transfer_config` 的 vLLM PD Connector，核对 `kv_role` 与 P/D 角色及 Connector 要求是否一致，并检查 `kv_port` 的分配和连通性。

### Coordinator无法查询KV Conductor

**问题描述**

Coordinator 日志出现 KV Conductor 连接或查询错误，请求回退到负载均衡。

**解决步骤**

1. 检查 `conductor_service`、`http_server_port` 和服务状态，确认 Coordinator 能访问 KV Conductor 的 `/health`。
2. 新 Coordinator 对接仅支持 JSON 的旧 KV Conductor 时，将 `kv_conductor_config.query_encoding` 设为 `json`。不要将该字段写在旧 `prefill_kv_event_config` 下。
3. 若日志显示查询熔断，修复连接或查询错误后等待 30 秒冷却窗口结束，再检查后续查询是否恢复。

### HBM命中正常，CPU/Disk始终为零

**问题描述**

KV Conductor 查询已有 HBM 命中，但在期望使用 CPU/Disk 缓存的请求中，未观察到对应介质的命中。

**解决步骤**

1. 检查 `store_backend` 及 pool / 介质事件端口配置，确认相应服务已启动并完成注册。
2. 确认引擎 offload 事件和后端存储确认事件均已到达 KV Conductor。
3. YuanRong CPU/Disk 使用节点基础端口，不加 `dp_rank`；核对节点 IP、模型标识及目标 DP。
4. 若事件齐备仍无命中，按[跨介质匹配说明](../../../design/kv_conductor.md#匹配逻辑与查询流程)检查前缀是否连续、续查是否属于同一 `(instance_id, dp_rank)`。
