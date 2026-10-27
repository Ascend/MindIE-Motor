# EPD Disaggregation Deployment Feature

## Feature Introduction

**Disaggregated encoder** runs the visual encoder stage of a multimodal large language model in a process separate from the prefill/decode stage. For the advantages of deploying these two stages in independent vLLM instances, see [vLLM Ascend EPD Disaggregation Feature Description](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/epd_disaggregation.html), which mainly include:

- **Independent and fine-grained scaling**: the visual encoder is lightweight, while the language model is several orders of magnitude larger, so encoder instances can be scaled in or out independently of the language model parallelism.

- **Lower time to first token (TTFT)**: pure text requests bypass the visual encoder completely, and the encoder output is injected only into the required attention layers, shortening the prefill critical path.

- **Cross-process reuse and caching of encoder output**: encoder results can be reused by multiple instances through a shared cache, avoiding repeated computation.

MindIE Motor supports deploying the EPD disaggregation feature in `infer_service_set` and `multi_deployment` modes, and supports the `CPCD` and `CDP` scheduling modes.

### How It Works

```text
                ┌─────────────────────────────────────────────────────────────────┐
                │                                                                 │
                │                             MindIE Motor                        │
                │                                                                 │
                └────────────────────────────────┬────────────────────────────────┘
                                                 │
                                                 │
                                                 │
                                                 │
                                                 │
       ┌─────────────────────────────────────────┼───────────────────────────────────────┐
       │                                         │                                       │
       │                                         │                                       │
       │                                         │                                       │
       │                                         │                                       │
       │                                         │                                       │
       ▼                                         ▼                                       ▼
┌──────────────┐                         ┌──────────────┐                         ┌──────────────┐
│              │      Encoder Cache      │              │        KV Cache         │              │
│    Encode    │───────────────────────► │    Prefill   │───────────────────────► │    Decode    │
│   instance   │     Transfer Engine     │   instance   │     Transfer Engine     │   instance   │
│              │                         │              │                         │              │
└──────────────┘                         └──────────────┘                         └──────────────┘
```

The processing flow of a multimodal request is as follows:

1. **Encode stage**: after the request reaches the Coordinator, the Router first schedules an E instance to perform visual encoding. An E instance is scheduled only when the request contains multimodal content (`image_url` or `video_url` in `messages`); pure text requests directly enter the prefill stage.
2. **Encoder cache transfer**: after the E instance completes encoding, it transfers the encoder cache to the P instance through the Encoder Cache Transfer Engine (`ec-transfer-config`).
3. **Prefill/Decode stage**: after the P instance completes prefill and generates the KV Cache, the KV Cache is transferred to the D instance for decoding through the KV Transfer Engine (`kv_transfer_config`). This link is exactly the same as that of PD disaggregation deployment.

In both the `CPCD` and `CDP` scheduling modes, the E instance is scheduled first, and then scheduling proceeds according to the previous PD disaggregation logic.

### Constraints and Limitations

| Constraint Dimension | Requirement |
|----------|------|
| Hardware | <ul><li>Atlas 800I A2</li><li>Atlas 800I A3</li></ul>  |
| Deployment scenario | <ul><li>The `infer_service_set` and `multi_deployment` deployment modes are supported;</li><li>The `CPCD` and `CDP` scheduling modes are supported;</li><li>The `single_container` deployment mode is not supported.</li></ul> |
| Engine | Only the vLLM engine is supported (`ec-transfer-config` is a configuration specific to vLLM Ascend EPD disaggregation). |
| Feature mutual exclusion | It cannot be used together with PD aggregation deployment (`hybrid_instances_num`, union instances); the PD aggregation configuration rejects fields related to P/D disaggregation, and the Coordinator does not schedule E instances to perform encoding in an aggregation topology. |
| Software dependency | <ul><li>vLLM Ascend must support the EPD disaggregation feature;</li><li>KV transfer between P and D instances depends on Mooncake (MooncakeConnectorV1);</li><li>When `ECExampleConnector` is used, E instances and P instances must access the same shared storage path (shared_storage_path).</li></ul> |
| Other limitations | <ul><li>The model weights used for EPD disaggregation deployment must support multimodal understanding capability;</li><li>The current EPD disaggregation feature of vLLM Ascend supports two types of connectors;</li><li>The `ec_connector` and `ec_connector_extra_config` configurations of E instances and P instances must be consistent.</li></ul> |

