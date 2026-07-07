# KV Cache 亲和性调度能力部署

## 特性介绍

PyMotor KV Cache 亲和性调度依赖自研 **kv-conductor** 组件——一个 Rust 实现的轻量级 KV cache
索引服务。它通过 ZMQ SUB 订阅引擎的 KV 事件，维护 radix 前缀树，回答缓存命中查询，
从而将请求路由到已缓存最长 token 前缀的 Worker。

组件源码和构建说明见 [kv-conductor README](../../../../kv-conductor/README.md)。

## 镜像准备

kv-conductor 需要编译 Rust 二进制并打包进 Docker 镜像。详见
[kv-conductor README - Docker](../../../../kv-conductor/README.md#docker)。

```bash
cd kv-conductor
cargo build --release --features zmq
docker build -t kv-conductor:latest .
```

对于 e2e mock 测试环境，使用 `mock/conductor_cli.sh`：

```bash
cd kv-conductor
./mock/conductor_cli.sh build    # 构建 kv-conductor + zmq-publisher 镜像
./mock/conductor_cli.sh up       # 部署单端口模式（N 个 Publisher）
./mock/conductor_cli.sh up-multi # 部署多端口模式（XPU + CPU/DISK 双端口）
```

## 配置

### user_config.json

Coordinator 的 `motor_coordinator_config.scheduler_config` 中通过 `kv_conductor_config`
子配置统一管理 kv-conductor 的连接、注册、查询、重注册参数。引擎侧的 `kv-events-config`（vLLM publisher 配置）
与 `kv_transfer_config`（KV 传输配置）一并放在 `engine_config` 中：

```json
{
  "version": "v2.0",
  "motor_deploy_config": {
    "p_instances_num": 1,
    "d_instances_num": 1,
    "single_p_instance_pod_num": 1,
    "single_d_instance_pod_num": 1,
    "p_pod_npu_num": 4,
    "d_pod_npu_num": 4,
    "image_name": "mindie-motor-vllm:latest",
    "job_id": "mindie-motor",
    "hardware_type": "800I_A2",
    "weight_mount_path": "/mnt/weight/"
  },
  "motor_coordinator_config": {
    "scheduler_config": {
      "scheduler_type": "kv_cache_affinity",
      "kv_conductor_config": {
        "store_backend": "Mooncake",
        "block_size": 128,
        "pool_endpoint": "tcp://kvp-master:5557",
        "xpu_endpoint": "tcp://*:50090"
      }
    }
  },
  "motor_engine_prefill_config": {
    "engine_type": "vllm",
    "engine_config": {
      "served_model_name": "qwen3-8B",
      "model": "/mnt/weight/qwen3_8B",
      "data_parallel_size": 2,
      "tensor_parallel_size": 2,
      "pipeline_parallel_size": 1,
      "max_model_len": 2048,
      "enforce-eager": true,
      "kv-events-config": {
        "publisher": "zmq",
        "enable_kv_cache_events": true,
        "endpoint": "tcp://*:5557",
        "topic": "kv-events"
      },
      "kv-transfer-config": {
        "kv_connector": "MultiConnector",
        "kv_role": "kv_producer",
        "kv_connector_extra_config": {
          "connectors": [
            {
              "kv_connector": "MooncakeLayerwiseConnector",
              "kv_role": "kv_producer",
              "kv_port": "20001",
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
      "served_model_name": "qwen3-8B",
      "model": "/mnt/weight/qwen3_8B",
      "data_parallel_size": 2,
      "tensor_parallel_size": 2,
      "max_model_len": 2048,
      "kv-transfer-config": {
        "kv_connector": "MultiConnector",
        "kv_role": "kv_consumer",
        "kv_connector_extra_config": {
          "connectors": [
            {
              "kv_connector": "MooncakeLayerwiseConnector",
              "kv_role": "kv_consumer",
              "kv_port": "20002",
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
  },
  "kv_conductor_config": {
    "http_server_port": 13333
  }
}
```

### 配置说明

| 字段 | 层级 | 说明 |
|------|------|------|
| `scheduler_type` | `scheduler_config` | 设为 `"kv_cache_affinity"` 启用亲和性调度 |
| `kv_conductor_config` | `scheduler_config` | kv-conductor 连接、注册、查询、重注册配置（Coordinator → kv-conductor） |
| `kv_conductor_config.store_backend` | 子配置 | 池化后端类型：`"Mooncake"` / `"Memcache"` / `"YuanRong"` |
| `kv_conductor_config.block_size` | 子配置 | 事件广播 hash 粒度（token 数）。标准模型等于引擎 `--block-size`（默认 128）；DeepSeek V4 等混合模型需设为引擎各 KV group block_size 的 GCD（如 4）。见下方说明 |
| `kv_conductor_config.pool_endpoint` | 子配置 | 中心化后端的池服务地址 |
| `kv_conductor_config.xpu_endpoint` | 子配置 | Per-DP HBM 端口模式 |
| `kv_conductor_config.cpu_endpoint` | 子配置 | Per-DP CPU/DDR 端口模式（YuanRong 等多介质后端） |
| `kv_conductor_config.disk_endpoint` | 子配置 | Per-DP DISK/SSD 端口模式（YuanRong 等多介质后端） |
| `kv_conductor_config.replay_endpoint` | 子配置 | Per-DP replay 端口模式，conductor 重启恢复时回放缓冲的 KV 事件 |
| `kv_conductor_config.re_register_interval_sec` | 子配置 | 周期性重注册间隔（秒）。0 或负数禁用定时重注册（默认 0） |
| `kv-events-config` | `engine_config` | vLLM KV 事件发布配置（引擎侧，publisher 设置） |
| `kv-transfer-config` | `engine_config` | KV 传输配置（Mooncake / AscendStore connector） |
| `kv_conductor_config.http_server_port` | 顶层 | kv-conductor HTTP 端口，默认 13333 |

**三种后端的 `kv_conductor_config` 配置示例：**

Mooncake（中心化 pool + per-DP HBM）：

```json
"kv_conductor_config": {
  "store_backend": "Mooncake",
  "pool_endpoint": "tcp://kvp-master:5557",
  "xpu_endpoint": "tcp://*:50090"
}
```

YuanRong（per-DP 多端口）：

```json
"kv_conductor_config": {
  "store_backend": "YuanRong",
  "block_size": 128,
  "xpu_endpoint": "tcp://*:15557",
  "cpu_endpoint": "tcp://*:15558",
  "disk_endpoint": "tcp://*:15558"
}
```

Memcache（中心化 pool + 精确 dp_rank）：

```json
"kv_conductor_config": {
  "store_backend": "Memcache",
  "pool_endpoint": "tcp://kvp-master:5557",
  "xpu_endpoint": "tcp://*:50090"
}
```

> **注意**：`kv-events-config` 是 vLLM 原生配置，用于控制引擎侧的 KV 事件**发布**行为。
> `kv_conductor_config` 是 Motor 配置，用于 Coordinator 向 kv-conductor **注册**。
> 两者分离，互不干扰——vLLM 不会解析 `kv_conductor_config`，Coordinator 也不会
> 把 `kv-events-config` 发给 conductor。

### DeepSeek V4 / 混合 KV Cache 模型

DeepSeek V4 开启了 `--no-disable-hybrid-kv-cache-manager`，引擎内部有**多种 KV cache group**，每种 block_size 不同：

| KV Group | block_size | 说明 |
|----------|-----------|------|
| Full MLA | 128（随 `--block-size`） | 全注意力层 |
| SWA MLA  | 64 | Sliding Window MLA |
| C128 状态 | 8 | 压缩 KV 状态 |
| C4 状态   | 4 | 压缩 KV 状态 |

引擎内部用 `hash_block_size = GCD([128, 64, 8, 4]) = 4` 计算事件的 block hashes。
因此 `kv_conductor_config.block_size` **必须设 4**，不能是 128：

```json
"kv_conductor_config": {
  "store_backend": "Mooncake",
  "block_size": 4,
  "pool_endpoint": "tcp://kvp-master:5557",
  "xpu_endpoint": "tcp://*:50090"
}
```

否则 conductor 查询时用 128 粒度的 hash 去匹配引擎用 4 粒度存的 hash，永远命中不了。

引擎启动日志中会打印实际采用的 hash_block_size，可以据此确认：

```text
# vLLM 日志输出示例
hash_block_size = 4
```

## 部署流程

```bash
cd examples/deployer
python deploy.py --config_dir ../infer_engines/vllm
```

执行成功后显示 `... all deploy end.`。

## PD 混部场景

PD 混部使用 `motor_engine_union_config`，Coordinator 自动从 union 段读取配置：

```json
{
  "motor_coordinator_config": {
    "scheduler_config": {
      "deploy_mode": "single_node",
      "scheduler_type": "kv_cache_affinity",
      "kv_conductor_config": {
        "store_backend": "Mooncake",
        "block_size": 128,
        "pool_endpoint": "tcp://kvp-master:5557",
        "xpu_endpoint": "tcp://*:50090"
      }
    }
  },
  "motor_engine_union_config": {
    "engine_type": "vllm",
    "engine_config": {
      "served_model_name": "qwen3",
      "model": "/mnt/weight/Qwen3-0.6B/",
      "max_model_len": 10000,
      "kv-events-config": {
        "publisher": "zmq",
        "enable_kv_cache_events": true,
        "endpoint": "tcp://*:5557",
        "topic": "kv-events"
      },
      "kv-transfer-config": {
        "kv_connector": "AscendStoreConnector",
        "kv_role": "kv_both",
        "kv_connector_extra_config": {
          "register_buffer": true,
          "mooncake_rpc_port": "0"
        }
      }
    }
  }
}
```

部署说明见 [PD 混部服务部署](../deployment/k8s/pd_aggregation_deployment.md)。

## e2e Mock 测试

kv-conductor 项目提供完整的 mock 测试工具链，无需真实引擎即可验证
多端口订阅、事件注入、前缀树查询等完整链路。

```bash
cd kv-conductor

# 单端口压测（8 个 Publisher, dp_rank 0~7）
./mock/conductor_cli.sh build && ./mock/conductor_cli.sh up
kubectl -n mindie-motor port-forward deploy/mindie-motor-kv-conductor 13333:13333 &
./mock/conductor_cli.sh bench --count 100 --throughput

# 多端口测试（XPU:15557 + CPU/DISK:15558）
./mock/conductor_cli.sh build && ./mock/conductor_cli.sh up-multi
./mock/conductor_cli.sh quick-multi
```

详见 [mock/README.md](../../../../kv-conductor/mock/README.md)。
