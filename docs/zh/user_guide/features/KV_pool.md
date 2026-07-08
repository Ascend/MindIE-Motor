# KV池化能力部署

---

## 功能介绍

允许P/D实例通过KV缓存池共享KV Cache，P实例将计算好的KV Cache推入缓存池，D实例从缓存池拉取并复用，从而在PD分离场景下提升显存利用率和推理吞吐。

---

## 前置说明

- 必须已使用 motor 部署 PD 分离推理服务，KV 池化在该服务基础上开启，不会对 controller 和 coordinator 产生影响。
- KV 池化能力的约束条件，详情参考： [vllm-ascend kv_pool](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/kv_pool.html)。
- 开启池化能力前请先参考 [pyMotor 快速开始](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/README.md)，确保环境能正常完成基础的服务部署。
- **仅当 `vllm-ascend` 版本早于 `v0.17.0rc2`（不含 `v0.17.0rc2`）时需要打补丁**（见快速实践步骤二）；`v0.17.0rc2` 及以上版本请直接跳过补丁步骤。
- 后续所有操作只在 k8s 集群的管理节点（master 节点）执行。

---

## 快速实践

1. 已预先使用 motor 部署 PD 分离推理服务，且该服务正常运行。

2. （按需）应用补丁

   > **【重要提示】**
   > **仅当 `vllm-ascend` 版本早于 `v0.17.0rc2`（不含 `v0.17.0rc2`）时才需要打此补丁。**
   > 如果您的 `vllm-ascend` 版本为 `v0.17.0rc2` 及以上，补丁已合入主干，**请直接跳过本节内容，无需进行打补丁操作**。

   由于 vllm 代码的 layerwise KV-cache 传输叠加 KV 池化存在推理 bug，需要应用 `vllm_multi_connector.patch` 补丁，具体操作步骤可参考 [pyMotor 应用补丁](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/patch/README.md)。

