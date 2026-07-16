# KV池化能力部署

## 功能介绍

允许P/D实例通过KV缓存池共享KV Cache，P实例将计算好的KV Cache推入缓存池，D实例从缓存池拉取并复用，从而在PD分离场景下提升显存利用率和推理吞吐。

MindIE Motor KV池化能力基于vllm-ascend本身池化能力，能力介绍和环境依赖可参考[vllm-ascend池化文档](https://docs.vllm.ai/projects/ascend/zh-cn/main/user_guide/feature_guide/kv_pool.html)。

通过修改`user_config.json`配置文件后即可通过`deploy.py`脚本完成服务部署。

## 前置说明

- 必须已使用 motor 部署 PD 分离推理服务，KV 池化在该服务基础上开启，不会对 controller 和 coordinator 产生影响。
- KV 池化能力的约束条件，详情参考：[vllm-ascend kv_pool](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/kv_pool.html)。
- 开启池化能力前请先参考[MindIE Motor快速开始](../../quick_start.md)，确保环境能正常完成基础的PD分离服务部署。
- **仅当 `vllm-ascend` 版本早于 `v0.17.0rc2`（不含 `v0.17.0rc2`）时才需要打补丁**（见下方应用补丁章节）；`v0.17.0rc2` 及以上版本请直接跳过补丁步骤。
- 后续所有操作只在 k8s 集群的管理节点（master 节点）执行。

## 应用补丁

> **【重要提示】**
> **仅当 `vllm-ascend` 版本早于 `v0.17.0rc2`（不含 `v0.17.0rc2`）时才需要打此补丁。**
> 如果您的 `vllm-ascend` 版本为 `v0.17.0rc2` 及以上，补丁已合入主干，**请直接跳过本节内容，无需进行打补丁操作**。

由于vllm代码的layerwise KV-cache传输叠加KV池化存在推理bug，需要应用vllm_multi_connector.patch补丁，具体操作步骤可参考[MindIE Motor应用补丁](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/patch/README.md)。

## 配置 user_config.json

MindIE Motor开启KV池化能力只需修改`user_config.json`配置文件，其余配置项与不开启池化时保持一致即可。需要关注以下两处配置。

> 注意：开启池化能力前请参考[MindIE Motor快速开始](../../quick_start.md)，确保环境能正常完成基础的PD分离服务部署。

### 1. kv_transfer_config（P/D 实例 engine_config 内）

池化通过 `MultiConnector` 组合传输连接器（`connectors[0]`）与池化后端连接器（`connectors[1]`）实现。以 `MooncakeLayerwiseConnector`（layerwise P/D 协同）+ `AscendStoreConnector`（KV 池后端）为例：

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
        "use_layerwise": true,
        "connectors": [
          {
            "kv_connector": "MooncakeLayerwiseConnector",
            "kv_role": "kv_producer",
            "kv_port": "30001",
            "kv_connector_extra_config": {
              "send_type": "PUT"
            }
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
        "use_layerwise": true,
        "connectors": [
          {
            "kv_connector": "MooncakeLayerwiseConnector",
            "kv_role": "kv_consumer",
            "kv_port": "30001",
            "kv_connector_extra_config": {
              "send_type": "PUT"
            }
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

其中 `AscendStoreConnector` 的 `backend` 字段决定使用的池化后端。各后端之间其余结构完全相同，**仅 `backend` 取值不同**：

| 池化后端 | `backend` 值 | 说明 |
|----------|-------------|------|
| [Mooncake](backend/mooncake.md) | `mooncake` | 天然支持，无需额外安装 |
| [MemCache](backend/memcache.md) | `memcache` | 默认后端，天然支持，无需额外安装 |
| Yuanrong | `yuanrong` | TODO：后续版本支持 |

> 关于 Connector 的更多原理，以及识别白名单与 `dispatch_profile` 逃生口，请参见 [PD 分离特性说明](../../../design/pd_disaggregation.md#connector-驱动执行计划)。

### 2. kv_cache_store_config（全局配置）

`kv_cache_store_config` 为 KV 池化全局配置，P/D 实例共享（以默认后端 MemCache 为例）：

```json
"kv_cache_store_config": {
  "backend": "memcache",
  "local_service_mode": "standalone",
  "dram_size": "100GB"
}
```

`backend` 决定池化后端，需与 `AscendStoreConnector` 中的 `backend` 保持一致。各后端参数说明如下：

**通用参数**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `backend` | string | `memcache` | 池化后端：`mooncake`、`memcache`；未配置时默认 `memcache` |
| `metadata_server` | string | `P2PHANDSHAKE` | 元数据服务器模式，默认为点对点握手模式 |
| `protocol` | string | `ascend` | 底层传输协议 |
| `device_name` | string | `""` | 指定绑定的网卡名称，为空则自动选择 |
| `global_segment_size` | string | `1GB` | 全局共享显存段大小 |
| `port` | int（可选） | `50088` | KV Pool 服务端口；未配置时 deploy.py 将按默认值补齐 |
| `default_kv_lease_ttl` | int（可选） | `11000` | KV 对象默认租约 TTL（毫秒）；配置值需大于 `env.json` 中 vllm 实例的 `ASCEND_CONNECT_TIMEOUT` 和 `ASCEND_TRANSFER_TIMEOUT` |

**Mooncake 专属参数**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `eviction_high_watermark_ratio` | float | 0.9 | 池化空间高水位驱逐线，传递给 `mooncake_master` 进程 |
| `eviction_ratio` | float | 0.1 | 单次驱逐比例，传递给 `mooncake_master` 进程 |

**MemCache 专属参数**

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `config_store_port` | int（可选） | `50089` | MemCache MetaService 配置存储端口 |
| `metrics_port` | int（可选） | `50090` | MemCache MetaService 监控上报端口 |
| `local_service_mode` | string（可选） | A2：`inprocess`，A3/A5：`standalone` | LocalService 部署模式：`inprocess`（与 vLLM 同进程）或 `standalone`（独立进程） |
| `dram_size` | string（可选） | `"10GB"` | **每个节点**贡献给 KV 池化的 DRAM 总内存大小。`inprocess` 模式下 daemon 会自动除以本节点 DP 数得到单进程 `dram.size`；`standalone` 模式下独立 LS 直接使用该值。格式如 `"100GB"`。默认 10GB，后续版本将通过 `memfabric_hybrid.mem_scan.stat()` 自动扫描 |

> 使用 MemCache 后端时，deploy.py 会自动启动 MemCache MetaService（对标 Mooncake 的 `mooncake_master`），无需手动干预。

---

## 部署服务

在 `examples/deployer` 目录下通过 deploy.py 脚本部署服务：

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
- P 与 D 实例会根据 `kv_cache_store_config` 自动拉起对应后端的 master 进程（Mooncake 后端为 `mooncake_master`，MemCache 后端为 MetaService），管理共享显存池。

默认使用 MemCache 后端，无需额外操作即可直接部署。如果需要使用其他后端，请参考下方对应文档完成安装，再将 `AscendStoreConnector` 和 `kv_cache_store_config` 中的 `backend` 替换为对应的值：

| 后端 | 文档 |
|------|------|
| Mooncake | [backend/mooncake.md](backend/mooncake.md) |
| MemCache | [backend/memcache.md](backend/memcache.md) |
| Yuanrong | TODO：后续版本支持 |

---

## 原理说明

### KV 池化整体流程

MindIE Motor KV 池化能力基于 vllm-ascend 的 KV 传输层实现。整体流程如下：

1. **PreFill 阶段**：P 实例完成 PreFill 计算后，将 KV Cache 通过 `MooncakeLayerwiseConnector` 按 layer 粒度推入共享的 KV 缓存池。
2. **KV 缓存池管理**：`kv_cache_store_config` 控制缓存池的元数据服务模式、传输协议、全局共享段大小及驱逐策略。缓存池在多个实例间共享显存资源，提升整体利用率。
3. **Decode 阶段**：D 实例从缓存池中拉取对应 sequence 的 KV Cache，直接用于 Decode 计算，无需重复计算。
4. **P/D 协同**：P 与 D 实例之间通过配置相同的 `kv_port` 和 `kv_connector` 建立连接，通过 `kv_role` 区分生产者/消费者角色。

池化后端通过 `AscendStoreConnector` 的 `backend` 字段切换。MemCache 后端由 deployer 自动拉起 MetaService 进程管理缓存池元数据，Mooncake 后端则使用 `mooncake_master` 进程。各后端的详细说明见对应的后端文档。

### 部署流程

在 `examples/deployer` 目录下执行全量部署：

```bash
cd examples/deployer
python deploy.py --config_dir ../infer_engines/vllm
```

完成后：

- 集群中会创建/更新 ConfigMap `motor-config`（内容来自当前输入的 `user_config.json`），后续扩缩容与刷新的基线。
- `output/deployment/` 下会生成各服务 YAML。
- P 与 D 实例会根据 `kv_cache_store_config` 自动拉起对应后端的 master 进程，管理共享显存池。

### 关键配置调优建议

- **`global_segment_size`**：根据模型大小和并发量调整，过小会导致频繁驱逐；过大则浪费显存。建议设为模型 KV Cache 预估大小的 1.5~2 倍。
- **`eviction_high_watermark_ratio`** 与 **`eviction_ratio`**（Mooncake 专属）：当池化空间使用率达到 `eviction_high_watermark_ratio` 时触发驱逐，每次驱逐 `eviction_ratio` 比例的空间。高并发场景可适度降低驱逐比例以减少抖动。
- **`default_kv_lease_ttl`**：控制 KV 对象的租约有效期，需确保大于传输超时时间（`ASCEND_CONNECT_TIMEOUT` / `ASCEND_TRANSFER_TIMEOUT`），避免租约在传输完成前过期。

---

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
