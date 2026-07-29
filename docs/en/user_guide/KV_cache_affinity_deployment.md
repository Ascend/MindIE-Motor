# KV Cache Affinity Scheduling Capability Deployment

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:05:49.988Z pushedAt=2026-07-01T02:56:50.466Z -->

## Feature Introduction

The PyMotor KV Cache affinity scheduling capability depends on the Mooncake Conductor component from the Mooncake community. For an introduction to related capabilities and interfaces, see [Mooncake Conductor Introduction Document](https://github.com/yejj710/Mooncake/blob/6dca8cc76ce074fa9c41f02e9a2195c7c1c9308f/docs/source/design/conductor/indexer-api-design.md).

After modifying the `user_config.json` configuration file, you can complete service deployment using the `deploy.py` script.

## Image Preparation

Since the code related to the Mooncake Conductor component has not yet been merged into the main branch, the current image does not include Mooncake Conductor. You need to install the Mooncake Conductor service component separately based on the image. The installation method is as follows.

1. Use the following command to start the container.

   ```bash

   docker run -it --name mooncake_patch --privileged=true --net=host --shm-size=128g <commit ID> bash
   # Replace the commit ID of the base image

   ```

2. Prepare the Go environment.

   * Download the Go installation file.

      ```bash

      wget https://mirrors.aliyun.com/golang/go1.23.8.linux-arm64.tar.gz
      tar -C /usr/local -xzf go1.23.8.linux-arm64.tar.gz
      echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc

      ```

   * Set golang environment variables.

      ```bash

      go env -w GOSUMDB=off # Do not verify CA certificate
      go env -w GOPROXY=direct # Directly access GitHub to pull

      ```

3. Download libzmq-related dependencies.

   ```bash

   #ubuntu:
   apt update
   apt install libzmq5 libzmq3-dev

   ```

   ```bash

   #openeuler
   dnf install zeromq zeromq-devel

   ```

4. Download the Mooncake source code and compile mooncake_conductor.

   ```bash

   git clone https://github.com/kvcache-ai/Mooncake.git -b dev/kv-indexer
   cd Mooncake/mooncake-conductor/conductor-ctrl/
   go mod tidy
   go build -o mooncake_conductor main.go
   mv mooncake_conductor /usr/local/bin/

   ```

5. Save the image using the following command.

   ```bash

   docker commit -a "add Mooncake Conductor" mooncake_patch mindie-motor-vllm:dev-26.0.0.B060-800I-A3-py311-Ubuntu24.04-lts-aarch64-patch

   ```

## Deployment Process

To enable the KV Cache affinity tuning capability in PyMotor, you only need to modify the `user_config.json` configuration file and then run the `deploy.py` script to complete service deployment. The specific process is as follows.

### NOTE

Before enabling the KV Cache affinity tuning capability, refer to [PyMotor Quick Start](../user_guide/quick_start.md) to ensure that the environment is properly set up for basic service deployment.

### Configuring `user_config.json`

Refer to the `kv-events-config` configuration in the [vllm kv_events documentation](https://docs.vllm.ai/en/stable/api/vllm/config/kv_events/). In the `user_config.json` configuration file, add the `kv-events-config` configuration to the P instance. Using the instance `user_config.json` in [PyMotor Quick Start](../user_guide/quick_start.md) as the reference baseline, an example of the configuration file adapted to enable the KV Cache affinity scheduling capability is as follows:

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
    "model_config": {
      "model_name": "qwen3-8B",
      "model_path": "/mnt/weight/qwen3_8B",
      "npu_mem_utils": 0.9,
      "prefill_parallel_config": {
        "dp_size": 2,
        "tp_size": 2,
        "pp_size": 1,
        "enable_ep": false,
        "dp_rpc_port": 9000,
        "world_size": 4
      }
    },
    "engine_config": {
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
    "model_config": {
      "model_name": "qwen3-8B",
      "model_path": "/mnt/weight/qwen3_8B",
      "npu_mem_utils": 0.9,
      "decode_parallel_config": {
        "dp_size": 2,
        "tp_size": 2,
        "pp_size": 1,
        "enable_ep": false,
        "dp_rpc_port": 9000,
        "world_size": 4
      }
    },
    "engine_config": {
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

NOTE

* In the `motor_coordinator_config` configuration, setting `scheduler_type` under `scheduler_config` to `kv_cache_affinity` indicates that the KV Cache affinity scheduling algorithm is used for scheduling.

* In the `motor_engine_prefill_config` configuration, adding the `kv-events-config` configuration under `engine_config` indicates that the KV Cache event publishing capability is enabled for P instances.

* The `http_server_port` field in `kv_conductor_config` (for example, `13333`) is used to configure the service port of the KV conductor. If not configured, `deploy.py` will supplement and adapt it with the default value `13333`.

### Deploying the Service

Deploy the service using the `deploy.py` script in the `examples/deployer` directory. You can specify a configuration directory or specify configuration files individually:

```bash
cd examples/deployer
# Method 1: Specify a configuration directory (recommended)
python deploy.py --config_dir ../infer_engines/vllm

# Method 2: Specify configuration files individually
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```

If you see the following output after execution, it indicates success:

```bash
...... all deploy end.
```
