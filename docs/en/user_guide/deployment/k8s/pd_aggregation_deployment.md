# PD Co-location Deployment

## Scenario Introduction

### Introduction to PD Co-location Deployment

**PD co-location deployment** deploys the Prefill and Decode capabilities in the same type of Engine Server instance. During deployment, the prefill and decode roles are no longer started separately; instead, the `union` role carries the complete inference capability. The Coordinator distributes requests to available union instances in the `single_node` scheduling mode.

Compared with [PD disaggregation deployment](./pd_disaggregation_deployment.md), PD co-location deployment eliminates the P/D role splitting and KV cross-role transfer configuration, and is suitable for scenarios such as quick verification, small- and medium-scale services, limited resources, or when independent planning of the P/D instance ratio is not yet required. If the service requires independent resource planning for the Prefill and Decode phases, independent scaling, or the use of PD disaggregation-related capabilities, PD disaggregation deployment is recommended.

### Deployment Entry Points and Process

The deployment process revolves around three entry points:

1. `user_config.json`: the overall configuration for deployment and services. For PD co-location deployment, focus on the `hybrid_*` fields, `motor_engine_union_config`, and the `single_node` scheduling mode of the Coordinator.

2. `env.json`: environment variables for each component. For PD co-location deployment, the Engine Server environment variables are configured in `motor_engine_union_env`.

3. Deployment script `deploy.py`: reads the preceding configurations, generates K8s YAML files, updates the startup scripts, creates ConfigMaps, and executes `kubectl apply`.

**Deployment method**: PD co-location deployment uses the CRD method (`infer_service_set`) by default, where the `union` role in InferServiceSet starts the mixed deployment instances. If you need to continue using the traditional multi-YAML Deployment, you can explicitly set `multi_deployment` in `motor_deploy_config.deploy_mode`; however, the default CRD method is recommended.

### Restrictions and Constraints

- This feature is supported on the Atlas 800I A2 inference server and the Atlas 800I A3 SuperPoD server.

- The supported model range is the same as that of the selected inference engine (for example, vLLM Ascend).

- `hybrid_instances_num` indicates the number of union instances. During scaling, only this field is allowed to be modified.

- `hybrid_pod_npu_num`, parallelism parameters, and the model path must be consistent with the actual hardware resources and the model weights path.

- For the CRD method, the MindCluster InferServiceSet CRD and the corresponding controller must be installed in the cluster in advance.

### Hardware Environment

The hardware environments supported by PD co-location deployment are as follows.

**Table 1** Hardware list supported by PD co-location deployment

| Type | Model | Memory |
|------|------|------|
| Server | Atlas 800I A2 inference server | 32 GB/64 GB |
| Server | Atlas 800I A3 SuperPoD server | 64 GB |

>[!NOTE]NOTE
>
>- The cluster must have parameter-plane interconnection: that is, the ports corresponding to the server NPU cards are in the same VLAN and can communicate with each other through RoCE.
>- To ensure stable service running, users should strictly control the permissions of self-created Pods to prevent high-privilege Pods from modifying MindIE internal parameters and causing exceptions.

## Preparing the Image

Before deploying the PD co-location deployment service, prepare a usable inference image on each compute node. The image requirements are consistent with those of PD disaggregation deployment: the image must include MindIE Motor, the selected inference engine (such as vLLM), and its Ascend adaptation components. For image acquisition, offline import, and custom build methods, see the "Preparing the Image" section in [PD Separate Service Deployment](./pd_disaggregation_deployment.md).

>[!NOTE]NOTE
>
>All K8s nodes participating in the deployment must be able to load or pull the image specified by `image_name` locally. Otherwise, the Pod may remain in the `ImagePullBackOff` or `ErrImagePull` state because the image is unavailable.

## Deployment Directory Structure

Upload the `examples` directory in this repository to the master node of the K8s cluster. The `examples` directory can be obtained in either of the following two ways:

- **Obtain from this code repository**: Upload the `examples` directory under the repository root to the master server.

- **Obtain from the container image**: If the complete code repository is unavailable but the MindIE Motor inference image has been pulled, use the preconfigured example directory in the image at **`/tmp/motor/examples`** (the directory structure is consistent with the `examples` directory in the repository). Run the following command on the machine where the image has been pulled (replace `IMAGE` with the actual image name or image ID, which can be consistent with `motor_deploy_config.image_name` in `user_config.json`):

  ```bash
  IMAGE="<image name or image ID>"

  cid=$(docker create "$IMAGE")
  docker cp "$cid:/tmp/motor/examples" ./examples
  docker rm "$cid"
  ```

  Upload the obtained `examples` directory to the master server.

  If Podman is used, replace `docker` in the command with `podman`.

