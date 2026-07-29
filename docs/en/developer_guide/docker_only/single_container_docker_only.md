# Guide to Deploying Single-Container PD Disaggregation with Docker Only

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:07:13.370Z pushedAt=2026-06-30T02:32:58.433Z -->

## Feature Introduction

This document describes the **end-to-end process** of deploying single-container PyMotor PD disaggregation inference using **Docker containers + host mount configuration** only, **without the Kubernetes deployer**.

## Deployment Process

### Preparing the `user_config.json` and `env.json` Configuration Files

You can obtain the [user_config.json](../../../../examples/infer_engines/vllm/user_config.json) and [env.json](../../../../examples/infer_engines/vllm/env.json) templates from the following paths. This document primarily introduces adaptation related to the docker-only deployment method. For other features, refer to [quick_start](../../user_guide/quick_start.md).

For single-container scenarios, specify the single-container deployment mode and update the default port in `user_config.json`.

- **motor_coordinator_config.api_config.coordinator_api_infer_port**: coordinator inference port (default 1025).

- **motor_coordinator_config.api_config.coordinator_api_mgmt_port**: coordinator management port (default 1026).

- **motor_controller_config.api_config.controller_api_port**: controller management port (default 1026).

- **motor_nodemanger_config.api_config.node_manager_port**: nodemanger management port (default 1026).

- **motor_deploy_config.deploy_mode**: The value **single_container** indicates a single-container scenario, while other values indicate multi-container.

- **motor_coordinator_config.scheduler_config.deploy_mode**: The value **pd_disaggregation_single_container** indicates single-container scheduling mode, while other values indicate multi-container deployment.

The following is an example:

```json
{
  "motor_deploy_config": {
    ...
    "deploy_mode": "single_container"
  },
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
    "scheduler_config": {
      "deploy_mode": "pd_disaggregation_single_container"
    }
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
CONFIGMAP_PATH="xxx" # Service startup script path, which must be mounted into the container
USER_CONFIG_PATH="xxx" # user_config.json path
ENV_PATH="xxx" # env.json path

mkdir -p $CONFIGMAP_PATH
# The container startup script boot.sh calls other scripts in the startup directory at runtime, so they need to be copied together to the $CONFIGMAP_PATH directory
cp -f $EXAMPLES_PATH/deployer/startup/boot.sh $CONFIGMAP_PATH/boot.sh
cp -f $EXAMPLES_PATH/deployer/startup/common.sh $CONFIGMAP_PATH/common.sh
cp -f $EXAMPLES_PATH/deployer/startup/hccl_tools.py $CONFIGMAP_PATH/hccl_tools.py
cp -f $EXAMPLES_PATH/deployer/startup/mooncake_config.py $CONFIGMAP_PATH/mooncake_config.py
cp -f $EXAMPLES_PATH/deployer/startup/roles/* $CONFIGMAP_PATH/

# Copy the prepared user_config.json and env.json configuration files to the $CONFIGMAP_PATH directory
cp -f $USER_CONFIG_PATH $CONFIGMAP_PATH/user_config.json
cp -f $ENV_PATH $CONFIGMAP_PATH/env.json

# If environment variables have already been loaded but changes have been made, clear the old environment variables first
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

# Load the environment variables from user_config.json and env.json and apply them to the container startup script.
python $EXAMPLES_PATH/deployer/startup/set_env_docker.py --configmap_path $CONFIGMAP_PATH
```

Execution method:

```bash
sh prepare.sh
```

### Starting the Service with Docker

Prepare the startup script start_docker.sh. Example below — update CONFIGMAP_PATH and IMAGE_NAME with your actual values:

