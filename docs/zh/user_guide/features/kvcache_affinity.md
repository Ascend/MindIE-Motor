# KV Cache 亲和性调度

## 特性介绍

KV Cache 亲和性调度结合缓存前缀命中和当前负载，为请求选择推理 endpoint。
kv-conductor 维护缓存索引，Coordinator 使用索引结果估计剩余 prefill 工作量并选择推理 endpoint。
收益主要来自复用已有 KV、减少重复 prefill 计算；具体吞吐和时延收益取决于请求分布、缓存介质和负载。

- **kv-conductor**：订阅事件，维护 HBM 前缀树及 CPU/Disk 连续边索引，返回各 DP 的互斥命中块数。
- **Coordinator**：获取请求 token IDs，对命中块按介质加权，结合当前负载计算调度分数。
- **引擎及 KV Cache Store**：负责实际 KV 复用和数据传输。Conductor 只维护索引，不搬运 KV 数据。

亲和调度适用于 PD 分离的 Prefill（P）角色和 PD 混部的 Union（U）角色。
通过 `prefill_scheduler_type` 为 P/U 开启 `kv_cache_affinity`；Decode（D）通过
`decode_scheduler_type` 选择负载均衡或轮询。两个配置项默认均为 `load_balance`。

| 子策略 | 初次选点行为 |
|--------|--------------|
| `unified`（默认） | 对所有可用候选融合加权缓存收益与负载，选择综合成本最低的endpoint。 |
| `load_gated` | 先保留负载最低的 N 个 endpoint，再按加权前缀匹配长度择优，并列时优先低负载。 |