The main directory structure related to PD co-location deployment in **`examples`** is as follows:

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
└── infer_engines/
    └── vllm/
        └── pd_hybrid/
            ├── user_config.json   # PD co-location user configuration example
            ├── env.json           # PD co-location environment variable configuration example
            └── README.md          # Example description
```

- The PD co-location example configuration is located at `examples/infer_engines/vllm/pd_hybrid/`.

- For details about how to use the deployment tool, see `examples/deployer/README.md`.

## Preparing Model-related Configuration Files

You can refer to [**MindIE Motor Configuration Auto-generation Guide**](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/models/README.md) to **automatically generate** the configuration files `user_config.json` and `env.json`. If manual configuration is required, refer to the following content.

### Configuring `user_config.json`

For PD co-location deployment, you can directly refer to `examples/infer_engines/vllm/pd_hybrid/user_config.json`. The root node of this file contains `version`, `motor_deploy_config`, `motor_controller_config`, `motor_coordinator_config`, and `motor_engine_union_config`.

- **`motor_deploy_config` (deployment and resources)**

  `motor_deploy_config` contains deployment- and resource-related configuration.

  **Configuration example**:

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

  **Configuration item description**:

  | Configuration Item | Type | Description |
  |--------|------|------|
  | deploy_mode | string | Deployment mode. For PD co-location deployment, `infer_service_set` is recommended. If this item is not configured, this mode is used by default. |
  | hybrid_instances_num | int | Number of union instances, ranging from 1 to 16. |
  | single_hybrid_instance_pod_num | int | Number of Pods corresponding to a single union instance, greater than or equal to 1. |
  | hybrid_pod_npu_num | int | Number of NPU cards occupied by a single union Pod. |
  | image_name | string | Name of the inference image, which must contain the MindIE Motor and inference engine runtime environment. |
  | job_id | string | Name of the deployment task, which is also used as the K8s namespace, for example, `mindie-motor`. |
  | hardware_type | string | Hardware type: `800I_A2` or `800I_A3`. |
  | weight_mount_path | string | Mount path of the model weights on the host. The `model` in the container must be consistent with this mount path. |

- **`motor_coordinator_config`**

  In the PD co-location deployment scenario, you no longer need to configure the Coordinator scheduling mode. The Coordinator automatically selects `PDHybridRouter` based on the running `union` instances.

  **Configuration example (default load balancing)**:

  ```json
  "motor_coordinator_config": {}
  ```

  **Configuration example (KV cache affinity scheduling, optional)**:

  ```json
  "motor_coordinator_config": {
    "scheduler_config": {
      "scheduler_type": "kv_cache_affinity",
      "kv_affinity_mode": "unified",
      "kv_affinity_load_weight": 1.0
    }
  }
  ```

  When KV cache affinity is enabled, modify `motor_coordinator_config` and `motor_engine_union_config` in `examples/infer_engines/vllm/pd_hybrid/user_config.json` as shown in the preceding example and add `kv_conductor_config`. No separate configuration file is required. For KV Conductor installation and deployment instructions, see [KV Cache Affinity Deployment](../../features/KV_cache_affinity.md).

  | Configuration Item | Type | Description |
  |--------|------|------|
  | scheduler_config.scheduler_type | string | Scheduling type. Defaults to `load_balance`; set to `kv_cache_affinity` when KV affinity is enabled. |
  | scheduler_config.kv_affinity_mode | string | KV affinity sub-policy: `unified` (default) or `load_gated`. |
  | scheduler_config.kv_affinity_load_weight | float | Load weight in unified mode. Defaults to `1.0`. |
  | scheduler_config.kv_affinity_overlap_credit | float | Discount coefficient of the cached prefix on the prefill cost. Defaults to `1.0`. |
  | scheduler_config.kv_affinity_prefill_load_scale | float | Prefill cost weight in unified mode. Defaults to `1.0`. |
  | scheduler_config.kv_affinity_load_gate_topn | int | Minimum number of load endpoints retained in load_gated mode; falls back to `2` when set to `0`. |

- **`motor_engine_union_config` (mixed deployment engine)**

  `motor_engine_union_config` is used to configure the mixed deployment Engine Server. Its structure is similar to `motor_engine_prefill_config`/`motor_engine_decode_config` in PD disaggregation, but it does not require configuring separate P/D engine sets, nor does it require configuring the producer/consumer roles of `kv_transfer_config`.

  **Configuration example**:

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

  **Configuration item description**:

  | Configuration Item | Type | Description |
  |--------|------|------|
  | engine_type | string | Engine type, for example, `vllm`. |
  | engine_config | object | Engine-related configuration, including model information, parallelism strategy, and engine-native parameters. |
  | engine_config.served_model_name | string | Model name exposed to external services. |
  | engine_config.model | string | Model weights path inside the container, which must be consistent with the mounted `weight_mount_path`. |
  | engine_config.gpu_memory_utilization | float | Upper limit of NPU memory usage ratio, ranging from 0 to 1. |
  | engine_config.data_parallel_size | int | Data parallel size. |
  | engine_config.tensor_parallel_size | int | Tensor parallel size. |
  | engine_config.pipeline_parallel_size | int | Pipeline parallel size. |
  | engine_config.enable_expert_parallel | bool | Whether to enable EP. |
  | engine_config.data_parallel_rpc_port | int | RPC port on the DP side. |
  | engine_config.max_model_len | int | Maximum model context length. |
  | engine_config.kv-events-config | object | Configures KV event publishing when KV cache affinity is enabled (see [KV Cache Affinity Deployment](../../features/KV_cache_affinity.md)). |
  | engine_config.enable-prefix-caching | bool | It is recommended to enable prefix caching when KV cache affinity is enabled. |
  | Other keys | - | Engine-native parameters. Fill them in directly according to the selected engine documentation. |

  When KV cache affinity is enabled, the Coordinator **automatically merges** `prefill_kv_event_config` from `motor_engine_union_config.engine_config.kv-events-config` (no need to configure `motor_engine_prefill_config` again). In addition, `kv_conductor_config` (containing at least `http_server_port`) must be configured at the root node of `user_config.json`.

### Configuring `env.json`

For PD co-location deployment, you can directly refer to `examples/infer_engines/vllm/pd_hybrid/env.json`. The environment variables of the mixed deployment Engine Server are configured in `motor_engine_union_env`.

**Configuration example**:

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

| Configuration Item | Description |
|--------|------|
| motor_common_env | Environment variables shared by all components, such as the CANN installation path and the log root directory |
| motor_engine_union_env | NPU, HCCL, OMP, and other environment variables of the union instance, which can be tuned according to the server model and the model |

Save the file after modification. You do not need to manually modify the startup script. The next time `deploy.py` is executed, the preceding environment variables will be regenerated and injected.

## Executing Deployment (`deploy.py`)

### Security and Permission Notes

- It is recommended that the deployment script be executed by the K8s cluster administrator to avoid the risk of arbitrary command execution or container escape caused by tampering with the script or configuration.

- The write, update, and delete permissions of MindIE-related ConfigMaps (such as `motor-config`) must be strictly controlled.

- When modifying YAML templates, use secure images and secure mount paths, and avoid soft links, dangerous system paths, and sensitive business paths.

### Prerequisites

- Kubernetes, MindCluster, NPU driver, and firmware have been installed.

- A namespace with the same name as `job_id` has been created, for example:

  ```bash
  kubectl create namespace mindie-motor
  ```

- The model weights on the host have been placed in the path specified by `weight_mount_path` (for example, `/mnt/weight/`).

- The inference image specified by `image_name` has been prepared on each compute node.

### Deployment Command

Execute the command in the `examples/deployer` directory. Two methods of specifying the configuration are supported.

**(Recommended) Method 1: Specify the configuration directory**.

```bash
cd examples/deployer
python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid
```

**Method 2: Specify the configuration file path separately**.

```bash
cd examples/deployer
python3 deploy.py \
  --user_config_path ../infer_engines/vllm/pd_hybrid/user_config.json \
  --env_config_path ../infer_engines/vllm/pd_hybrid/env.json
