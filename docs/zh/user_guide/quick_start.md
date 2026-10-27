# 快速入门

本文档通过**简单快速**的部署案例，以Atlas 800I A2推理服务器（8卡）、Qwen3-8B模型、P/D实例各1个的场景为例，指导开发者体验基于MindIE Motor的PD分离服务部署流程。

如需详细的PD分离部署指导，请参考[PD 分离部署指导](./deployment/k8s/pd_disaggregation_deployment.md)。

## 什么是PD分离？

模型推理的Prefill阶段和Decode阶段分别实例化部署在不同的硬件资源上进行推理，提升推理性能，其特性介绍详情请参考[PD分离说明](./features/pd_disaggregation.md)。

## 环境要求

- 支持Atlas 800I A2推理服务器、Atlas 800I A3超节点服务器和Ascend 950PR系列产品。
- 至少需要1台已完成[环境准备](./environment_preparation.md)的服务器。

## 模型下载

请自行下载Qwen3-8B模型的权重文件并将权重文件上传至服务器任意目录（例：/mnt/weight）。执行以下命令，修改文件权限：

```bash
chmod -R 755 /mnt/weight
```

## 镜像准备

- 方式一：进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，在搜索框输入`motor`，单击“mindie-motor”卡片，在“镜像下载”页签根据设备型号下载对应的MindIE Motor镜像。
- 方式二：请参考[准备MindIE Motor镜像](./maintenance/build_motor_image_from_vllm_ascend.md)章节自制MindIE Motor镜像。

## 服务部署

1. 准备服务启动脚本。

   MindIE Motor官方完整镜像内已保存服务启动脚本（/tmp/motor/examples），可通过以下命令将镜像内的文件拷贝至宿主机。

   ```bash
   IMAGE="<镜像名或镜像ID>"

   cid=$(docker create "$IMAGE")
   docker cp "$cid:/tmp/motor/examples" ./examples
   docker rm "$cid"
   ```

   请将上述脚本（examples目录）上传至k8s集群的管理节点（master节点），后续部署操作均在管理节点执行。

2. 配置服务化参数。

   配置文件（user_config.json、env.json）可通过以下三种方式获取：

   - **方式一：使用已有典配（推荐常用模型）**

    代码仓中已有按模型与硬件提供典型配置，路径如下：

     ```text
     model_configs/vllm/models/<模型名>/<硬件型号>/
     ```

     例如：DeepSeek-V4-Flash模型在Atlas 800I A2推理服务器上的典配目录为： model_configs/vllm/models/deepseek_v4_flash/A2/。

     其中包含user_config.json与env.json配置文件。选用典配后，按实际场景修改镜像名（image_name）、权重路径（weight_mount_path/model）等少量字段即可部署。

     当前已提供的模型目录包括：deepseek_v3.1、deepseek_v4_flash、deepseek_v4_pro、glm_5、glm_5.1、qwen_235b。

     SGLang 典配位于 `model_configs/sglang/models/<模型名>/<硬件型号>/`，部署步骤见[SGLang PD 分离服务部署](./deployment/k8s/pd_disaggregation_sglang.md)。

   - **方式二：自动生成**

     将vLLM Ascend社区部署脚本一键转换为MindIE Motor配置。

     将vLLM Ascend社区脚本复制到`examples/deployer/config_tool/`目录下对应模板文件后，进入`examples/deployer/`目录执行以下命令（以PD分离服务部署、Atlas 800I A2推理服务器为例）：

     ```bash
     python3 deploy.py --mode general_config --deploy-scenario separate --hardware-type A2
     ```

     生成的user_config.json和env.json配置文件结果件位于`examples/deployer/config_tool/output_config/`目录，一键转换工具详细步骤请参考[MindIE Motor配置自动生成指导](../../../model_configs/vllm/models/README.md)。

   - **方式三：手工编辑**

     下文以Qwen3-8B、P/D各1实例为例，直接编辑`model_configs/vllm/`目录下的user_config.json和env.json配置文件。

     在管理节点执行以下命令，进入服务启动脚本所在目录。

     ```bash
     cd examples/deployer/
     ```

     - **修改user_config.json配置文件**

       ```bash
       vim ../../model_configs/vllm/user_config.json
       ```

       user_config.json配置文件的完整示例如下所示（可直接复制使用，5项`xxxxxx`变量需用户根据实际内容自行修改，如需了解各字段含义请参考[user_config.json配置文件全量参数说明](./configuration/config_reference.md)）：

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
           "image_name": "xxxxxx镜像名称。例如：mindie-motor-vllm:dev-26.1.0.B050-800I-A2-py311-Ubuntu24.04-lts-aarch64",
           "job_id": "mindie-motor",
           "hardware_type": "xxxxxx硬件类型。Atlas 800I A2推理服务器：800I_A2； Atlas 800I A3超节点服务器：800I_A3；Ascend 950PR系列产品：Ascend950",
           "weight_mount_path": "xxxxxx权重文件路径。例如：/mnt/weight/qwen3_8B"
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

     - **修改env.json配置文件**

       ```bash
       vim ../../model_configs/vllm/env.json
       ```

       env.json配置文件完整示例如下所示（可直接复制使用）：

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

3. 创建命名空间（namespace）。

   namespace的值必须与`user_config.json`中的`job_id`字段相同（默认值：mindie-motor）。

   ```bash
   kubectl create ns mindie-motor
   ```

4. 启动与终止服务。

   在`examples/deployer`目录下通过`deploy.py`脚本部署PD分离服务（更多参数与用法请参考[Deployer部署工具说明](../../../examples/deployer/README.md)）：

   ```bash
   python3 deploy.py --config_dir ../../model_configs/vllm
   ```

   需要终止服务时，在同一目录执行以下命令：

   ```bash
   bash delete.sh mindie-motor
   ```

   命令中的mindie-motor为命名空间，根据实际名称修改。

5. 查看日志。

   执行以下命令，将`name_space`字段填写为命名空间名称（例如：mindie-motor）。

   ```bash
   vim log_collect/log_config.ini
   ```

   然后执行以下命令收集日志：

   ```bash
   bash show_log.sh
   ```

   所有业务日志（controller、coordinator、P/D实例）均会保存在`examples/deployer/log_collect/log`目录下，并持续刷新，直到服务被终止。

## 推理验证

新建一个命令行窗口，在K8s集群的管理节点（master节点）执行以下命令：

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

```Output
{"detail":"Service is not available"}
```

等待一段时间后再次尝试，回显类似如下内容说明推理服务已就绪：

```Output
data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3-8B","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}],"prompt_token_ids":null}

data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3-8B","choices":[{"index":0,"delta":{"content":"<think>"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3-8B","choices":[{"index":0,"delta":{"content":"\n"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3-8B","choices":[{"index":0,"delta":{"content":"Okay"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

...

data: [DONE]
```
