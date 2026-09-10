# KV Cache 亲和性调度

## 特性介绍

KV Cache亲和性调度通过自研kv-conductor组件（Rust实现），维护全局KV Cache前缀树索引，将请求路由到已缓存最长token前缀的Worker，减少跨实例KV Cache传输开销，提升推理吞吐。

>[!NOTE] 说明
>
>- kv-conductor已集成在MindIE Motor的Python包内，随build.sh条件编译进wheel包。部署时通过以下命令启动，无需额外安装独立二进制。
>
>   ```bash
>   python -m motor.kv_conductor
>   ```
>
>- 不部署Controller/Node Manager组件时，详情请参考[Coordinator 独立部署](../deployment/standalone.md)进行部署。

### 工作原理

kv-conductor和Coordinator组件的功能分工如下所示：

- kv-conductor：索引三层介质（NPU HBM/CPU/Disk），查询时返回各DP的互斥命中npu_blocks/ cpu_blocks/disk_blocks，以及未加权覆盖长度 matched_tokens（互斥块之和 × block_size）。
- Coordinator：按*_blocks与scheduler_config.kv_affinity中的介质权重加权得到亲和匹配长度，再与 endpoint 实时负载融合后选路。

**子策略**：

| 模式 | 行为 |
|------|------|
| unified（默认） | 单一评分（越低越好）= prefill_load_scale × max(0, isl − overlap_credit × matched_tokens) + load_weight × load_cost |
| load_gated | 先保留负载最低的 N 个 endpoint，再从中选择缓存前缀最长的（matched_tokens最大；并列取负载更低） |

**业务流程**

1. KV Cache事件发布：P实例完成Prefill计算后，通过kv-events-config中配置的ZMQ端点发布KV Cache事件（包含block hashes、token IDs、parent hash等）。
2. Conductor索引：kv-conductor作为ZMQ SUB主动connect到各P节点绑定的事件端点（连接方向conductor → P，事件数据流P → conductor），根据token IDs重算XXH3内容哈希，构建HBM RadixTree + CPU/Disk continuation-edge索引。
3. 亲和性调度决策：Coordinator（scheduler_type: kv_cache_affinity）将token IDs发给 kv-conductor，按各endpoint的互斥*_blocks与kv_affinity介质权重加权得到亲和匹配长度，再按评分策略选择最优Worker。

**部署流程**

deploy.py执行后的关键动作：

- 创建/更新ConfigMap `motor-config`（通过user_config.json配置文件生成）。
- 生成各服务的YAML文件至output/deployment/目录。
- kv-conductor独立部署，通过`python -m motor.kv_conductor --host … --port …`命令启动。
- Coordinator调度器按kv_cache_affinity策略进行亲和性路由。

**Conductor查询结果**

POST/query返回示例如下所示：

```json
{
  "default": {
    "vllm-prefill-1": {
      "longest_matched": 640,
      "DP": {
        "0": {
          "matched_tokens": 640,
          "npu_blocks": 3,
          "cpu_blocks": 2,
          "disk_blocks": 0
        }
      }
    }
  }
}
```

| 字段 | 计算 |
|------|------|
| npu_blocks/cpu_blocks/disk_blocks | 互斥真实命中块数（优先级：NPU > CPU > Disk；同前缀副本只归最高层） |
| matched_tokens | (npu + cpu + disk) × block_size（未加权真实覆盖） |
| longest_matched | 实例内各 DP `matched_tokens` 的最大值 |

匹配方式：

| 介质 | 匹配方式 |
|------|----------|
| HBM（NPU） | RadixTree最长连续前缀（从root走到第一个缺失） |
| CPU | continuation-edge连续边匹配：从HBM断点续查；root链（首块副本）无条件走，更长副本不被上游较短命中掩盖 |
| Disk | continuation-edge：从max(HBM, CPU)断点续查；root链同CPU层无条件走 |

**调度评分模型**

调度器优先使用 `DP[<dp_rank>]` 的互斥 `*_blocks` 按介质权重计分（兼容旧版裸 `int` / 仅有 `matched_tokens` 的响应），并截断为不超过prompt长度 `isl`：

```text
effective_blocks = npu×w_npu + cpu×w_cpu + disk×w_disk
matched_tokens   = min(round(effective_blocks × block_size), isl)
prefill_cost     = max(0, isl − overlap_credit × matched_tokens)
load_cost        = endpoint 实时 workload
```

