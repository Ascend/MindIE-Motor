# Quick Start

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:05:26.895Z pushedAt=2026-07-01T09:36:37.564Z -->

## Product Introduction

MindIE PyMotor is an inference service framework designed for PD-disaggregated deployment of general-purpose large language models (LLMs). It provides inference capabilities through an open, extensible service-oriented platform architecture and supports integration with mainstream inference framework interfaces to meet the high-performance inference requirements.

## Key Features

| Feature       | Description              |
| ------------ | ----------------- |
| **PD disaggregation** | The Prefill and Decode stages of model inference are instantiated and deployed on different machine resources to perform inference simultaneously, improving inference performance. For details about its features, see [PD Disaggregation](./service_deployment/pd_disaggregation_deployment.md). |

## Quick Start

### Environment Setup

This document uses the Atlas 800I A2 inference server and the Qwen3-8B model as examples to help developers quickly get started with MindIE PyMotor for LLM PD disaggregation and inference processes.

#### Prerequisites

In a physical machine deployment scenario, you need to install the NPU driver and firmware and deploy Docker on the physical machine. Perform the following steps to determine whether the NPU driver, NPU firmware, K8s cluster, and Docker have been installed.

- Run the following command to check whether the NPU driver and firmware is installed:

  ```bash
  npu-smi info
  ```

  **Figure 1** Echoed information

  ![image](https://www.hiascend.com/doc_center/source/en/mindie/22RC1/quickstart/figure/en-us_image_0000002474350016.png)

  **Table 1** Atlas A2 inference products

  | Product Model | Reference Document |
  | --- | --- |
  | Atlas 800I A2 | "[Physical Machine Installation and Uninstallation](https://support.huawei.com/enterprise/en/doc/EDOC1100438838/b1977c97)" in *Atlas A2 Center Inference and Training Hardware 24.1.0 NPU Driver and Firmware Installation Guide* |

- Run the following command to check whether the K8s cluster is ready:

  ```bash
  kubectl get node -A
  ```

  The following information indicates that the K8s cluster is ready:

  ```bash
  NAME         STATUS   ROLES                         AGE   VERSION
  ```

- Run the following command to check whether Docker is installed and started:

  ```bash
  docker ps
  ```

  The following information indicates that Docker is installed and started:

  ```bash
  CONTAINER ID        IMAGE        COMMAND         CREATED        STATUS         PORTS           NAMES
  ```

#### Obtaining Model Weights

1. Download the weights first. Here, Qwen3-8B is used as an example. Go to the official website to download the weight file and upload it to any directory on the server (such as `/mnt/weight`).

2. Run the following command to modify the weight file permissions:

   ```bash
   chmod -R 755 /mnt/weight
   ```

#### Obtaining the Container Image

Go to the [Ascend Official Image Repository](https://www.hiascend.com/en/developer/ascendhub) and select and download the corresponding PyMotor image based on the product model.

This image already includes the basic environment required for model running.

### PD Disaggregation

> [!NOTE]Deployment method description
> Currently, the **CRD method** (based on MindCluster's PD disaggregation CRD and Operator) is used for deployment by default. This method has not yet completed adaptation verification for reliability, availability, and serviceability (RAS) capabilities and pooling capabilities. If you require RAS or KV pooling capabilities, you can configure `motor_deploy_config.deploy_mode` to `multi_deployment` in `user_config.json` to switch to the original multi-YAML Deployment method. For complete deployment instructions, see [PD Disaggregation Deployment](./service_deployment/pd_disaggregation_deployment.md).

1. **Prepare the `examples` directory and upload it to the master server of the K8s cluster**.

   - **(Method 1) Obtain from the code repository**: Upload the `examples` directory under the repository root to the master server.

   - **(Method 2) Obtain from the container image**: If you do not have the complete code repository but have pulled the PyMotor inference image, you can use the example directory pre-installed in the image, with the path **`/tmp/motor/examples`** (the directory structure is consistent with `examples/` in the repository). Execute the following on the machine where the image has been pulled (replace `IMAGE` with the actual image name or image ID, which can be consistent with `motor_deploy_config.image_name` in `user_config.json`):

     ```bash
     IMAGE="<Image name or image ID>"

     cid=$(docker create "$IMAGE")
     docker cp "$cid:/tmp/motor/examples" ./examples
     docker rm "$cid"
     ```

     Upload the obtained `examples` directory to the master server using **Method 1**. If you are using Podman, replace `docker` in the command with `podman`.

2. **Configure service parameters**.

   - Open the `examples/infer_engines/vllm/user_config.json` file (or the corresponding model configuration under `examples/infer_engines/vllm/models/`, such as `examples/infer_engines/vllm/models/deepseek/v3_1/user_config.json`, whichever is actually used).

     ```bash
     cd examples/infer_engines/vllm
     vim user_config.json
     ```

   - Modify the configuration parameters in `user_config.json` based on your actual situation. (The following uses Qwen3-8B as an example.)

      ```json
      {
        "version": "v2.0",
        "motor_deploy_config": {
          "p_instances_num": 1,
          "d_instances_num": 1,
          "single_p_instance_pod_num": 1,
          "single_d_instance_pod_num": 1,
          "p_pod_npu_num": 4,
          "d_pod_npu_num": 4,
          "image_name": "",
          "job_id": "mindie-motor",
          "hardware_type": "800I_A3",
          "weight_mount_path": "/mnt/weight/"
        },
        "motor_controller_config": {},
        "motor_coordinator_config": {},
        "motor_engine_prefill_config": {
          "engine_type": "vllm",
          "motor_nodemanger_config": {},
          "enable_multi_endpoints": true,
          "model_config": {
            "model_name": "qwen3-8B",
            "model_path": "/mnt/weight/qwen3_8B",
            "npu_mem_utils": 0.9,
            "prefill_config": {
              "dp_size": 1,
              "tp_size": 4,
              "pp_size": 1,
              "enable_ep": false,
              "dp_rpc_port": 9000,
              "world_size": 4
            }
          },
          "engine_config": {
            "enforce-eager": true,
            "max_model_len": 2048,
            "kv_transfer_config": {
              "kv_connector": "MooncakeLayerwiseConnector",
              "kv_buffer_device": "npu",
              "kv_role": "kv_producer",
              "kv_parallel_size": 1,
              "kv_port": "30001",
              "engine_id": "0",
              "kv_rank": 0,
              "kv_connector_module_path": "vllm_ascend.distributed.mooncake_layerwise_connector",
              "kv_connector_extra_config": {}
            }
          }
        },
        "motor_engine_decode_config": {
          "engine_type": "vllm",
          "motor_nodemanger_config": {},
          "enable_multi_endpoints": true,
          "model_config": {
            "model_name": "qwen3-8B",
            "model_path": "/mnt/weight/qwen3_8B",
            "npu_mem_utils": 0.9,
            "parallel_config": {
              "dp_size": 1,
              "tp_size": 4,
              "pp_size": 1,
              "enable_ep": false,
              "dp_rpc_port": 9000,
              "world_size": 4
            }
          },
          "engine_config": {
            "max_model_len": 2048,
            "kv_transfer_config": {
              "kv_connector": "MooncakeLayerwiseConnector",
              "kv_buffer_device": "npu",
              "kv_role": "kv_consumer",
              "kv_parallel_size": 1,
              "kv_port": "30001",
              "engine_id": "0",
              "kv_rank": 0,
              "kv_connector_module_path": "vllm_ascend.distributed.mooncake_layerwise_connector",
              "kv_connector_extra_config": {}
            }
          }
        }
      }
      ```

    The parameters above are described as follows:

        | Configuration Item | Value Type | Value Range | Description |
        | --- | --- | --- | --- |
        | version | string | v2.0 | Configuration file version |
        | p_instances_num | int | ≥1 | Number of P instances |
        | d_instances_num | int | ≥1 | Number of D instances |
        | single_p_instance_pod_num | int | ≥1 | Number of pod containers occupied by a single P instance |
        | single_d_instance_pod_num | int | ≥1 | Number of pod containers occupied by a single D instance |
        | p_pod_npu_num | int | ≥1 | Number of NPU cards occupied by a single P node pod container |
        | d_pod_npu_num | int | ≥1 | Number of NPU cards occupied by a single D node pod container |
        | image_name | string | String | Name of the image loaded by Docker, for example, "vllm-ascend:b150_motor" |
        | job_id | string | String | Name of the PD disaggregation task, for example, "mindie-pymotor" |
        | hardware_type | string | [800I_A2, 800I_A3] | Server hardware type |
        | weight_mount_path | string | String | Weight file path |
        | motor_controller_config | dict | Controller component configuration | Any specific configuration item can be set here |
        | motor_coordinator_config | dict | Coordinator component configuration | Any specific configuration item can be set here |
        | engine_type | string | String | Type of the connected inference engine, for example, "vllm" |
        | motor_nodemanager_config | dict | nodemanager component configuration | Any specific configuration item can be set here |
        | model_name | string | String | Model name, for example, "qwen3_8B" |
        | model_path | string | File path | Path where the model weight file is located |
        | npu_mem_utils | float | Decimal between 0 and 1 | Upper limit of NPU memory usage ratio, for example, "0.95" |
        | prefill_config.dp_size | int | ≥1 | Data parallelism parameter |
        | prefill_config.tp_size | int | ≥1 | Tensor parallelism parameter |
        | prefill_config.pp_size | int | ≥1 | Pipeline parallelism parameter |
        | prefill_config.enable_ep | bool | [true, false] | Expert parallelism switch |
        | prefill_config.dp_rpc_port | int | Valid port range | Port number for RPC communication |
        | prefill_config.world_size | int | ≥1 | Total number of cards for a single instance. Explicit specification is recommended due to different calculation methods of different engines. |
        | engine_config | dict | Native parameters of the inference engine | Refer to the description of the corresponding inference engine and fill in directly as a JSON object. |

   - Configure the k8s namespace. Set the `namespace` value to `job_id` in `user_config.json`.

     ```bash
     kubectl create ns mindie-motor
     ```

3. **Configure environment variables**.

   - Open the `examples/infer_engines/vllm/env.json` file.

     ```bash
     cd examples/infer_engines/vllm
     vim env.json
     ```

   - Modify the configuration parameters in `env.json` based on the actual situation.

     ```bash
     {
        "version": "2.0.0",
        "motor_common_env": {
          "CANN_INSTALL_PATH": "/usr/local/Ascend"
        },
        "motor_controller_env": {},
        "motor_coordinator_env": {},
        "motor_engine_prefill_env": {},
        "motor_engine_decode_env": {}
     }
     ```

4. **Start the service**

   Execute in the `examples/deployer` directory. Two methods of specifying the configuration are supported:

   **Method 1: Specify the configuration directory (recommended)**. The directory must contain `user_config.json` and `env.json`:

   ```bash
   cd examples/deployer
   python3 deploy.py --config_dir ../infer_engines/vllm
   ```

   **Method 2: Specify the configuration file paths separately**. `--user_config_path` and `--env_config_path` must be specified at the same time:

   ```bash
   cd examples/deployer
   python3 deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
   ```

   You can also use the shorthand `--config` and `--env`.

5. **Send a request.**

   Run the following command:

   ```bash
   curl -X POST http://127.0.0.1:31015/v1/chat/completions \
   -H "Content-Type: application/json" \
   -d '{
   "model": "qwen3-8B",
   "messages": [
   {
   "role": "system",
   "content": "You are a helpful assistant."
   },
   {
   "role": "user",
   "content": "who are you?"
   }
   ],
   "max_tokens":36,
   "stream":true
   }'
   ```

   If the returned result is as follows, the service is not ready yet:

   ```json
   {"detail":"Service is not available"}
   ```

   Wait for a while and try again. An echo similar to the following indicates that the inference service is ready.

   ```json
   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}],"prompt_token_ids":null}
   
   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"<think>"},"logprobs":null,"finish_reason":null,"token_ids":null}]}
   
   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"\n"},"logprobs":null,"finish_reason":null,"token_ids":null}]}
   
   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"Okay"},"logprobs":null,"finish_reason":null,"token_ids":null}]}
   
   ...
   
   data: [DONE]
   ```
