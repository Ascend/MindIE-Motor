# Quick Start

This document uses a **simple and quick** deployment case (an Atlas 800I A2 with 8 cards, the Qwen3-8B model, and one P instance and one D instance) to guide developers through the PD disaggregation service deployment process based on MindIE Motor.

For detailed PD disaggregation deployment guidance, see [PD Disaggregation Deployment Guide](./deployment/k8s/pd_disaggregation_deployment.md).

## What Is PD Disaggregation

The Prefill and Decode phases of model inference are instantiated and deployed on different hardware resources for inference to improve inference performance. For details about its features, see [PD Disaggregation](./features/pd_disaggregation.md).

## Environment Requirements

- The Atlas 800I A2, Atlas 800I A3, and Ascend 950PR products are supported.

- At least one server that has completed [environment preparation](./environment_preparation.md) is required.

## Downloading the Model

Download the weight files of the Qwen3-8B model by yourself and upload them to any directory on the server (for example, /mnt/weight). Run the following command to modify the file permissions:

```bash
chmod -R 755 /mnt/weight
```

## Preparing the Image

- Method 1: Go to the [Ascend official image repository](https://www.hiascend.com/developer/ascendhub), enter `motor` in the search box, click the "mindie-motor" card, and download the corresponding MindIE Motor image based on the device model on the "Image Download" tab.

- Method 2: Build the MindIE Motor image by yourself by referring to [Preparing the MindIE Motor Image](./maintenance/build_motor_image_from_vllm_ascend.md).

## Service Deployment

1. Prepare the service startup script.

   The official complete MindIE Motor image already contains the service startup script (/tmp/motor/examples). You can copy the files from the image to the host by running the following commands:

   ```bash
   IMAGE="<image name or image ID>"

   cid=$(docker create "$IMAGE")
   docker cp "$cid:/tmp/motor/examples" ./examples
   docker rm "$cid"
   ```

   Upload the script (the examples directory) to the management node (master node) of the k8s cluster. All subsequent deployment operations are performed on the management node.

2. Configure the service parameters.

   The configuration files (user_config.json and env.json) can be obtained in any of the following three ways:

   - **Method 1: Use an existing typical configuration (recommended for common models)**

     The code repository already provides typical configurations by model and hardware. The path is as follows:

     ```text
     model_configs/vllm/models/<model name>/<hardware model>/
     ```

     For example, the typical configuration directory of the DeepSeek-V4-Flash model on the Atlas 800I A2 is: model_configs/vllm/models/deepseek_v4_flash/A2/.

     It contains the user_config.json and env.json configuration files. After selecting a typical configuration, you only need to modify a few fields according to the actual scenario, such as the image name (image_name) and the weight path (weight_mount_path/model), before deployment.

     The currently provided model directories include: deepseek_v3.1, deepseek_v4_flash, deepseek_v4_pro, glm_5, glm_5.1, and qwen_235b.

   - **Method 2: Automatically generate the configuration**
     Convert a vLLM Ascend community deployment script into a MindIE Motor configuration with one click.

     After copying the vLLM Ascend community script to the corresponding template file in the `examples/deployer/config_tool/` directory, go to the `examples/deployer/` directory and run the following command (using PD disaggregation service deployment on an Atlas 800I A2 as an example):

     ```bash
     python3 deploy.py --mode general_config --deploy-scenario separate --hardware-type A2
     ```

     The generated user_config.json and env.json configuration files are located in the `examples/deployer/config_tool/output_config/` directory. For detailed steps of the one-click conversion tool, see [MindIE Motor Configuration Generation Guide](../../../model_configs/vllm/models/README_EN.md).

   - **Method 3: Edit the configuration manually**

     The following uses Qwen3-8B with one P instance and one D instance as an example to directly edit the user_config.json and env.json configuration files in the `model_configs/vllm/` directory.

     On the management node, run the following command to enter the directory where the service startup script is located.

     ```bash
     cd examples/deployer/
     ```

     - **Modify the user_config.json configuration file**

       ```bash
       vim ../../model_configs/vllm/user_config.json
       ```

       The complete example of the user_config.json configuration file is as follows (you can copy and use it directly. The five `xxxxxx` variables need to be modified by yourself according to the actual situation. For the meaning of each field, see [Full Parameter Description of the user_config.json Configuration File](./configuration/config_reference.md)):

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
           "image_name": "xxxxxx image name. For example: mindie-motor-vllm:dev-26.1.0.B050-800I-A2-py311-Ubuntu24.04-lts-aarch64",
           "job_id": "mindie-motor",
           "hardware_type": "xxxxxx hardware type. Atlas 800I A2: 800I_A2; Atlas 800I A3: 800I_A3; Ascend 950PR products: Ascend950",
           "weight_mount_path": "xxxxxx weight file path. For example: /mnt/weight/qwen3_8B"
         },
         "motor_controller_config": {},
         "motor_coordinator_config": {},
         "motor_engine_prefill_config": {
           "engine_type": "vllm",
           "motor_nodemanger_config": {},
           "engine_config": {
             "served_model_name": "qwen3-8B",
             "model": "xxxxxx. Weight file path. For example: /mnt/weight/qwen3_8B",
             "gpu_memory_utilization": 0.9,
             "data_parallel_size": 1,
             "tensor_parallel_size": 2,
             "pipeline_parallel_size": 1,
             "data_parallel_rpc_port": 9000,
             "enable_expert_parallel": false,
             "enforce-eager": true,
             "max_model_len": 2048,
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
             "served_model_name": "qwen3-8B",
             "model": "xxxxxx. Weight file path. For example: /mnt/weight/qwen3_8B",
             "gpu_memory_utilization": 0.9,
             "data_parallel_size": 1,
             "tensor_parallel_size": 2,
             "pipeline_parallel_size": 1,
             "data_parallel_rpc_port": 9000,
             "enable_expert_parallel": false,
             "max_model_len": 2048,
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

     - **Modify the env.json configuration file**

       ```bash
       vim ../../model_configs/vllm/env.json
       ```

       The complete example of the env.json configuration file is as follows (can be copied and used directly):

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

3. Create a namespace.

   The value of the namespace must be the same as the `job_id` field in `user_config.json` (default value: mindie-motor).

   ```bash
   kubectl create ns mindie-motor
   ```

4. Start and stop the service.

   In the `examples/deployer` directory, deploy the PD disaggregation service through the `deploy.py` script (for more parameters and usage, see [Deployer Deployment Tool Description](../../../examples/deployer/README.md)):

   ```bash
   python3 deploy.py --config_dir ../../model_configs/vllm
   ```

   To terminate the service, run the following command in the same directory:

   ```bash
   bash delete.sh mindie-motor
   ```

   mindie-motor in the command is the namespace; change it based on the actual name.

5. View logs.

   Run the following command and set the `name_space` field to the namespace name (for example, mindie-motor).

   ```bash
   vim log_collect/log_config.ini
   ```

   Then run the following command to collect logs:

   ```bash
   bash show_log.sh
   ```

   All service logs (controller, coordinator, and P/D instances) are saved in the `examples/deployer/log_collect/log` directory and are continuously refreshed until the service is terminated.

## Inference Verification

Open a new command-line window and run the following command on the management node (master node) of the K8s cluster:

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
        "max_tokens": 36,
        "stream": true
    }'
```

If the following result is returned, the service has not finished starting:

```Output
{"detail":"Service is not available"}
```

Wait for a while and try again. A response similar to the following indicates that the inference service is ready:

```Output
data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3-8B","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}],"prompt_token_ids":null}

data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3-8B","choices":[{"index":0,"delta":{"content":"<think>"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3-8B","choices":[{"index":0,"delta":{"content":"\n"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3-8B","choices":[{"index":0,"delta":{"content":"Okay"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

...

data: [DONE]
```
