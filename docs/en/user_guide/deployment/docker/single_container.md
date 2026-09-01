# Deploying a Single-Container PD Service with Docker Only

## Feature Introduction

This document describes the **end-to-end process** of deploying a single-container MindIE Motor PD inference service **without using the Kubernetes deployer** and by using only **Docker containers + host-mounted configuration**. It applies to both PD co-location and PD disaggregation.

| Deployment Mode | Engine Instance | Cross-role KV Transfer | Processes Started in a Single Container |
| :--- | :--- | :--- | :--- |
| PD disaggregation | Prefill and Decode instances | Required | Coordinator, Controller, Prefill/Decode NodeManager |
| PD co-location | union instance | Not required | Coordinator, Controller, union NodeManager |

## Deployment Process

Using `/mnt/motor` as the root path, the directory structure is as follows:

```text
/mnt/motor/
├── prepare.sh
├── start_motor.sh
├── start_docker.sh
├── user_config.json
├── env.json
├── examples/
└── configmap/ # The files in this directory are automatically generated.
    ├── boot.sh
    ├── common.sh
    ├── hccl_tools.py
    ├── all_combine_in_single_container.sh
    ├── controller.sh
    ├── coordinator.sh
    ├── engine.sh
    ├── kv_conductor.sh
    ├── kv_cache_store.sh
    ├── kv_store_backends.mooncake.mooncake.sh
    ├── kv_store_backends.mooncake.mooncake_config.py
    ├── kv_store_backends.memcache.memcache.sh
    ├── kv_store_backends.memcache.memcache_meta_service.py
    ├── kv_store_backends.memcache.mmc-local-inprocess.conf
    ├── mf_store.sh
    ├── user_config.json
    └── env.json
```

### Preparing `examples`

For how to obtain `examples`, see the "Service Deployment" section in [Quick Start](../../quick_start.md). After copying it from the image to the host, set `EXAMPLES_PATH` in the subsequent `prepare.sh` to the absolute path of this directory.

### Preparing Model-Related Configuration Files

1. Prepare the `user_config.json` and `env.json` files.

   For a complete description of the configuration fields, see [Full Parameter Description of user_config](../../configuration/config_reference.md).

   - PD disaggregation scenario

     MindIE Motor already provides [**PD disaggregation configuration examples**](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/models/README.md) for common models (deepseek_v4_flash, deepseek_v4_pro, glm 5.2, etc.). **Users can use them directly after modifying a small number of configurations.**

     For models without a typical configuration provided, refer to the [MindIE Motor Configuration Auto-Generation Guide](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/models/README.md) to automatically generate the `user_config.json` and `env.json` configuration files.

   - PD co-location scenario

     Refer to the [**MindIE Motor Configuration Auto-Generation Guide**](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/models/README.md) to **automatically generate** the `user_config.json` and `env.json` configuration files for the PD co-location scenario.

2. Adjust the port configuration

   Both modes must be configured:

   - `motor_deploy_config.deploy_mode`: must be set to `single_container`.

   - For the Coordinator inference, management, and observability ports, `1025`, `1026`, and `1027` are recommended, respectively.

   - For the Controller management and observability ports, `2026` and `2027` are recommended to avoid conflicts with the Coordinator.

   - The NodeManager port must be configured under `motor_nodemanger_config.api_config` of the corresponding engine section.

    The following shows port configuration examples:

    - Port configuration example for the PD disaggregation scenario

      ```json
      {
        "motor_deploy_config": {
          ...
          "deploy_mode": "single_container",
          "p_instances_num": 1,                 // Do not modify
          "d_instances_num": 1                  // Do not modify
        },
        "motor_controller_config": {
          ...
          "api_config": {
            "controller_api_port": 2026,
            "observability_api_port": 2027
          }
        },
        "motor_coordinator_config": {
          ...
          "api_config": {
            "coordinator_api_infer_port": 1025,
            "coordinator_api_mgmt_port": 1026,
            "coordinator_obs_port": 1027
          }
        },
        "motor_engine_prefill_config": {
          ...
          "motor_nodemanger_config": {
            "api_config": {
              "node_manager_port": 3026
            }
          }
        },
        "motor_engine_decode_config": {
          ...
          "motor_nodemanger_config": {
            "api_config": {
              "node_manager_port": 4026
            }
          }
        },
        ...
      }
      ```

      `env.json` uses `motor_engine_prefill_env` and `motor_engine_decode_env`, respectively. In addition, `kv_transfer_config` must be correctly set in the P/D engine configuration. When KV Cache Store is enabled, the corresponding environment variables must also be prepared.

    - PD co-location scenario

      ```json
      {
        "motor_deploy_config": {
          ...
          "deploy_mode": "single_container",
          "hybrid_instances_num": 1,            // Do not modify
          "single_hybrid_instance_pod_num": 1,  // Do not modify
          "hybrid_pod_npu_num": 2               // Do not modify
        },
        "motor_controller_config": {
          ...
          "api_config": {
            "controller_api_port": 2026,
            "observability_api_port": 2027
          }
        },
        "motor_coordinator_config": {
          ...
          "api_config": {
            "coordinator_api_infer_port": 1025,
            "coordinator_api_mgmt_port": 1026,
            "coordinator_obs_port": 1027
          }
        },
        "motor_engine_union_config": {
          "engine_type": "vllm",
          ...
          "motor_nodemanger_config": {
            "api_config": {
              "node_manager_port": 3026
            }
          }
        },
        ...
      }
      ```

      `env.json` uses `motor_engine_union_env` to configure the environment variables of the union instance. PD co-location does not require `kv_transfer_config`.