3. 修改 `user_config.json` 配置文件

   在 `examples/infer_engines/vllm/user_config.json` 中，添加 `kv_transfer_config` 和 `kv_cache_pool_config` 配置项。具体配置格式参见下方[典型配置](#典型配置)章节。

   关键要点：
   - 注意 `motor_engine_prefill_config.engine_config.kv_transfer_config` 的配置方法有变化。
   - 新增 `kv_cache_pool_config` 全局配置，用于 KV 池化。
   - 其余配置项与不开启池化时保持一致。

4. 部署服务

   在 `examples/deployer` 目录下执行部署命令：

   ```bash
   cd examples/deployer
   # 方式一：指定配置目录（推荐）
   python deploy.py --config_dir ../infer_engines/vllm

   # 方式二：单独指定配置文件
   python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
   ```

5. 验证结果

   ```bash
   kubectl get pod -A -owide
   ```

   预期 P/D 实例启动成功，服务正常运行。

---

## 典型配置

### 1. 配置示例

以 [PyMotor 快速开始](../quick_start.md) 中的 `user_config.json` 为基线，开启 KV 池化后的完整配置示例如下：

```json
{
  "version": "v2.0",
  "motor_deploy_config": {
    "..."
  },
  "motor_controller_config": {
    "..."
  },
  "motor_coordinator_config": {
    "..."
  },
  "motor_nodemanger_config": {
    "..."
  },
  "motor_engine_prefill_config": {
    "engine_type": "vllm",
    "motor_nodemanger_config": {},
    "engine_config": {
      "..."
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
                "lookup_rpc_port": "0",
                "backend": "mooncake"
              }
            }
          ]
        }
      }
    }
  },
  "motor_engine_decode_config": {
    "engine_type": "vllm",
    "motor_nodemanger_config": {},
    "engine_config": {
      "..."
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
                "lookup_rpc_port": "1",
                "backend": "mooncake"
              }
            }
          ]
        }
      }
    }
  },
  "kv_cache_pool_config": {
    "metadata_server": "P2PHANDSHAKE",
    "protocol": "ascend",
    "device_name": "",
    "global_segment_size": "1GB",
    "eviction_high_watermark_ratio": 0.9,
    "eviction_ratio": 0.1
  }
}
```

### 2. 参数说明

各项参数功能说明：

**`kv_cache_pool_config`（KV 池化全局配置）**

| 配置项 | 取值类型 | 取值范围 | 配置说明 |
| --- | --- | --- | --- |
| **metadata_server** | string | `P2PHANDSHAKE` | 元数据服务器模式，默认为 `P2PHANDSHAKE`（点对点握手模式）。 |
| **protocol** | string | `ascend` / `rdma` / `tcp` | 底层传输协议，默认为 `ascend`。 |
| device_name | string | 网卡名称 | 指定绑定的网卡名称，为空则自动选择。 |
| **global_segment_size** | string | 如 `1GB`、`20GB` | 全局共享显存段大小，默认为 `1GB`。 |
| eviction_high_watermark_ratio | float | 0~1 | 池化空间高水位驱逐线，与 `eviction_ratio` 配合用于 `mooncake_master` 启动参数。 |
| eviction_ratio | float | 0~1 | 单次驱逐比例。 |
| port | int | 1024~65535 | （可选）KV Pool 服务端口；未配置时 `deploy.py` 默认补充为 `50088`。 |
| default_kv_lease_ttl | int | 毫秒 | （可选）KV 对象的默认租约 TTL（毫秒），需大于 `env.json` 中 `ASCEND_CONNECT_TIMEOUT` 和 `ASCEND_TRANSFER_TIMEOUT`，默认值 `11000`。 |

---

## 原理说明

### KV 池化整体流程

pyMotor KV 池化能力基于 vllm-ascend 的 Mooncake 传输层实现。整体流程如下：

1. **PreFill 阶段**：P 实例完成 PreFill 计算后，将 KV Cache 通过 `MooncakeLayerwiseConnector` 按 layer 粒度推入共享的 KV 缓存池。
2. **KV 缓存池管理**：`kv_cache_pool_config` 控制缓存池的元数据服务模式、传输协议、全局共享段大小及驱逐策略。缓存池在多个实例间共享显存资源，提升整体利用率。
3. **Decode 阶段**：D 实例从缓存池中拉取对应 sequence 的 KV Cache，直接用于 Decode 计算，无需重复计算。
4. **P/D 协同**：P 与 D 实例之间通过配置相同的 `kv_port` 和 `kv_connector` 建立连接，通过 `kv_role` 区分生产者/消费者角色。

> 关于 Connector 的更多原理，以及识别白名单与 `dispatch_profile` 逃生口，请参见 [PD 分离特性说明](../../design/pd_disaggregation.md#connector-驱动执行计划)。

### 部署流程

在 `examples/deployer` 目录下执行全量部署：

```bash
cd examples/deployer
python deploy.py --config_dir ../infer_engines/vllm
```

完成后：

- 集群中会创建/更新 ConfigMap `motor-config`（内容来自当前输入的 `user_config.json`），后续扩缩容与刷新的基线。
- `output/deployment/` 下会生成各服务 YAML。
- P 与 D 实例会基于 `kv_cache_pool_config` 自动拉起 `mooncake_master` 进程，管理共享显存池。

### 关键配置调优建议

- **`global_segment_size`**：根据模型大小和并发量调整，过小会导致频繁驱逐；过大则浪费显存。建议设为模型 KV Cache 预估大小的 1.5~2 倍。
- **`eviction_high_watermark_ratio`** 与 **`eviction_ratio`**：当池化空间使用率达到 `eviction_high_watermark_ratio` 时触发驱逐，每次驱逐 `eviction_ratio` 比例的空间。高并发场景可适度降低驱逐比例以减少抖动。
- **`default_kv_lease_ttl`**：控制 KV 对象的租约有效期，需确保大于传输超时时间（`ASCEND_CONNECT_TIMEOUT` / `ASCEND_TRANSFER_TIMEOUT`），避免租约在传输完成前过期。

---

## 常见问题

1. **服务启动后 P/D 实例间无法传输 KV Cache**

   请检查 `kv_role` 是否正确（P 为 `kv_producer`，D 为 `kv_consumer`）。

2. **P 实例推理性能下降**

   KV 池化开启后，P 实例需要额外将 KV Cache 推入缓存池，可能带来少量性能开销。可适当增大 `kv_parallel_size` 以提升传输效率。

3. **D 实例拉取 KV Cache 超时**

   检查 `env.json` 中 `ASCEND_CONNECT_TIMEOUT` 和 `ASCEND_TRANSFER_TIMEOUT` 是否足够大，以及 `default_kv_lease_ttl` 是否大于这两个超时时间。
