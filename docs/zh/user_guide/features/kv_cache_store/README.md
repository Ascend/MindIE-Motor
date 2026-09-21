# KV池化能力

## 特性介绍

KV 池化允许 P/D（Prefill/Decode）实例通过外部存储共享和复用 KV Cache，从而减少重复计算并提升推理吞吐量。UCM 是 KV 池化能力中的一种功能，通过复用跨请求的相同前缀，减少 Prefill 计算。

### 工作原理

KV 池化通过 `MultiConnector` 组合传输 Connector 和 Store Connector。不同 Store Connector 都接入 vLLM 的 KV Cache 查询、加载和保存流程，但内部存储实现不同。

**AscendStoreConnector**

- P/D 实例都加载 `AscendStoreConnector`，P 侧以 `kv_producer` 写入 KV Pool，D 侧以 `kv_consumer` 查询并加载 KV Cache。
- `AscendStoreConnector` 负责统一的匹配、加载和保存流程，其 `backend` 决定底层使用 MemCache 还是 Mooncake Store。
- `kv_cache_store_config` 配置所选后端的服务地址、端口和运行参数。其中，MemCache 使用 MetaService，Mooncake 使用 `mooncake_master`。
- `connectors[0]` 的 Mooncake 传输 Connector 负责 P/D 实时 KV 传输，与 `connectors[1]` 的共享 KV Pool 职责不同。

**UCMConnector**

- 首次请求由 Prefill 计算 KV Cache，`UCMConnector` 按 `store_pipeline` 将可复用前缀写入 Cache、POSIX 等存储层。
- 后续请求出现相同前缀时，Prefill 通过 UCM 查询并加载已保存的 KV Cache，减少重复 Prefill 计算。
- 本次请求的 P/D 实时 KV 传输仍由 Mooncake 传输 Connector 完成。当前分布式 P/D 方案的 Decode 不加载 UCM。

### 核心功能

MindIE Motor 使用 `MultiConnector` 组合 P/D 传输 Connector 与 Store Connector。当前支持以下池化功能：

- **基于 Mooncake 的共享 KV Pool**：P/D 均加载 `AscendStoreConnector`，并配置 `backend: "mooncake"`，分别写入和读取共享 KV Cache。详细信息请参见 [Mooncake Backend](./backend/mooncake.md)。
- **基于 MemCache 的共享 KV Pool**：P/D 均加载 `AscendStoreConnector`，并配置 `backend: "memcache"`，分别写入和读取共享 KV Cache。详细信息请参见 [MemCache Backend](./backend/memcache.md)。
- **UCM**：通过 `UCMConnector` 的 `store_pipeline` 组合 Cache、POSIX 等 UCM Store。当前分布式 P/D 方案在 Prefill 保存和加载跨请求前缀，Decode 不加载 UCM。详细信息请参见 [MindIE Motor 中部署 UCM](./backend/ucm.md)。

> [!NOTE] 说明
>
> - MemCache 和 Mooncake 是 `AscendStoreConnector` 的 backend；UCM 是另一个 Store Connector，但仍属于 KV 池化能力。不要配置 `"backend": "ucm"`。
> - 此外，`MooncakeConnectorV1`、`MooncakeHybridConnector` 等负责 P/D 实时传输，与 `AscendStoreConnector` 的 Mooncake Backend 不是同一层配置。

### 约束与限制

