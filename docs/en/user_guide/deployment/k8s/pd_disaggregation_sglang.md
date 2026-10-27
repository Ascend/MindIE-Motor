# SGLang PD Disaggregation Deployment Guide

This document describes how to complete a K8s **PD disaggregation** deployment with the **SGLang** engine based on MindIE Motor.

The general PD disaggregation procedure is the same as that in the [PD disaggregation deployment guide](./pd_disaggregation_deployment.md). This document covers only **the mandatory changes for SGLang and the verified reference configuration usage**.

> [!NOTE]NOTE
>
> - Deploy by setting `engine_type` to `sglang`;
> - The client accesses only the **Coordinator** northbound interfaces, so switching engines does not require changing the call method.

## Deployment Notes

| Item | Description |
| --- | --- |
| Configuration field | `engine_type: sglang` |
| Engine startup | `python3 -m sglang.launch_server` |
| PD coordination | bootstrap (`bootstrap_host` / `bootstrap_port` / `bootstrap_room`) |
| Mandatory ports | Business port, and `disaggregation-bootstrap-port` |
| Transport / storage | Configure `disaggregation-transfer-backend: ascend`; the Deployer additionally starts **MF Store** |
| Health probe | Native generative `GET /health` |
| Configuration | Use the reference configurations under `model_configs/sglang/` and modify them for the actual environment |

## Pre-check for 950 Series Servers

