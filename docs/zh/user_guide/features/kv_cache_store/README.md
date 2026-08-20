# KV池化能力部署

## 功能介绍

KV池化允许 P/D（Prefill/Decode）实例通过外部存储共享和复用 KV Cache，从而减少重复计算并提升推理吞吐。UCM  是 KV池化能力中的一种功能，通过复用跨请求的相同前缀，减少 Prefill 计算。

MindIE Motor 使用 `MultiConnector` 组合 P/D 传输 Connector 与 Store Connector。当前支持的池化功能如下：

| KV池化功能 | Store Connector | 存储实现 | 使用方式 |
|-----------|-----------------|----------|----------|
| 共享 KV Pool | `AscendStoreConnector` | 通过 `backend` 选择 MemCache 或 Mooncake Store | P/D 均加载 Store Connector，分别写入和读取共享 KV Cache |
| UCM  | `UCMConnector` | 通过 `store_pipeline` 组合 Cache、Posix 等 UCM Store | 当前分布式 PD 方案在 Prefill 保存和加载跨请求前缀，Decode 不加载 UCM |

> [!IMPORTANT]Connector 与 backend 的层级
>
> MemCache 和 Mooncake 是 `AscendStoreConnector` 的 backend；UCM 是另一个 Store Connector，但仍属于 KV池化能力。不要配置 `"backend": "ucm"`。此外，`MooncakeConnectorV1`、`MooncakeHybridConnector` 等负责 P/D 实时传输，与 `AscendStoreConnector` 的 Mooncake Backend 不是同一层配置。