```shell
# Privileged container is disabled by default. To enable it, change --privileged=false to --privileged=true
CONFIGMAP_PATH="xxx" # CONFIGMAP_PATH must be match with prepare.sh and must use an absolute path
IMAGE_NAME="xxx" # Image name

# Read visible devices from environment variables. By default, host Ascend devices are automatically detected and concatenated with commas, such as "0,1,2,3"
if [ -z "$ASCEND_VISIBLE_DEVICES" ]; then
    ASCEND_VISIBLE_DEVICES=$(ls /dev/davinci[0-9]* 2>/dev/null | sed 's/[^0-9]//g' | paste -sd "," -)
fi
ASCEND_DEVICES="--device=/dev/davinci_manager --device=/dev/devmm_svm --device=/dev/hisi_hdc"
# Loop mount the cards specified by ASCEND_VISIBLE_DEVICES
IFS=',' read -ra ADDR <<< "$ASCEND_VISIBLE_DEVICES"
for i in "${ADDR[@]}"; do
    ASCEND_DEVICES="$ASCEND_DEVICES --device=/dev/davinci$i"
done

docker run -u root --rm --name single_container \
-e ASCEND_RUNTIME_OPTIONS=NODRV --privileged=false \
-e CONFIGMAP_PATH=$CONFIGMAP_PATH \
-e CONFIG_PATH=/usr/local/Ascend/pyMotor/conf \
-e ROLE=SINGLE_CONTAINER \
-e KVP_MASTER_SERVICE=$KVP_MASTER_SERVICE \
-e KV_POOL_PORT=$KV_POOL_PORT \
-e KV_POOL_EVICTION_HIGH_WATERMARK_RATIO=$KV_POOL_EVICTION_HIGH_WATERMARK_RATIO \
-e KV_POOL_EVICTION_RATIO=$KV_POOL_EVICTION_RATIO \
-p $ENDPOINT_PORT_RANGE:$ENDPOINT_PORT_RANGE \
-p $KV_PORT_RANGE:$KV_PORT_RANGE \
$ASCEND_DEVICES \
-v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
-v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/ \
-v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
-v /usr/local/sbin:/usr/local/sbin \
-v /var/log/npu/:/usr/slog \
-v /mnt:/mnt \
$IMAGE_NAME \
bash -c "export POD_IP=\$(grep \$(hostname) /etc/hosts | cut -f1) && source \$CONFIGMAP_PATH/boot.sh"
```

Environment variable description:

| Variable | Description | Value |
| :--- | :--- | :--- |
| `CONFIGMAP_PATH` | Path to the startup script | Must match the path in Section 2.2 and be mounted into the container. |
| `IMAGE_NAME` | Image name | Versioned image, which must be discoverable via `docker images`. |
| `ASCEND_VISIBLE_DEVICES` | Visible devices | List of devices to mount, e.g., `"0,1,2,3"`. Defaults to auto-detected host Ascend devices |
| `ENDPOINT_PORT_RANGE` | Port range for endpoint mapping | For non-host network deployments, endpoint port mapping is configured with a default starting port of `10000`. Ports are allocated in the order of P first, then D, with an offset of 2 per DP port, corresponding to inference and management. |
| `KV_PORT_RANGE` | Port range for kv_port mapping | For non-host network deployments, configure the `kv_port` port mapping. The starting port is specified by the `kv_port` value under `motor_engine_prefill_config` in `user-config.json`. Ports are assigned sequentially, with P first, then D, incrementing by 1 per instance. |
| `KVP_MASTER_SERVICE` | Domain name for mooncake_master deployment | If KV pool is enabled, set to any non-empty string (e.g., `kvp_master`); `boot.sh` will auto-resolve it to the container IP. If disabled, leave empty. |
| `KV_POOL_PORT` | Port for mooncake_master service | If KV pool is enabled, set to any valid port (e.g., `50088`). If disabled, leave empty. |
| `KV_POOL_EVICTION_HIGH_WATERMARK_RATIO` | High watermark ratio for mooncake_master eviction | If KV pool is enabled, value in range `[0,1]`. If disabled, leave empty. |
| `KV_POOL_EVICTION_RATIO` | Eviction ratio for mooncake_master | If KV pool is enabled, value in range `[0,1]`. If disabled, leave empty. |

Service startup example (1P1D):

```shell
# If pooling is enabled, set KVP_MASTER_SERVICE to any non-empty string, such as kvp_master. If pooling is not enabled, set it to empty.
ASCEND_VISIBLE_DEVICES=0,1 KVP_MASTER_SERVICE="" KV_POOL_PORT=50088 KV_POOL_EVICTION_HIGH_WATERMARK_RATIO=0.9 KV_POOL_EVICTION_RATIO=0.1 sh start_docker.sh
```
