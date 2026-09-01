# Docker-only Deployment of Multi-container PD Service

## Feature Introduction

This document describes the **end-to-end process** of deploying a multi-container MindIE Motor PD inference service **without using the Kubernetes deployer** and using only **Docker containers + host-mounted configuration**, applicable to both PD co-location and PD disaggregation.

| Deployment Mode | Engine Container | Cross-role KV Transfer | Startup Method |
| :--- | :--- | :--- | :--- |
| PD disaggregation | Prefill and Decode containers | Required | Start P/D instances separately, and start the KV Cache Store as needed. |
| PD co-location | union container | Not required | Start each union instance separately according to the nodes it occupies. |

## Deployment Process

### Preparing `examples`

For how to obtain `examples`, see the "Service Deployment" section in [Quick Start](../../quick_start.md). After copying it from the image to the host, set `EXAMPLES_PATH` in the subsequent `prepare.sh` to the absolute path of this directory.

### Preparing Model-Related Configuration Files

1. Prepare the `user_config.json` and `env.json` files.

   For a complete description of the configuration fields, see [Full Parameter Description of `user_config`](../../configuration/config_reference.md).

   - PD disaggregation scenario

     MindIE Motor provides [**PD disaggregation configuration examples**](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/models/README.md) for common models (deepseek_v4_flash, deepseek_v4_pro, glm 5.2, etc.). **Users can use them directly after modifying a small number of configurations.**

     For models without typical configurations provided, refer to the [MindIE Motor Configuration Auto-Generation Guide](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/models/README.md) to automatically generate the `user_config.json` and `env.json` configuration files.

   - PD co-location scenario

     Refer to the [**MindIE Motor Configuration Auto-Generation Guide**](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/models/README.md) to **automatically generate** the `user_config.json` and `env.json` configuration files for the PD co-location scenario.

