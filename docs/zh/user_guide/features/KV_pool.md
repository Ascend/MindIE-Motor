# KV池化能力部署

## 特性介绍

pyMotor KV池化能力基于vllm-ascend本身池化能力，能力介绍和环境依赖可参考[vllm-ascend池化文档](https://docs.vllm.ai/projects/ascend/zh-cn/main/user_guide/feature_guide/kv_pool.html)。

通过修改user_config.json配置文件后即可通过deploy.py脚本完成服务部署。

## 部署流程

pyMotor开启KV池化能力只需修改user_config.json配置文件后，通过deploy.py脚本即可完成服务部署，具体流程如下。
> 注意：开启池化能力前请参考[pyMotor快速开始](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/README.md)，确保环境能正常完成基础的服务部署。

### 应用补丁

> **【重要提示】**
> **仅当 `vllm-ascend` 版本早于 `v0.17.0rc2`（不含 `v0.17.0rc2`）时才需要打此补丁。**
> 如果您的 `vllm-ascend` 版本为 `v0.17.0rc2` 及以上，补丁已合入主干，**请直接跳过本节内容，无需进行打补丁操作**。

由于vllm代码的layerwise KV-cache传输叠加KV池化存在推理bug，需要应用vllm_multi_connector.patch补丁，具体操作步骤可参考[pyMotor应用补丁](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/patch/README.md)。

### 配置user_config.json

同[vllm-ascend池化文档](https://docs.vllm.ai/projects/ascend/zh-cn/main/user_guide/feature_guide/kv_pool.html)中kv-transfer-config配置，在user_config.json配置文件中只需要调整P/D实例 `kv_transfer_config` 内的配置以及 `kv_cache_pool_config` 配置。其他配置内容与不开启池化时保持一致即可。以[PyMotor快速开始](../quick_start.md)中实例user_config.json为参考基线，适配打开KV池化后的配置文件示例如下（省略了其他无关的配置项）：

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
    "engine_config": {
      "served_model_name": "...",
      "model": "...",
      "gpu_memory_utilization": 0.9,
      "data_parallel_size": 1,
      "tensor_parallel_size": 1,
      "pipeline_parallel_size": 1,
      "enable_expert_parallel": false,
      "data_parallel_rpc_port": 9000,
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
    "engine_config": {
      "served_model_name": "...",
      "model": "...",
      "gpu_memory_utilization": 0.9,
      "data_parallel_size": 1,
      "tensor_parallel_size": 1,
      "pipeline_parallel_size": 1,
      "enable_expert_parallel": false,
      "data_parallel_rpc_port": 9000,
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

说明：`kv_cache_pool_config` 为 KV 池化全局配置项，具体参数说明如下：

- `metadata_server`：元数据服务器模式，默认为 `P2PHANDSHAKE`（点对点握手模式）。
- `protocol`：底层传输协议，默认为 `ascend`。
- `device_name`：指定绑定的网卡名称，为空则自动选择。
- `global_segment_size`：全局共享显存段大小，默认为 `1GB`。
- `eviction_high_watermark_ratio` 与 `eviction_ratio`：用于 `mooncake_master` 进程启动参数，分别代表池化空间高水位驱逐线与单次驱逐比例。
- `port`：（可选）用于配置 KV Pool 的服务端口；若未配置，`deploy.py` 会按默认值 `50088` 进行补充和适配。
- `default_kv_lease_ttl`：（可选）控制 KV 对象的默认租约 TTL（毫秒）；配置值需大于`env.json`中vllm实例的环境变量`ASCEND_CONNECT_TIMEOUT`和`ASCEND_TRANSFER_TIMEOUT`。默认值11000。

> 关于 Connector：本例 `kv_connector` 使用 `MultiConnector`，其中 `connectors[0]`（`MooncakeLayerwiseConnector`，传输层）决定 P/D 协同 capability，`connectors[1]`（`AscendStoreConnector`，KV 池后端）不参与判定、无需在识别白名单中。识别白名单与 `dispatch_profile` 逃生口见 [PD 分离特性说明](../../design/pd_disaggregation.md#connector-驱动执行计划)。

### 部署服务

在 `examples/deployer` 目录下通过 deploy.py 脚本部署服务。支持指定配置目录或单独指定配置文件：

```bash
cd examples/deployer
# 方式一：指定配置目录（推荐）
python deploy.py --config_dir ../infer_engines/vllm

# 方式二：单独指定配置文件
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```
