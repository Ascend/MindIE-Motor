# KV Cache亲和性调度能力部署

## 特性介绍

PyMotor KV Cache亲和性调度能力依赖Mooncake社区的Mooncake conductor组件，相关能力和接口的介绍可参考[Mooncake Conductor介绍文档](https://github.com/yejj710/Mooncake/blob/6dca8cc76ce074fa9c41f02e9a2195c7c1c9308f/docs/source/design/conductor/indexer-api-design.md)。

通过修改user_config.json配置文件后即可通过deploy.py脚本完成服务部署。

## 镜像准备

由于当前Mooncake Conductor组件相关代码还未上库主线分支，当前镜像中不含Mooncake Conductor，需要基于镜像额外安装Mooncake Conductor服务组件。安装方法如下：

1. 使用以下命令启动容器。

   ```bash

   docker run -it --name mooncake_patch --privileged=true --net=host --shm-size=128g <commit ID> bash
   # 需要替换基础镜像的commit ID

   ```

2. go环境准备。
   * 下载golang安装文件。

      ```bash

      wget https://mirrors.aliyun.com/golang/go1.23.8.linux-arm64.tar.gz
      tar -C /usr/local -xzf go1.23.8.linux-arm64.tar.gz
      echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc

      ```

   * golang环境变量设置。

      ```bash

      go env -w GOSUMDB=off # 不验证CA证书
      go env -w GOPROXY=direct # 直接访问github拉取

      ```

3. 下载libzmq相关依赖。

   ```bash

   #ubuntu:
   apt update
   apt install libzmq5 libzmq3-dev

   ```

   ```bash

   #openeuler
   dnf install zeromq zeromq-devel

   ```

4. 下载mooncake 源码并编译mooncake_conductor。

   ```bash

   git clone https://github.com/kvcache-ai/Mooncake.git -b dev/kv-indexer
   cd Mooncake/mooncake-conductor/conductor-ctrl/
   go mod tidy
   go build -o mooncake_conductor main.go
   mv mooncake_conductor /usr/local/bin/

   ```

5. 使用以下命令保存镜像。

   ```bash

   docker commit -a "add Mooncake Conductor" mooncake_patch mindie-motor-vllm:dev-26.0.0.B060-800I-A3-py311-Ubuntu24.04-lts-aarch64-patch

   ```

## 部署流程

PyMotor开启KV Cache亲和性调度能力只需修改user_config.json配置文件后，通过deploy.py脚本即可完成服务部署，具体流程如下。

### 注意

开启KV Cache亲和性调度能力前请参考[PyMotor快速开始](../quick_start.md)，确保环境能正常完成基础的服务部署。

### 配置user_config.json

参考[vllm kv_events文档](https://docs.vllm.ai/en/stable/api/vllm/config/kv_events/)中kv-events-config配置，在user_config.json配置文件中需要在P实例中增加kv-events-config配置，以[PyMotor快速开始](../quick_start.md)中实例user_config.json为参考基线，适配打开KV Cache亲和性调度能力的配置文件示例如下

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
    "image_name": "",
    "job_id": "mindie-motor",
    "hardware_type": "800I_A2",
    "weight_mount_path": "/mnt/weight/"
  },
  "motor_controller_config": {
  },
  "motor_coordinator_config": {
    "scheduler_config": {
      "scheduler_type": "kv_cache_affinity"
    }
  },
  "motor_nodemanger_config": {
  },
  "motor_engine_prefill_config": {
    "engine_type": "vllm",
    "engine_config": {
      "served_model_name": "qwen3-8B",
      "model": "/mnt/weight/qwen3_8B",
      "gpu_memory_utilization": 0.9,
      "data_parallel_size": 2,
      "tensor_parallel_size": 2,
      "pipeline_parallel_size": 1,
      "enable_expert_parallel": false,
      "data_parallel_rpc_port": 9000,
      "kv-events-config": {
        "publisher": "zmq",
        "enable_kv_cache_events": true,
        "endpoint": "tcp://*:5557",
        "topic": "kv-events",
        "replay_endpoint": "tcp://*:6667"
      },
      "enable-prefix-caching": true,
      "api-server-count": 1,
      "enforce-eager": true,
      "max_model_len": 2048,
      "kv_transfer_config": {
        "kv_connector": "MultiConnector",
        "kv_role": "kv_producer",
        "kv_connector_extra_config": {
          "use_layerwise": false,
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
      "gpu_memory_utilization": 0.9,
      "data_parallel_size": 2,
      "tensor_parallel_size": 2,
      "pipeline_parallel_size": 1,
      "enable_expert_parallel": false,
      "data_parallel_rpc_port": 9000,
      "enable-prefix-caching": true,
      "api-server-count": 1,
      "max_model_len": 2048,
      "kv_transfer_config": {
        "kv_connector": "MultiConnector",
        "kv_role": "kv_consumer",
        "kv_connector_extra_config": {
          "use_layerwise": false,
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
    "kvevent_instance": {
      "mooncake_master": {
          "type": "Mooncake"
      }
    },
    "http_server_port": 13333
  }
}
```

说明：

* 在`motor_coordinator_config`配置中，`scheduler_config`下的`scheduler_type`配置为`kv_cache_affinity`表示采用KV Cache亲和性调度算法进行调度。
* 在`motor_engine_prefill_config`配置中，`engine_config`下增加`kv-events-config`配置，表示P实例开启KV Cache事件发布能力。
* `kv_conductor_config` 中的 `http_server_port` 字段（例如 `13333`）用于配置 KV conductor的服务端口；若未配置，`deploy.py` 会按默认值 `13333` 进行补充和适配。
* 关于 Connector：本例 `kv_connector` 使用 `MultiConnector`，其中 `connectors[0]`（`MooncakeLayerwiseConnector`，传输层）决定 P/D 协同 capability，`connectors[1]`（`AscendStoreConnector`，KV 池后端）不参与判定、无需在识别白名单中。识别白名单与 `dispatch_profile` 逃生口见 [PD 分离特性说明](../../design/pd_disaggregation.md#connector-驱动执行计划)。

### PD 混部场景

以 `examples/infer_engines/vllm/pd_hybrid/user_config.json` 为参考基线，在同一配置文件中适配 KV Cache 亲和性调度能力。PD 混部不使用 `motor_engine_prefill_config`，应将 `kv-events-config` 与 `enable-prefix-caching` 配置在 `motor_engine_union_config.engine_config` 中；Coordinator 启动时会从 union 引擎段自动合并 `prefill_kv_event_config`（`endpoint`、`replay_endpoint`、`model_path` 等），无需手写 prefill 段配置。

配置文件示例如下：

```json
{
  "version": "v2.0",
  "motor_deploy_config": {
    "deploy_mode": "infer_service_set",
    "hybrid_instances_num": 1,
    "single_hybrid_instance_pod_num": 1,
    "hybrid_pod_npu_num": 2,
    "image_name": "",
    "job_id": "mindie-motor",
    "hardware_type": "800I_A3",
    "weight_mount_path": "/mnt/weight/"
  },
  "motor_controller_config": {},
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
      "served_model_name": "qwen3",
      "model": "/mnt/weight/Qwen3-0.6B/",
      "gpu_memory_utilization": 0.9,
      "data_parallel_size": 1,
      "tensor_parallel_size": 1,
      "pipeline_parallel_size": 1,
      "data_parallel_rpc_port": 9000,
      "enable_expert_parallel": false,
      "max_model_len": 10000,
      "max-num-batched-tokens": 9000,
      "max-num-seqs": 4,
      "seed": 1024,
      "async-scheduling": true,
      "trust-remote-code": true,
      "kv-events-config": {
        "publisher": "zmq",
        "enable_kv_cache_events": true,
        "endpoint": "tcp://*:5557",
        "topic": "kv-events",
        "replay_endpoint": "tcp://*:6667"
      },
      "kv-transfer-config": {
        "kv_connector": "AscendStoreConnector",
        "kv_role": "kv_both",
        "kv_connector_extra_config": {
          "register_buffer": true,
          "use_layerwise": false,
          "mooncake_rpc_port": "0"
        }
      }
    }
  },
  "kv_cache_pool_config": {
    "metadata_server": "P2PHANDSHAKE",
    "protocol": "ascend",
    "device_name": "",
    "global_segment_size": "10GB",
    "eviction_high_watermark_ratio": 0.9,
    "eviction_ratio": 0.1
  },
  "kv_conductor_config": {
    "kvevent_instance": {
      "mooncake_master": {
        "type": "Mooncake"
      }
    },
    "http_server_port": 13333
  }
}
```

部署说明见 [PD 混部服务部署](../deployment/k8s/pd_aggregation_deployment.md)。

### 部署服务

在 `examples/deployer` 目录下通过 deploy.py 脚本部署服务。支持指定配置目录或单独指定配置文件：

```bash
cd examples/deployer
# PD 分离：指定配置目录（推荐）
python deploy.py --config_dir ../infer_engines/vllm

# PD 分离：单独指定配置文件
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json

# PD 混部：在同一 user_config.json 中完成 KV 亲和适配后部署
python deploy.py --config_dir ../infer_engines/vllm/pd_hybrid
```

执行后看到如下内容，说明执行成功：

```bash
...... all deploy end.
```
