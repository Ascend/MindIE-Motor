# EPD分离部署能力说明

## 特性介绍

**分离式编码器**将多模态大语言模型的视觉编码器阶段运行在与预填充/解码阶段分离的进程中。
将这两个阶段部署在独立的 vLLM 实例中的优势详见：[vLLM Ascend EPD分离特性说明](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/epd_disaggregation.html)

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

MindIE Motor部署EPD分离特性支持`infer_service_set`和`multi_deployment`模式下部署，
支持`CPCD`和`CDP`调度模式，在两种调度模式下都是先调度E实例，然后再按照之前逻辑进行调度。
通过修改user_config.json配置文件后即可通过deploy.py脚本完成服务部署。

## 部署流程

MindIE Motor部署EPD分离只需修改user_config.json配置文件后，通过deploy.py脚本即可完成服务部署，具体流程如下。

### 注意

1. 部署EPD分离使用的模型权重需支持多模态理解能力，本文以**Qwen3-VL-30B-A3B-Instruct**模型为例进行说明。
2. 当前vLLM Ascend的EPD分离特性支持两种`connector`，本文以`ECExampleConnector`为例进行说明。

### 配置user_config.json

以[MindIE Motor快速开始](../quick_start.md)中实例user_config.json为参考基线，适配EPD分离部署的配置

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

说明：

* 在`motor_deploy_config`配置中，`e_instances_num`表示E实例的个数，`single_e_instance_pod_num`表示每个E实例占用的pod个数，`e_pod_npu_num`表示每个E实例的pod占用的NPU卡数。
* 增加`motor_engine_encode_config`配置，其中E实例的`engine_config`需要增加`ec-transfer-config`配置，
并将`ec_role`配置为`ec_producer`。详细参考[vLLM Ascend EPD分离特性说明](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/epd_disaggregation.html)
* 同时`motor_engine_prefill_config` 中的 `engine_config` 需要增加`ec-transfer-config`配置，并将`ec_role`配置为`ec_consumer`。

### 部署服务

在 `examples/deployer` 目录下通过 deploy.py 脚本部署服务。支持指定配置目录或单独指定配置文件：

```bash
cd examples/deployer
# 方式一：指定配置目录（推荐）
python deploy.py --config_dir ../infer_engines/vllm

# 方式二：单独指定配置文件
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```

执行后看到如下内容，说明执行成功：

```bash
...... all deploy end.
```
