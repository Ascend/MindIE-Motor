# EPD Disaggregation Deployment Capability

## Feature Introduction

**Disaggregated encoder** runs the visual encoder stage of a multimodal large language model in a process separate from the prefill/decode stage. For the advantages of deploying these two stages in independent vLLM instances, see [vLLM Ascend EPD Disaggregation Feature Description](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/epd_disaggregation.html)

```text
                ┌─────────────────────────────────────────────────────────────────┐
                │                                                                 │
                │                             MindIE Motor                             │
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
│              │                         │              │                         │              │
│              │      Encoder Cache      │              │        KV Cache         │              │
│    Encode    │───────────────────────► │    Prefill   │───────────────────────► │    Decode    │
│   instance   │     Transfer Engine     │   instance   │     Transfer Engine     │   instance   │
│              │                         │              │                         │              │
└──────────────┘                         └──────────────┘                         └──────────────┘
```

MindIE Motor supports deploying the EPD disaggregation feature in `infer_service_set` and `multi_deployment` modes, and supports the `CPCD` and `CDP` scheduling modes. In both scheduling modes, the E instance is scheduled first, and then scheduling proceeds according to the previous logic. After modifying the `user_config.json` configuration file, service deployment can be completed through the `deploy.py` script.

## Deployment Process

To deploy the EPD disaggregation feature in MindIE Motor, you only need to modify the `user_config.json` configuration file and then run the `deploy.py` script to complete service deployment. The specific process is as follows.

### Note

1. The model weights used for EPD disaggregation deployment must support multimodal understanding capability. This document uses the **Qwen3-VL-30B-A3B-Instruct** model as an example.

2. The current EPD disaggregation feature of vLLM Ascend supports two types of `connector`. This document uses `ECExampleConnector` as an example.

### Configuring `user_config.json`

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
        "kv_connector": "MooncakeLayerwiseConnector",
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
        "kv_connector": "MooncakeLayerwiseConnector",
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

Note:

* In the `motor_deploy_config` configuration, `e_instances_num` indicates the number of E instances, `single_e_instance_pod_num` indicates the number of pods occupied by each E instance, and `e_pod_npu_num` indicates the number of NPU cards occupied by the pod of each E instance.

* Add the `motor_engine_encode_config` configuration. For the E-instance, the `engine_config` must include the `ec-transfer-config` setting with `ec_role` set to `ec_producer`. For details, see [vLLM Ascend EPD Disaggregation Feature Description](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/epd_disaggregation.html).

* At the same time, the `engine_config` in `motor_engine_prefill_config` needs to include the `ec-transfer-config` setting with `ec_role` set to `ec_consumer`.

### Deploying the Service

Deploy the service using the `deploy.py` script in the `examples/deployer` directory. You can specify a configuration directory or specify configuration files individually.

```bash
cd examples/deployer
# (Recommended) Method 1: Specify the configuration directory
python deploy.py --config_dir ../infer_engines/vllm

# Method 2: Specify configuration files individually
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```

If the following content is displayed after execution, the execution is successful:

```bash
...... all deploy end.
```