## Feature Usage

### Environment Preparation

- Basic deployment has been completed by referring to [MindIE Motor Quick Start](../quick_start.md).
- The model weights used for EPD disaggregation deployment must support multimodal understanding capability. This document uses the Qwen3-VL-30B-A3B-Instruct model as an example.
- The current EPD disaggregation feature of vLLM Ascend supports two types of connectors. This document uses `ECExampleConnector` as an example.

### Usage Examples

To deploy the EPD disaggregation feature in MindIE Motor, you only need to modify the user_config.json configuration file and then run the deploy.py script to complete service deployment. The specific process is as follows:

1. Modify the user_config.json configuration file.

   Use the `user_config.json` instance in [MindIE Motor Quick Start](../quick_start.md) as the reference baseline, and adapt the configuration for EPD disaggregation deployment.

   ```json
   {
     "version": "v2.0",
     "motor_deploy_config": {
       "e_instances_num": 2,
       "p_instances_num": 1,
       "d_instances_num": 1,
       "single_e_instance_pod_num": 1,
       "single_p_instance_pod_num": 1,
       "single_d_instance_pod_num": 1,
       "e_pod_npu_num": 2,
       "p_pod_npu_num": 2,
       "d_pod_npu_num": 2,
       "image_name": "",
       "job_id": "mindie-motor",
       "hardware_type": "800I_A2",
       "weight_mount_path": "/mnt/weight/",
       "deploy_mode": "multi_deployment"
     },
     "motor_controller_config": {
     },
     "motor_coordinator_config": {
     },
     "motor_engine_encode_config": {
       "engine_type": "vllm",
       "motor_nodemanger_config": {},
       "engine_config": {
         "served_model_name": "qwen3",
         "model": "/mnt/weight/Qwen3-VL-30B-A3B-Instruct",
         "gpu_memory_utilization": 0.9,
         "data_parallel_size": 1,
         "tensor_parallel_size": 1,
         "pipeline_parallel_size": 1,
         "enable_expert_parallel": false,
         "data_parallel_rpc_port": 9000,
         "enforce_eager": true,
         "no-enable-prefix-caching": true,
         "seed": 1024,
         "max_model_len": 128000,
         "trust-remote-code": true,
         "allowed-local-media-path": "/mnt/share/patch/media_path/",
         "ec-transfer-config": {
           "ec_connector": "ECExampleConnector",
           "ec_role": "ec_producer",
           "ec_connector_extra_config": {"shared_storage_path": "/mnt/share/patch/ec_cache"}
         }
       }
     },
     "motor_engine_prefill_config": {
       "engine_type": "vllm",
       "motor_nodemanger_config": {},
       "engine_config": {
         "served_model_name": "qwen3",
         "model": "/mnt/weight/Qwen3-VL-30B-A3B-Instruct",
         "gpu_memory_utilization": 0.9,
         "data_parallel_size": 1,
         "tensor_parallel_size": 2,
         "pipeline_parallel_size": 1,
         "enable_expert_parallel": false,
         "data_parallel_rpc_port": 9000,
         "seed": 1024,
         "max_model_len": 128000,
         "trust-remote-code": true,
         "no-enable-prefix-caching": true,
         "allowed-local-media-path": "/mnt/share/patch/media_path/",
         "ec-transfer-config": {
           "ec_connector": "ECExampleConnector",
           "ec_role": "ec_consumer",
           "ec_connector_extra_config": {"shared_storage_path": "/mnt/share/patch/ec_cache"}
         },
         "kv_transfer_config": {
           "kv_connector": "MooncakeConnectorV1",
           "kv_buffer_device": "npu",
           "kv_role": "kv_producer",
           "kv_parallel_size": 1,
           "kv_port": "30001",
           "engine_id": "0",
           "kv_rank": 0,
           "kv_connector_extra_config": {}
         }
       }
     },
     "motor_engine_decode_config": {
       "engine_type": "vllm",
       "motor_nodemanger_config": {},
       "engine_config": {
         "served_model_name": "qwen3",
         "model": "/mnt/weight/Qwen3-VL-30B-A3B-Instruct",
         "gpu_memory_utilization": 0.9,
         "data_parallel_size": 1,
         "tensor_parallel_size": 2,
         "pipeline_parallel_size": 1,
         "enable_expert_parallel": false,
         "data_parallel_rpc_port": 9000,
         "seed": 1024,
         "max_model_len": 128000,
         "trust-remote-code": true,
         "no-enable-prefix-caching": true,
         "allowed-local-media-path": "/mnt/share/patch/media_path/",
         "kv_transfer_config": {
           "kv_connector": "MooncakeConnectorV1",
           "kv_buffer_device": "npu",
           "kv_role": "kv_consumer",
           "kv_parallel_size": 1,
           "kv_port": "30001",
           "engine_id": "0",
           "kv_rank": 0,
           "kv_connector_extra_config": {}
         }
       }
     }
   }
   ```

   The configuration items are described as follows:

   | Configuration Item | Type | Value Range | Mandatory | Default Value | Description |
   |--------|------|----------|------|--------|------|
   | e_instances_num | int | Integer ≥0 | No (mandatory when EPD disaggregation is enabled) | Not configured | Number of E instances. If it is not configured or is 0, no E instance is deployed, that is, EPD disaggregation is not enabled. |
   | p_instances_num | int | [1,16] | Yes | None | Number of P instances. |
   | d_instances_num | int | [1,16] | Yes | None | Number of D instances.|
   | single_e_instance_pod_num | int | ≥1 | No | 1 | Number of Pods occupied by each E instance. |
   | single_p_instance_pod_num | int | ≥1 | No | 1 | Number of Pods occupied by each P instance. |
   | single_d_instance_pod_num | int | ≥1 | No | 1 | Number of Pods occupied by each D instance. |
   | e_pod_npu_num | int | ≥1 | No | 1 | Number of NPU cards occupied by the Pods of each E instance. |
   | p_pod_npu_num | int | ≥1 | No | 1 | Number of NPU cards occupied by the Pods of each P instance. |
   | d_pod_npu_num | int | ≥1 | No | 1 | Number of NPU cards occupied by the Pods of each D instance. |
   | deploy_mode | string | infer_service_set, multi_deployment, single_container | No | infer_service_set | Deployment mode. EPD disaggregation supports `infer_service_set` and `multi_deployment`. This field cannot be modified when the configuration is updated through `--update_instance_num`. |
   | ec_connector | string | EC connectors supported by vLLM Ascend | Yes (in `ec-transfer-config`) | None | Connector type. This document uses `ECExampleConnector` as an example, and the configuration must be the same on E instances and P instances. |
   | ec_role | string | ec_producer, ec_consumer | Yes (in `ec-transfer-config`) | None | Role configuration. It is `ec_producer` on E instances and `ec_consumer` on P instances. |
   | shared_storage_path | string | A valid storage path | Yes (in `ec_connector_extra_config`) | None | Encoder cache directory shared by E instances and P instances. The configuration must be the same on both sides and the path must be accessible. |

   > [!NOTE]NOTE
   >
   >- In the motor_deploy_config configuration, `e_instances_num` indicates the number of E instances, `single_e_instance_pod_num` indicates the number of pods occupied by each E instance, and `e_pod_npu_num` indicates the number of NPU cards occupied by the pod of each E instance.
   >- Add the motor_engine_encode_config configuration. For the E-instance, the engine_config must include the `ec-transfer-config` setting with `ec_role` set to `ec_producer`. For details, see [vLLM Ascend EPD Disaggregation Feature Description](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/epd_disaggregation.html).
   >- At the same time, the engine_config in motor_engine_prefill_config needs to include the `ec-transfer-config` setting with `ec_role` set to `ec_consumer`.
   >- In `infer_service_set` mode, `single_e_instance_pod_num` and `e_pod_npu_num` default to 1 when they are not configured; in `multi_deployment` mode, the default values of the engine Deployment template are used when they are not configured. Explicit configuration is recommended.