```

If you need to check only the YAML generation, add `--dry-run`:

```bash
python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid --dry-run
```

`deploy.py` performs the following steps in sequence:

1. Read `user_config.json` and `env.json`.

2. Generate the K8s resources for the Controller, Coordinator, and union instances based on `motor_deploy_config`.

3. Write environment variables such as `motor_engine_union_env` into the startup script.

4. Create or update the `motor-config` ConfigMap.

5. Run `kubectl apply` to start the service.

### Viewing Cluster Status and Logs

- View the Pod list:

  ```bash
  kubectl get pods -n <job_id>
  ```

  In the default CRD mode, InferServiceSet starts the Pods corresponding to the controller, coordinator, and union roles. A Pod status of Running only indicates that it has been successfully scheduled and started; whether the service is ready still needs to be further confirmed based on the logs.

- To view logs, use `show_log.sh`.

  1. Configure `log_collect/log_config.ini` and set the `name_space` attribute to the actual namespace, which is `mindie-motor` here.

  2. Run `show_log.sh` in the `examples/deployer` directory to obtain/view logs.

      ```bash
      cd examples/deployer
      bash show_log.sh
      ```

      Logs are generated in the `examples/deployer/log_collect/log/<YYYYMMDD_hhmmss>` directory.

      The log file of a single Pod is named in the format `<pod_name>_<node_name>_<retry_count>.log`, for example, `vllm-0-controller-0-xxxx_node01_0.log`.

  3. You can use the `tail` command to view logs.

      ```bash
      cd examples/deployer
      tail -f log_collect/log/<YYYYMMDD_hhmmss>/<pod_name>_<node_name>_<retry_count>.log
      ```

- View the log of a single Pod directly.

  ```bash
  kubectl logs <pod_name> -n <job_id>
  ```

- To enter the container for troubleshooting, run the following command:

  ```bash
  kubectl exec -it <pod_name> -n <job_id> -- bash
  ```

## Sending Inference Requests

After the service is ready, you can test whether the service is started properly through the `/v1/chat/completions` API. The inference entry point is the port exposed by the Coordinator (default 31015). Replace `<IP>` with the actual access address and replace `model` with the model name configured in `served_model_name`.

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

If `{"detail":"Service is not available"}` is returned, the service is not ready yet. Retry later and check the Pod logs. If streaming JSON is returned, the inference is working properly.

>[!NOTE]NOTE
>
>The HTTP protocol poses security risks. In production environments, you are advised to enable HTTPS. For TLS configuration, see the `tls_config` section in [PD Disaggregation Deployment](./pd_disaggregation_deployment.md).

## Manual Scaling

For PD co-location deployment scaling, modify only `motor_deploy_config.hybrid_instances_num`, and then run:

```bash
cd examples/deployer
python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid --update_instance_num
```

NOTE:

- `hybrid_instances_num` must be greater than 0 and no more than 16.

- The scaling baseline comes from the cluster ConfigMap `motor-config`.

- Apart from `hybrid_instances_num`, no other configuration items may be modified at the same time.

- In the default CRD mode, the script updates the replicas of the union role in `infer_service.yaml` and then applies, and the CRD controller completes the scaling.

For more details, see [Manual Scaling User Guide](../../features/manual_scaling.md).

## Uninstallation

Run `delete.sh` in the `examples/deployer` directory to delete the K8s ConfigMap and the applied YAML in the namespace corresponding to the current `job_id`, and clean up the environment variable functions injected by `deploy.py` in the startup script.

```bash
cd examples/deployer
bash delete.sh <namespace>
```

For example:

```bash
bash delete.sh mindie-motor
```

>[!NOTE]NOTE
>Replace the namespace with the name actually created. The uninstallation script must be run in the `examples/deployer` directory; otherwise, the `output_yamls` path cannot be located correctly and an error is reported.

## Troubleshooting and Precautions

- **Service not ready**: If the inference API returns `{"detail":"Service is not available"}`, it is usually because the union instance or Coordinator is not fully ready. Wait for a while and retry, and check the Pod logs to confirm that there are no startup errors.

- **Image and weights**: Ensure that `image_name` can be pulled normally within the cluster, that `weight_mount_path` exists on the host, and that `engine_config.model` points to the correct path inside the container.

- **Incorrect instance count configuration**: `hybrid_instances_num` must be greater than 0 and no more than 16. During scaling, only this field is allowed to be modified.

- **Incorrect instance role**: For PD co-location deployment, ensure that the deployment produces a `union` role instance. The Coordinator automatically identifies it, and no additional scheduling mode configuration is required.

- **Deployment failure**: If the deployment fails, uninstall the cluster first, troubleshoot and modify the configuration, and then redeploy.

- **Impact of the Prefix Cache feature on performance testing**: Prefix Cache is enabled by default. If you want to obtain baseline performance data of the inference service, add `"no-enable-prefix-caching": true` to the `engine_config` of vLLM.