| 约束维度 | 要求 |
|----------|------|
| 硬件 | MemCache 后端的 `local_service_mode` 默认值与硬件类型相关。<ul><li>Atlas 800I A2 推理服务器和 Atlas 850 超节点服务器：默认为`inprocess`</li><li>Atlas 800I A3 超节点服务器：默认为 `standalone`</li></ul> |
| 部署场景 | 仅支持 PD 分离部署场景。 |
| 引擎 | 仅支持 vLLM 推理引擎。 |
| 特性互斥 | <ul><li>禁止配置 `"backend": "ucm"`，UCM 不通过 `AscendStoreConnector` 的 backend 机制。</li><li>`AscendStoreConnector` 和 `kv_cache_store_config` 中的 `backend` 必须保持一致。</li><li>`MooncakeConnectorV1` 等负责 P/D 实时传输，与 `AscendStoreConnector` 的 Mooncake Backend 不是同一层配置，不可混淆。</li></ul> |
| 软件依赖 | <ul><li>依赖 vllm-ascend KV 传输层。</li><li>MemCache 无需额外安装，已预装在 Motor 镜像中。</li><li>Mooncake 由 vllm-ascend 集成，无需额外安装。</li><li>UCM 需额外安装 UCM wheel。</li></ul> |
| 其他限制 | <ul><li>`lookup_rpc_port` 无需手动填写，由 Motor 自动适配。</li><li>`default_kv_lease_ttl` 需大于 `env.json` 中的 `ASCEND_CONNECT_TIMEOUT` 和 `ASCEND_TRANSFER_TIMEOUT`。</li><li>Mooncake 后端的 `eviction_high_watermark_ratio` 和 `eviction_ratio` 为必填参数，`deploy.py` 会进行强制校验。</li><li>MemCache 内部配置项在对应模式的 `mmc-local-inprocess.conf` 或 `mmc-local-standalone.conf` 中管理，无需在 `user_config.json` 中配置。</li></ul> |

## 特性使用

### 环境准备

- 已按照 [PD 分离服务部署](../../deployment/k8s/pd_disaggregation_deployment.md) 完成 P/D 分离推理服务部署，并且服务运行正常。KV 池化在该服务基础上开启，不会改变 Controller 和 Coordinator 的部署方式。
- KV 池化的通用约束请参见 [vllm-ascend kv_pool](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/kv_pool.html)。启用 UCM 功能时，还需满足 [UCM 部署文档](../../features/kv_cache_store/backend/ucm.md) 中的要求。
- 开启前请先参考 [MindIE Motor 快速开始](../../quick_start.md)，确保基础 P/D 分离服务可以正常部署。
- 后续所有操作只在 K8s 集群的管理节点（master 节点）执行。

### 使用场景

- 场景一：共享 KV Pool（使用 `AscendStoreConnector`）
  P/D 实例均加载 `AscendStoreConnector`，通过 `backend` 选择 MemCache 或 Mooncake Store 作为共享 KV Cache 后端。适用于需要 P/D 实例共享 KV Cache 以降低传输开销的场景。
- 场景二：UCM（使用 `UCMConnector`）
  Prefill 通过 `UCMConnector` 接入 UCM Store Pipeline，保存和加载跨请求相同前缀的 KV Cache，减少重复 Prefill 计算。Decode 不加载 UCM，P/D 实时 KV 传输仍由 Mooncake 传输 Connector 完成。适用于请求之间存在大量相同前缀（如共享 Prompt 前缀）的场景。

### 使用样例

#### 使用 AscendStoreConnector 部署样例

池化通过 `MultiConnector` 组合传输连接器（`connectors[0]`）与池化后端连接器（`connectors[1]`）实现。以下以 `MooncakeConnectorV1`（P/D 协同）和 `AscendStoreConnector`（KV 池后端）为例。

使用 `AscendStoreConnector` 时，需要同时配置 P/D 实例的 `kv_transfer_config` 和全局 `kv_cache_store_config`。

**操作步骤**

P/D传输Connector选型如下表所示：

**表 1** <a id="table_Connector"></a>P/D传输Connector选型

| 模型 attention 架构 | `connectors[0]` | 典型模型 |
|---------------------|-----------------|----------|
| 标准 attention | `MooncakeConnectorV1` | Qwen3、GLM-5、DeepSeek V3.1 |
| 混合 attention | `MooncakeHybridConnector` | DeepSeek V4 / V4 Flash / V4 Pro |

>[!WARNING] 注意
> 混合attention模型误配置为 `MooncakeConnectorV1` 时，Decode节点可能在推理时崩溃重启。

