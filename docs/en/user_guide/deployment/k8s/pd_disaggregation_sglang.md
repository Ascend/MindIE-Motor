# PD-Disaggregated Deployment (Based on SGLang Inference Engine)

This document describes how to complete a **PD disaggregation** deployment in a Kubernetes environment with the SGLang inference engine of MindIE Motor, and provides guidance on configuration optimization practices for production environments. If the underlying inference engine is vLLM, see [PD disaggregation deployment guide (vLLM inference engine)](./pd_disaggregation_deployment.md).

## Deployment Notes

| Item | Description |
| --- | --- |
| Configuration field | `engine_type: sglang` |
| Engine startup | `python3 -m sglang.launch_server` |
| PD coordination | bootstrap (`bootstrap_host` / `bootstrap_port` / `bootstrap_room`) |
| Mandatory ports | Business port, and `disaggregation-bootstrap-port` |
| Transport / Storage | Configure `disaggregation-transfer-backend: ascend`; the Deployer additionally starts **MF Store** |
| Health probe | Native generative `GET /health` |
| Configuration file generation | Use the reference configuration files under `model_configs/sglang/` and modify them for the actual environment |

>[!NOTE] Note
>The client accesses only the **Coordinator** northbound interfaces, so switching engines does not require changing the call method.

## Environment Pre-check

When deploying inference services with Ascend 950PR&950DT products, you need to prepare the **hixlep configuration path**. (Ignore this step for other hardware products.)