默认权重：w_npu=1.0，w_cpu=1.0，w_disk=0.0。

- unified（默认，分数越低越好）：

  ```text
  score = prefill_load_scale × prefill_cost + load_weight × load_cost
  ```

  - load_weight = 0 时为纯亲和性（最长前缀优先）。
  - 无缓存前缀但负载显著更低的endpoint仍可能胜出，避免热点前缀聚集。

- load_gated：

  - 按 `load_cost` 升序保留最低的N个endpoint（N = kv_affinity.load_gate_topn，小于等于0时为2）。
  - 在候选集内按 `matched_tokens` 降序、`load_cost` 升序排序。

### 约束与限制

**依据原文上下文内容重组，请进行人工校验。**

| 约束维度 | 要求 |
|----------|------|
| 硬件 | **内容缺失，需要人工补齐**。 |
| 部署场景 | PD分离服务部署或者PD混部服务部署。 |
| 引擎 | 基于vLLM引擎（engine_type: "vllm"）。 |
| 特性互斥 | **内容缺失，需要人工补齐。** |
| 软件依赖 | 镜像需包含kv-conductor二进制，已随MindIE Motor wheel包打包；自行构建时需预编译二进制（KV_CONDUCTOR_PREBUILT）或Rust工具链（cargo）。 |
| 其他限制 | kv_conductor_config.block_size须与引擎实际 `--block-size`（hash_block_size）一致，否则查询命中率始终为 0；开启 DCP 时需按（引擎block_size × DCP大小）配置。后续操作均在K8s集群管理节点（master 节点）执行。 |

## 特性使用

### 环境准备

- 已完成PD分离推理服务或者PD混部服务部署。
- 开启KV Cache亲和性调度前，请参考[MindIE Motor 快速开始](../quick_start.md)启动服务，确保基础服务部署正常。
- 镜像需包含kv-conductor二进制。若使用官方发布镜像，二进制已随MindIE Motor wheel包打包；若自行构建，可通过预编译二进制（KV_CONDUCTOR_PREBUILT）或Rust工具链（cargo）打包进wheel包。
- 后续操作均在K8s集群管理节点（master节点）执行。

### 使用样例

**依据原文上下文内容重组，请进行人工校验。**

引擎的 `kv-transfer-config`（PD 传输）属于 PD 分离基础配置，非亲和性子系统引入；KV Cache Store 池化功能单独通过 `kv_cache_store_config` 开启，请参考《[KV Cache Store](kv_cache_store/README.md)》。

#### PD分离/混部服务部署

**操作步骤**

1. 已按照[PD 分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md)或者[PD 混部服务部署](../deployment/k8s/pd_aggregation_deployment.md)完成服务部署，并且服务正常运行。

2. 构建带kv-conductor二进制的镜像包。

   kv-conductor已集成在MindIE Motor的wheel包内，随 `build.sh` 打包，通过以下方式获取二进制：

   - 已有预编译二进制（推荐）：使用以下命令指定路径，`build.sh` 直接复制并打包进wheel包：

     ```bash
     KV_CONDUCTOR_PREBUILT=/path/to/kv-conductor bash build.sh
     ```

   - Rust工具链（cargo环境）：`bash build.sh` 自动编译（cargo build --release）并打包。
   - 两者皆无：跳过编译并输出 [WARNING]，kv-conductor不包含在wheel包中（其他功能不受影响）。

   >[!NOTE] 说明
   >使用官方发布镜像时，kv-conductor二进制已随wheel打包，可跳过该步骤。

   <a id="构建"></a>