2. Deploy the service.

   Deploy the service using the deploy.py script in the examples/deployer directory. You can specify a configuration directory or specify configuration files individually:

   ```bash
   cd examples/deployer
   # (Recommended) Method 1: Specify the configuration directory
   python deploy.py --config_dir ../../model_configs/vllm

   # Method 2: Specify configuration files individually
   python deploy.py --user_config_path ../../model_configs/vllm/user_config.json --env_config_path ../../model_configs/vllm/env.json
   ```

   If the following content is displayed after execution, the execution is successful:

   ```bash
   ...... all deploy end.
   ```

### Verification

1. Confirm that the E, P, and D instances are all started successfully.

   ```bash
   kubectl get pod -n mindie-motor -owide
   ```

   In addition to the Controller and the Coordinator, in `multi_deployment` mode you can see the Pods generated by the E-instance Deployment (when engine_type is vllm, they are named `vllm-e0` and `vllm-e1`, that is, `{engine base name}-e{instance index}`) and the P/D instance Pods (`vllm-p0` and `vllm-d0`), all in the Running state; in `infer_service_set` mode, the number of encode-role replicas of the InferServiceSet is the same as `e_instances_num`.

2. Send a multimodal inference request to verify that the request is encoded by the E instance and then prefilled and decoded by the P/D instances (replace `{coordinator_ip}:{port}` with the Coordinator service address and port. For details, see [MindIE Motor Quick Start](../quick_start.md)).

   ```bash
   curl -X POST http://{coordinator_ip}:{port}/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "qwen3",
       "messages": [
         {
           "role": "user",
           "content": [
             {"type": "text", "text": "Describe the content of this image"},
             {"type": "image_url", "image_url": {"url": "<base64-encoded image>"}}
           ]
         }
       ],
       "max_tokens": 128
     }'
   ```

   HTTP 200 is returned, and the response body contains the `choices` field. In the Coordinator logs, you can see that the request completes encoding on the E instance first (the `PD_Encode` stage).