1. 在 `user_config.json` 配置文件中，分别在 P 实例的 `motor_engine_prefill_config` 字段和 D 实例的 `motor_engine_decode_config` 字段下，为 `engine_config` 配置 **kv_transfer_config** 字段。

     - **P 实例（motor_engine_prefill_config）：**

       ```json
       "motor_engine_prefill_config": {
         "engine_type": "vllm",
         "engine_config": {
           "...": "...",
           "kv_transfer_config": {
             "kv_connector": "MultiConnector",
             "kv_role": "kv_producer",
             "kv_connector_extra_config": {
               "connectors": [
                 {
                   "kv_connector": "MooncakeConnectorV1",
                   "kv_role": "kv_producer",
                   "kv_port": "30001"
                 },
                 {
                   "kv_connector": "AscendStoreConnector",
                   "kv_role": "kv_producer",
                   "kv_connector_extra_config": {
                     "backend": "memcache"
                   }
                 }
               ]
             }
           }
         }
       }
       ```

     - **D 实例（motor_engine_decode_config）：**

       ```json
       "motor_engine_decode_config": {
         "engine_type": "vllm",
         "engine_config": {
           "...": "...",
           "kv_transfer_config": {
             "kv_connector": "MultiConnector",
             "kv_role": "kv_consumer",
             "kv_connector_extra_config": {
               "connectors": [
                 {
                   "kv_connector": "MooncakeConnectorV1",
                   "kv_role": "kv_consumer",
                   "kv_port": "30002"
                 },
                 {
                   "kv_connector": "AscendStoreConnector",
                   "kv_role": "kv_consumer",
                   "kv_connector_extra_config": {
                     "backend": "memcache"
                   }
                 }
               ]
             }
           }
         }
       }
       ```

       **关键参数说明：**
       - kv_connector：连接器类型，`MultiConnector` 用于组合多个 Connector。
       - connectors[0]：P/D 实时传输 Connector，负责 P/D 实例间的 KV Cache 传输。
       - connectors[1]：池化后端 Store Connector，负责读写共享 KV Pool。
       - backend：`AscendStoreConnector` 的后端类型，可选 `memcache` 或 `mooncake`，需与全局 `kv_cache_store_config.backend` 保持一致。
       - lookup_rpc_port：无需手动填写，每个 DP 实例的值由 Motor 自动适配。

2. 配置 kv_cache_store_config（全局配置）。

    `kv_cache_store_config` 为 KV 池化全局配置，由 P/D 实例共享。以下以默认后端 MemCache 为例：

    ```json
    "kv_cache_store_config": {
      "backend": "memcache",
      "local_service_mode": "standalone"
    }
    ```

    `backend` 决定池化后端，需与 `AscendStoreConnector` 中的 `backend` 保持一致。各后端参数说明如下：

    **表 2** 通用参数

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    | --- | --- | --- | --- | --- | --- |
    | `backend` | string | `mooncake`、`memcache` | 选填 | `memcache` | 池化后端；未配置时默认 `memcache` |
    | `target_job_id` | string | 目标服务的 motor_deploy_config.job_id（即目标 namespace） | 选填 | 未配置 | 复用其他 K8s 推理服务的 kv_store |

    **表 3** Mooncake 专属参数

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    | --- | --- | --- | --- | --- | --- |
    | `metadata_server` | string | `P2PHANDSHAKE` | 选填 | `P2PHANDSHAKE` | 元数据服务器模式，默认为点对点握手模式 |
    | `protocol` | string | `ascend` | 选填 | `ascend` | 底层传输协议 |
    | `device_name` | string | 网卡名称 | 选填 | `""` | 指定绑定的网卡名称，为空则自动选择 |
    | `global_segment_size` | string | 显存段大小 | 选填 | `1GB` | 全局共享显存段大小 |
    | `port` | int | 端口号 | 选填 | `50088` | KV Pool 服务端口；未配置时 deploy.py 将按默认值补齐 |
    | `default_kv_lease_ttl` | int | 需大于 `ASCEND_CONNECT_TIMEOUT` 和 `ASCEND_TRANSFER_TIMEOUT` | 选填 | `11000` | KV 对象默认租约 TTL（毫秒） |
    | `eviction_high_watermark_ratio` | float | 建议 0.9 | 必填 | 无 | 池化空间高水位驱逐线，传递给 `mooncake_master` 进程；deploy.py 强制校验，缺失报错 |
    | `eviction_ratio` | float | 建议 0.1 | 必填 | 无 | 单次驱逐比例，传递给 `mooncake_master` 进程；deploy.py 强制校验，缺失报错 |
    | `store_mode` | string | `embedded`、`standalone` | 选填 | `embedded` | Store 部署模式 |
    | `local_buffer_size` | string | 大小值 | 选填 | `1GB` | standalone 模式下引擎侧传输 staging buffer |
    | `store_http_port` | int | 端口号 | 选填 | `0` | standalone 模式下 store 进程的 REST 端口；默认 `0` 由内核分配临时端口 |

    **表 4** MemCache 专属参数

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    | --- | --- | --- | --- | --- | --- |
    | `local_service_mode` | string | `inprocess`、`standalone` | 选填 | Atlas 800I A2/850：`inprocess`；Atlas 800I A3：`standalone` | LocalService 部署模式：`inprocess`（与 vLLM 同进程）或 `standalone`（独立进程） |

    > [!NOTE] 说明
    > 所有 MemCache 内部配置项（DRAM 池大小、通信协议、MetaService 端口、SSD 缓存、UBSIO 参数等）均由用户在对应模式的 `mmc-local-inprocess.conf` 或 `mmc-local-standalone.conf` 中管理，无需在 `user_config.json` 中配置。请参见 [MemCache 后端文档](./backend/memcache.md)。