Only when 950 series servers are used: on each server, check whether `/lib/route.conf`, `/etc/hccl_rootinfo.json`, and `/etc/hixlep` exist and are correct. If they do not exist or are incorrect, see the [hixlep configuration file generation documentation](https://gitcode.com/cann/hixl/wiki/A5%20LocalCommRes%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md), and use the "D2D scenario" when generating `/etc/hixlep`.

## Obtaining the Startup Scripts and Image

### Environment Requirements

- The inference nodes have completed [environment preparation](../../environment_preparation.md).

- The model weights have been placed in a path accessible to the cluster and are consistent with `weight_mount_path` / `model-path`.

- Both P and D have at least the number of NPUs required by the reference configuration (see "Recommended Reference Configuration" below).

### Preparing the Image

Load the image to **all relevant nodes** of the K8s cluster. Manually install MindIE Motor based on the Ascend SGLang base image. See [Installing MindIE Motor based on the vllm-ascend/sglang image](../../maintenance/build_motor_image_from_vllm_ascend.md#installing-mindie-motor-based-on-the-vllm-ascendsglang-image).

### Preparing examples

Place `examples` on the K8s master node by copying the `examples/` directory from the code repository.

## Preparing the Configuration File

Select a reference configuration from the repository directly and modify it for the actual environment before deployment.

### Recommended Reference Configuration

| Typical scenario | Directory | Description |
| --- | --- | --- |
| GLM 5.1 / Atlas 800I A3 1P1D | `model_configs/sglang/models/glm5.1/A3/` | GLM 5.1 PD disaggregation reference; for more reference configurations, see [SGLang GLM 5.1 Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/glm_5_1) |
| GLM 5.2 / Atlas 800I A3 1P1D | `model_configs/sglang/models/glm5.2/A3/` | GLM 5.2 PD disaggregation reference; for more reference configurations, see [SGLang GLM 5.2 Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/glm_5_2) |
| Qwen3-8B / Atlas 800I A2 | `model_configs/sglang/models/qwen_8b/A2/` | Low resource usage, suitable for getting the flow working; for more reference configurations, see [SGLang Qwen3-8B Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/qwen3_8b) |
| More models | `model_configs/sglang/` | `user_config.json` / `env.json` in the root directory and subdirectories |

Each reference configuration directory must contain:

- `user_config.json`: deployment topology, Motor component configuration, and engine parameters

- `env.json`: environment variables for each role

### Mandatory Changes Before Deployment

Open `user_config.json` in the reference configuration and confirm at least the following:

| Field | Position | Description |
| --- | --- | --- |
| `image_name` | `motor_deploy_config` | Consistent with an image available in the cluster |
| `job_id` | `motor_deploy_config` | The K8s namespace name |
| `hardware_type` | `motor_deploy_config` | Hardware type, such as `800I_A2` / `800I_A3` |
| `weight_mount_path` | `motor_deploy_config` | Root path for mounting the weights on the host |
| `p_*` / `d_*` instances and card count | `motor_deploy_config` | Match the node NPUs and the parallelism |
| `*_node_selector` | `motor_deploy_config` | Not mandatory; binds Controller / Coordinator / P / D / MF Store by cluster hostname |
| `coordinator_infer_node_port` etc. | `motor_deploy_config` | Not mandatory; avoids conflicts with existing NodePorts in the live network |
| `engine_type` | P/D engine section | Engine type, which must be `sglang` |
| `model-path` / `served-model-name` | `engine_config` | Weight path and external model name |
| `disaggregation-bootstrap-port` | **Prefill** `engine_config` | SGLang PD bootstrap port; if it is not configured, no bootstrap metadata is generated |

### Minimal Engine Configuration Example

The following is only a `Qwen3-8B` example. For more detailed configurations, see `model_configs/sglang/models/{model_name}/{hardware_type}/`.

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

Notes:

- `engine_config` is **passed through** as `sglang.launch_server` parameters by the NodeManager component, and the field names follow the CLI of the SGLang version in use (kebab-case is supported, and some fields also accept underscore).

- On the Prefill side, `disaggregation-bootstrap-port` (or `disaggregation_bootstrap_port`) is written to `bootstrap_port` in the instance metadata for the SGLang Adapter of the Coordinator to perform P/D pairing; **do not** mix it with the business port `endpoint_config.service_ports`.

- For first-time setup, `scheduler_type: load_balance` and `reschedule_config.enable: false` are recommended. Advanced features are described below.

For a full description of the fields, see the [user_config configuration reference](../../configuration/config_reference.md).

## Service Deployment and Verification

The following operations are performed on the K8s master node, using the GLM 5.1 Atlas 800I A3 reference configuration as an example.

### Starting the Service

```bash
# <namespace> must be the same as job_id in user_config.json
kubectl create namespace <namespace>

cd examples/deployer
python3 deploy.py --config_dir ../../model_configs/sglang/models/glm5.1/A3
```

Loading the weights of a large model may take several minutes. Wait until P/D are Ready before testing.

### Checking the Status

```bash
kubectl get pods -n <namespace> -o wide
```

| Runtime type (example) | Description |
| --- | --- |
| `mindie-motor-controller-*` | Service management |
| `mindie-motor-coordinator-*` | Request scheduling / service entry point |
| `mindie-motor-mf-store-*` or `mf-store` | Serves KV Cache transfer |
| `sglang-p*` | Prefill instance |
| `sglang-d*` | Decode instance |
| `kv-conductor-*` (optional) | Serves the KV affinity feature |

A Pod in `Running` state does not mean that the service is fully ready; the actual startup status must be confirmed with the logs and `/v1/models`.

### Viewing Logs

```bash
cd examples/deployer
# Edit log_collect/log_config.ini: change name_space to job_id
vim log_collect/log_config.ini
bash show_log.sh
```

### Verifying the Service

```bash
# <node_ip>: IP of the node that exposes the Coordinator Infer NodePort
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

- If `{"detail":"Service is not available"}` is returned: the service is not ready yet. Retry later and check the P/D / MF Store logs.

- If streaming JSON is returned: inference is working normally. For more interfaces, see [Service interfaces](../../api/service_interfaces.md).

### Terminating the Service

```bash
cd examples/deployer
bash delete.sh <namespace>
```

Before redeploying, confirm that the old Pods have exited and that the NPUs have been released, and then run `deploy.py`. Avoid using only `kubectl rollout restart` or a partial rebuild, which may cause persistent Pending.

## Feature Configuration Guide

The configuration method of general capabilities is the same as that in [PD disaggregation deployment guide · Feature configuration](./pd_disaggregation_deployment.md#feature-configuration-guide). Only the points related to SGLang are listed below.

### Health Probe

- NodeManager performs `GET /health` readiness checks and heartbeats against the business port.

- SGLang uses the **native generative** `/health` (a lightweight generation is run when idle and can be skipped when busy), and Motor does **not** create a virtual-inference Worker.

- Setting `enable_virtual_inference` to `false` does **not** disable the SGLang generative `/health`.

- For details, see [Virtual Inference Health Probe](../../features/sim_inference.md).

### ChunkPrefill

Configure it in the `engine_config` on the P side, for example:

```json
"chunked-prefill-size": 4096
```

- It is used to control the chunking of ultra-long Prefill and to mitigate the mutual blocking of long and short requests.

- The value must be verified against the model and the available memory. For details, see the SGLang official documentation.

### Asymmetric TP / PCP / MTP

All of these are native engine capabilities and can simply be passed through `engine_config`, for example:

- Asymmetric TP: the `tp-size` of P and D can differ. Related performance is not guaranteed for non-MLA models. For details, see the SGLang official documentation.

- PCP: settings on the Prefill side such as `enable-prefill-cp`, `cp-strategy`, and `attn-cp-size`. For details, see the SGLang official documentation.

- MTP: settings on the Decode side such as `speculative-algorithm` and the related draft parameters. For details, see the SGLang official documentation.

### Fault Rescheduling

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

- Disabled by default. Once enabled, the streaming/non-streaming `completions` scenarios and the non-streaming `chat` scenario are already supported.

- **Known limitation**: the streaming path depends on the engine returning token ids to continue the stream. SGLang currently does not support enabling `return_token_ids` and `stream` at the same time for chat streaming, which makes chat streaming rescheduling unavailable.

- For details, see [Request rescheduling](../../features/fault_tolerance/rescheduler.md).

### KV Cache Affinity Scheduling (Optional)

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

If the reference configuration already contains affinity scheduling, or if it needs to be enabled, check that all of the following are present:

- `motor_coordinator_config`: `scheduler_type: kv_cache_affinity`

- `motor_engine_prefill_config`: `kv-events-config` (ZMQ publishing), and do **not** disable the SGLang Radix (do not set `disable-radix-cache`)

- `kv_conductor_config`: configure `http_server_port`

To disable it, change the setting back to `load_balance` and remove `kv-events-config` and `kv_conductor_config`. For details, see [KV Cache affinity scheduling](../../features/kvcache_affinity.md).

### KV Pooling (Mooncake / Memcache)

The SGLang inference engine plans to support this in the next version.

## Appendix

### O&M Tips

- **Service is not available**: wait for P/D / MF Store to become Ready first. Check whether the Prefill fails to initialize SMEM / transport because MF Store is not ready.

- **Image and weights**: confirm that each node can pull `image_name` and that `model-path` is readable in the container.

- **NPU Pending**: before deployment, check whether the Ascend resources of the target nodes are fully occupied by other tasks. Do not run Motor and pure SGLang on the same machine in parallel and compete for cards.

- **Redeployment**: fully uninstall and wait for the resources to be released before running `deploy.py`. Do not rely on `rollout restart` for configuration switching.

- **PP scenario**: with multiple PP stages, ensure that the bootstrap and collective communication-related environment is consistent with the reference configuration, and that NodeManager can inject the environment variables related to PP bootstrap.

### Known Limitations

| Item | Description |
| --- | --- |
| Anthropic interface `/v1/messages` | Depends on upstream SGLang. Only `/v1/messages/count_tokens` is available |
| chat streaming + rescheduling | Limited by the engine's `return_token_ids`; currently unavailable |

### examples Directory Structure (SGLang)

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

- For the deployment tool description, see `examples/deployer/README.md`.

- For the service topology and the `deploy_mode` description, see [Deployment mode configuration](./README.md).