3. Port configuration planning description

    | Component | Recommended Port | Description |
    | :--- | :--- | :--- |
    | Coordinator inference | 1025 | Exposed to the host through `-p 31015:1025`. |
    | Coordinator management | 1026 | Internal management interface of the container. |
    | Coordinator observability | 1027 | Can be exposed through `-p 31017:1027` as needed. |
    | Controller management | 2026 | Avoid the Coordinator management port. |
    | Controller observability | 2027 | Avoid the Coordinator observability port. |
    | union / Prefill NodeManager | Starting from `3026` | Planned per instance. |
    | Decode NodeManager | Starting from `4026` | Distinguished from Prefill. |

If `coordinator_api_infer_port` is modified, the container-side port in the `docker run` port mapping must be changed accordingly.

### Preparing the configmap

During the preparation phase, copy the configuration files and startup scripts to the directory corresponding to the environment variable **`CONFIGMAP_PATH`**, and load the environment variables through `set_env_docker.py`. The following is an example of the preparation phase script **h`prepare.s`** (**`EXAMPLES_PATH`**, **`CONFIGMAP_PATH`**, **`USER_CONFIG_PATH`**, and **`ENV_PATH`** must be changed to the actual paths):

The following uses `/mnt/motor` as the root path as an example.

```shell
EXAMPLES_PATH="/mnt/motor/examples/"
CONFIGMAP_PATH="/mnt/motor/configmap/"
USER_CONFIG_PATH="/mnt/motor/user_config.json"
ENV_PATH="/mnt/motor/env.json"

mkdir -p $CONFIGMAP_PATH

# The container startup script boot.sh invokes other scripts in the startup directory at runtime, so copy them all to the $CONFIGMAP_PATH directory
cp -f $EXAMPLES_PATH/deployer/startup/boot.sh $CONFIGMAP_PATH/boot.sh
cp -f $EXAMPLES_PATH/deployer/startup/common.sh $CONFIGMAP_PATH/common.sh
cp -f $EXAMPLES_PATH/deployer/startup/hccl_tools.py $CONFIGMAP_PATH/hccl_tools.py
cp -f $EXAMPLES_PATH/deployer/startup/roles/*.sh $CONFIGMAP_PATH/
cp -f $EXAMPLES_PATH/deployer/startup/roles/kv_store_backends/mooncake/mooncake.sh $CONFIGMAP_PATH/kv_store_backends.mooncake.mooncake.sh
cp -f $EXAMPLES_PATH/deployer/startup/roles/kv_store_backends/mooncake/mooncake_config.py $CONFIGMAP_PATH/kv_store_backends.mooncake.mooncake_config.py
cp -f $EXAMPLES_PATH/deployer/startup/roles/kv_store_backends/memcache/memcache.sh $CONFIGMAP_PATH/kv_store_backends.memcache.memcache.sh
cp -f $EXAMPLES_PATH/deployer/startup/roles/kv_store_backends/memcache/memcache_meta_service.py $CONFIGMAP_PATH/kv_store_backends.memcache.memcache_meta_service.py
cp -f $EXAMPLES_PATH/deployer/startup/roles/kv_store_backends/memcache/mmc-local-inprocess.conf $CONFIGMAP_PATH/kv_store_backends.memcache.mmc-local-inprocess.conf

# Copy the prepared user_config.json and env.json configuration files to the $CONFIGMAP_PATH directory
cp -f $USER_CONFIG_PATH $CONFIGMAP_PATH/user_config.json
cp -f $ENV_PATH $CONFIGMAP_PATH/env.json

# If the environment variables have been loaded but changed, clear the old environment variables first
sed -i '/^function set_controller_env()/,/^}/d' $CONFIGMAP_PATH/controller.sh
sed -i '/^function set_coordinator_env()/,/^}/d' $CONFIGMAP_PATH/coordinator.sh
sed -i '/^function set_prefill_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_decode_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_union_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_common_env()/,/^}/d' $CONFIGMAP_PATH/common.sh
sed -i '/^function set_kv_store_env()/,/^}/d' $CONFIGMAP_PATH/kv_cache_store.sh
sed -i '/^function set_kv_conductor_env()/,/^}/d' $CONFIGMAP_PATH/kv_conductor.sh
sed -i '/^function set_controller_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_coordinator_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_prefill_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_decode_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_union_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_kv_store_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_kv_conductor_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/./,$!d' $CONFIGMAP_PATH/common.sh

# Load the environment variables from user_config.json and env.json, and apply them to the container startup script
python $EXAMPLES_PATH/deployer/startup/set_env_docker.py --configmap_path $CONFIGMAP_PATH
```