3. （可选）<a id="step3"></a>配置多套服务共享kv_store。

    当集群中存在多套 K8s 推理服务（各自对应独立的 `job_id` / namespace）时，可通过 `target_job_id` 让后续服务复用第一套已部署的 kv_store，而无需重复拉起 MetaService / mooncake_master Pod。

    **配置示例：**

    - 第一套服务（提供 kv_store）：

      ```json
      "motor_deploy_config": {
        "job_id": "service-a"
      },
      "kv_cache_store_config": {
        "backend": "memcache"
      }
      ```

    - 第二套服务（复用第一套的 kv_store）：

      ```json
      "motor_deploy_config": {
        "job_id": "service-b"
      },
      "kv_cache_store_config": {
        "backend": "memcache",
        "target_job_id": "service-a"
      }
      ```

    **行为说明：**

    | 场景 | 行为 |
    | --- | --- |
    | 未配置 `target_job_id` | 在本 namespace 新建 kv_store Pod |
    | `target_job_id` 与自身 `job_id` 相同 | 在本 namespace 新建 kv_store Pod |
    | `target_job_id` 指向其他服务，且目标 namespace 中存在 kv_store Service 与 Running 状态的 kv_store Pod | 复用目标 kv_store 域名，本套不部署 kv_store Pod |
    | `target_job_id` 写错，或目标 namespace 中无可用 kv_store | 回退为在本 namespace 新建 kv_store Pod |

    > [!NOTE] 说明
    > 两套服务的 `deploy_mode`（`multi_deployment` / `infer_service_set`）应保持一致，否则 Service 名称可能对不上，复用会失败并回退为新建。
4. 使用以下命令在 `examples/deployer` 目录下通过 `deploy.py` 脚本部署服务。

    ```bash
    cd examples/deployer

    # 方式一：指定配置目录（推荐）
    python deploy.py --config_dir ../infer_engines/vllm

    # 方式二：单独指定配置文件
    python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
    ```

    **部署完成后**

    - 集群中会创建/更新 ConfigMap `motor-config`（内容来自当前输入的 `user_config.json`），作为后续扩缩容与刷新的基线。
    - `output_yamls/` 下会生成各服务的 YAML 文件。
    - 使用 `AscendStoreConnector` 时，deployer 根据 `kv_cache_store_config.backend` 拉起对应服务：Mooncake 使用 `mooncake_master`，MemCache 使用 MetaService，并按配置准备 LocalService。

    **部署注意**

    - **Mooncake 无探针**：`mooncake_master` 不提供 `/livez`。kv-store 的 startup / liveness / readiness 仅对 MemCache MetaService 做 HTTP 探活；`backend` 为 `mooncake`（或其它非 memcache 后端）时，探针会跳过 HTTP 请求并直接成功。因此 Mooncake 的 kv-store Pod Ready **不代表** `mooncake_master` 已就绪，不能用 kubelet 探针判断 Mooncake 健康状态。

#### 使用 UCMConnector 部署样例

**操作步骤**

