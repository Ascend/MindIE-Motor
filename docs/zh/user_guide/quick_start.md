# 快速入门

本文档通过**简单快速**的部署案例（以Atlas 800I A2服务器、Qwen3-8B模型、P/D实例各一个的场景为例）指导开发者体验基于MindIE-Motor的PD分离服务部署流程。

如果详细的PD分离部署指导，请参考[PD分离部署指导](./deployment/k8s/pd_disaggregation_deployment.md)。

---

## 什么是PD分离？

模型推理的Prefill阶段和Decode阶段分别实例化部署在不同的硬件资源上进行推理，提升推理性能，其特性介绍详情请参见[PD分离部署](./features/pd_disaggregation.md)。

---

## 环境要求

- 支持Atlas 800I A2或者Atlas 800 A3 超节点服务器。

- 至少需要1台已完成[环境准备](./environment_preparation.md)的服务器。

---

## 模型下载

请自行下载Qwen3-8B模型的权重文件并将权重文件上传至服务器任意目录（以`/mnt/weight`为例）。执行以下命令，修改文件权限：

   ```bash
   chmod -R 755 /mnt/weight
   ```

---

## 镜像准备

进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，在搜索框查询 `motor`，进入搜索结果后根据设备型号下载对应的MindIE-Motor镜像。

---

## 服务部署

1. **准备服务启动脚本**。

     MindIE-Motor官方完整镜像内已保存服务启动脚本（`/tmp/motor/examples`），可通过以下命令将镜像内的文件拷贝至宿主机。

       ```bash
       IMAGE="<镜像名或镜像ID>"

       cid=$(docker create "$IMAGE")
       docker cp "$cid:/tmp/motor/examples" ./examples
       docker rm "$cid"
       ```

    请将上述脚本目录（examples目录）上传至**k8s集群的管理节点（master节点），后续部署操作均在管理节点执行**。

2. **配置服务化参数**。

   在管理节点执行以下命令，进入服务启动脚本所在目录并修改配置文件。

     ```bash
     cd examples/deployer/
     vim ../infer_engines/vllm/user_config.json
     ```

   user_config.json文件**完整示例**如下（可直接复制使用，4项xxxxxx内容需用户自行修改，如需了解各字段含义可参考 [user_config 全量参数说明](./deployment/k8s/config_reference.md)。）：

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
          "hardware_type": "xxxxxx 硬件类型。A2：800I_A2 A3：800I_A3",
          "weight_mount_path": "/mnt/weight/"
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
            "model": "xxxxxx。权重文件路径。例如：/mnt/weight/qwen3_8B",
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

3. **配置环境变量**。

   执行以下命令修改环境变量配置文件。

     ```bash
     vim ../infer_engines/vllm/env.json
     ```

   env.json文件**完整示例**如下（可直接复制使用）：

     ```bash
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
      "motor_kv_cache_pool_env": {},
      "motor_kv_conductor_env": {}
    }
     ```

4. **启动与终止服务**

   创建命名空间（namespace），namespace 的值必须与 `user_config.json` 中的 `job_id`字段相同（默认值为mindie-motor）。

     ```bash
     kubectl create ns mindie-motor
     ```

   执行以下命令，部署PD分离服务：

   ```bash
   python3 deploy.py --config_dir ../infer_engines/vllm
   ```

   需要终止服务时，执行以下命令即可：

   ```bash
   bash delete.sh 命名空间(填入手动创建的命名空间名称，例如：mindie-motor)
   ```

5. **查看日志**。

   执行 `vim log_collect/log_config.ini` 命令，将 `name_space` 填写为命名空间名称（例如：mindie-motor），然后执行以下命令收集日志：

   ```bash
   bash show_log.sh
   ```

   所有业务日志（controller、coordinator、P/D实例）均会保存于 `examples/deployer/log_collect/log`目录下，并持续刷新，直到服务被终止。

---

## 推理验证

新建一个命令行窗口，在k8s集群的管理节点（master节点）执行以下命令：

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

返回结果如果如下，则说明尚未启动就绪：

   ```json
   {"detail":"Service is not available"}
   ```

等待一段时间后再次尝试。回显类似如下内容说明推理服务已就绪：

   ```json
   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}],"prompt_token_ids":null}

   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"<think>"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"\n"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"Okay"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

   ...

   data: [DONE]
   ```
