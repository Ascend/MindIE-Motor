# MindIE Motor Configuration Auto-generation Guide

The one-click deployment tool of MindIE Motor can "convert the deployment scripts from the vllm-ascend community into Motor deployment configurations", reducing operational costs and ensuring consistency with the downstream inference engine configuration.

**Configuration examples for commonly deployed models are prepared in the same directory**: deepseek_v3.1, deepseek_v4_flash, deepseek_v4_pro, glm_5, glm_5.1, qwen3_5_397b, qwen_235b.

## Directory Overview

The configuration generation script is stored in the [examples/deployer/config_tool/](../../../deployer/config_tool/) directory. The functions of each file are as follows.

```bash
examples/deployer/config_tool/
├── vllm_to_motor.py                    # Configuration conversion script
├── run_dp_template_hybrid.sh           # Paste by user: vLLM-ascend co-deployment startup script
├── run_dp_template_prefill.sh          # Paste by user: vLLM-ascend P instance startup script
├── run_dp_template_decode.sh           # Paste by user: vLLM-ascend D instance startup script
└── output_config/                      # Generated Motor configuration content
    ├── user_config.json
    └── env.json
```

## Precautions

1. When performing [Step 2: Find the model deployment script in the vllm-ascend community](#step02) in "Usage", ensure that the vllm-ascend version in the deployment image is consistent with the community version, **to avoid applying new configurations to old code**.

2. The generated Motor configuration provides only one feasible model sharding example. **Users can adjust the number of servers occupied by the service and the model sharding strategy based on the number of cluster servers. When adjusting the model sharding strategy, pay attention to the following parameters**.

      | Configuration Item | Value Type | Value Range | Configuration Description |
      | --- | --- | --- | --- |
      | p_instances_num | int | ≥ 1 | Number of Prefill instances. |
      | d_instances_num | int | ≥ 1 | Number of Decode instances. |
      | single_p_instance_pod_num | int | ≥ 1 | Number of Pods into which one P instance is split. |
      | single_d_instance_pod_num | int | ≥ 1 | Number of Pods into which one D instance is split. |
      | p_pod_npu_num | int | ≥ 1, up to 16 cards per Pod | Number of NPU cards used by each P Pod. |
      | d_pod_npu_num | int | ≥ 1, up to 16 cards per Pod | Number of NPU cards used by each D Pod. |
      | data_parallel_size | int | ≥ 1 | Data parallelism (DP) size. |
      | tensor_parallel_size | int | ≥ 1 | Tensor parallelism (TP) size. |

      Number of NPU cards occupied by one P instance = `single_p_instance_pod_num` (number of Pods occupied, across how many machines) × `p_pod_npu_num` (number of NPUs occupied by each Pod) = `data_parallel_size` × `tensor_parallel_size`

3. The generated Motor configuration supports only the successful deployment of basic inference services. **Motor feature adjustments (for example, active/standby switchover, KV affinity scheduling, and service rate limiting) require users to modify the configuration manually**.

4. Single-container PD disaggregation and single-container PD co-location deployment scenarios are not supported currently.

## Usage

1. Enter the main directory of the configuration scripts.

    ```bash
    cd examples/deployer/
    ```

2. <a id="step02"></a>Find the deployment script in the vllm-ascend community.

    Go to the [vllm-ascend model deployment guide](https://github.com/vllm-project/vllm-ascend/tree/main/docs/source/tutorials/models), select the corresponding document based on the model, and find the model deployment script (usually named `run_dp_template.sh`) in the `Online Service Deployment` section of the document (usually Section 5). Focus on the **PD co-location script** (with the subsection title `Single-Node Online Deployment`) and the **PD disaggregation deployment script** (with the subsection title `Multi-Node PD Separation Deployment`).

    **Example**:

      Under the [dsv4 flash deployment guide](https://github.com/vllm-project/vllm-ascend/blob/main/docs/source/tutorials/models/DeepSeek-V4-Flash.md#51-single-node-online-deployment).

    - Under the "A3 series" subsection of Section 5.1 is the **PD co-location script**, whose content is as follows:

      ```bash
      export OMP_PROC_BIND=false
      export OMP_NUM_THREADS=10
      ...

      vllm serve /root/.cache/modelscope/hub/models/vllm-ascend/DeepSeek-V4-Flash-w8a8-mtp \
          --max-model-len 1048576 \
          --max-num-batched-tokens 10240 \
          ...
      ```

    - The script content of `run_dp_template.sh` in Section 5.2.1 is the **PD disaggregation deployment script**. The following uses the P instance script as an example:

      ```bash
      nic_name="xxxx" # change to your own nic name
      local_ip=xx.xx.xx.1 # change to your own ip

      export HCCL_IF_IP=$local_ip
      export GLOO_SOCKET_IFNAME=$nic_name
      ...

      vllm serve /root/.cache/modelscope/hub/models/vllm-ascend/DeepSeek-V4-Flash-w8a8-mtp \
          --host 0.0.0.0 \
          --port $2 \
          ...
      ```

    >[!NOTE]NOTE
    >In the scenario where one instance occupies multiple servers, the vllm-ascend community may provide multiple scripts for deploying one instance (corresponding to multiple servers respectively). There is no obvious difference in the configuration of these deployment scripts, and you only need to focus on one of them.
    >
    >For example: in Section 5.2 of the [qwen3-235B model deployment guide](https://github.com/vllm-project/vllm-ascend/blob/main/docs/source/tutorials/models/Qwen3-235B-A22B.md#52-multi-node-pd-separation-deployment), both Decode node 0 and Decode node 1 exist. When using them, select either one as the D instance deployment script.

3. Copy the vllm-ascend model deployment script to the `examples/deployer/config_tool/` directory.

    The `run_dp_template_prefill.sh`, `run_dp_template_decode.sh`, and `run_dp_template_hybrid.sh` files are used to store the vllm-ascend deployment scripts. These files are all stored in the `examples/deployer/config_tool/` directory.

    - **Scenario 1**: Deploy a PD disaggregation service through Motor.

       No additional modification is required. Directly copy the PD disaggregation deployment script from the URL above into the run_dp_template_prefill.sh (P node) and run_dp_template_decode.sh (D node) files.

    - **Scenario 2**: Deploy a PD co-location deployment service through Motor.

       No additional modification is required. Directly copy the PD co-location deployment script from the URL above into the `run_dp_template_hybrid.sh` file.

4. Generate the Motor configuration.

    Based on the scenario, run the following commands to directly generate the Motor configuration:

    ```bash
    # PD disaggregation, Atlas 800I A2 Inference Server
    python3 deploy.py --mode general_config  --deploy-scenario separate --hardware-type A2
    # PD co-location deployment, Atlas 800I A2 Inference Server
    python3 deploy.py --mode general_config  --deploy-scenario hybrid --hardware-type A2
    # PD disaggregation, Atlas 800I A3 SuperPoD Server
    python3 deploy.py --mode general_config --deploy-scenario separate --hardware-type A3
    # PD co-location deployment, Atlas 800I A3 SuperPoD Server
    python3 deploy.py --mode general_config  --deploy-scenario hybrid --hardware-type A3
    # PD disaggregation, Atlas 850 SuperPoD Server
    python3 deploy.py --mode general_config --deploy-scenario separate --hardware-type A5
    # PD co-location deployment, Atlas 850 SuperPoD Server
    python3 deploy.py --mode general_config --deploy-scenario hybrid --hardware-type A5
    ```

5. View the results and fine-tune them.

   Enter the `output_config` directory to view the generated `user_config.json` and `env.json` files.

    ```bash
    cd examples/deployer/config_tool/output_config && ls
    ```

   The following content in the `user_config.json` file must be manually filled in by the user based on the actual situation:

    ```bash
    {
      "version": "v2.0",
      "motor_deploy_config": {
        ...
        "image_name": "<Manually fill in the image name>",
        ...
        "weight_mount_path": "<Fill in the access path of the model weight file based on the actual situation>"
      },
      ...
      "motor_engine_prefill_config": {
        ...
        "engine_config": {
          ...
          "model": "<Fill in the access path of the model weight file based on the actual situation>",
          ...
        }
      },
      "motor_engine_decode_config": {
        ...
        "engine_config": {
          ...
          "model": "<Fill in the access path of the model weight file based on the actual situation>",
          ...
        }
      }
    }
    ```

    The `env.json` file does not need to be modified. At this point, the configuration file generation is complete.