1. UCM 属于 KV 池化功能，通过 `UCMConnector` 接入，不复用 `AscendStoreConnector` 的 backend 机制。

   - Prefill 的 `connectors[0]` 是 Mooncake P/D 传输 Connector，`connectors[1]` 是 `UCMConnector`。
   - `UCMConnector` 保持 `kv_role: "kv_both"`，UCM Store Pipeline 配置内联在其 `kv_connector_extra_config` 中。
   - Decode 只配置与 Prefill 匹配的 Mooncake P/D 传输 Connector，不加载 `UCMConnector`。
   - UCM 的 `store_pipeline`、`storage_backends` 和容量参数决定前缀缓存如何保存，不使用 `AscendStoreConnector.backend`。

2. 当前 Motor UCM 样例仍配置了 `kv_cache_store_config.backend: "mooncake"`，用于当前 deployer 生成 Mooncake kv_store/master 资源；这是部署适配配置，不表示 UCM 变成了 Mooncake Backend。UCM 的实际 Store 由 `UCMConnector` 中的 `store_pipeline` 决定。
   > [!NOTE] 说明
   > 完整配置、存储挂载、部署及验证步骤请参见 [MindIE Motor 中部署 UCM](./backend/ucm.md)。
3. 使用以下命令在 `examples/deployer` 目录下通过 `deploy.py` 脚本部署服务。

    ```bash
    cd examples/deployer

    # 方式一：指定配置目录（推荐）
    python deploy.py --config_dir ../infer_engines/vllm

    # 方式二：单独指定配置文件
    python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
    ```

    **部署完成后**

    - 集群中会创建/更新 ConfigMap `motor-config`（内容来自当前输入的 `user_config.json`），作为后续扩缩容与刷新的基线。
    - `output_yamls/` 下会生成各服务 YAML 文件。
    - 使用 UCM 时，还需按 UCM 部署文档为 Prefill 安装 UCM、挂载 UCM Store 所需目录；当前样例同时生成 Mooncake kv_store/master 资源。

### 验证特性

1. 确认 `kv_store` Pod 已成功启动。

   ```bash
   kubectl get pods -n <namespace> -l deploy-name=mindie-motor-kv-store
   ```

   预期输出中包含状态为 `Running` 且已就绪的 `kv-store` Pod。

   > [!NOTE] 说明
   > Mooncake 后端时，kv-store Pod Ready 不能证明 `mooncake_master` 已就绪（无 `/livez` 探针）。请结合 master 日志或 `:50090/metrics` 确认，详见上文「部署注意」。

2. 确认 P/D 引擎 Pod 正常启动，日志中不包含 KV 传输相关错误。

   ```bash
   kubectl logs <pod-name> -n <namespace> | grep -iE "kv_cache|kv_store"
   ```

   根据所选后端检查连接和初始化日志，确认未出现 KV Cache 连接、写入或读取相关的 error 级别错误。

3. 连续发送两次包含相同长前缀的推理请求，验证 KV Cache 写入和复用链路。

   ```bash
   curl -X POST http://<服务IP>:<服务端口>/v1/chat/completions \
        -H "Content-Type: application/json" \
        -d '{"model":"<model_name>","messages":[{"role":"user","content":"<相同的长前缀和问题>"}]}'
   ```

   两次请求均应返回 HTTP 200 及正常推理响应。同时检查所选后端及 P/D 实例日志，第一次请求应完成 KV Cache 写入，第二次请求应出现对应缓存读取或命中记录。仅请求成功不能证明 KV 池化已经生效。

## 调优建议

- **增大 `kv_parallel_size`**：KV 池化开启后，P 实例需要额外将 KV Cache 推入缓存池，可能带来少量性能开销。可适当增大 `kv_parallel_size` 以提升传输效率。
- **配置驱逐参数**：Mooncake 后端的 `eviction_high_watermark_ratio`（建议 0.9）和 `eviction_ratio`（建议 0.1）控制池化空间的驱逐行为，应根据实际显存容量调整。
- **调整租约 TTL**：`default_kv_lease_ttl` 需大于 `env.json` 中的 `ASCEND_CONNECT_TIMEOUT` 和 `ASCEND_TRANSFER_TIMEOUT`，避免 KV Cache 在传输过程中被提前清理。

## 常见问题

