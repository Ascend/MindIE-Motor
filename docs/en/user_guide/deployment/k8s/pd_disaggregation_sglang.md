# PD-Disaggregated Deployment (Based on SGLang Inference Engine)

This document guides developers through completing a PD disaggregation deployment in a Kubernetes environment based on the SGLang inference engine of MindIE Motor.

The complete deployment process is as follows: 1. Obtain the image; 2. Prepare configuration files; 3. Deploy and verify the service.

The following uses the DeepSeek-V4-Flash model and an Atlas 800I A3 super-node server as an example to demonstrate the complete deployment process.

## Obtaining the Image

- Basic checks

  1. The deployment node has completed [Environment Preparation](../../environment_preparation.md).

  2. Ensure that the model weight path is readable and that the number of NPU cards meets the standard configuration requirements.

- Downloading the image

  1. Go to the [Ascend Image Repository](https://www.hiascend.com/developer/ascendhub) and open the **mindie-motor** image repository.

  2. Open the **Image Download** tab, search for `sglang` in the search box on the right, and select the image version `3.2.0-sglang0.5.21-a3-ubuntu22.04-py3.12`.

  3. Select and download the ARM or x86 image according to your OS architecture.

  4. After downloading, ensure that the image is available on every worker node in the Kubernetes cluster.

## Preparing Configuration Files

Obtain the configuration files for this example from the MindIE Motor repository:

1. Clone the repository and go to the repository root:

   ```bash
   git clone https://gitcode.com/Ascend/MindIE-Motor.git
   cd MindIE-Motor
   ```

2. The configuration files are located in `model_configs/sglang/models/deepseek_v4_flash_0731/A3`, namely the `env.json` and `user_config.json` files in that directory.

3. Modify the following fields in `user_config.json` according to your actual environment:

   image_name: Enter the name of the image downloaded in the previous section.

   weight_mount_path: The mount directory of the model weights inside the container.

   model-path: Located in the `engine_config` of Prefill and Decode. Enter the model weight path under the mount directory.

   served-model-name: In this example, it is `deepseek-v4-flash`.

   job_id: In this example, it is `mindie-motor-sglang`, which will be used as the Kubernetes namespace.

## Service Deployment and Verification

Perform the following operations on the master node of the Kubernetes cluster.

1. Obtain the startup script

   The startup script is already included when you clone the repository, so you can use it directly. The script is located in `MindIE-Motor/examples/deployer`.

2. Start the service

   ```bash
   # The namespace must be the same as job_id in user_config.json
   kubectl create namespace mindie-motor-sglang

   # --config_dir specifies the directory where the configuration files are located
   cd examples/deployer
   python3 deploy.py --config_dir ../../model_configs/sglang/models/deepseek_v4_flash_0731/A3
   ```

3. Check the status

   ```bash
   kubectl get pods -n mindie-motor-sglang -o wide
   ```

   | Service Pod Name | Description |
   | ---- | ---- |
   | mindie-motor-controller-*`  | Service management                                    |
   | mindie-motor-coordinator-*` | Request scheduling and service entry point            |
   | mf-store                   | KV Cache transfer  |
   | sglang-p*                  | Prefill instance  |
   | sglang-d*                  | Decode instance  |
   | kv-conductor-*             | KV affinity scheduling (not present if not enabled)   |

4. View logs

   ```bash
   # Change name_space to mindie-motor-sglang
   vim log_collect/log_config.ini
   # Collect logs and save them to disk
   bash show_log.sh
   ```

5. Verify the service

   ```bash
   # <node_ip>: IP of the node that exposes the Coordinator Infer NodePort
   # <infer_port>: coordinator_infer_node_port in user_config.json
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

   If `{"detail":"Service is not available"}` is returned, the service is not ready yet. Wait until Prefill, Decode, and MF Store are ready, and then try again.

   If streaming JSON is returned, inference is working properly. For more APIs, see [Service Interfaces](../../api/service_interfaces.md).

6. Stop the service

   ```bash
   # Run this command in the examples/deployer directory. The namespace must be the same as job_id.
   bash delete.sh mindie-motor-sglang
   ```