3. 修改user_config.json配置文件，参数详细解释请参见[表1](#table001)~[表4](#table004)。

   - **PD分离服务部署配置**

      在examples/infer_engines/vllm/user_config.json配置文件中修改以下配置项。以[快速开始](../quick_start.md)的PD分离配置为基线，仅展示增量部分（`...` 为已有不变配置，无须关注）。

     KV Cache亲和性调度只需在已有PD分离服务部署配置的基础上增加以下配置：

     - scheduler_type：设置为"kv_cache_affinity"，表示启用亲和性调度器。
     - kv-events-config：配置P实例发布KV Cache事件的ZMQ端点，endpoint为事件发布端点，replay_endpoint为事件回放端点。
     - kv_conductor_config：配置kv-conductor服务参数，block_size为事件广播的hash粒度（须与引擎 `--block-size` 一致），http_server_port为kv-conductor HTTP API端口（默认为：13333）。

      ```json
      {
        "version": "v2.0",
        "motor_deploy_config": {
          "..."
        },
        "motor_coordinator_config": {
          "scheduler_config": {
            "scheduler_type": "kv_cache_affinity"
          }
        },
        "motor_engine_prefill_config": {
          "engine_type": "vllm",
          "engine_config": {
            "...",
            "kv-events-config": {
              "publisher": "zmq",
              "enable_kv_cache_events": true,
              "endpoint": "tcp://*:5557",
              "topic": "kv-events",
              "replay_endpoint": "tcp://*:6667"
            }
          }
        },
        "motor_engine_decode_config": {
          "engine_type": "vllm",
          "engine_config": {
            "..."
          }
        },
        "kv_conductor_config": {
          "block_size": 128,
          "http_server_port": 13333
        }
      }
      ```

   <br>

   - **PD混部服务部署配置**

      KV Cache亲和性调度只需在已有PD混部服务部署配置的基础上增加以下配置：
      - scheduler_config.deploy_mode：PD混部部署模式设为"single_node"。
      - scheduler_config.scheduler_type：设为 "kv_cache_affinity"，表示启用亲和性调度器。
      - motor_engine_union_config：PD混部使用union字段，kv-events-config配置在union字段的engine_config中。

      >[!NOTE] 说明
      > kv_affinity字段中的子参数 mode / load_weight / overlap_credit / prefill_load_scale / w_npu / w_cpu / w_disk 等均有默认值，示例中无需配置；如果需要调整评分行为时请参考「scheduler_config（调度器亲和性参数）」设置。

      ```json
      {
        "version": "v2.0",
        "motor_deploy_config": {
          "..."
        },
        "motor_coordinator_config": {
          "scheduler_config": {
            "deploy_mode": "single_node",
            "scheduler_type": "kv_cache_affinity"
          }
        },
        "motor_engine_union_config": {
          "engine_type": "vllm",
          "enable_multi_endpoints": true,
          "engine_config": {
            "...",
            "kv-events-config": {
              "publisher": "zmq",
              "enable_kv_cache_events": true,
              "endpoint": "tcp://*:5557",
              "topic": "kv-events",
              "replay_endpoint": "tcp://*:6667"
            }
          }
        },
        "kv_conductor_config": {
          "block_size": 128,
          "http_server_port": 13333
        }
      }
      ```

    **依据原文上下文内容重组，请进行人工校验。**

    **kv_conductor_config（kv-conductor 全局配置）**

    该字段写在 `user_config.json` **顶层**。部署脚本用它生成 K8s Service 端口；Coordinator 加载时会合并进 `scheduler_config.kv_conductor_config`（也可直接写在 `scheduler_config` 下，效果相同）。

    **表 1** kv_conductor_config参数说明<a id="table001"></a>

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    |--------|------|----------|-----------|--------|------|
    | block_size | uint | ≥ 1 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 事件广播的 hash 粒度（token 数）。须与引擎 `--block-size` / `hash_block_size` 一致。标准模型默认 128；**DeepSeek V4 的取值见「DeepSeek V4 / 混合 KV Cache 模型」** |
    | http_server_port | int | 1024–65535 | 选填 | `13333` | kv-conductor HTTP API 端口，Coordinator 通过此端口查询缓存命中 |
    | re_register_interval_sec | int | ≥ 0 | 选填 | `0` | 周期性重注册间隔（秒），0 或负数禁用 |
    | conductor_service | string | hostname / IP | 选填 | 空（禁用） | kv-conductor 服务地址；空则禁用。部署时也可由环境变量注入 |
    | engine_type | string | 如 `vLLM` | 选填 | `vLLM` | 注册时上报的引擎类型 |
    | model_path | string | 路径 / 名称 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 注册时的 `modelname` |
    | endpoint | string | `tcp://*:<port>` | 选填 | 自动推导 | 默认端口模式：`*` 替换为 endpoint IP，端口加 `dp_rank`；注册时写入 `medium_endpoints.npu`。**自动从引擎 `kv-events-config.endpoint` 推导，无需配置** |
    | replay_endpoint | string | `tcp://*:<port>` | 选填 | 自动推导 | Per-DP replay 端口，conductor 重启恢复时回放缓冲的 KV 事件（可选）。**自动从引擎 `kv-events-config.replay_endpoint` 推导，无需配置** |
    | npu_endpoint | string | `tcp://*:<port>` | 选填 | 自动推导 | Per-DP HBM（NPU）端口模式的显式覆盖项。**一般无需配置**（见下方端口推导说明），仅在需要覆盖自动推导的默认端口时使用 |

    以下参数为 CPU/Disk 二级缓存（L2）相关，开启池化后端时使用：
    **表 2** 开启池化后端时参数说明<a id="table002"></a>

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    |--------|------|----------|-----------|--------|------|
    | pool_endpoint | string | `tcp://<host>:<port>` | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 中心化后端（Mooncake/Memcache）的池服务地址 |
    | cpu_endpoint | string | `tcp://*:<port>` | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | Per-DP CPU/DDR 端口 |
    | disk_endpoint | string | `tcp://*:<port>` | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | Per-DP DISK/SSD 端口 |
    | store_backend | string | `Mooncake` / `Memcache` / `YuanRong` | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 池化后端类型。Mooncake/Memcache：先注册 pool，再按 DP 注册 `npu`；YuanRong：按 DP 注册 `npu`/`cpu`/`disk` |

    >[!NOTE] 说明
    >**端口推导与 DP 偏移**：注册给 kv-conductor 的事件端口无需在 `kv_conductor_config` 中配置，统一由引擎配置 `motor_engine_prefill_config.engine_config["kv-events-config"].endpoint`（如 `tcp://*:5557`）定义。启动时传入 vLLM 的即为该原始端口；vLLM 内部按 `data_parallel_rank` 对端口做偏移后实际监听（如 DP0 → `tcp://*:5557`、DP1 → `tcp://*:5558`）。Coordinator 加载配置时自动将 `endpoint` / `replay_endpoint` 推导进 `kv_conductor_config`，注册时按同样的DP秩将 `*` 替换为 endpoint IP、端口加 `dp_rank`（如 `tcp://10.0.0.1:5557`、`tcp://10.0.0.1:5558`），与 vLLM 实际监听端口一致。因此 prefill / decode / union 的引擎配置中配置好 `kv-events-config` 即可，`npu_endpoint` 等手动配置仅用于覆盖默认推导值。
    >
    > kv-conductor 进程本身仅接受 `--host` / `--port` 启动参数，无介质权重配置。

    **scheduler_config**

    该字段为调度器亲和性配置参数说明：

    **表 3** scheduler_config参数说明<a id="table003"></a>

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    |--------|------|----------|-----------|--------|------|
    | scheduler_type | string | `kv_cache_affinity` | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 启用 KV Cache 亲和性调度 |
    | kv_affinity.mode | string | `unified` / `load_gated` | 选填 | `unified` | 评分子策略 |
    | kv_affinity.load_weight | float | `[0, +∞)` | 选填 | `1.0` | `unified` 下 endpoint 实时负载权重。`1.0`（默认）与亲和折扣后的 prefill 成本同等重要；`0` 表示纯亲和性 |
    | kv_affinity.overlap_credit | float | `[0, +∞)` | 选填 | `1.0` | 缓存前缀对 prefill 成本的折扣系数。值越大，已缓存前缀折扣越高 |
    | kv_affinity.prefill_load_scale | float | `[0, +∞)` | 选填 | `1.0` | `unified` 下亲和折扣后的 prefill 成本权重 |
    | kv_affinity.load_gate_topn | int | `[0, +∞)` | 选填 | `0` | `load_gated` 下保留负载最低的 N 个 endpoint。`0` 时回退为 `2` |
    | kv_affinity.w_npu | float | `[0, +∞)` | 选填 | `1.0` | 互斥 NPU 命中块权重 |
    | kv_affinity.w_cpu | float | `[0, +∞)` | 选填 | `1.0` | 互斥 CPU 命中块权重 |
    | kv_affinity.w_disk | float | `[0, +∞)` | 选填 | `0.0` | 互斥 Disk 命中块权重（默认不计 Disk） |

    **kv-events-config**

    该字段为引擎侧KV事件发布配置参数说明：

    **表 4** kv-events-config参数说明<a id="table004"></a>

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    |--------|------|----------|-----------|--------|------|
    | publisher | string | `zmq` | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 事件发布后端，当前仅支持 `zmq` |
    | enable_kv_cache_events | bool | `true` / `false` | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 是否启用 KV Cache 事件，设为 `true` |
    | endpoint | string | `tcp://*:<port>` | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | P 实例发布 KV 事件的 ZMQ 端点 |
    | topic | string | 自定义 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 事件 ZMQ 主题 |
    | replay_endpoint | string | `tcp://*:<port>` | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 事件回放端点，供 conductor 重启后恢复索引（可选） |

    > [!NOTE] 说明
    > kv-events-config是vLLM原生配置，控制引擎侧的KV事件发布行为；kv_conductor_config是MindIE Motor的配置，控制Coordinator如何注册和查询kv-conductor。两者的端口信息由Coordinator自动打通——`kv_conductor_config.endpoint` / `replay_endpoint` 自动从引擎的kv-events-config推导，无需重复配置。

4. 使用以下命令部署服务。

   ```bash
   cd examples/deployer
   # 方式一：指定配置目录（推荐）
   python deploy.py --config_dir ../infer_engines/vllm

   # 方式二：单独指定配置文件
   python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
   ```

     - --config_dir：指定user_config.json和env.json配置文件的目录（推荐）；
     - --user_config_path：指定user_config.json配置文件路径。
     - --env_config_path：指定env.json配置文件路径。

5. 使用以下命令确认部署结果。

   ```bash
   kubectl get pod -A -owide
   ```

   预期输出：P/D 实例和 kv-conductor 均启动成功（**缺少启动成功的回显，需要补充**），Coordinator 日志中可看到「KV Conductor registered」字样。

#### DeepSeek V4/混合KV Cache模型部署

DeepSeek V4 部署时，引擎 `--block-size` 与 `kv_conductor_config.block_size` **均必须设为 512**，二者保持一致，否则 conductor 查询命中率始终为 0：

```json
"kv_conductor_config": {
  "block_size": 512
}
```

> `block_size` 与端口一样是**注册制**：Coordinator 注册实例时将其上报给 kv-conductor，取值须与该实例引擎的实际 `--block-size` 一致；未显式配置时自动从引擎配置推导。当前 Coordinator 按全局一个 `block_size` 注册所有实例，因此要求各引擎实例的 `--block-size` 保持一致。

引擎侧启动参数示例：

```bash
vllm serve ... --block-size 512
```

引擎启动日志会打印实际的 `hash_block_size`，可据此确认：

```text
# vLLM 日志输出
hash_block_size = 512
```

> **DCP 特例**：vLLM 开启 DCP（Decode Context Parallel，解码上下文并行）后，引擎侧前缀哈希粒度按 DCP 大小放大，`kv_conductor_config.block_size` 需相应配置为 **引擎 `block_size` × DCP 大小**，否则 hash 粒度不匹配，conductor 查询命中率同样为 0。DeepSeek V4 开启 DCP（通常 DCP 大小为 2）后，引擎 block size 一般变为 1024，此时 `kv_conductor_config.block_size` 应配置为 1024。
>
> **警告**：若 `kv_conductor_config.block_size` 与引擎实际 `hash_block_size` 不一致（例如仍用默认 128），conductor 查询时 hash 粒度不匹配，命中率始终为 0。

### 验证特性

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

服务部署完成后，可通过以下方式验证 KV Cache 亲和性调度特性是否生效：

1. 确认 kv-conductor 启动并注册成功：

   ```bash
   kubectl get pod -A | grep kv-conductor
   ```

   预期输出：kv-conductor Pod 处于 Running 状态，Coordinator 日志中可看到「KV Conductor registered」字样。

2. 查询 kv-conductor 缓存命中结果：

   通过 kv-conductor HTTP API 发起 `POST /query` 查询各 endpoint 的缓存命中情况，返回 `matched_tokens` / `npu_blocks` / `cpu_blocks` / `disk_blocks` 等字段：

   ```json
   {
     "default": {
       "vllm-prefill-1": {
         "longest_matched": 640,
         "DP": {
           "0": {
             "matched_tokens": 640,
             "npu_blocks": 3,
             "cpu_blocks": 2,
             "disk_blocks": 0
           }
         }
       }
     }
   }
   ```

   预期输出：`matched_tokens` / `npu_blocks` 等字段反映真实的缓存命中情况（命中块数非 0 说明索引生效）。

3. 发送推理请求验证亲和性路由生效：

   向服务发送推理请求，若请求被路由到已缓存最长 token 前缀的 Worker，且缓存查询命中率正常（非 0），说明亲和性调度特性已生效。若命中率始终为 0，请参考「常见问题」中「命中率始终为 0」的排查步骤。

## 调优建议

**依据原文上下文内容重组，请进行人工校验。**

| 场景 | 关键参数 | 建议取值 | 说明 |
|------|----------|----------|------|
| 纯吞吐优先 | `kv_affinity.mode` / `kv_affinity.load_weight` | `unified` / `0` | 纯亲和性，不感知负载 |
| 负载均衡优先 | `kv_affinity.mode` / `kv_affinity.load_weight` | `unified` / `2.0` | 负载权重更高 |
| 延迟敏感（保守） | `kv_affinity.mode` / `kv_affinity.load_gate_topn` | `load_gated` / `3` | 只在低负载中选最优前缀 |
| DeepSeek V4 | `kv_conductor_config.block_size` | `512` | 引擎 `--block-size` 同步设为 512 |
| 端口冲突规避 | `kv_conductor_config.http_server_port` | 默认 `13333` | 确保不与集群其他服务端口冲突 |

## 常见问题

**依据原文上下文内容重组，请进行人工校验。**

### 服务启动后 P/D 实例间无法传输 KV Cache

**问题描述**：服务启动后，P/D 实例之间无法传输 KV Cache。

**原因分析**：`kv_transfer_config` 中 `kv_role` 配置错误，或 `kv_port` 不一致。

**解决步骤**：

1. 检查 `kv_transfer_config` 中 `kv_role` 是否正确：P 为 `kv_producer`，D 为 `kv_consumer`。
2. 检查 `kv_port` 是否一致。

### Coordinator 无法连接到 kv-conductor

**问题描述**：Coordinator 无法连接到 kv-conductor，亲和性调度不生效。

**原因分析**：kv-conductor Pod 未启动、`http_server_port` 配置错误或端口被占用。

**解决步骤**：

1. 确认 kv-conductor pod 已启动：`kubectl get pod -A | grep kv-conductor`。
2. 检查 `kv_conductor_config.http_server_port` 是否配置正确且未被占用。
3. 查看 kv-conductor 日志：`kubectl logs <kv-conductor-pod>`。

### P 实例发布 KV Cache 事件失败

**问题描述**：P 实例发布 KV Cache 事件失败。

**原因分析**：`kv-events-config` 中 `endpoint` / `replay_endpoint` 配置不正确，或 conductor → P 方向网络不可达（conductor 主动 connect P 的事件端口）。

**解决步骤**：

1. 检查 `kv-events-config` 中 `endpoint` 和 `replay_endpoint` 配置是否正确（P 侧绑定）。
2. 检查 `kv_conductor_config.npu_endpoint` 是否与其一致。
3. 确认 **conductor → P** 方向的网络是否可达（conductor 主动 connect P 的事件端口）。

### 命中率始终为 0

**问题描述**：KV Cache 亲和性调度查询命中率始终为 0。

**原因分析**：`block_size` 与引擎实际 `hash_block_size` 不一致、`enable_kv_cache_events` 未开启、事件端点配置错误或 kv-conductor 注册/查询报错。

**解决步骤**：

1. 检查 `kv_conductor_config.block_size` 是否与引擎实际的 `hash_block_size` 一致（见引擎日志）。
2. 确认 `kv-events-config.enable_kv_cache_events` 设为 `true`。
3. 确认引擎 `kv-events-config.endpoint` 配置正确（Coordinator 会自动推导注册地址并做 DP 端口偏移）。
4. 查看 Coordinator 日志检查 kv-conductor 注册和查询是否有报错。

### kv-conductor 未包含在 wheel 包中

**问题描述**：构建产物中不包含 kv-conductor 二进制。

**原因分析**：构建环境缺少 Rust 工具链，`build.sh` 已自动跳过编译。

**解决步骤**：安装 rustup 后重新执行 `bash build.sh`。请参考「构建」小节。