### 服务启动后 P/D 实例间无法传输 KV Cache

**问题描述**

服务启动后 P/D 实例间无法传输 KV Cache。

**原因分析**

`kv_role` 配置错误。P 实例应为 `kv_producer`，D 实例应为 `kv_consumer`，配置错误会导致传输方向不匹配。

**解决步骤**

通过 `motor-config` ConfigMap 查看实际部署使用的 `user_config.json`，确认 `kv_transfer_config.kv_role` 是否符合要求。

  ```bash
  kubectl get configmap motor-config -n <namespace> -o yaml | grep -A 15 kv_transfer_config
  ```

  如不符合，修改 `user_config.json` 后重新执行 `python deploy.py --config_dir ../infer_engines/vllm` 部署。

### P 实例推理性能下降

**问题描述**

KV 池化开启后，P 实例推理性能下降。

**原因分析**

KV 池化开启后，P 实例需要额外将 KV Cache 推入缓存池，可能带来少量性能开销。

**解决步骤**

可适当增大 `kv_parallel_size` 以提升传输效率。

### D 实例拉取 KV Cache 超时

**问题描述**

D 实例拉取 KV Cache 超时。

**原因分析**

`ASCEND_CONNECT_TIMEOUT` 与 `ASCEND_TRANSFER_TIMEOUT` 不足，或 `default_kv_lease_ttl` 小于这两个超时时间。

**解决步骤**

检查 `env.json` 中 `ASCEND_CONNECT_TIMEOUT` 和 `ASCEND_TRANSFER_TIMEOUT` 是否足够大，并确认 `default_kv_lease_ttl` 大于这两个超时时间；修改后重新部署。

### MemCache MetaService 启动失败

**问题描述**

MemCache MetaService 启动失败。

**原因分析**

`config_store_port`、`metrics_port` 端口被占用，或 `POD_IP` 环境变量未正确注入。

**解决步骤**

检查 `kv_cache_store_config` 中 `config_store_port` 和 `metrics_port` 是否被占用；确认 `kv_cache_store_template.yaml` 中 `fieldRef: status.podIP` 是否正确注入 `POD_IP`；通过 `kubectl logs` 查看 MetaService Pod 日志定位具体错误。

### 切换后端后配置未生效

**问题描述**

切换后端后配置未生效。

**原因分析**

`AscendStoreConnector` 和 `kv_cache_store_config` 中的 `backend` 不一致。

**解决步骤**

确保两处 `backend` 值相同；修改后重新执行 `python deploy.py --config_dir ../infer_engines/vllm` 部署。

### 为什么 UCM 样例中仍然有 backend: "mooncake"`

**问题描述**

UCM 样例中仍存在 `backend: "mooncake"` 配置。

**原因分析**

这是当前 PyMotor deployer 用来生成 Mooncake kv_store/master 资源的配置，不是 UCM 的存储后端。UCM 的实际 Store 由 `UCMConnector` 中的 `store_pipeline` 决定。

**解决步骤**

无需修改此配置；UCM Store 配置应查看 `UCMConnector` 中的 `store_pipeline` 和 `storage_backends`。

### 推理时 Decode 节点反复崩溃重启

**问题描述**

推理过程中 Decode 节点反复崩溃重启。

**原因分析**

混合attention模型将 `connectors[0]` 配置成了`MooncakeConnectorV1`。

**解决步骤**

使用混合attention模型时，将 `connectors[0]` 配置为`MooncakeHybridConnector`。

### 开启 MemCache 池化后，长序列请求把 Decode 实例打挂，报错 shm_crash

**问题描述**

开启 MemCache 池化后，短请求可正常推理；长序列请求会把 Decode 实例打挂，日志出现 `shm_crash`。

**原因分析**

引擎 Pod 的 `/dev/shm` 默认仅为 `4Gi`。MemCache 池化在长序列场景下需要更大的共享内存，容量不足时 Decode 侧会因 shm 不足崩溃。

**解决步骤**

在 `user_config.json` 的 `motor_deploy_config` 中配置 `"dshm_size": "32Gi"`（或按现场负载继续增大）后重新部署。压测验证：128 条请求（80k-1k）在 `32Gi` 下服务仍可正常运行。
