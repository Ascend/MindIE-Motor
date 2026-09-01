# KV Cache Affinity Tuning Capability Deployment

## Feature Introduction

The MindIE Motor KV Cache affinity scheduling capability relies on the Mooncake Conductor component from the Mooncake community. It allows the scheduler to preferentially dispatch requests to the instance that has cached the corresponding KV based on the KV Cache location, thereby reducing the overhead of cross-instance KV Cache transfer and improving inference throughput and response speed. For details about the related capabilities and interfaces, see [Mooncake Conductor Introduction](https://github.com/yejj710/Mooncake/blob/6dca8cc76ce074fa9c41f02e9a2195c7c1c9308f/docs/source/design/conductor/indexer-api-design.md).

After modifying the `user_config.json` configuration file, you can complete service deployment through the `deploy.py` script.

## Prerequisites

- A PD disaggregation inference service must have been deployed using MindIE Motor. KV Cache affinity tuning is enabled on top of this service.

- Before enabling the affinity tuning capability, refer to [MindIE Motor Quick Start](../quick_start.md) to ensure that the environment is ready for basic service deployment.

- The code related to the Mooncake Conductor component has not yet been merged into the mainline branch, and the current image does not include Mooncake Conductor. **You need to install the Mooncake Conductor service component additionally based on the existing image** (see Step 2 of Quick Practice).

- All subsequent operations are performed only on the management node (master node) of the k8s cluster.

## Quick Practice

1. A PD disaggregation inference service has been deployed in advance using Motor, and the service is running properly.

2. Prepare an image that includes Mooncake Conductor.

   1. Start the container.

      ```bash
      docker run -it --name mooncake_patch --privileged=true --net=host --shm-size=128g <commit ID> bash
      # Replace the commit ID of the base image
      ```

   2. Prepare the go environment.

      ```bash
      # Download the golang installation file
      wget https://mirrors.aliyun.com/golang/go1.23.8.linux-arm64.tar.gz
      tar -C /usr/local -xzf go1.23.8.linux-arm64.tar.gz
      echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.bashrc

      # Set golang environment variables
      go env -w GOSUMDB=off   # Do not verify the CA certificate
      go env -w GOPROXY=direct # Directly access github to pull
      ```

   3. Install libzmq-related dependencies.

      ```bash
      # Ubuntu
      apt update
      apt install libzmq5 libzmq3-dev
      ```

      ```bash
      # openEuler
      dnf install zeromq zeromq-devel
      ```

   4. Download the Mooncake source code and compile `mooncake_conductor`.

      ```bash
      git clone https://github.com/kvcache-ai/Mooncake.git -b dev/kv-indexer
      cd Mooncake/mooncake-conductor/conductor-ctrl/
      go mod tidy
      go build -o mooncake_conductor main.go
      mv mooncake_conductor /usr/local/bin/
      ```

   5. Save the image.

      ```bash
      docker commit -a "add Mooncake Conductor" mooncake_patch mindie-motor-vllm:dev-26.0.0.B060-800I-A3-py311-Ubuntu24.04-lts-aarch64-patch
      ```

3. Modify the `user_config.json` configuration file.

   In `examples/infer_engines/vllm/user_config.json`, add or modify the `kv_conductor_config` and `kv-events-config` configuration items. For the specific configuration format, see the [Typical Configuration](#typical-configuration) section below.

   Key points:

   - In `motor_coordinator_config`, set `scheduler_config.scheduler_type` to `kv_cache_affinity`.

   - In `motor_engine_prefill_config.engine_config`, add `kv-events-config` to enable the KV Cache event publishing capability of the P instance.

   - Add the global `kv_conductor_config` configuration to specify parameters such as the Conductor service port.

   - Keep the remaining configuration items consistent with those when affinity scheduling is not enabled.

4. Deploy the service.

    Run the deployment command in the `examples/deployer` directory:

      ```bash
      cd examples/deployer
      # (Recommended) Method 1: Specify the configuration directory
      python deploy.py --config_dir ../infer_engines/vllm

      # Method 2: Specify the configuration file separately
      python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
      ```

5. Verify the result.

   ```bash
   kubectl get pod -A -o wide
   ```

    It is expected that the P/D instances start successfully and the service runs normally.

## Typical Configuration

### PD Disaggregation Configuration Example

Using the `user_config.json` in [MindIE Motor Quick Start](../quick_start.md) as the baseline, the complete configuration example after enabling KV Cache affinity scheduling is as follows:

```json
{
  "version": "v2.0",
  "motor_deploy_config": {
    "..."
  },
  "motor_controller_config": {},
  "motor_coordinator_config": {
    "scheduler_config": {
      "scheduler_type": "kv_cache_affinity"
    }
  },
  "motor_nodemanger_config": {},
  "motor_engine_prefill_config": {
    "engine_type": "vllm",
    "engine_config": {
      "..."
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
    "http_server_port": 13333
  }
}
```

### PD Co-location Configuration Example

PD co-location does not use `motor_engine_prefill_config`. Instead, `kv-events-config` and `enable-prefix-caching` should be configured in `motor_engine_union_config.engine_config`. When the Coordinator starts, it automatically merges `prefill_kv_event_config` (`endpoint`, `replay_endpoint`, `model_path`, etc.) from the union engine section, so there is no need to manually write the prefill section configuration.

```json
{
  "version": "v2.0",
  "motor_deploy_config": {
    "..."
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
      "..."
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
    "http_server_port": 13333
  }
}
```

For detailed instructions on PD co-location deployment, see [PD Co-location Deployment](../deployment/k8s/pd_aggregation_deployment.md).

### Parameter Description

Description of each parameter:

**`kv_conductor_config` (KV Conductor global configuration)**

| Configuration Item | Value Type | Value Range | Configuration Description |
| --- | --- | --- | --- |
| kvevent_instance | dict | - | KV event instance configuration. Currently only the `Mooncake` type is supported. |
| kvevent_instance.mooncake_master.type | string | `Mooncake` | KV event backend type, fixed to `Mooncake`. |
| http_server_port | int | 1024~65535 | HTTP service port of KV Conductor. If not configured, `deploy.py` supplements it with `13333` by default. |

**`motor_coordinator_config.scheduler_config` (scheduler configuration)**

| Configuration Item | Value Type | Value Range | Configuration Description |
| --- | --- | --- | --- |
| **scheduler_type** | string | `kv_cache_affinity` | Set to `kv_cache_affinity` to use the KV Cache affinity scheduling algorithm. |
| **kv_affinity_mode** | string | `unified` / `load_gated` | KV Cache affinity sub-strategy. `unified` (default): a single score fuses affinity and real-time load, and the endpoint with the lowest score is selected. `load_gated`: first retain the N endpoints with the lowest load, then select the endpoint with the longest cache prefix among them. |
| **kv_affinity_load_weight** | float | `[0, +∞)` | Weight of the endpoint real-time load in `unified` mode. `1.0` means the load is equally important as the prefill cost after affinity discount. `0` means pure affinity (longest prefix first, without load awareness). Default value: `1.0`. |
| **kv_affinity_overlap_credit** | float | `[0, +∞)` | Discount coefficient of the cache prefix on the prefill cost. The larger the value, the higher the discount of the existing cache prefix on the prefill cost. Default value: `1.0`. |
| **kv_affinity_prefill_load_scale** | float | `[0, +∞)` | Weight of the prefill cost (after affinity discount) in `unified` mode. Default value: `1.0`. |
| **kv_affinity_load_gate_topn** | int | `[0, +∞)` | In `load_gated` mode, first retain the N endpoints with the lowest load, then select the optimal endpoint among them by affinity. When set to `0`, it falls back to `2`. Default value: `0`. |

**`motor_engine_prefill_config.engine_config.kv-events-config` (P instance KV event configuration)**

| Configuration Item | Value Type | Value Range | Configuration Description |
| --- | --- | --- | --- |
| **publisher** | string | `zmq` | Event publishing backend. Currently only `zmq` is supported. |
| **enable_kv_cache_events** | bool | `true` / `false` | Whether to enable KV Cache events. Set to `true`. |
| **endpoint** | string | `tcp://*:<port>` | Endpoint for the P instance to publish events. |
| **topic** | string | Custom | Event topic. |
| **replay_endpoint** | string | `tcp://*:<port>` | Event replay endpoint. |

> **About Connector**: In the example, `kv_connector` uses `MultiConnector`, where `connectors[0]` (`MooncakeLayerwiseConnector`, the transport layer) determines the P/D collaboration capability, and `connectors[1]` (`AscendStoreConnector`, the KV pool backend) does not participate in the determination and does not need to be in the identification whitelist. For details about the identification whitelist and the `dispatch_profile` escape hatch.

## Principle Description

### Overall Process of KV Cache Affinity Tuning

The KV Cache affinity tuning capability of MindIE Motor is implemented based on the Mooncake Conductor component. The overall process is as follows:

1. **KV Cache event publishing**: After the P instance completes PreFill computation, it publishes KV Cache events (including the KV Cache location information of the sequence) through the ZMQ endpoint configured in `kv-events-config`.

2. **Conductor event collection**: The Mooncake Conductor component receives and indexes the KV Cache events published by the P instance, maintaining a global KV Cache location mapping table.

3. **Affinity tuning decision**: When allocating requests, the scheduler in the Coordinator (`scheduler_type: kv_cache_affinity`) queries the KV Cache location information in Conductor and preferentially schedules requests to the D instance that caches the corresponding KV Cache, thereby reducing cross-node KV Cache transfer.

4. **P/D collaboration**: The P and D instances establish a transfer channel through the `kv_connector` configured in `kv_transfer_config`, with the producer/consumer roles distinguished by `kv_role`.

### Deployment Process

Run full deployment in the `examples/deployer` directory:

```bash
cd examples/deployer
python deploy.py --config_dir ../infer_engines/vllm
```

If the following content is displayed after execution, the execution is successful:

```bash
...... all deploy end.
```

After completion:

- The ConfigMap `motor-config` is created or updated in the cluster (its content comes from the currently input `user_config.json`), serving as the baseline for subsequent scaling and refresh.

- YAML files for each service are generated under `output/deployment/`.

- The scheduler in the Coordinator performs affinity scheduling according to the `kv_cache_affinity` policy.

### Key Configuration Tuning Suggestions

- **`http_server_port`**: the KV Conductor service port. Ensure that it does not conflict with other service ports in the cluster. The default value is `13333`.

- **`endpoint` and `replay_endpoint`**: the event publishing and replay ports of the P instance. Ensure that the network between P/D instances is reachable and that the ports are not occupied.

- **`use_layerwise`**: In the KV Cache affinity scheduling scenario, it is recommended to set this to `false`, so that the Conductor manages the global KV Cache location information without the need to transfer it separately at the layer granularity.

## FAQs

1. **KV Cache cannot be transferred between P/D instances after the service starts**

   Check whether `kv_role` in `kv_transfer_config` is correct (P is `kv_producer`, D is `kv_consumer`), and whether `kv_port` is configured consistently.

2. **Coordinator cannot connect to the Conductor service**

   Check whether `http_server_port` in `kv_conductor_config` is configured correctly, and ensure that the Conductor service port is not occupied.

3. **The P instance fails to publish KV Cache events**

   Check whether the `endpoint` and `replay_endpoint` in `kv-events-config` are configured correctly, and whether the network between the P instance and the Conductor is reachable.