Execution method:

```bash
sh prepare.sh
```

After execution, some scripts are generated in the `/mnt/motor/configmap/` directory.

### Preparing the Motor Startup Script

Prepare the `start_motor.sh` script. Both modes share this script; PD co-location does not enable KV Cache Store, so keep `KVS_MASTER_SERVICE` empty. PD disaggregation configures KV Cache Store as needed.

```sh
CONFIGMAP_PATH="/mnt/motor/configmap" # CONFIGMAP_PATH must be consistent with prepare.sh and must use an absolute path
CONFIG_PATH=/usr/local/Ascend/pyMotor/conf

ROLE=SINGLE_CONTAINER

# Mooncake pooling configuration
# When PD disaggregation enables Mooncake KV Cache Store, set KV_STORE_BACKEND to mooncake
# Set KVS_MASTER_SERVICE to any non-empty string; set both to empty when not enabled or when using PD co-location
KV_STORE_BACKEND=""
KVS_MASTER_SERVICE=""
KV_CACHE_STORE_PORT=50088
KV_STORE_EVICTION_HIGH_WATERMARK_RATIO=0.9
KV_STORE_EVICTION_RATIO=0.1
DEFAULT_KV_LEASE_TTL=11000

source $CONFIGMAP_PATH/boot.sh
```

Environment variable description:

| Variable name | Meaning | Value |
| :--- | :--- | :--- |
| KV_STORE_BACKEND | KV Cache Store backend | Set to `mooncake` when PD disaggregation enables Mooncake; set to empty when not enabled or when using PD co-location. |
| KVS_MASTER_SERVICE | Mooncake KV Cache Store address | Set to any non-empty string when PD disaggregation enables it; the startup script adapts it to the container IP; set to empty when not enabled or when using PD co-location. |
| KV_CACHE_STORE_PORT | Mooncake KV Cache Store port | Set to a valid port when enabled, such as `50088`. |
| KV_STORE_EVICTION_HIGH_WATERMARK_RATIO | KV Cache Store high watermark ratio | Value range 0-1 when enabled. |
| KV_STORE_EVICTION_RATIO | KV Cache Store eviction ratio | Value range 0-1 when enabled. |
| DEFAULT_KV_LEASE_TTL | Default lease TTL of KV objects (milliseconds) | The configured value must be greater than `ASCEND_CONNECT_TIMEOUT` and `ASCEND_TRANSFER_TIMEOUT` in `env.json`; default `11000`. |

### Preparing the Docker Startup Script

Prepare the startup script `start_docker.sh`. The following is a script example (**`CONFIGMAP_PATH`** and **`WEIGHT_MOUNT_PATH`** must be changed to actual absolute paths, and **`IMAGE_NAME`** must be changed to the actual image name). **`WEIGHT_MOUNT_PATH`** must be consistent with `weight_mount_path` in `user_config.json` and the model path:

```shell
# Privileged containers are disabled by default. To enable them, change --privileged=false to --privileged=true
CONFIGMAP_PATH="/mnt/motor/configmap" # CONFIGMAP_PATH must be consistent with prepare.sh and must use an absolute path
IMAGE_NAME="xxx" # Image name
WEIGHT_MOUNT_PATH="xxx" # Host weight directory. Must use an absolute path

ASCEND_DEVICES="--device=/dev/davinci_manager --device=/dev/devmm_svm --device=/dev/hisi_hdc"

docker run -u root --rm --name single_container \
-e ASCEND_RUNTIME_OPTIONS=NODRV --privileged=false \
$ASCEND_DEVICES \
-v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
-v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/ \
-v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
-v /usr/local/sbin:/usr/local/sbin \
-v /var/log/npu/:/usr/slog \
-v /mnt:/mnt \
-v $CONFIGMAP_PATH:$CONFIGMAP_PATH \
-v $WEIGHT_MOUNT_PATH:$WEIGHT_MOUNT_PATH:ro \
-p 31015:1025 \
-p 31017:1027 \
$IMAGE_NAME \
bash -c "export POD_IP=\$(grep \$(hostname) /etc/hosts | cut -f1) && source /mnt/motor/start_motor.sh"
```

**Note: The mount path must include /mnt.**

### Starting Docker

The script automatically starts the union instance or Prefill/Decode instances based on `user_config.json`.

PD disaggregation startup example (1P1D):

```shell
ASCEND_VISIBLE_DEVICES=0,1 sh start_docker.sh
```

PD co-location startup example (1 union instance):

```shell
ASCEND_VISIBLE_DEVICES=0,1 sh start_docker.sh
```

### Service Verification

After the service is ready, run the following command on the host. Replace `<IP>` with the host IP or `127.0.0.1`, and replace `model` with the model name configured in `user_config.json`.

```bash
curl -X POST http://<IP>:31015/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-8B",
    "messages": [
      {
        "role": "user",
        "content": "who are you?"
      }
    ],
    "max_tokens": 36,
    "stream": true
  }'
```

If `{"detail":"Service is not available"}` is returned, the service is not ready yet. Retry later and check `docker logs single_container`. If streaming JSON is returned, inference is working properly.

>[!NOTE]NOTE
>
>The HTTP protocol poses security risks. It is recommended to enable HTTPS in production environments. For interface and TLS configuration, see [Service Interfaces](../../api/service_interfaces.md).

### Additional Environment Modifications for the Atlas 850 SuperPoD Server

When creating a container on the Atlas 850 SuperPoD Server, make the following adjustments:

**Network**: Use `--network host` instead of the `-p` port mapping. In this case, service verification uses `http://<IP>:1025/v1/chat/completions`.

**Additional mount paths**:

| Host Path | Container Path | Description |
| :--- | :--- | :--- |
| `/dev/ummu` | `/dev/ummu` | Inter-card UB interconnect memory device on the Atlas 850 SuperPoD Server; UB memory pool access depends on this path. |
| `/dev/uburma` | `/dev/uburma` | UB RDMA communication device node between servers. |
| `/usr/lib64` | `/usr/lib64` | UB user-mode communication libraries such as `liburma`. |
| `/etc/hixlep` | `/etc/hixlep` | UB link topology. |
| `/etc/hccl_rootinfo.json` | `/etc/hccl_rootinfo.json` | HCCL cluster link establishment configuration file. |
| `/usr/local/bin/npu-smi` | `/usr/local/bin/npu-smi` | NPU management tool. |
| `/usr/local/dcmi` | `/usr/local/dcmi` | DCMI library directory. |

Startup example snippet for the Atlas 850 SuperPoD Server:

```shell
ASCEND_DEVICES="--device=/dev/davinci_manager --device=/dev/hisi_hdc"

docker run -u root --rm --name single_container \
  --network host \
  ... \
  -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
  -v /usr/lib64:/usr/lib64 \
  -v /etc/hixlep:/etc/hixlep \
  -v /etc/hccl_rootinfo.json:/etc/hccl_rootinfo.json \
  -v /usr/local/dcmi:/usr/local/dcmi \
  -v /dev/ummu:/dev/ummu \
  -v /dev/uburma:/dev/uburma \
  ... \
```
