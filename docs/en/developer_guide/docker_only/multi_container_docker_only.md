# Guide to Deploying Multi-Container PD Disaggregation with Docker-Only

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:07:17.310Z pushedAt=2026-06-30T02:17:37.044Z -->

## Feature Introduction

This document describes the **end-to-end process** of deploying multi-container PyMotor PD disaggregation inference using **Docker containers + host-mounted configuration** **without the Kubernetes deployer**.

## Deployment Process

### Preparing the `user_config.json` and `env.json` Configuration Files

You can obtain the [user_config.json](../../../../examples/infer_engines/vllm/user_config.json) and [env.json](../../../../examples/infer_engines/vllm/env.json) templates from the following paths. This document mainly introduces adaptation related to the docker-only deployment method. For other features, refer to [quick_start](../../user_guide/quick_start.md).

If there is no scenario where multiple containers are deployed on the same node, no adaptation is required. When the node where the coordinator or controller is deployed is the same node as the P/D instance, you need to modify the default ports in the `user_config.json` configuration file:

- **motor_coordinator_config.api_config.coordinator_api_infer_port**: coordinator inference port (default 1025).

- **motor_coordinator_config.api_config.coordinator_api_mgmt_port**: coordinator management port (default 1026).

- **motor_controller_config.api_config.controller_api_port**: controller management port (default 1026).

- **motor_nodemanger_config.api_config.node_manager_port**: nodemanger management port (default 1026).

Example:

```json
{
  "motor_controller_config": {
    ...
    "api_config": {
      "controller_api_port": 2026
    }
  },
  "motor_coordinator_config": {
    ...
    "api_config": {
      "coordinator_api_infer_port": 1025,
      "coordinator_api_mgmt_port": 1026
    },
  },
  "motor_engine_prefill_config": {
    ...
    "motor_nodemanger_config": {
      "api_config": {
        "node_manager_port": 3026
      }
    },
  },
  "motor_engine_decode_config": {
    ...
    "motor_nodemanger_config": {
      "api_config": {
        "node_manager_port": 3026
      }
    },
  },
  ...
}
```

### Preparing CONFIGMAP_PATH

During the preparation phase, copy the configuration files and startup scripts to the directory specified by the `CONFIGMAP_PATH` environment variable, and load environment variables using `set_env_docker.py`. Below is an example `prepare.sh` script for this phase (update `EXAMPLES_PATH`, `CONFIGMAP_PATH`, `USER_CONFIG_PATH`, and `ENV_PATH` with the actual paths):

```shell
EXAMPLES_PATH="xxx" # Host examples deployment script path
CONFIGMAP_PATH="xxx" # Service startup script path, which needs to be mounted into the container
USER_CONFIG_PATH="xxx" # user_config.json path
ENV_PATH="xxx" # env.json path

mkdir -p $CONFIGMAP_PATH
# Container startup script boot.sh, which calls other scripts in the startup directory at runtime and needs to be copied to the $CONFIGMAP_PATH directory
cp -f $EXAMPLES_PATH/deployer/startup/boot.sh $CONFIGMAP_PATH/boot.sh
cp -f $EXAMPLES_PATH/deployer/startup/common.sh $CONFIGMAP_PATH/common.sh
cp -f $EXAMPLES_PATH/deployer/startup/hccl_tools.py $CONFIGMAP_PATH/hccl_tools.py
cp -f $EXAMPLES_PATH/deployer/startup/mooncake_config.py $CONFIGMAP_PATH/mooncake_config.py
cp -f $EXAMPLES_PATH/deployer/startup/roles/* $CONFIGMAP_PATH/

# Copy the prepared user_config.json and env.json configuration files to the $CONFIGMAP_PATH directory
cp -f $USER_CONFIG_PATH $CONFIGMAP_PATH/user_config.json
cp -f $ENV_PATH $CONFIGMAP_PATH/env.json

# If environment variables have already been loaded but changes occur, clear the old environment variables first
sed -i '/^function set_controller_env()/,/^}/d' $CONFIGMAP_PATH/controller.sh
sed -i '/^function set_coordinator_env()/,/^}/d' $CONFIGMAP_PATH/coordinator.sh
sed -i '/^function set_prefill_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_decode_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_common_env()/,/^}/d' $CONFIGMAP_PATH/common.sh
sed -i '/^function set_kv_pool_env()/,/^}/d' $CONFIGMAP_PATH/kv_pool.sh
sed -i '/^function set_kv_conductor_env()/,/^}/d' $CONFIGMAP_PATH/kv_conductor.sh
sed -i '/^function set_controller_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_coordinator_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_prefill_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_decode_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_kv_pool_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_kv_conductor_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/./,$!d' $CONFIGMAP_PATH/common.sh

# Load the environment variables from user_config.json and env.json and apply them to the container startup script
python $EXAMPLES_PATH/deployer/startup/set_env_docker.py --configmap_path $CONFIGMAP_PATH
```

Execution method:

```bash
sh prepare.sh
```

### Starting the Service with Docker

Prepare the startup script `start_docker.sh`. Below is an example script (update **CONFIGMAP_PATH** and **IMAGE_NAME** with the actual path and image name):

