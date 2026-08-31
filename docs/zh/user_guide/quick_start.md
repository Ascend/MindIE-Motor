# 快速入门

本文档通过**简单快速**的部署案例（以Atlas 800I A2 推理服务器、Qwen3-8B模型、P/D 实例各一个的场景为例）指导开发者体验基于MindIE Motor的PD分离服务部署流程。

如需详细的PD分离部署指导，请参考 [PD 分离部署指导](./deployment/k8s/pd_disaggregation_deployment.md)。

## 什么是 PD 分离？

模型推理的 Prefill 阶段和 Decode 阶段分别实例化部署在不同的硬件资源上进行推理，提升推理性能，其特性介绍详情请参见 [PD分离说明](./features/pd_disaggregation.md)。

## 环境要求

- 支持 Atlas 800I A2 推理服务器、Atlas 800I A3 超节点服务器和Atlas 850 超节点服务器。
- 至少需要 1 台已完成 [环境准备](./environment_preparation.md) 的服务器。

## 模型下载

请自行下载 Qwen3-8B 模型的权重文件并将权重文件上传至服务器任意目录（以 `/mnt/weight` 为例）。执行以下命令，修改文件权限：

```bash
chmod -R 755 /mnt/weight
```

## 镜像准备

- 方式一：进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，在搜索框查询 `motor`，进入搜索结果后根据设备型号下载对应的MindIE-Motor镜像。
- 方式二：参考[准备MindIE Motor镜像](./maintenance/build_motor_image_from_vllm_ascend.md)章节自制MindIE Motor镜像。

## 服务部署

1. **准备服务启动脚本**

   MindIE Motor官方完整镜像内已保存服务启动脚本（`/tmp/motor/examples`），可通过以下命令将镜像内的文件拷贝至宿主机。

   ```bash
   IMAGE="<镜像名或镜像ID>"

   cid=$(docker create "$IMAGE")
   docker cp "$cid:/tmp/motor/examples" ./examples
   docker rm "$cid"
   ```

   请将上述脚本目录（examples 目录）上传至k8s集群的管理节点（master 节点），后续部署操作均在管理节点执行。

2. **配置服务化参数**

   配置文件（`user_config.json`、`env.json`）可通过以下方式获取：

   - **使用已有典配（推荐常用模型）**：`examples/infer_engines/vllm/models/` 下已按模型与硬件提供典型配置，路径规则为：

     ```text
     examples/infer_engines/vllm/models/<模型名>/<硬件型号>/
     ```

     例如 DeepSeek-V4-Flash 在 Atlas 800I A2 推理服务器 上的典配目录为 `examples/infer_engines/vllm/models/deepseek_v4_flash/A2/`（内含 `user_config.json` 与 `env.json`）。选用典配后，按实际场景修改镜像名（`image_name`）、权重路径（`weight_mount_path` / `model`）等少量字段即可部署。当前已提供的模型目录包括 `deepseek_v3.1`、`deepseek_v4_flash`、`deepseek_v4_pro`、`glm_5`、`glm_5.1`、`qwen_235b` 等，硬件子目录为 `A2` / `A3` / `A5`（以实际目录为准）。

   - **自动生成**：可将 vllm-ascend 社区部署脚本一键转换为 Motor 配置。将社区脚本粘贴到 `examples/deployer/config_tool/` 下对应模板后，在 `examples/deployer/` 执行（以 PD 分离、Atlas 800I A2 推理服务器 为例）：

     ```bash
     python3 deploy.py --mode general_config --deploy-scenario separate --hardware-type A2
     ```

     生成结果位于 `examples/deployer/config_tool/output_config/`。完整步骤与注意事项见 [MindIE Motor 配置自动生成指导](../../../examples/infer_engines/vllm/models/README.md)。

   - **手工编辑（本快速入门）**：下文以 Qwen3-8B、P/D 各 1 实例为例，直接编辑 `examples/infer_engines/vllm/` 下配置。

     在管理节点执行以下命令，进入服务启动脚本所在目录并修改配置文件。

     ```bash
     cd examples/deployer/
     ```

     - **修改 `user_config.json`**

       ```bash
       vim ../infer_engines/vllm/user_config.json
       ```

       `user_config.json` 文件**完整示例**如下（可直接复制使用，5 项 xxxxxx 内容需用户自行修改，如需了解各字段含义可参考 [user_config 全量参数说明](./configuration/config_reference.md)）：

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
           "image_name": "xxxxxxx 镜像名称。例如：mindie-motor-vllm:dev-26.1.0.B050-800I-A2-py311-Ubuntu24.04-lts-aarch64",
           "job_id": "mindie-motor",
           "hardware_type": "xxxxxx 硬件类型。Atlas 800I A2 推理服务器：800I_A2， Atlas 800I A3 超节点服务器：800I_A3",
           "weight_mount_path": "xxxxxx 权重文件路径。例如：/mnt/weight/qwen3_8B"
         },
         "motor_controller_config": {},
         "motor_coordinator_config": {},
         "motor_engine_prefill_config": {
           "engine_type": "vllm",
           "motor_nodemanger_config": {},
           "engine_config": {
             "served_model_name": "qwen3-8B",
             "model": "xxxxxx。权重文件路径。例如：/mnt/weight/qwen3_8B",
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
             "model": "xxxxxx。权重文件路径。例如：/mnt/weight/qwen3_8B",
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

     - **修改 `env.json`**

       ```bash
       vim ../infer_engines/vllm/env.json
       ```

       `env.json` 文件**完整示例**如下（可直接复制使用）：

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

3. **启动与终止服务**

   创建命名空间（namespace），namespace 的值必须与 `user_config.json` 中的 `job_id` 字段相同（默认值为 mindie-motor）。

   ```bash
   kubectl create ns mindie-motor
   ```

   在 `examples/deployer` 目录下通过 `deploy.py` 脚本部署 PD 分离服务（更多参数与用法见 [Deployer 部署工具说明](../../../examples/deployer/README.md)）：

   ```bash
   cd examples/deployer
   python3 deploy.py --config_dir ../infer_engines/vllm
   ```

   需要终止服务时，在同一目录执行以下命令即可：

   ```bash
   bash delete.sh 命名空间(填入手动创建的命名空间名称，例如：mindie-motor)
   ```

4. **查看日志**

   执行 `vim log_collect/log_config.ini` 命令，将 `name_space` 填写为命名空间名称（例如：mindie-motor），然后执行以下命令收集日志：

   ```bash
   bash show_log.sh
   ```

   所有业务日志（controller、coordinator、P/D 实例）均会保存于 `examples/deployer/log_collect/log` 目录下，并持续刷新，直到服务被终止。

## 推理验证

新建一个命令行窗口，在 k8s 集群的管理节点（master 节点）执行以下命令：

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

如果返回如下结果，则说明尚未启动就绪：

```json
{"detail":"Service is not available"}
```

等待一段时间后再次尝试，回显类似如下内容说明推理服务已就绪：

```text
data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}],"prompt_token_ids":null}

data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"<think>"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"\n"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"Okay"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

...

data: [DONE]
```
