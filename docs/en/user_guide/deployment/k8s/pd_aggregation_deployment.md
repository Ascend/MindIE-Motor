# PD-Aggregated Deployment (Based on the vLLM Inference Engine)

## Scenario Introduction

### Introduction to PD Aggregation

PD aggregation deploys the Prefill and Decode instances in the same type of native engine instance. During deployment, the prefill and decode roles are no longer started separately; instead, the `union` role carries the complete inference capability. The Coordinator distributes requests to available union instances in the `single_node` scheduling mode.

Compared with [PD disaggregation](./pd_disaggregation_deployment.md), PD aggregation eliminates the P/D role splitting and the KV cross-role transfer configuration, and is suitable for scenarios such as quick verification, small- and medium-scale services, limited resources, or when independent planning of the P/D instance ratio is not yet required. If the service requires independent resource planning for the Prefill and Decode phases, independent scaling, or the use of PD disaggregation-related capabilities, PD disaggregation deployment is recommended.

### Deployment Entry Points and Process

The deployment process revolves around three entry points:

- `user_config.json`: the overall configuration for deployment and services. For PD aggregation, focus on the `hybrid_*` fields, `motor_engine_union_config`, and the `single_node` scheduling mode of the Coordinator.
- `env.json`: environment variables for each component. For PD aggregation, the native engine environment variables are configured in the `motor_engine_union_env` field.
- `deploy.py`: reads the `user_config.json` and `env.json` configuration files, generates K8s YAML files, updates the startup scripts, creates ConfigMaps, and runs "kubectl apply".

**Deployment Methods**:

- (Recommended) CRD method (`infer_service_set`): PD aggregation uses the CRD method (`infer_service_set`) by default, and the aggregation instances are started by the `union` role in `InferServiceSet`.
- Multi-YAML deployment method: explicitly set the `motor_deploy_config.deploy_mode` field to `multi_deployment`.

### Restrictions and Constraints