3. Send a pure text request to verify that the request bypasses the E instance and directly enters the prefill stage (the request body only needs to contain plain text such as `"content": "hello"`), and the service also returns normally.

## FAQs

### Multimodal Requests Do Not Pass Through the E Instance

**Symptom**

After a multimodal request is sent, the E instance has no request record, and the request is directly processed by the P/D instances.

**Cause Analysis**

The Coordinator schedules E instances only for multimodal requests whose content contains `image_url` or `video_url`, and requires that the E, P, and D instances have all been registered and are ready. If any condition is not met, the request skips the encoding stage.

**Solution**

1. Check whether the request is in the standard multimodal format (`content` in `messages` is a list containing `image_url`/`video_url`).
2. Check whether the E instance is registered properly: run `kubectl get pod -n mindie-motor` to confirm that the E-instance Pod is in the Running state.
3. Check the Coordinator logs to confirm the instance readiness status.

### Service Is Not Ready (Service is not available)

**Symptom**

The inference request returns `{"detail":"Service is not available"}`.

**Cause Analysis**

The EPD disaggregation topology requires that all three types of instances (E, P, and D) are registered before the service is ready. If any type of instance is missing or has not completed registration, the service is unavailable.

**Solution**

1. Confirm that `e_instances_num`, `p_instances_num`, and `d_instances_num` in `user_config.json` are correctly configured and that the corresponding instances have all been started.
2. Check the engine logs of the instances that are not ready: `kubectl logs <pod-name> -n mindie-motor`.

### Encoder Cache Transfer Fails

**Symptom**

After the E instance completes encoding, the P instance cannot obtain the encoder cache, and inference fails.

**Cause Analysis**

The `ec-transfer-config` configurations of the E instance and the P instance are inconsistent, or the shared storage path is inaccessible.

**Solution**

1. Check whether the `ec_connector` and `ec_connector_extra_config.shared_storage_path` configurations of the E instance (`ec_producer`) and the P instance (`ec_consumer`) are completely consistent.
2. Confirm that the directory corresponding to `shared_storage_path` is accessible on the nodes where the E instance and the P instance reside, and that it has read and write permissions.

### KV Cache Cannot Be Transferred Between P and D Instances

**Symptom**

The KV Cache cannot be transferred between the P instance and the D instance, and inference fails.

**Cause Analysis**

`kv_role` in `kv_transfer_config` is incorrectly configured, or `kv_port` is inconsistent.

**Solution**

1. Check whether `kv_role` in `kv_transfer_config` is correct: `kv_producer` for P and `kv_consumer` for D.
2. Check whether `kv_port` is the same on the P instance and the D instance.