2. Adjust the port configuration.

   When the Coordinator, Controller, and Engine containers are deployed on different nodes, the default ports can be used. When multiple roles are deployed on the same node, it is recommended to explicitly configure the following ports:

   - The Coordinator inference, management, and observability ports use `1025`, `1026`, and `1027`, respectively.

   - The Controller management and observability ports use `2026` and `2027`.

   - The union/Prefill NodeManager ports are planned starting from `3026`; the Decode NodeManager ports are planned starting from `4026`.

    The port configuration examples are as follows:

    - PD disaggregation scenario

      ```json
      {
        "motor_deploy_config": {
          ...
          "p_instances_num": 1,                 // Do not modify
          "d_instances_num": 1,                 // Do not modify
          "single_p_instance_pod_num": 2,       // Do not modify
          "single_d_instance_pod_num": 4        // Do not modify
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

      `env.json` uses `motor_engine_prefill_env` and `motor_engine_decode_env`, respectively. In addition, `kv_transfer_config` must be correctly set in the P/D engine configuration; when KV Cache Store is enabled, the corresponding environment variables must also be prepared.

    - PD co-location scenario

      ```json
      {
        "motor_deploy_config": {
          ...
          "hybrid_instances_num": 1,            // Do not modify.
          "single_hybrid_instance_pod_num": 2,  // Do not modify.
          "hybrid_pod_npu_num": 2               // Do not modify.
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

      `env.json` uses `motor_engine_union_env`. PD co-location does not require `kv_transfer_config`.

3. Port configuration planning description

    | Component | Recommended Port | Description |
    | :--- | :--- | :--- |
    | Coordinator inference | 1025 | Uses the host network and is accessed through the Coordinator node IP. |
    | Coordinator management | 1026 | Must not be occupied by other roles on the same node. |
    | Coordinator observability | 1027 | `/metrics`. |
    | Controller management | 2026 | Avoid `1026` when deployed on the same node. |
    | Controller observability | 2027 | Avoid `1027` when deployed on the same node. |
    | union / Prefill NodeManager | Starting from `3026` | Planned by instance and node. |
    | Decode NodeManager | Starting from `4026` | Distinguished from Prefill. |

### Preparing `CONFIGMAP_PATH`

During preparation, copy the configuration files and startup scripts to the directory corresponding to the environment variable **`CONFIGMAP_PATH`**, and load the environment variables through `set_env_docker.py`. The following is an example of the preparation script **`prepare.sh`** (**`EXAMPLES_PATH`**, **`CONFIGMAP_PATH`**, **`USER_CONFIG_PATH`**, and **`ENV_PATH`** must be changed to the actual paths):

```shell
EXAMPLES_PATH="xxx" # Host path v the examples deployment script
CONFIGMAP_PATH="xxx" # Path to the service startup script, which must be mounted into the container
USER_CONFIG_PATH="xxx" # Path to user_config.json
ENV_PATH="xxx" # Path to env.json

mkdir -p $CONFIGMAP_PATH
# Container startup script boot.sh. At runtime, it invokes other scripts in the startup directory, so copy them all to the $CONFIGMAP_PATH directory
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

# If the environment variables have already been loaded but have changed, clear the old environment variables first
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

### Starting Services with Docker

Prepare the startup script `start_docker.sh`. The following is a script example (**`CONFIGMAP_PATH`** and **`WEIGHT_MOUNT_PATH`** must be changed to the actual absolute paths, and **`IMAGE_NAME`** must be changed to the actual image name). **`WEIGHT_MOUNT_PATH`** must be consistent with `weight_mount_path` in `user_config.json` and the model path:

```shell
# Privileged containers are not enabled by default. To enable them, change --privileged=false to --privileged=true
CONFIGMAP_PATH="xxx" # CONFIGMAP_PATH must be consistent with prepare.sh and must use an absolute path
IMAGE_NAME="xxx" # Image name
WEIGHT_MOUNT_PATH="xxx" # Host weight directory. Must use an absolute path

if [ "$ENABLE_IPC_HOST" = "enable" ]; then
    SET_IPC_HOST_STR="--ipc=host"
fi

# Read visible cards from environment variables. By default, automatically detect the host Ascend cards and join them with commas, such as "0,1,2,3"
if [ -z "$ASCEND_VISIBLE_DEVICES" ]; then
    ASCEND_VISIBLE_DEVICES=$(ls /dev/davinci[0-9]* 2>/dev/null | sed 's/[^0-9]//g' | paste -sd "," -)
fi
ASCEND_DEVICES="--device=/dev/davinci_manager --device=/dev/devmm_svm --device=/dev/hisi_hdc"
# Mount the cards specified by ASCEND_VISIBLE_DEVICES in a loop
IFS=',' read -ra ADDR <<< "$ASCEND_VISIBLE_DEVICES"
for i in "${ADDR[@]}"; do
    ASCEND_DEVICES="$ASCEND_DEVICES --device=/dev/davinci$i"
done

docker run -u root --rm --name $CONTAINER_NAME --net=host $SET_IPC_HOST_STR \
-e ASCEND_RUNTIME_OPTIONS=NODRV --privileged=false \
-e CONFIGMAP_PATH=$CONFIGMAP_PATH \
-e CONFIG_PATH=/usr/local/Ascend/pyMotor/conf \
-e ROLE=$ROLE \
-e JOB_NAME=$JOB_NAME \
-e COORDINATOR_SERVICE=$COORDINATOR_SERVICE \
-e CONTROLLER_SERVICE=$CONTROLLER_SERVICE \
-e POD_IP=$POD_IP \
-e KV_STORE_BACKEND=$KV_STORE_BACKEND \
-e KVS_MASTER_SERVICE=$KVS_MASTER_SERVICE \
-e KV_CACHE_STORE_PORT=$KV_CACHE_STORE_PORT \
-e KV_STORE_EVICTION_HIGH_WATERMARK_RATIO=$KV_STORE_EVICTION_HIGH_WATERMARK_RATIO \
-e KV_STORE_EVICTION_RATIO=$KV_STORE_EVICTION_RATIO \
-e DEFAULT_KV_LEASE_TTL=$DEFAULT_KV_LEASE_TTL \
$ASCEND_DEVICES \
-v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
-v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/ \
-v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
-v /usr/local/sbin:/usr/local/sbin \
-v /var/log/npu/:/usr/slog \
-v /mnt:/mnt \
-v $CONFIGMAP_PATH:$CONFIGMAP_PATH \
-v $WEIGHT_MOUNT_PATH:$WEIGHT_MOUNT_PATH:ro \
$IMAGE_NAME \
bash -c "source \$CONFIGMAP_PATH/boot.sh"
```

Environment variable description:

| Variable  | Meaning      | Value    |
| :------- | :------- | :------- |
| CONFIGMAP_PATH       | Startup script path   | Consistent with section 2.2. Must be mounted into the container. |
| IMAGE_NAME  | Image name    | Version image. Ensure that it can be queried with docker images.        |
| WEIGHT_MOUNT_PATH    | Host path of the model weights | Consistent with `weight_mount_path` in `user_config.json` and the model path. Must use an absolute path.     |
| CONTAINER_NAME       | Container name    | Not limited.    |
| ASCEND_VISIBLE_DEVICES    | Visible cards    | Specify the cards to mount, such as "0,1,2,3". By default, automatically detect the host Ascend cards. |
| ENABLE_IPC_HOST      | Whether to enable --ipc=host | `enable` or other.     |
| ROLE | Deployment role | `coordinator`/`controller`/`union`/`prefill`/`decode`/`kv_store`. |
| JOB_NAME | Engine instance task name | Required for union/prefill/decode. Each instance must be unique. |
| COORDINATOR_SERVICE  | Coordinator address   | Set to the IP of the Coordinator deployment node.    |
| CONTROLLER_SERVICE   | Controller address    | Set to the IP of the Controller deployment node. |
| POD_IP  | Container IP   | Uses the host network. The value is the host IP.   |
| KV_STORE_BACKEND     | KV Cache Store backend         | Set to `mooncake` when Mooncake is enabled for PD disaggregation; set to empty when not enabled or when PD co-location is used.         |
| KVS_MASTER_SERVICE   | KV Cache Store address         | Set to the IP of the node where the KV Cache Store resides when PD disaggregation is enabled; set to empty when not enabled or when PD co-location is used.    |
| KV_CACHE_STORE_PORT  | KV Cache Store port         | Set a valid port when enabled, such as `50088`.  |
| KV_STORE_EVICTION_HIGH_WATERMARK_RATIO | KV Cache Store high watermark ratio   | Value range 0-1 when enabled.  |
| KV_STORE_EVICTION_RATIO   | KV Cache Store eviction ratio     | Value range 0-1 when enabled.  |
| DEFAULT_KV_LEASE_TTL | Default lease TTL of KV objects (milliseconds) | The configured value must be greater than `ASCEND_CONNECT_TIMEOUT` and `ASCEND_TRANSFER_TIMEOUT` in `env.json`. Default: `11000`. |

In both modes, start the Coordinator and Controller first:

```shell
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" JOB_NAME="" ROLE="coordinator" POD_IP="<IP0>" CONTAINER_NAME="docker_coordinator" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" JOB_NAME="" ROLE="controller" POD_IP="<IP1>" CONTAINER_NAME="docker_controller" sh start_docker.sh
```

#### Starting PD Disaggregation Instances

The following example deploys 1P1D: P occupies `<IP0>` and `<IP1>`, and D occupies `<IP2>` to `<IP5>`. Multiple containers of the same instance must be started together.

```shell
# If KV Cache Store is enabled, start it on the corresponding node first; skip it when not enabled
ROLE=kv_store POD_IP="<IP2>" KV_STORE_BACKEND=mooncake KVS_MASTER_SERVICE="<IP2>" KV_CACHE_STORE_PORT=50088 KV_STORE_EVICTION_HIGH_WATERMARK_RATIO=0.9 KV_STORE_EVICTION_RATIO=0.1 DEFAULT_KV_LEASE_TTL=11000 CONTAINER_NAME="docker_kv_store" sh start_docker.sh

COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="p0" ROLE="prefill" POD_IP="<IP0>" CONTAINER_NAME="docker_p0_node0" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="p0" ROLE="prefill" POD_IP="<IP1>" CONTAINER_NAME="docker_p0_node1" sh start_docker.sh

COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP2>" CONTAINER_NAME="docker_d0_node0" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP3>" CONTAINER_NAME="docker_d0_node1" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP4>" CONTAINER_NAME="docker_d0_node2" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP5>" CONTAINER_NAME="docker_d0_node3" sh start_docker.sh
```

When KV Cache Store is enabled, `KV_STORE_BACKEND` in the Prefill/Decode startup command must also be set to `mooncake`, `KVS_MASTER_SERVICE` must be set to the KV Cache Store node IP, and `ENABLE_IPC_HOST=enable` must be set as needed.

#### Starting a PD Co-location Instance

The following example deploys one union instance occupying two nodes, `<IP0>` and `<IP1>`:

```shell
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" JOB_NAME="u0" ROLE="union" POD_IP="<IP0>" CONTAINER_NAME="docker_u0_node0" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" JOB_NAME="u0" ROLE="union" POD_IP="<IP1>" CONTAINER_NAME="docker_u0_node1" sh start_docker.sh
```

### Service Verification

After the service is ready, run the following command on any machine that can access the Coordinator. Replace `<IP0>` with the IP address of the Coordinator deployment node, and replace `model` with the model name configured in `user_config.json`.

```bash
curl -X POST http://<IP0>:1025/v1/chat/completions \
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

If `{"detail":"Service is not available"}` is returned, the service is not ready yet. Retry later and check `docker logs docker_coordinator`. If streaming JSON is returned, inference is running normally.

>[!NOTE]NOTE
>
>The HTTP protocol poses security risks. It is recommended to enable HTTPS in production environments. For interface and TLS configuration, see [Service Interfaces](../../api/service_interfaces.md).

### Additional Environment Modifications for Atlas 850 SuperPoD Server

When creating containers on the Atlas 850 SuperPoD Server, make the following adjustments:

**Network**: The `docker run` command in the main text already uses `--net=host`. The Atlas 850 SuperPoD Server scenario continues to use the host network.

**Additional mount paths**:

| Host Path | Container Path | Description |
| :--- | :--- | :--- |
| `/dev/ummu` | `/dev/ummu` | UB interconnect memory device between cards on the Atlas 850 SuperPoD Server. UB memory pool access depends on this path. |
| `/dev/uburma` | `/dev/uburma` | UB RDMA communication device node between servers. |
| `/usr/lib64` | `/usr/lib64` | UB user-mode communication libraries such as `liburma`. |
| `/etc/hixlep` | `/etc/hixlep` | UB link topology structure. |
| `/etc/hccl_rootinfo.json` | `/etc/hccl_rootinfo.json` | HCCL cluster link establishment configuration file. |
| `/usr/local/bin/npu-smi` | `/usr/local/bin/npu-smi` | NPU management tool. |
| `/usr/local/dcmi` | `/usr/local/dcmi` | DCMI library directory, the front-end interface for npu-smi card query and management. |

Startup example snippet for the Atlas 850 SuperPoD Server (modified based on the preceding instance):

```shell
ASCEND_DEVICES="--device=/dev/davinci_manager --device=/dev/hisi_hdc"
# Loop through ASCEND_VISIBLE_DEVICES to append --device=/dev/davinci$i

docker run -u root --rm --name $CONTAINER_NAME \
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