```shell
# Privileged container is not enabled by default. To enable it, change --privileged=false to --privileged=true
CONFIGMAP_PATH="xxx" # CONFIGMAP_PATH must match with prepare.sh and must use an absolute path
IMAGE_NAME="xxx" # Image name

if [ "$ENABLE_IPC_HOST" = "enable" ]; then
    SET_IPC_HOST_STR="--ipc=host"
fi

# Read visible cards from environment variables. By default, the host Ascend cards are automatically detected and concatenated with commas, such as "0,1,2,3"
if [ -z "$ASCEND_VISIBLE_DEVICES" ]; then
    ASCEND_VISIBLE_DEVICES=$(ls /dev/davinci[0-9]* 2>/dev/null | sed 's/[^0-9]//g' | paste -sd "," -)
fi
ASCEND_DEVICES="--device=/dev/davinci_manager --device=/dev/devmm_svm --device=/dev/hisi_hdc"
# Loop mount the cards specified by ASCEND_VISIBLE_DEVICES
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
-e KVP_MASTER_SERVICE=$KVP_MASTER_SERVICE \
-e KV_POOL_PORT=$KV_POOL_PORT \
-e KV_POOL_EVICTION_HIGH_WATERMARK_RATIO=$KV_POOL_EVICTION_HIGH_WATERMARK_RATIO \
-e KV_POOL_EVICTION_RATIO=$KV_POOL_EVICTION_RATIO \
$ASCEND_DEVICES \
-v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
-v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/ \
-v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
-v /usr/local/sbin:/usr/local/sbin \
-v /var/log/npu/:/usr/slog \
-v /mnt:/mnt \
$IMAGE_NAME \
bash -c "source \$CONFIGMAP_PATH/boot.sh"
```

Environment variable description:

| Variable | Description | Value |
| :--- | :--- | :--- |
| `CONFIGMAP_PATH` | Path to the startup script | Must match section 2.2 and be mounted into the container |
| `IMAGE_NAME` | Image name | Versioned image, which must be discoverable via `docker images` |
| `CONTAINER_NAME` | Container name | Any valid name |
| `ASCEND_VISIBLE_DEVICES` | Visible Ascend devices | Comma-separated list, e.g., `"0,1,2,3"`; defaults to auto-detected host Ascend devices |
| `ENABLE_IPC_HOST` | Whether to enable `--ipc=host` | `enable` or other |
| `ROLE` | Deployment role | `coordinator`, `controller`, `prefill`, `decode`, or `kv_pool` |
| `JOB_NAME` | PD instance task name | Required for `prefill` and `decode`; must be unique per instance |
| `COORDINATOR_SERVICE` | Coordinator domain name | Set to the host IP where the coordinator is deployed; required for `coordinator`, `controller`, `prefill`, and `decode` |
| `CONTROLLER_SERVICE` | Controller domain name | Set to the host IP where the controller is deployed; required for `coordinator`, `controller`, `prefill`, and `decode` |
| `POD_IP` | Container IP | When using host network mode, set to the host IP |
| `KVP_MASTER_SERVICE` | `mooncake_master` deployment domain | When `kv_pool` is enabled, set to `POD_IP`; otherwise leave empty |
| `KV_POOL_PORT` | `mooncake_master` deployment port | When `kv_pool` is enabled, set to any valid port, e.g., `50088`; otherwise leave empty |
| `KV_POOL_EVICTION_HIGH_WATERMARK_RATIO` | `mooncake_master` high watermark ratio | When `kv_pool` is enabled, set to a value between 0 and 1; otherwise leave empty |
| `KV_POOL_EVICTION_RATIO` | `mooncake_master` eviction ratio | When `kv_pool` is enabled, set to a value between 0 and 1; otherwise leave empty |

Service startup example (1P1D):

```shell
# Startup sequence: coordinator/controller/kv_pool (optional) first, then P/D instances. Multiple containers of the same instance must be started together
# Assume the coordinator is deployed on node <IP0>, the controller on node <IP1>, and kv_pool (if any) on node <IP2>.
# Assume deploying 1P1D, P occupies 2 machines, D occupies 4 machines, P deployment nodes are <IP0><IP1> in sequence, D deployment nodes are <IP2><IP3><IP4><IP5> in sequence
# Start the Coordinator/Controller service, assumed to be deployed on node <IP0>
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" JOB_NAME="" ROLE="coordinator" POD_IP="<IP0>" CONTAINER_NAME="docker_coordinator" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" JOB_NAME="" ROLE="controller" POD_IP="<IP1>"CONTAINER_NAME="docker_controller"  sh start_docker.sh

# If pooling is enabled (optional), start kv_pool, assumed to be deployed on node <IP0>
ROLE=kv_pool POD_IP="<IP0>" KVP_MASTER_SERVICE="<IP2>" KV_POOL_PORT=50088 KV_POOL_EVICTION_HIGH_WATERMARK_RATIO=0.9 KV_POOL_EVICTION_RATIO=0.1 CONTAINER_NAME="docker_kv_pool" sh start_docker.sh

# Start PD instances
# If pooling is enabled, set KVP_MASTER_SERVICE to the IP of the kv_pool deployment node (i.e., <IP2>); if pooling is not enabled, set it to empty
# If pooling is enabled, set ENABLE_IPC_HOST to "enable"; if pooling is not enabled, leave it empty
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVP_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="p0" ROLE="prefill" POD_IP="<IP0>" CONTAINER_NAME="docker_p0"  sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVP_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="p0" ROLE="prefill" POD_IP="<IP1>" CONTAINER_NAME="docker_p0"  sh start_docker.sh

COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVP_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP2>" CONTAINER_NAME="docker_d0"  sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVP_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP3>" CONTAINER_NAME="docker_d0"  sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVP_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP4>" CONTAINER_NAME="docker_d0"  sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVP_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP5>" CONTAINER_NAME="docker_d0"  sh start_docker.sh
```