MindIE Motor KV池化基于 vllm-ascend 的 KV 传输层实现，通用约束和环境依赖可参考 [vllm-ascend 池化文档](https://docs.vllm.ai/projects/ascend/zh-cn/main/user_guide/feature_guide/kv_pool.html)。UCM 的额外依赖和部署方式见 [在 PyMotor 中部署 UCM](backend/ucm.md)。

KV池化主要通过 `user_config.json` 配置；使用 UCM 功能时还需要准备 UCM wheel，并为 Prefill 挂载缓存存储。完成对应配置后，通过 `deploy.py` 部署。

## 前置说明

- 必须已使用 MindIE Motor 部署 PD 分离推理服务；KV池化在该服务基础上开启，不会改变 Controller 和 Coordinator 的部署方式。
- KV池化的通用约束见 [vllm-ascend kv_pool](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/kv_pool.html)；启用 UCM 功能时还需满足 [UCM 部署文档](backend/ucm.md)中的要求。
- 开启前请先参考 [MindIE Motor 快速开始](../../quick_start_motor.md)，确保基础 PD 分离服务可以正常部署。
- 后续所有操作只在 k8s 集群的管理节点（master 节点）执行。

## 配置 `user_config.json`

### 使用 `AscendStoreConnector`

使用 `AscendStoreConnector` 时，需要同时配置 P/D 实例的 `kv_transfer_config` 和全局 `kv_cache_store_config`。

#### kv_transfer_config（P/D 实例 engine_config 内）

池化通过 `MultiConnector` 组合传输连接器（`connectors[0]`）与池化后端连接器（`connectors[1]`）实现。以 `MooncakeConnectorV1`（P/D 协同）+ `AscendStoreConnector`（KV 池后端）为例：

**P 实例（motor_engine_prefill_config）：**

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

**D 实例（motor_engine_decode_config）：**

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

> `lookup_rpc_port` 无需手动填写，每个 DP 实例的值由 Motor 自动适配。

其中 `AscendStoreConnector` 的 `backend` 字段决定使用的池化后端。Connector 结构保持一致，切换后端时还需要同步修改全局 `kv_cache_store_config` 中的 `backend` 及对应后端参数：

| 池化后端 | `backend` 值 | 说明 |
|----------|-------------|------|
| [Mooncake](backend/mooncake.md) | `mooncake` | 天然支持，无需额外安装 |
| [MemCache](backend/memcache.md) | `memcache` | 默认后端，天然支持，无需额外安装 |
| Yuanrong | `yuanrong` | TODO：后续版本支持 |

> 关于 Connector 的识别白名单和 `MultiConnector` 传输层规则，请参见 [PD 分离特性说明](../../../design/pd_disaggregation.md#connector-驱动执行计划)。

#### kv_cache_store_config（全局配置）

`kv_cache_store_config` 为 KV 池化全局配置，P/D 实例共享（以默认后端 MemCache 为例）：

```json
"kv_cache_store_config": {
  "backend": "memcache",
  "local_service_mode": "standalone",
}
```

`backend` 决定池化后端，需与 `AscendStoreConnector` 中的 `backend` 保持一致。各后端参数说明如下：

**通用参数**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `backend` | string | `memcache` | 池化后端：`mooncake`、`memcache`；未配置时默认 `memcache` |
| `target_job_id` | string（可选） | 未配置 | 复用其他 K8s 推理服务的 kv_store。值为目标服务的 `motor_deploy_config.job_id`（即目标 namespace）。详见下方 [多套服务共享 kv_store](#多套服务共享-kv_store) |

**Mooncake 专属参数**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `metadata_server` | string | `P2PHANDSHAKE` | 元数据服务器模式，默认为点对点握手模式 |
| `protocol` | string | `ascend` | 底层传输协议 |
| `device_name` | string | `""` | 指定绑定的网卡名称，为空则自动选择 |
| `global_segment_size` | string | `1GB` | 全局共享显存段大小 |
| `port` | int（可选） | `50088` | KV Pool 服务端口；未配置时 deploy.py 将按默认值补齐 |
| `default_kv_lease_ttl` | int（可选） | `11000` | KV 对象默认租约 TTL（毫秒）；配置值需大于 `env.json` 中 vllm 实例的 `ASCEND_CONNECT_TIMEOUT` 和 `ASCEND_TRANSFER_TIMEOUT` |
| `eviction_high_watermark_ratio` | float | 0.9 | 池化空间高水位驱逐线，传递给 `mooncake_master` 进程 |
| `eviction_ratio` | float | 0.1 | 单次驱逐比例，传递给 `mooncake_master` 进程 |

**MemCache 专属参数**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `local_service_mode` | string（可选） | <ul><li>Atlas 800I A2 推理服务器/Atlas 850 超节点服务器：`inprocess`。</li><li>Atlas 800I A3 超节点服务器：`standalone`</li></ul> | LocalService 部署模式：`inprocess`（与 vLLM 同进程）或 `standalone`（独立进程） |

> **所有 memcache 内部配置项**（DRAM 池大小、通信协议、MetaService 端口、SSD 缓存、UBSIO 参数等）均由用户直接在 `mmc-local-inprocess.conf` 中管理，无需在 `user_config.json` 中配置。详见 [MemCache 后端文档](backend/memcache.md)。

#### 多套服务共享 kv_store

当集群中存在多套 K8s 推理服务（各自对应独立的 `job_id` / namespace）时，可通过 `target_job_id` 让后续服务复用第一套已部署的 kv_store，而无需重复拉起 MetaService / mooncake_master Pod。

**配置示例**

第一套服务（提供 kv_store）：

```json
"motor_deploy_config": {
  "job_id": "service-a"
},
"kv_cache_store_config": {
  "backend": "memcache"
}
```

第二套服务（复用第一套的 kv_store）：

```json
"motor_deploy_config": {
  "job_id": "service-b"
},
"kv_cache_store_config": {
  "backend": "memcache",
  "target_job_id": "service-a"
}
```

**行为说明**

| 场景 | 行为 |
|------|------|
| 未配置 `target_job_id` | 在本 namespace 新建 kv_store Pod |
| `target_job_id` 与自身 `job_id` 相同 | 在本 namespace 新建 kv_store Pod |
| `target_job_id` 指向其他服务，且目标 namespace 中存在 kv_store Service 与 Running 状态的 kv_store Pod | 复用目标 kv_store 域名，本套不部署 kv_store Pod |
| `target_job_id` 写错，或目标 namespace 中无可用 kv_store | 回退为在本 namespace 新建 kv_store Pod |

复用时，P/D 引擎 Pod 的环境变量 `KVS_MASTER_SERVICE` 会指向目标 namespace 下的 kv_store 完整域名，例如：

`mindie-motor-kvs-master.service-a.svc.cluster.local`

（InferServiceSet 模式下 Service 名称会带 CRD 前缀，deployer 会自动按模板拼接。）

> **注意**
>
> - 两套服务的 `deploy_mode`（`multi_deployment` / `infer_service_set`）应保持一致，否则 Service 名称可能对不上，复用会失败并回退为新建。
> - InferServiceSet 模板中若无 `kv-store` role（未使用 KV 池化的精简模板），deployer 会跳过 kv_store 域名解析，不影响部署。
> - 使用 `--update_instance_num` 扩缩容时，multi_deployment 模式同样会解析 `target_job_id`，确保新扩容的 engine Pod 能连上正确的 kv_store。

### 使用 `UCMConnector`

UCM  属于 KV池化功能，但不复用 `AscendStoreConnector` 的 backend 机制，而是通过 `UCMConnector` 接入：

- Prefill 的 `connectors[0]` 是 Mooncake P/D 传输 Connector，`connectors[1]` 是 `UCMConnector`。
- `UCMConnector` 保持 `kv_role: "kv_both"`，UCM Store Pipeline 配置内联在其 `kv_connector_extra_config` 中。
- Decode 只配置与 Prefill 匹配的 Mooncake P/D 传输 Connector，不加载 `UCMConnector`。
- UCM 的 `store_pipeline`、`storage_backends` 和容量参数决定前缀缓存如何保存，不使用 `AscendStoreConnector.backend`。

当前 PyMotor UCM 样例仍配置了 `kv_cache_store_config.backend: "mooncake"`，用于当前 deployer 生成 Mooncake kv_store/master 资源；这是部署适配配置，不表示 UCM 变成了 Mooncake Backend。UCM 的实际 Store 由 `UCMConnector` 中的 `store_pipeline` 决定。完整配置、存储挂载、部署及验证步骤见 [在 PyMotor 中部署 UCM](backend/ucm.md)。

---

## 部署服务

在 `examples/deployer` 目录下通过 `deploy.py` 脚本部署服务：

```bash
cd examples/deployer

# 方式一：指定配置目录（推荐）
python deploy.py --config_dir ../infer_engines/vllm

# 方式二：单独指定配置文件
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```

完成后：

- 集群中会创建/更新 ConfigMap `motor-config`（内容来自当前输入的 `user_config.json`），后续扩缩容与刷新的基线。
- `output/deployment/` 下会生成各服务 YAML。
- 使用 `AscendStoreConnector` 时，deployer 根据 `kv_cache_store_config.backend` 拉起对应服务：Mooncake 使用 `mooncake_master`，MemCache 使用 MetaService，并按配置准备 LocalService。
- 启用 UCM  时，还需按 UCM 部署文档为 Prefill 安装 UCM、挂载 UCM Store 所需目录；当前样例同时生成 Mooncake kv_store/master 资源。

根据所用 Store Connector 和存储实现阅读对应文档：

| KV池化功能     | Store Connector | 存储实现 | 文档 |
|------------|-----------------|----------|------|
| 共享 KV Pool | `AscendStoreConnector` | `backend: "mooncake"` | [Mooncake Backend](backend/mooncake.md) |
| 共享 KV Pool | `AscendStoreConnector` | `backend: "memcache"` | [MemCache Backend](backend/memcache.md) |
| UCM        | `UCMConnector` | UCM Store Pipeline | [在 PyMotor 中部署 UCM](backend/ucm.md) |
| 共享 KV Pool | `AscendStoreConnector` | `backend: "yuanrong"` | TODO：后续版本支持 |

---

## 原理说明

KV池化通过 `MultiConnector` 组合传输 Connector 和 Store Connector。不同 Store Connector 都接入 vLLM 的 KV Cache 查询、加载和保存流程，但内部存储实现不同。

### `AscendStoreConnector`

1. P/D 实例都加载 `AscendStoreConnector`，P 侧以 `kv_producer` 写入 KV Pool，D 侧以 `kv_consumer` 查询并加载 KV Cache。
2. `AscendStoreConnector` 负责统一的匹配、加载和保存流程，其 `backend` 决定底层使用 MemCache 还是 Mooncake Store。
3. `kv_cache_store_config` 配置所选后端的服务地址、端口和运行参数；其中 MemCache 使用 MetaService，Mooncake 使用 `mooncake_master`。
4. `connectors[0]` 的 Mooncake 传输 Connector 负责 P/D 实时 KV 传输，与 `connectors[1]` 的共享 KV Pool 是不同职责。

### `UCMConnector`

1. 首次请求由 Prefill 计算 KV Cache，`UCMConnector` 按 `store_pipeline` 将可复用前缀写入 Cache、Posix 等存储层。
2. 后续请求出现相同前缀时，Prefill 通过 UCM 查询并加载已保存的 KV Cache，减少重复 Prefill 计算。
3. 本次请求的 P/D 实时 KV 传输仍由 Mooncake 传输 Connector 完成；当前分布式 PD 方案的 Decode 不加载 UCM。

## 常见问题

1. **服务启动后 P/D 实例间无法传输 KV Cache**

   请检查 `kv_role` 是否正确（P 为 `kv_producer`，D 为 `kv_consumer`）。

2. **P 实例推理性能下降**

   KV 池化开启后，P 实例需要额外将 KV Cache 推入缓存池，可能带来少量性能开销。可适当增大 `kv_parallel_size` 以提升传输效率。

3. **D 实例拉取 KV Cache 超时**

   检查 `env.json` 中 `ASCEND_CONNECT_TIMEOUT` 和 `ASCEND_TRANSFER_TIMEOUT` 是否足够大，以及 `default_kv_lease_ttl` 是否大于这两个超时时间。

4. **MemCache MetaService 启动失败**

   检查 `kv_cache_store_config` 中 `config_store_port` 和 `metrics_port` 是否被占用，以及 `POD_IP` 环境变量是否正确注入（由 `kv_store_template.yaml` 中 `fieldRef: status.podIP` 提供）。

5. **切换后端后配置未生效**

   `AscendStoreConnector` 和 `kv_cache_store_config` 中的 `backend` 必须保持一致。如果仅修改了一处，会导致后端不匹配。请确保两处 `backend` 值相同。

6. **为什么 UCM 样例中仍然有 `backend: "mooncake"`**

   这是当前 PyMotor deployer 用来生成 Mooncake kv_store/master 资源的配置，不是 UCM 的存储后端。UCM Store 应查看 `UCMConnector` 中的 `store_pipeline` 和 `storage_backends`。
