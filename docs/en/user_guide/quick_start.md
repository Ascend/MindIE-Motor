# Quick Start

This document provides a quick-start deployment guide to help developers experience the PD disaggregation deployment process based on MindIE Motor, using a simple scenario (Atlas 800I A2 inference server, Qwen3-8B model, with one P-instance and one D-instance) as an example.

For detailed PD disaggregation deployment guidance, see [PD Disaggregation Deployment Guide](./deployment/k8s/pd_disaggregation_deployment.md).

## What Is PD Disaggregation

The Prefill and Decode phases of model inference are instantiated and deployed on different hardware resources for inference to improve inference performance. For details about its features, see [PD Disaggregation](./features/pd_disaggregation.md).

## Environment Requirements

- The Atlas 800I A2 inference server, Atlas 800 A3 SuperPoD Server, and Atlas 850 SuperPoD Server are supported.

- At least one server that has completed [environment preparation](./environment_preparation.md) is required.

## Downloading the Model

Download the weight files of the Qwen3-8B model by yourself and upload them to any directory on the server (using `/mnt/weight` as an example). Run the following command to modify the file permissions:

   ```bash
   chmod -R 755 /mnt/weight
   ```

## Preparing the Image

- Method 1: Go to the [Ascend official image repository](https://www.hiascend.com/developer/ascendhub), search for `motor`, and download the corresponding MindIE Motor image based on the device model from the search results.

- Method 2: Build the MindIE Motor image by yourself by referring to [Preparing the MindIE Motor Image](./maintenance/build_motor_image_from_vllm_ascend.md).

## Service Deployment

1. Prepare the service startup script.

     The official complete MindIE Motor image already contains the service startup script (`/tmp/motor/examples`). You can copy the files from the image to the host by running the following commands:

      ```bash
      IMAGE="<image name or image ID>"

      cid=$(docker create "$IMAGE")
      docker cp "$cid:/tmp/motor/examples" ./examples
      docker rm "$cid"
      ```

    Upload the script directory (the `examples` directory) to the management node (master node) of the k8s cluster. All subsequent deployment operations are performed on the management node.

2. Configure the service parameters.

   On the management node, run the following command to enter the directory where the service startup script is located and modify the configuration file:

   ```bash
   cd examples/deployer/
   vim ../infer_engines/vllm/user_config.json
   ```

   >[!NOTE]NOTE
   >When using the Atlas 850 SuperPoD Server, configure its configuration file by referring to the configuration example in [deepseek_v4_flash](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/models/deepseek_v4_flash/).

   The complete example of the `user_config.json` file is as follows (you can copy and use it directly. The five *xxxxxx* items need to be modified by yourself. For the meaning of each field, see [Full Parameter Description of user_config](./configuration/config_reference.md).):

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
          "image_name": "xxxxxxx image name. For example: mindie-motor-vllm:dev-26.1.0.B050-800I-A2-py311-Ubuntu24.04-lts-aarch64",
          "job_id": "mindie-motor",
          "hardware_type": "xxxxxx hardware type. A2: 800I_A2 A3: 800I_A3",
          "weight_mount_path": "xxxxxx weight file path. For example: /mnt/weight/qwen3_8B"
        },
        "motor_controller_config": {},
        "motor_coordinator_config": {},
        "motor_engine_prefill_config": {
          "engine_type": "vllm",
          "motor_nodemanger_config": {},
          "engine_config": {
            "served_model_name": "qwen3-8B",
            "model": "xxxxxx weight file path. For example: /mnt/weight/qwen3_8B",
            "gpu_memory_utilization": 0.9,
            "data_parallel_size": 1,
            "tensor_parallel_size": 2,
            "pipeline_parallel_size": 1,
            "data_parallel_rpc_port": 9000,
            "enable_expert_parallel": false,
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
              "kv_connector_extra_config": {}
            }
          }
        },
        "motor_engine_decode_config": {
          "engine_type": "vllm",
          "motor_nodemanger_config": {},
          "engine_config": {
            "served_model_name": "qwen3-8B",
            "model": "xxxxxx weight file path. For example: /mnt/weight/qwen3_8B",
            "gpu_memory_utilization": 0.9,
            "data_parallel_size": 1,
            "tensor_parallel_size": 2,
            "pipeline_parallel_size": 1,
            "data_parallel_rpc_port": 9000,
            "enable_expert_parallel": false,
            "max_model_len": 2048,
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

3. Configure environment variables.

   Run the following command to modify the environment variable configuration file.

     ```bash
     vim ../infer_engines/vllm/env.json
     ```

   The complete example of the `env.json` file is as follows (can be copied and used directly):

     ```json
    {
      "version": "2.0.0",
      "motor_common_env": {
        "CANN_INSTALL_PATH": "/usr/local/Ascend",
        "MOTOR_LOG_ROOT_PATH": "/root/ascend/log"
      },
      "motor_controller_env": {},
      "motor_coordinator_env": {},
      "motor_engine_prefill_env": {
        "HCCL_BUFFSIZE": 200,
        "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
        "HCCL_OP_EXPANSION_MODE": "AIV",
        "OMP_PROC_BIND": "false",
        "OMP_NUM_THREADS": 100,
        "ASCEND_BUFFER_POOL": "0:0"
      },
      "motor_engine_decode_env": {
        "HCCL_BUFFSIZE": 200,
        "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
        "HCCL_OP_EXPANSION_MODE": "AIV",
        "OMP_PROC_BIND": "false",
        "OMP_NUM_THREADS": 100,
        "ASCEND_BUFFER_POOL": "0:0"
      },
      "motor_kv_cache_store_env": {},
      "motor_kv_conductor_env": {}
    }
     ```

4. Start and stop the service.

   Create a namespace. The value of the namespace must be the same as the `job_id` field in `user_config.json` (the default value is `mindie-motor`).

     ```bash
     kubectl create ns mindie-motor
     ```

   Run the following command to deploy the PD disaggregation service:

   ```bash
   python3 deploy.py --config_dir ../infer_engines/vllm
   ```

   To terminate the service, run the following command:

   ```bash
   bash delete.sh namespace (namespace is the name of the manually created namespace e.g., mindie-motor)
   ```

5. View logs.

   Run the `vim log_collect/log_config.ini` command, set `name_space` to the namespace name (for example, `mindie-motor`), and then run the following command to collect logs:

   ```bash
   bash show_log.sh
   ```

   All service logs (controller, coordinator, and P/D instances) are saved in the `examples/deployer/log_collect/log` directory and are continuously refreshed until the service is terminated.

## Inference Verification

Open a new command-line window and run the following command on the management node (master node) of the k8s cluster:

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

If the following result is returned, the service has not finished starting:

   ```json
   {"detail":"Service is not available"}
   ```

Wait for a while and try again. A response similar to the following indicates that the inference service is ready:

   ```json
   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}],"prompt_token_ids":null}

   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"<think>"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"\n"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"Okay"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

   ...

   data: [DONE]
   ```