- This feature is supported on Atlas 800I A2 and Atlas 800I A3.
- The supported model range is the same as that of the selected inference engine. For details, see the [model support list](https://www.hiascend.com/en-sg/developer/models).
- `hybrid_instances_num` indicates the number of union instances. During scaling, only this field can be modified.
- `hybrid_pod_npu_num`, the parallelism parameters, and the model path must be consistent with the actual hardware resources and the model weight path.
- For the CRD method, the MindCluster InferServiceSet CRD and the corresponding controller must be installed in the cluster in advance.

### Hardware Environment

The hardware environments supported by PD aggregation are as follows.

**Table 1** Hardware list supported by PD aggregation

| Type | Model | Memory |
|------|------|------|
| Server | Atlas 800I A2 | 32 GB/64 GB |
| Server | Atlas 800I A3 | 64 GB |

>[!NOTE] Note
>
>- The cluster must have parameter-plane interconnection, that is, the ports corresponding to the server NPU cards are in the same VLAN and can communicate with each other through RoCE.
>- To ensure stable service running, users should strictly control the permissions of self-created Pods to prevent high-privilege Pods from modifying MindIE internal parameters and causing exceptions.

## Preparing the Image

Before deploying the PD aggregation service, prepare a usable inference image on each compute node.

- Method 1: Download the official complete MindIE Motor image

  Go to the [Ascend official image repository](https://www.hiascend.com/developer/ascendhub), enter `motor` in the search box, click the "mindie-motor" card, and download the corresponding MindIE Motor image based on the device model on the "Image Version" tab.
- Method 2: Install MindIE Motor in an existing image

  The base image already has components such as CANN, vLLM, and vLLM Ascend installed. To additionally install MindIE Motor, see [Installing MindIE Motor Based on the vllm-ascend/sglang Image](../../maintenance/build_motor_image_from_vllm_ascend.md#installing-mindie-motor-based-on-the-vllm-ascendsglang-image).

After obtaining the image, use the following command to load the image to the server:

   ```bash
   docker load -i xxxx.tar
   ```

After the image is imported, use the following command to check whether the Docker image exists:

   ```bash
   docker images
   ```

>[!NOTE] Note
>
>All K8s nodes participating in the deployment must be able to load or pull the image specified by `image_name` locally. Otherwise, the Pod may be in the ImagePullBackOff or ErrImagePull state because the image is unavailable.

## Deployment Directory Structure

Upload the examples directory in this repository to the master node of the K8s cluster. The examples directory can be obtained in either of the following two ways:

- Obtain from this code repository: upload the examples directory under the repository root to the master server.

- Obtain from the container image: if the complete code repository is unavailable but the MindIE Motor inference image has been pulled, use the example directory preinstalled in the image. The path is /tmp/motor/examples (the directory structure is consistent with the examples directory in the repository).

  Run the following commands on the machine where the image has been pulled (replace `IMAGE` with the actual image name or image ID, which can be consistent with the value of the motor_deploy_config.image_name field in the user_config.json configuration file):

  ```bash
  # Define a variable IMAGE that stores the image name or image ID
  IMAGE="<image name or image ID>"
  # Create a temporary container that is not started, and record the container ID
  cid=$(docker create "$IMAGE")
  # Copy the /tmp/motor/examples directory in the container to the current directory on the host
  docker cp "$cid:/tmp/motor/examples" ./examples
  # Delete the temporary container $cid created above to release resources and avoid leaving useless containers
  docker rm "$cid"
  ```

  >[!NOTE] Note
  >If Podman commands are used, replace `docker` with `podman` in the preceding commands.

The main file and directory structure related to PD aggregation in the examples directory is as follows:

```text
examples/
├── deployer/                  # Deployment tool directory
│   ├── deploy.py              # Deployment entry script
│   ├── delete.sh              # Uninstallation script
│   ├── show_log.sh            # Log viewing script
│   ├── yaml_template/         # K8s YAML template
│   ├── startup/               # Startup script
│   ├── log_collect/           # Log collection
│   └── output_yamls/          # Generated YAML output directory
└── model_configs/
    └── vllm/
        └── pd_hybrid/
            ├── user_config.json   # PD aggregation user configuration example
            ├── env.json           # PD aggregation environment variable configuration example
            └── README.md          # Example description
```

- For the PD aggregation example configuration, see [PD Aggregation Deployment Example](../../../../../model_configs/vllm/pd_hybrid/README.md).
- For details about how to use the deployment tool, see [Deployer Deployment Tool](../../../../../examples/deployer/README.md).

## Configuration Files

### Configuring the user_config.json File

For PD aggregation, you can directly refer to the model_configs/vllm/pd_hybrid/user_config.json file in the code repository for configuration.

**motor_deploy_config**

The parameters under the motor_deploy_config field are mainly used for deployment- and resource-related configuration. The configuration example is as follows:

```json
"motor_deploy_config": {
  "deploy_mode": "infer_service_set",
  "hybrid_instances_num": 1,
  "single_hybrid_instance_pod_num": 1,
  "hybrid_pod_npu_num": 2,
  "image_name": "",
  "job_id": "mindie-motor",
  "hardware_type": "800I_A3",
  "weight_mount_path": "/mnt/weight/"
}
```

**Table 2** Description of motor_deploy_config field parameters

| Configuration Item | Type | Description |
|--------|------|------|
| deploy_mode | string | Deployment mode. If this item is not configured, the default value is infer_service_set.<br>For PD aggregation, the infer_service_set method is recommended. |
| hybrid_instances_num | int | Number of union instances, ranging from 1 to 16. |
| single_hybrid_instance_pod_num | int | Number of Pods corresponding to a single union instance, greater than or equal to 1. |
| hybrid_pod_npu_num | int | Number of NPU cards occupied by a single union Pod. |
| image_name | string | Name of the inference image, which must contain the MindIE Motor and inference engine runtime environment. |
| job_id | string | Name of the deployment task, which is also used as the K8s namespace, for example, mindie-motor. |
| hardware_type | string | Hardware type. The value is 800I_A2 or 800I_A3. |
| weight_mount_path | string | Mount path of the model weights on the host. The model in the container must be consistent with this mount path. |

**motor_coordinator_config**

In the PD aggregation scenario, the Coordinator scheduling mode does not need to be configured. The Coordinator automatically selects PDHybridRouter based on the running union instances.

**Configuration example (default load balancing)**:

```json
"motor_coordinator_config": {}
```

**Configuration example (KV Cache affinity scheduling, optional)**:

When KV Cache affinity scheduling is enabled, modify the motor_coordinator_config and motor_engine_union_config fields in the model_configs/vllm/pd_hybrid/user_config.json configuration file as shown in the following example, and add the kv_conductor_config field. For details, see [KV Cache Affinity Scheduling](../../features/kvcache_affinity.md).

```json
"motor_coordinator_config": {
  "scheduler_config": {
    "scheduler_type": "kv_cache_affinity",
    "kv_affinity": {
      "mode": "unified",
      "load_weight": 1.0
    }
  }
}
```

**Table 3** Description of KV Cache affinity scheduling parameters

| Configuration Item | Type | Description |
|--------|------|------|
| scheduler_config.scheduler_type | string | Scheduling type. The default value is load_balance.<br>When KV Cache affinity scheduling is enabled, set it to kv_cache_affinity. |
| scheduler_config.kv_affinity.mode | string | KV affinity sub-policy.<ul><li>unified (default)</li><li>load_gated</li></ul> |
| scheduler_config.kv_affinity.load_weight | float | Load weight in unified mode. The default value is 1.0. |
| scheduler_config.kv_affinity.overlap_credit | float | Discount coefficient of the cached prefix on the prefill cost. The default value is 1.0. |
| scheduler_config.kv_affinity.prefill_load_scale | float | Prefill cost weight in unified mode. The default value is 1.0. |
| scheduler_config.kv_affinity.load_gate_topn | int | Minimum number of load endpoints retained in load_gated mode. When the value is 0, it falls back to 2. |
| scheduler_config.kv_affinity.w_npu / w_cpu / w_disk | float | Weights of hit blocks in mutually exclusive media. The default value is 1.0/1.0/0.0. |
| scheduler_config.kv_affinity.hit_rate_threshold | float | Affinity hit rate threshold, in the range [0, 1]. The default value 0 indicates that the threshold is disabled.<br>When the value is greater than 0, affinity is used only if the maximum prefix hit rate is higher than the threshold; otherwise, load balancing is used. |

**motor_engine_union_config**

The motor_engine_union_config field is used to configure the PD aggregation native engine. Its structure is similar to that of the motor_engine_prefill_config/motor_engine_decode_config fields in PD disaggregation, but the two engine sets for P and D do not need to be configured separately, and the producer/consumer roles of the kv_transfer_config field do not need to be configured. The configuration example is as follows.

```json
"motor_engine_union_config": {
  "engine_type": "vllm",
  "engine_config": {
    "served_model_name": "qwen3-8B",
    "model": "/mnt/weight/qwen3_8B",
    "gpu_memory_utilization": 0.9,
    "data_parallel_size": 1,
    "tensor_parallel_size": 1,
    "pipeline_parallel_size": 1,
    "enable_expert_parallel": false,
    "data_parallel_rpc_port": 9000,
    "enforce-eager": true,
    "max_model_len": 2048
  }
}
```

**Table 4** Description of motor_engine_union_config field parameters

| Configuration Item | Type | Description |
|--------|------|------|
| engine_type | string | Engine type, for example, vllm. |
| engine_config | object | Engine-related configuration, including model information, parallelism strategy, and engine-native parameters. |
| engine_config.served_model_name | string | Model name exposed to external services. |
| engine_config.model | string | Model weights path in the container, which must be consistent with the path after the `weight_mount_path` is mounted. |
| engine_config.gpu_memory_utilization | float | Upper limit of the NPU memory usage ratio, in the range [0, 1]. |
| engine_config.data_parallel_size | int | Data parallel size. |
| engine_config.tensor_parallel_size | int | Tensor parallel size. |
| engine_config.pipeline_parallel_size | int | Pipeline parallel size. |
| engine_config.enable_expert_parallel | bool | Whether to enable EP. |
| engine_config.data_parallel_rpc_port | int | RPC port on the DP side. |
| engine_config.max_model_len | int | Maximum model context length. |
| engine_config.kv-events-config | object | Configures KV event publishing when KV Cache affinity is enabled (see [KV Cache Affinity Deployment](../../features/kvcache_affinity.md)). |
| engine_config.enable-prefix-caching | bool | It is recommended to enable prefix caching when KV Cache affinity is enabled. |
| Other keys | - | Engine-native parameters. Fill them in directly according to the documentation of the selected engine. |

>[!NOTE] Note
>When KV Cache affinity scheduling is enabled, the Coordinator automatically merges prefill_kv_event_config from the motor_engine_union_config.engine_config.kv-events-config field, and the motor_engine_prefill_config field does not need to be configured again. In addition, the kv_conductor_config field (containing at least the http_server_port parameter) must be configured at the root node of the user_config.json configuration file.

### Configuring the env.json File

For PD aggregation, you can directly refer to the model_configs/vllm/pd_hybrid/env.json file in the code repository for configuration. The environment variables of the PD aggregation native engine are configured in the motor_engine_union_env field. The configuration example is as follows.

```json
{
  "version": "2.0.0",
  "motor_common_env": {
    "CANN_INSTALL_PATH": "/usr/local/Ascend",
    "MOTOR_LOG_ROOT_PATH": "/root/ascend/log"
  },
  "motor_engine_union_env": {
    "HCCL_BUFFSIZE": 200,
    "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
    "HCCL_OP_EXPANSION_MODE": "AIV",
    "OMP_PROC_BIND": "false",
    "OMP_NUM_THREADS": 100,
    "ASCEND_BUFFER_POOL": "0:0"
  }
}
```

**Table 5** Description of key parameters in the env.json file

| Configuration Item | Description |
|--------|------|
| motor_common_env | Environment variables shared by all components, such as the CANN installation path and the log root directory. |
| motor_engine_union_env | NPU, HCCL, OMP, and other environment variables of union instances, which can be tuned based on the server model and the model. |

Save the file after modification. You do not need to manually modify the startup script; the next time deploy.py is executed, the preceding environment variables are regenerated and injected.

## Deploying the Service

### Security and Permission Notes

- It is recommended that the deployment script be executed by the K8s cluster administrator to avoid the risk of arbitrary command execution or container escape caused by tampering with the script or configuration.
- The write, update, and delete permissions of MindIE-related ConfigMaps (for example, motor-config) must be strictly controlled.
- When modifying YAML templates, use secure images and secure mount paths, and avoid soft links, dangerous system paths, and service-sensitive paths.

### Prerequisites

- Kubernetes, MindCluster, the NPU driver, and firmware have been set up as described in [Environment Setup](../../environment_preparation.md).
- A namespace with the same name as `job_id` has been created, for example:

  ```bash
  kubectl create namespace mindie-motor
  ```

- The model weights on the host have been placed in the path specified by the weight_mount_path parameter (for example, /mnt/weight/).
- The inference image specified by image_name has been prepared on each compute node.

### Deployment Commands

Run the following commands in the /examples/deployer directory. Two ways of specifying the configuration are supported.

- Method 1: Specify the configuration directory (recommended)

  ```bash
  cd examples/deployer
  python3 deploy.py --config_dir ../../model_configs/vllm/pd_hybrid
  ```

  >[!NOTE] Note
  >If you only need to check the YAML generation, add the `--dry-run` parameter to the command, as shown in the following example:
  >
  >```bash
  > python3 deploy.py --config_dir ../../model_configs/vllm/pd_hybrid --dry-run
  >```
  >
  >The `deploy.py` deployment script performs the following steps in sequence:
  >
  >1. Read the user_config.json and env.json configuration files.
  >2. Generate the K8s resources for the Controller, Coordinator, and union instances based on motor_deploy_config.
  >3. Write environment variables such as motor_engine_union_env into the startup script.
  >4. Create or update the motor-config ConfigMap.
  >5. Run kubectl apply to start the service.

- Method 2: Specify the configuration file paths separately

  ```bash
  cd examples/deployer
  python3 deploy.py \
    --user_config_path ../../model_configs/vllm/pd_hybrid/user_config.json \
    --env_config_path ../../model_configs/vllm/pd_hybrid/env.json
  ```

>[!NOTE] Note
>If the deployment fails, uninstall the cluster first, troubleshoot and modify the configuration, and then redeploy.

### Viewing Cluster Status and Logs

- **View the Pod list**:

  ```bash
  kubectl get pods -n <job_id>
  ```

  In the default CRD mode, InferServiceSet starts the Pods corresponding to the controller, coordinator, and union roles. A Pod status of Running only indicates that the Pod has been successfully scheduled and started; whether the service is ready still needs to be further confirmed based on the logs.

- **View logs**:

  1. Configure log_collect/log_config.ini and set the name_space attribute to the actual namespace, which is mindie-motor here.

  2. Run the following commands in the /examples/deployer directory to obtain/view the log path.

      ```bash
      cd examples/deployer
      bash show_log.sh
      ```

      Log generation path: examples/deployer/log_collect/log/\<YYYYMMDD_hhmmss>.

      The log file of a single Pod is named in the format \<pod_name>\_\<node_name>\_\<retry_count>.log, for example, vllm-0-controller-0-xxxx_node01_0.log.
  3. Use the following command to view the log content.

      ```bash
      tail -f log_collect/log/<YYYYMMDD_hhmmss>/<pod_name>_<node_name>_<retry_count>.log
      ```

- **View the log of a single Pod**:

  ```bash
  kubectl logs <pod_name> -n <job_id>
  ```

- **Enter the container to view logs**:

  ```bash
  kubectl exec -it <pod_name> -n <job_id> -- bash
  ```

## Sending Inference Requests

After the service is ready, you can use the /v1/chat/completions API to test whether the service is started properly. The inference entry point is the port exposed by the Coordinator (31015 by default). Replace `<IP>` with the actual access address, and replace `model` with the model name configured by the `served_model_name` parameter under the motor_engine_union_config field in the user_config.json configuration file.

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

If {"detail":"Service is not available"} is returned, the service is not ready yet. Retry later and check the Pod logs. If streaming JSON is returned, inference is working properly.

>[!NOTE] Note
>
>The HTTP protocol poses security risks. In production environments, you are advised to enable HTTPS.

## Manual Scaling

To scale PD aggregation, modify only the hybrid_instances_num parameter under the motor_deploy_config field in the user_config.json configuration file, and then run the following command in the examples/deployer directory:

```bash
python3 deploy.py --config_dir ../../model_configs/vllm/pd_hybrid --update_instance_num
```

>[!NOTE] Note
>
>- The value of the hybrid_instances_num parameter must be in the range (0, 16].
>- The scaling baseline comes from the cluster ConfigMap motor-config.
>- Only the hybrid_instances_num parameter under the motor_deploy_config field in the user_config.json configuration file can be modified. Modifying other parameters at the same time is not allowed.
>- In the default CRD mode, the script updates the replicas of the union role in infer_service.yaml and then runs apply, and the CRD controller completes the scaling.

For more details about manual scaling, see [Manual Scaling](../../features/manual_scaling.md).

## Uninstallation

Run the following command in the /examples/deployer directory to delete the K8s ConfigMap in the namespace corresponding to the current job_id and the YAML files that have been applied, and to clean up the environment variable functions injected by deploy.py in the startup script.

```bash
bash delete.sh <namespace>
```

For example:

```bash
bash delete.sh mindie-motor
```

>[!NOTE] Note
>Replace the namespace with the name that is actually created. The uninstallation script must be run in the /examples/deployer directory; otherwise, the output_yamls path cannot be found correctly and an error is reported.

## Troubleshooting and Precautions

- **Image and weights**: Ensure that image_name can be pulled normally within the cluster, that weight_mount_path exists on the host, and that engine_config.model points to the correct path in the container.
- **Incorrect instance role**: For PD aggregation, ensure that the deployment produces a `union` role instance. The Coordinator automatically identifies it, and no additional scheduling mode configuration is required.
- **Impact of the Prefix Cache feature on performance testing**: Prefix Cache is enabled by default. If you want to obtain baseline performance data of the inference service, add "no-enable-prefix-caching": true to the engine_config of vLLM.
