# 快速入门

## 产品简介

MindIE PyMotor是面向通用大模型PD分离部署场景的推理服务化框架，通过开放、可扩展的推理服务化平台架构提供推理服务化能力，支持对接业界主流推理框架接口，满足大语言模型的高性能推理需求。

## 关键特性

| 特性       | 说明              |
| ------------ | ----------------- |
| **PD分离部署** | 模型推理的Prefill阶段和Decode阶段分别实例化部署在不同的机器资源上同时进行推理，提升推理性能，其特性介绍详情请参见[PD分离部署](https://gitcode.com/Ascend/MindIE-Motor/blob/master/docs/zh/user_guide/service_deployment/pd_separation_service_deployment.md)。 |

## 快速开始

### 环境准备

本文档以Atlas 800I A2 推理服务器和Qwen3-8B模型为例，让开发者快速开始使用MindIE PyMotor进行大模型PD分离部署和推理流程。

#### 前提条件

物理机部署场景，需要在物理机安装NPU驱动固件以及部署Docker，执行如下步骤判断是否已安装NPU驱动固件、K8s集群和部署Docker。

- 执行以下命令查看NPU驱动固件是否安装。

  ```bash
  npu-smi info
  ```

  **图1** 回显信息

  ![image](https://www.hiascend.com/doc_center/source/zh/mindie/22RC1/quickstart/figure/zh-cn_image_0000002474350016.png)

  **表1** Atlas A2 推理系列产品

  | 产品型号 | 参考文档 |
  | --- | --- |
  | Atlas 800I A2 | 《Atlas A2 中心推理和训练硬件 24.1.0 NPU驱动和固件安装指南》中的“[物理机安装与卸载](https://support.huawei.com/enterprise/zh/doc/EDOC1100438838/b1977c97)”章节 |

- 执行以下命令查看K8s集群是否就绪。

  ```bash
  kubectl get node -A
  ```

  回显以下信息表示K8s集群已就绪。

  ```bash
  NAME         STATUS   ROLES                         AGE   VERSION
  ```

- 执行以下命令查看Docker是否已安装并启动。

  ```bash
  docker ps
  ```

  回显以下信息表示Docker已安装并启动。

  ```bash
  CONTAINER ID        IMAGE        COMMAND         CREATED        STATUS         PORTS           NAMES
  ```

#### 获取模型权重

1. 请先下载权重，这里以Qwen3-8B为例，请到官方下载权重文件并将权重文件上传至服务器任意目录（如`/mnt/weight`）。
2. 执行以下命令，修改权重文件权限：

   ```bash
   chmod -R 755 /mnt/weight
   ```

#### 获取容器镜像

进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，根据设备型号选择下载对应的PyMotor镜像。

该镜像已具备模型运行所需的基础环境。

### PD分离部署

> [!NOTE]部署方式说明
> 当前默认采用 **CRD 方式**（基于 MindCluster 的 PD 分离 CRD 与 Operator）进行部署。该方式尚未完成 RAS 能力与池化能力的适配验证。若您需要 RAS（可靠性、可用性、可服务性）或 KV 池化能力，可在 `user_config.json` 的 `motor_deploy_config.deploy_mode` 中配置为 `multi_deployment`，切换为原有的多 YAML Deployment 方式。完整部署说明请参考 [PD 分离服务部署](./deployment/k8s/pd_disaggregation_deployment.md)。

1. **将 examples 目录准备并上传至 K8s 集群的 master 服务器上**。

   - **（方式一）从本代码仓获取**：将仓库根目录下的 `examples` 目录上传至 master 服务器。

   - **（方式二）从容器镜像获取**：若无完整代码仓，但已拉取PyMotor推理镜像，可使用镜像内预置的示例目录，路径为 **`/tmp/motor/examples`**（目录结构与仓库中的 `examples/` 一致）。在已拉取镜像的机器上执行（将 `IMAGE` 替换为实际镜像名或镜像 ID，可与 `user_config.json` 中 `motor_deploy_config.image_name` 保持一致）：

     ```bash
     IMAGE="<镜像名或镜像ID>"

     cid=$(docker create "$IMAGE")
     docker cp "$cid:/tmp/motor/examples" ./examples
     docker rm "$cid"
     ```

     将得到的 `examples` 目录按**方式一**的方式上传至 master 服务器。若使用 Podman，将命令中的 `docker` 替换为 `podman` 即可。

2. **配置服务化参数**。

   - 打开 `examples/infer_engines/vllm/user_config.json` 文件（或 `examples/infer_engines/vllm/models/` 下对应模型配置，如 `examples/infer_engines/vllm/models/deepseek/v3_1/user_config.json`，以实际使用的为准）

     ```bash
     cd examples/infer_engines/vllm
     vim user_config.json
     ```

   - 根据实际情况修改`user_config.json`中的配置参数。（以下以Qwen3-8B为例）

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
          "engine_config": {
            "served_model_name": "qwen3-8B",
            "model": "/mnt/weight/qwen3_8B",
            "gpu_memory_utilization": 0.9,
            "data_parallel_size": 1,
            "tensor_parallel_size": 4,
            "pipeline_parallel_size": 1,
            "enable_expert_parallel": false,
            "data_parallel_rpc_port": 9000,
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
            "model": "/mnt/weight/qwen3_8B",
            "gpu_memory_utilization": 0.9,
            "data_parallel_size": 1,
            "tensor_parallel_size": 4,
            "pipeline_parallel_size": 1,
            "enable_expert_parallel": false,
            "data_parallel_rpc_port": 9000,
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

     如上的参数说明如下：

     | 配置项 | 取值类型 | 取值范围 | 配置说明 |
     | --- | --- | --- | --- |
     | version | string | v2.0 | 配置文件版本 |
     | p_instances_num | int | ≥1 | P实例个数 |
     | d_instances_num | int | ≥1 | D实例个数 |
     | single_p_instance_pod_num | int | ≥1 | 单个P实例所占pod容器个数 |
     | single_d_instance_pod_num | int | ≥1 | 单个D实例所占pod容器个数 |
     | p_pod_npu_num | int | ≥1 | 单个P节点pod容器所占用的NPU卡数 |
     | d_pod_npu_num | int | ≥1 | 单个D节点pod容器所占用的NPU卡数 |
     | image_name | string | 字符串 | docker加载的镜像名称，例如“vllm-ascend:b150_motor” |
     | job_id | string | 字符串 | PD分离部署任务名称，例如“mindie-pymotor” |
     | hardware_type | string | A2: 800I_A2<br>A3: 800I_A3<br>A5: 850-Atlas-8p-8 | 服务器硬件类型 |
     | weight_mount_path | string | 字符串 | 权重文件路径 |
     | motor_controller_config | dict | controller组件配置 | 在此处可以进行任意特定配置项的设置 |
     | motor_coordinator_config | dict | coordinator组件配置 | 在此处可以进行任意特定配置项的设置 |
     | engine_type | string | 字符串 | 对接的推理引擎类型，例如”vllm” |
     | motor_nodemanager_config | dict | nodemanager组件配置 | 在此处可以进行任意特定配置项的设置 |
     | served_model_name | string | 字符串 | 模型名称，例如”qwen3-8B” |
     | model | string | 文件路径 | 模型权重文件所在路径 |
     | gpu_memory_utilization | float | 0到1之间的小数 | NPU内存使用占比上限，例如”0.9” |
     | data_parallel_size | int | ≥1 | 数据并行参数 |
     | tensor_parallel_size | int | ≥1 | 张量并行参数 |
     | pipeline_parallel_size | int | ≥1 | 流水线并行参数 |
     | enable_expert_parallel | bool | [true, false] | 专家并行开关 |
     | data_parallel_rpc_port | int | 有效端口范围 | RPC通信的端口号 |
     | engine_config | dict | 推理引擎原生参数 | 与引擎 CLI 参数等价，直接以 JSON 键值填写（如 `tensor_parallel_size`、`enforce-eager`）；也可使用 [engine_config 命令行转换工具](operations/cli_to_engine_config_guide.md) 从命令行迁移 |

   - 配置 k8s 的 namespace，配置 namespace 值为 `user_config.json` 中的 `job_id`。

     ```bash
     kubectl create ns mindie-motor
     ```

3. **配置环境变量**。

   - 打开 `examples/infer_engines/vllm/env.json` 文件

     ```bash
     cd examples/infer_engines/vllm
     vim env.json
     ```

   - 根据实际情况修改`env.json`中的配置参数。

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

4. **启动服务**

   在 `examples/deployer` 目录下执行，支持两种指定配置的方式：

   **方式一：指定配置目录（推荐）**，目录下需包含 `user_config.json` 和 `env.json`：

   ```bash
   cd examples/deployer
   python3 deploy.py --config_dir ../infer_engines/vllm
   ```

   **方式二：单独指定配置文件路径**，`--user_config_path` 与 `--env_config_path` 必须同时指定：

   ```bash
   cd examples/deployer
   python3 deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
   ```

   也可使用简写 `--config` 和 `--env`。

5. **发送请求**

   执行以下命令：

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

   等待一段时间后再次尝试。回显类似如下内容说明推理服务已就绪

   ```json
   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"role":"assistant","content":""},"logprobs":null,"finish_reason":null}],"prompt_token_ids":null}

   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"<think>"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"\n"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

   data: {"id":"17658563046856100000c836403d","object":"chat.completion.chunk","created":1765856304,"model":"qwen3","choices":[{"index":0,"delta":{"content":"Okay"},"logprobs":null,"finish_reason":null,"token_ids":null}]}

   ...

   data: [DONE]
   ```