两种策略均支持 `hit_rate_threshold`，配置方法见[参数说明](#note001)。
调度评分、回退及并发分配的实现原理见 [KV Conductor 设计](../../design/kv_conductor.md#coordinator-调度集成)。

## 特性使用

### 环境准备

- 先完成 [PD 分离部署](../deployment/k8s/pd_disaggregation_deployment.md)或
  [PD 混部部署](../deployment/k8s/pd_aggregation_deployment.md)，确认基础推理正常。
  不部署 Controller / Node Manager 时，使用 [Coordinator 独立部署](../deployment/standalone.md)。
- 本文配置示例面向 vLLM；SGLang 的基线及缓存开关见
  [SGLang PD 分离部署](../deployment/k8s/pd_disaggregation_sglang.md)。
- 引擎需要发布可用的 KV 事件，并具备对应的缓存复用能力。P/U 的事件端口需从 conductor 所在网络可达。
- Coordinator 必须能得到与引擎一致的 token IDs。默认从引擎配置推导 `model_path`，本地 tokenizer
  需要可访问对应模型目录；Render 产生的 token IDs 可复用，配置见
  [Coordinator 组件说明](../../developer_guide/components/coordinator.md)。
  Tokenizer、chat template、工具调用模板不一致都会影响前缀匹配。
- 镜像中须能启动 kv-conductor，见下方组件检查方法。

### 使用样例

<a id="构建"></a>

1. 在实际运行镜像内确认 kv-conductor 组件是否可用。

    ```bash
    python -m motor.kv_conductor --help
    ```

    正常输出启动参数说明即可。若组件缺失，按 [KV Conductor 构建与安装](../../../../motor/kv_conductor/README.md#快速开始)
    准备包含该组件的镜像。

2. 配置user_config.json文件。

     - 以下配置参数是合并到已有配置的增量，不能作为完整部署配置直接使用。
     - `block_size: 128` 假设主注意力组事件粒度为 128；混合 KV 模型需按[实际事件粒度](#deepseek-v4)修改。
     - 下面的 PD 分离、PD 混部示例开启 **HBM 亲和**；CPU/Disk 需在此基础上继续配置[池化亲和](#pool-affinity)。

    在已有 `user_config.json` 中逐字段合并增量，保留模型、镜像、资源及基础传输配置。

    **PD 分离配置**

    基线文件：[`examples/infer_engines/vllm/user_config.json`](../../../../examples/infer_engines/vllm/user_config.json)。

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

    `kv-transfer-config` 属于 PD 分离基础传输配置，沿用已有部署设置。

    **PD混部配置**

    基线文件：[`examples/infer_engines/vllm/pd_hybrid/user_config.json`](../../../../examples/infer_engines/vllm/pd_hybrid/user_config.json)。
    将事件配置放在 `motor_engine_union_config`。

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

    以下以本文的 **K8s Deployer + vLLM** 部署为例，配置合并到已有 `user_config.json`。
    `scheduler_config` 位于 `motor_coordinator_config` 下，`kv_conductor_config` 写在顶层。

    **表 1** 必须配置或核对的参数

    | 参数 | 说明 |
    |--------|----------|
    | scheduler_config.prefill_scheduler_type | 显式设为 `kv_cache_affinity`，为 P/U 开启亲和调度。 |
    | P/U 的 engine_config.kv-events-config | 按示例设置 `publisher: zmq`、`enable_kv_cache_events: true` 和事件地址；PD 分离配置在 Prefill 段，PD 混部配置在 Union 段。 |
    | kv_conductor_config.http_server_port | 顶层显式填写，例如 `13333`；Deployer 依靠此项启用 conductor，不能只依赖运行时默认值。 |
    | kv_conductor_config.block_size | 必须核对主注意力组实际事件粒度，建议像示例一样显式填写。未填写时会从引擎 `block-size` 推导，缺省为 `128`；混合 KV 模型按[实际事件粒度](#deepseek-v4)覆盖。 |

    >[!NOTE] 说明
    >事件端口需从 conductor 所在网络可达；多 DP 时需核对基础事件端口加 DP 秩后的各端口。

    **表 2** 无需额外配置的参数

    | 参数 | 说明 |
    |--------|----------|
    | scheduler_config.kv_affinity | 可省略整个对象，先使用默认 `unified` 策略与权重。<br>默认 `hit_rate_threshold=0`，不设置额外命中率门槛，`w_disk=0` 不计 Disk 亲和收益；命中验证通过后再按[调优建议](#调优建议)调整。 |
    | scheduler_config.decode_scheduler_type<br>kv_conductor_config.query_encoding | 新配置可分别沿用默认 `load_balance`、`msgpack`。 |
    | conductor_service<br>model_path<br>conductor 侧 endpoint | 本文部署流程会注入服务地址，并从 P/U 引擎配置读取模型路径和事件地址，通常无需重复填写；Coordinator 仍须能访问该模型目录。 |

    **表 3** 特定场景再配置

    | 场景 | 说明 |
    |------|----------|
    | 独立部署或外接 conductor | 配置可访问的 `conductor_service` 与实际 HTTP 端口；无法从引擎配置读取时，补充 `model_path` 和 `endpoint`。 |
    | 使用 SGLang | 将 `kv_conductor_config.engine_type` 设为 `sglang`，并按 [SGLang 部署指导](../deployment/k8s/pd_disaggregation_sglang.md)配置引擎。 |
    | 使用 CPU/Disk 池化 | 按[池化亲和配置](#pool-affinity)接通两侧事件，并配置对应后端的订阅地址。 |
    | 需要重注册或历史事件回放 | 周期补注册设置正数 `re_register_interval_sec`（默认关闭）；历史回放配置引擎 `kv-events-config.replay_endpoint`。启用前确认[恢复能力边界](../../design/kv_conductor.md#registration-lifecycle)。 |
    | 对接仅支持 JSON 的旧 conductor | 将 `kv_conductor_config.query_encoding` 设为 `json`，见[查询异常排查](#coordinator无法查询conductor)。 |

    完整参数定义见[配置参考](../configuration/config_reference.md#motor_coordinator_config)，自动推导及旧配置迁移见[自动推导与兼容](../configuration/config_reference.md#kv_conductor_config-自动推导与兼容)。
3. 部署服务。

    ```bash
    cd examples/deployer
    # PD 分离
    python deploy.py --config_dir ../infer_engines/vllm

    # PD 混部使用其独立配置目录
    python deploy.py --config_dir ../infer_engines/vllm/pd_hybrid
    ```

    部署后按[验证结果](#验证特性)检查注册、缓存命中和实际选点。

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

1. **注册**：`/workers` 中应包含每个目标 P/U DP；池化场景还应有相应 pool。
   Coordinator 实际日志包括 `HBM DP registered`、`Pool registered`、`YuanRong node pool registered`；
   conductor 日志为 `worker registered`。
2. **事件**：发送推理请求后检查事件接收及解析，确认没有持续的 `block_size_mismatch`、订阅连接或解析错误。
   CPU/Disk 命中还需要引擎 offload 与后端存储确认两阶段事件。
3. **命中**：重复发送同一模型、同一模板且前缀超过一个 block 的请求，等待缓存事件可见后，
   检查 `conductor query response` 中各 DP 的覆盖。不要用不足一个 block 的请求验收命中。
4. **最终选点**：关联请求的 `scheduled` 日志，检查 `policy`、`matched`、`hbm/cpu/disk`、
   `instance/endpoint` 和 `repicked`。`policy=load_balance` 时结合门槛及回退原因解释结果。
5. **性能与恢复**：命中检查通过后，再用相同负载对比吞吐及 TTFT/尾延迟；需要恢复能力时，
   在测试环境单独验证 conductor 重启后的各 DP / pool 注册和命中恢复。

<a id="pool-affinity"></a>

### 进阶配置：池化亲和（CPU/Disk）

先按所选后端的 [KV 池化部署指导](kv_cache_store/README.md)完成存储池和 Store Connector 配置，
确认 KV 能写入、读取，再叠加本节配置。开启 `kv_cache_store_config` 后，还需同时接通
**引擎 offload 事件**和**存储后端确认事件**，conductor 才能建立 CPU/Disk 索引。
保留上面的 HBM 事件配置和亲和调度开关。

#### MemCache：在 vLLM PD 分离部署上开启 CPU 亲和

1. 按 [MemCache 的 KV events 配置](kv_cache_store/backend/memcache.md#kv-events-affinity)
   开启 MetaService 广播。当前启动脚本中的 `kv_events_enable` 等配置默认被注释，需要按该指导启用，
   设置事件端口、模型标识和块粒度，并重启 kv_store。所用 MemCache 版本须支持这些事件配置项。
2. 检查引擎能发布 Store Connector 的 offload 事件。使用 MultiConnector 时，核对所用版本是否已支持
   子 Connector 事件转发；缺失时按上述 MemCache 指导应用对应补丁。
3. 在已有 `user_config.json` 顶层 `kv_conductor_config` 中合并以下增量：

    ```json
    {
      "kv_conductor_config": {
        "store_backend": "Memcache",
        "pool_endpoint": "tcp://*:5557"
      }
    }
    ```

    `5557` 是示例广播端口，必须与 MetaService 的 `kv_events_endpoint` 和 Service 暴露端口一致。
    这里的 `*` 由 K8s 注入的 `KVS_MASTER_SERVICE` 替换；外接存储池时填写 conductor 可访问的实际域名或 IP。
    `pool_endpoint` 填事件广播地址，存储服务的读写端口不能用于此项。
4. 核对 MetaService 的 `kv_events_model_name` / `kv_events_block_size` 与 pool 注册使用的
   `kv_conductor_config.model_path` / `block_size` 一致，同时确认 pool 与目标 P/U 注册的模型标识一致。
   后端事件携带的节点 IP 需能关联到目标 P/U 节点；MemCache 的 `backend_id` 按后端部署指导自动注入。
5. 按[使用样例](#使用样例)应用Motor配置，再按下方[池化验收](#pool-affinity-validation)检查。
   CPU 的默认权重 `w_cpu=1.0`，无需额外配置评分开关。

#### Mooncake / YuanRong：已有事件源时的 Motor 配置

以下列出 Motor 的接入配置；应用前须确认后端和引擎已提供与 conductor 兼容的两侧事件。
各后端事件发布服务的安装、启动方式以实际使用版本为准。

| 后端 | 合并到顶层 `kv_conductor_config` 的配置 | 部署前需确认 |
|------|-----------------------------------------|--------------|
| Mooncake | `store_backend: "Mooncake"`；`pool_endpoint` 填中心 pool 的实际 ZMQ 事件广播地址 | [Mooncake 部署文档](kv_cache_store/backend/mooncake.md)当前主要覆盖存储服务，尚未给出完整的事件发布开启步骤；需先确认事件发布能力、地址及模型/块粒度 |
| YuanRong | `store_backend: "YuanRong"`；`cpu_endpoint` / `disk_endpoint` 分别填 `tcp://*:<对应介质事件端口>` | 节点级事件地址使用节点 IP 和基础端口，不加 DP 秩。显式配置实际事件地址，避免回退到 HBM 端口；[后端部署文档](kv_cache_store/backend/yuanrong.md)仍待补全 |

以上配置用于订阅已有事件源，不会替后端开启广播。接入及事件格式的详细约定见
[KV Conductor 设计](../../design/kv_conductor.md#后端适配抽象)。

#### Disk 参与评分时的可选配置

只有实际启用 Disk 存储、具备 Disk 事件并验证查询命中后，才需要调整 `w_disk`。
默认 `w_disk=0`，Disk 命中不计入亲和评分；需要计入时，在
`motor_coordinator_config.scheduler_config` 下合并，例如：

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

`1.0` 仅用于演示启用 Disk 评分，实际值需结合读取成本和性能验证调整。
修改权重不会开启 Disk 存储或补齐缺失的事件。

<a id="pool-affinity-validation"></a>

#### 池化验收

1. 按[验证特性](#验证特性)检查 `/workers`：除 P/U HBM DP 外，MemCache/Mooncake 还应有对应中心 pool，
   YuanRong 应有节点池。注册成功后继续确认引擎 offload 与后端存储确认事件均已到达。
2. 构造 **HBM 未覆盖、但 CPU 或 Disk 仍保留目标前缀**的请求，在 `conductor query response` 中检查
   目标 DP 的 `cpu_blocks` / `disk_blocks`。返回覆盖按 HBM → CPU → Disk 互斥计算，
   完全由 HBM 命中时，CPU/Disk 为零不能用于判定池化失败。
3. 关联最终 `scheduled` 日志，检查实际 `policy`、`matched`、`hbm/cpu/disk` 和选中的 endpoint。
   结合介质权重、命中率门槛及负载判断结果；索引命中仍需通过引擎日志或指标确认实际 KV 复用。

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

`kv_conductor_config.block_size` 与主注意力组事件粒度不一致时，收到的 `BlockStored` 事件
会以 `block_size_mismatch` 原因被丢弃，导致缓存索引不完整或请求无法获得亲和命中。

**解决步骤**

1. 短时启用 conductor 的 `RUST_LOG=trace`，查看 `event_parsed` 日志中的 `block_size` 和 `spec_kind`。
2. 以主注意力组（如 `mla_attention`）的事件粒度为准，确认实际 `block_size`；它不一定等于引擎 `--block-size`。
3. 按实际主组事件粒度修改配置。例如实际粒度为 512 时，在已有配置中合并以下增量；512 不是所有部署的固定值。

    ```json
    {
      "kv_conductor_config": {
        "block_size": 512
      }
    }
    ```

4. 按[使用样例](#使用样例)应用配置后，按[验证特性](#验证特性)检查事件接收和亲和命中。
5. DCP 配置或引擎版本变化后，重新核对事件粒度，不能只依据引擎页大小自动推导。

### 服务正常但始终没有亲和命中

**问题描述**

基础推理请求能够完成，但重复发送长前缀请求后，仍没有观察到缓存命中或亲和调度。

**解决步骤**

1. 核对 `prefill_scheduler_type` 是否为 `kv_cache_affinity`、P/U 事件发布是否开启，
   以及 `kv_conductor_config.conductor_service` 是否指向可访问的 conductor。
2. 检查 Coordinator 是否能加载模型目录中的 tokenizer、模板是否与引擎一致，
   并确认请求的实际 token 数不少于一个 `block_size`。
3. 在 `/workers` 中确认目标实例及 DP 已注册，核对事件端口、主注意力组事件粒度，
   以及查询中的模型标识与注册值是否一致。
4. 若 Conductor 已返回原始命中但实际 `policy=load_balance`，检查介质权重和 `hit_rate_threshold`。
   默认 `w_disk=0`，门槛判断要求加权命中率严格大于阈值。

若基础推理本身或 P/D 传输异常，先按对应的 [vLLM PD 部署](../deployment/k8s/pd_disaggregation_deployment.md)
或 [SGLang PD 部署](../deployment/k8s/pd_disaggregation_sglang.md)指导排查。
对于使用 `kv-transfer-config` 的 vLLM PD Connector，核对 `kv_role` 与 P/D 角色及 Connector 要求
是否一致，并检查 `kv_port` 的分配和连通性。

### Coordinator无法查询conductor

**问题描述**

Coordinator 日志出现 conductor 连接或查询错误，请求回退到负载均衡。

**解决步骤**

1. 检查 `conductor_service`、`http_server_port` 和服务状态，确认 Coordinator 能访问 conductor 的 `/health`。
2. 新 Coordinator 对接仅支持 JSON 的旧 conductor 时，将 `kv_conductor_config.query_encoding` 设为 `json`。
   不要将该字段写在旧 `prefill_kv_event_config` 下。
3. 若日志显示查询熔断，修复连接或查询错误后等待 30 秒冷却窗口结束，再检查后续查询是否恢复。

### HBM命中正常，CPU/Disk始终为零

**问题描述**

Conductor 查询已有 HBM 命中，但在期望使用 CPU/Disk 缓存的请求中，未观察到对应介质的命中。

**解决步骤**

1. 检查 `store_backend` 及 pool / 介质事件端口配置，确认相应服务已启动并完成注册。
2. 确认引擎 offload 事件和后端存储确认事件均已到达 conductor。
3. YuanRong CPU/Disk 使用节点基础端口，不加 `dp_rank`；核对节点 IP、模型标识及目标 DP。
4. 若事件齐备仍无命中，按[跨介质匹配说明](../../design/kv_conductor.md#匹配逻辑与查询流程)
   检查前缀是否连续、续查是否属于同一 `(instance_id, dp_rank)`。