On each server, check whether the `/lib/route.conf` and `/etc/hccl_rootinfo.json` configuration files and the `/etc/hixlep` directory (UB link topology) exist and are correct. If they do not exist or are incorrect, see the [hixlep configuration file generation documentation](https://gitcode.com/cann/hixl/wiki/A5%20LocalCommRes%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md) to generate the corresponding content. When generating `/etc/hixlep`, use the "D2D scenario".

## Constraints and Limitations

| Item | Description |
| --- | --- |
| Anthropic interface `/v1/messages` | Depends on upstream SGLang. Only `/v1/messages/count_tokens` is available |
| chat streaming + rescheduling | Limited by the engine's `return_token_ids`; currently unavailable |

## Obtaining the Startup Scripts and Image

### Environment Requirements

- The inference nodes have completed [environment preparation](../../environment_preparation.md).
- The model weights have been placed in a path accessible to the cluster and are consistent with `weight_mount_path` / `model-path`.
- Both P and D have at least the number of NPUs required by the reference configuration. For details, see [Recommended Reference Configuration](#recommended-reference-configuration).

### Preparing the Image

Obtain the image as follows and load it to all nodes of the K8s cluster:

Manually install MindIE Motor based on the Ascend SGLang base image. See [Installing MindIE Motor based on the vllm-ascend/sglang image](../../maintenance/build_motor_image_from_vllm_ascend.md#installing-mindie-motor-based-on-the-vllm-ascendsglang-image).

### Preparing the `examples` Directory

Copy the `examples/` directory from the code repository to the K8s master node. The directory structure is as follows:

```text
examples/
├── deployer/                         # deploy.py / delete.sh / show_log.sh
└── model_configs/
    └── sglang/
        ├── user_config.json          # Common template
        ├── env.json
        ├── models/
        │   └── glm5.1/A3/            # GLM 5.1 Atlas 800I A3 reference configuration
        └── pd_hybrid/
            └── deepseek_v4_flash/A3/ # deepseek-v4-flash Atlas 800I A3 aggregation reference configuration
```

>[!NOTE] Note
>
>- For details about the service topology and the `deploy_mode` description, see [deployment mode configuration](./README.md).
>- For usage of the deployment tool, see `examples/deployer/README.md`.

## Preparing the Configuration File

Select a reference configuration from the repository directly, modify it for the actual environment, and then deploy it.

### Recommended Reference Configuration

| Typical scenario | Directory | Description |
| --- | --- | --- |
| <ul><li>GLM 5.1</li><li>Atlas 800I A3 1P1D</li></ul> | `model_configs/sglang/models/glm5.1/A3/` | GLM 5.1 model PD disaggregation reference; for more reference configurations, see [SGLang GLM 5.1 Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/glm_5_1) |
| <ul><li>GLM 5.2</li><li>Atlas 800I A3 1P1D</li></ul> | `model_configs/sglang/models/glm5.2/A3/` | GLM 5.2 model PD disaggregation reference; for more reference configurations, see [SGLang GLM 5.2 Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/glm_5_2) |
| <ul><li>Qwen3-8B</li><li>Atlas 800I A2</li></ul> | `model_configs/sglang/models/qwen_8b/A2/` | Low resource usage, suitable for getting the flow working; for more reference configurations, see [SGLang Qwen3-8B Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/qwen3_8b) |
| More models | `model_configs/sglang/` | The `user_config.json` and `env.json` configuration files in the root directory and subdirectories |

Each reference configuration directory must contain:

- `user_config.json`: deployment topology, Motor component configuration, and engine parameters.
- `env.json`: environment variables for each role.

### Configuring the `user_config.json` File

When configuring the `user_config.json` file in a reference configuration, confirm the parameters listed below:

| Field | Module | Description |
| --- | --- | --- |
| `image_name` | `motor_deploy_config` | Consistent with an image available in the cluster |
| `job_id` | `motor_deploy_config` | The K8s namespace name |
| `hardware_type` | `motor_deploy_config` | Hardware type, such as `800I_A2` or `800I_A3` |
| `weight_mount_path` | `motor_deploy_config` | Root path for mounting the model weights on the host |
| `p_*` / `d_*` | `motor_deploy_config` | Instance and card count; must match the node NPUs and the parallelism |
| `*_node_selector` | `motor_deploy_config` | Not mandatory; binds Controller / Coordinator / P / D / MF Store by cluster hostname |
| `coordinator_infer_node_port` etc. | `motor_deploy_config` | Not mandatory; avoids conflicts with existing NodePorts in the live network |
| `engine_type` | P/D engine section | Engine type, which must be `sglang` |
| `model-path` / `served-model-name` | `engine_config` | Weight path and external model name |
| `disaggregation-bootstrap-port` | **Prefill** `engine_config` | SGLang PD bootstrap port; if it is not configured, no bootstrap metadata is generated |

### Configuration Example

The following is only a Qwen3-8B example. For more detailed configurations, see the `model_configs/sglang/models/{model_name}/{hardware_type}/` directory.

```json
"motor_engine_prefill_config": {
  "engine_type": "sglang",
  "engine_config": {
    "served-model-name": "qwen3-8B",
    "model-path": "/mnt/weight/Qwen3-8B",
    "tp-size": 2,
    "pp-size": 1,
    "dp-size": 1,
    "mem-fraction-static": 0.7,
    "disaggregation-bootstrap-port": 9100,
    "disaggregation-transfer-backend": "ascend",
    "attention-backend": "ascend",
    "trust-remote-code": true
  }
},
"motor_engine_decode_config": {
  "engine_type": "sglang",
  "engine_config": {
    "served-model-name": "qwen3-8B",
    "model-path": "/mnt/weight/Qwen3-8B",
    "tp-size": 2,
    "pp-size": 1,
    "dp-size": 1,
    "mem-fraction-static": 0.7,
    "disaggregation-transfer-backend": "ascend",
    "attention-backend": "ascend",
    "trust-remote-code": true
  }
}
```

>[!NOTE] Note
>
>- The `engine_config` module is **passed through** as `sglang.launch_server` parameters by the NodeManager component, and the field names follow the CLI of the SGLang version in use (kebab-case is supported, and some fields also accept underscore).
>- On the Prefill side, `disaggregation-bootstrap-port` (or `disaggregation_bootstrap_port`) is written to `bootstrap_port` in the instance metadata for the SGLang Adapter of the Coordinator to perform P/D pairing. **Do not** mix it with the service port `endpoint_config.service_ports`.
>- For first-time setup, `scheduler_type: load_balance` and `reschedule_config.enable: false` are recommended. For advanced features, see [Feature Configuration](#feature-configuration).
>- For a full description of all fields, see the [user_config.json configuration reference](../../configuration/config_reference.md).

## Service Deployment and Verification

The following operations are performed on the K8s master node, using the GLM 5.1 model and an Atlas 800I A3 as an example.

### Starting the Service

Run the following commands to start the service:

```bash
# <namespace> must be the same as job_id in user_config.json
kubectl create namespace <namespace>

cd examples/deployer
python3 deploy.py --config_dir ../../model_configs/sglang/models/glm5.1/A3
```

>[!NOTE] Note
>
>- **Image and weights**: Confirm that each node can pull `image_name` and that `model-path` is readable in the container.
>- **NPU Pending**: Before deployment, check whether the resources of the target nodes are fully occupied by other tasks. Do not run Motor and pure SGLang on the same machine in parallel and compete for cards.
>- **Redeployment**: Fully uninstall and wait for the resources to be released before running `deploy.py`. Do not rely on `rollout restart` for configuration switching.
>- **PP scenario**: With multiple PP stages, ensure that the bootstrap and collective communication-related environment is consistent with the reference configuration, and that NodeManager can inject the environment variables related to PP bootstrap.
>- Loading the weights of a large model may take several minutes. Wait until the P/D instances are Ready before retrying.

### Checking the Status

Run the following command to check the service status:

```bash
kubectl get pods -n <namespace> -o wide
```

| Runtime Type (Example) | Description |
| --- | --- |
| `mindie-motor-controller-*` | Service management |
| `mindie-motor-coordinator-*` | Request scheduling / service entry point |
| `mindie-motor-mf-store-*` or `mf-store` | Serves KV Cache transfer |
| `sglang-p*` | Prefill instance |
| `sglang-d*` | Decode instance |
| `kv-conductor-*` (optional) | Serves the KV affinity feature |

>[!NOTE] Note
>A Pod in `Running` state does not mean that the service is fully ready; the actual startup status must be confirmed with the logs and `/v1/models`.

### Viewing Logs

Run the following command to view the service logs:

```bash
cd examples/deployer
# Edit log_collect/log_config.ini: change name_space to job_id
vim log_collect/log_config.ini
bash show_log.sh
```

### Verifying the Service

Send the following inference request to verify that the service is working properly:

```bash
# <node_ip>: IP of the Coordinator Infer NodePort node
# <infer_port>: coordinator_infer_node_port in user_config
# <model>: served-model-name in engine_config
curl -s http://<node_ip>:<infer_port>/v1/models

curl -X POST http://<node_ip>:<infer_port>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model>",
    "messages": [{"role": "user", "content": "Who are you?"}],
    "max_tokens": 360,
    "stream": true
  }'
```

- If `{"detail":"Service is not available"}` is returned: the service is not ready yet. Wait for P/D and MF Store to become Ready first, and check whether the Prefill fails to initialize SMEM / transport because MF Store is not ready.
- If streaming JSON is returned: inference is working normally. For more interfaces, see [Service interfaces](../../api/service_interfaces.md).

### Terminating the Service

After inference is complete, run the following command to terminate the service if needed:

```bash
bash delete.sh <namespace>
```

>[!NOTE] Note
>Before redeploying, confirm that the old Pods have exited and that the NPUs have been released, and then run `deploy.py`. Avoid relying only on `kubectl rollout restart` or a partial rebuild, which may cause persistent Pending.

## Feature Configuration

The configuration method of general capabilities is the same as that in [PD disaggregation deployment guide · Feature Configuration](./pd_disaggregation_deployment.md#feature-configuration). Only the points related to SGLang are described below.

### Virtual Inference Health Probe

- NodeManager performs `GET /health` readiness checks and heartbeats against the business port.
- SGLang uses the **native generative** `/health` (a lightweight generation is run when idle and can be skipped when busy), and Motor does **not** create a virtual-inference Worker.
- Setting `enable_virtual_inference` to `false` does **not** disable the SGLang generative `/health`.
- For details about this feature, see [Virtual Inference Health Probe](../../features/sim_inference.md).

### ChunkPrefill

Configure it in the `engine_config` module on the P side, for example:

```json
"chunked-prefill-size": 4096
```

This feature is mainly used to control the ultra-long Prefill chunks and to mitigate the mutual blocking of long and short requests. The value must be verified against the model and the available memory. For details, see [SGLang](https://github.com/sgl-project/sglang).

### Asymmetric TP / PCP / MTP

Asymmetric TP, PCP, and MTP are all native engine capabilities and can simply be passed through the `engine_config` module, for example:

- Asymmetric TP: the `tp-size` of P and D can differ. Related performance is not guaranteed for non-MLA models. For details, see [SGLang](https://github.com/sgl-project/sglang).
- PCP: settings on the Prefill side such as `enable-prefill-cp`, `cp-strategy`, and `attn-cp-size`. For details, see [SGLang](https://github.com/sgl-project/sglang).
- MTP: settings on the Decode side such as `speculative-algorithm` and the related draft parameters. For details, see [SGLang](https://github.com/sgl-project/sglang).

### Fault Rescheduling

This feature is disabled by default. Once enabled, the streaming/non-streaming completions scenarios and the non-streaming chat scenario are already supported. The streaming path depends on the engine returning token IDs to continue the stream. SGLang currently does not support enabling `return_token_ids` and `stream` at the same time for chat streaming, which makes chat streaming rescheduling unavailable. For details about this feature, see [Request rescheduling](../../features/fault_tolerance/rescheduler.md). The following is a sample `user_config.json` configuration file:

```json
"motor_coordinator_config": {
  "exception_config": {
    "reschedule_config": { "enable": true },
    "max_retry": 5,
    "transport_max_retry": 3,
    "retry_delay": 0.2
  }
}
```

### (Optional) KV Cache Affinity Scheduling

To enable KV Cache affinity scheduling, check whether the following parameters are all configured:

- Under the `motor_coordinator_config.scheduler_config` module, configure `scheduler_type: "kv_cache_affinity"`.
- Under the `motor_engine_prefill_config.engine_config.kv-events-config` module, configure `"publisher": "zmq"`, and do **not** disable the SGLang Radix (do not set `disable-radix-cache`).
- The `kv_conductor_config` module must have `http_server_port` configured.

```json
"motor_coordinator_config": {
  "scheduler_config": {
    "scheduler_type": "kv_cache_affinity",
    "kv_affinity": { "mode": "unified" }
  }
},
"motor_engine_prefill_config": {
  "engine_config": {
    "kv-events-config": {
      "publisher": "zmq",
      "enable_kv_cache_events": true,
      "endpoint": "tcp://*:5557",
      "replay_endpoint": "tcp://*:6667",
      "topic": "kv-events"
    }
  }
},
"kv_conductor_config": {
  "http_server_port": 13333,
  "block_size": 128,
  "engine_type": "sglang"
}
```

To disable this feature, change the setting back to `load_balance` and remove the `kv-events-config` and `kv_conductor_config` modules. For details about this feature, see [KV Cache affinity scheduling](../../features/kvcache_affinity.md).
