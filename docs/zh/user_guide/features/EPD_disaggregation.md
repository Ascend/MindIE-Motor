# EPD分离部署特性

## 特性介绍

分离式编码器将多模态大语言模型的视觉编码器阶段运行在与预填充/解码阶段分离的进程中。将这两个阶段部署在独立的vLLM实例中的优势详见[vLLM Ascend EPD分离特性说明](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/epd_disaggregation.html)，主要包括：

- **独立、细粒度的扩缩容**：视觉编码器较轻量，语言模型规模大几个数量级，编码器实例可以独立于语言模型并行度单独增减。
- **更低的首Token时延（TTFT）**：纯文本请求完全绕过视觉编码器，编码器输出仅在需要的注意力层注入，缩短预填充关键路径。
- **编码器输出的跨进程复用与缓存**：编码器结果可通过共享缓存供多个实例复用，避免重复计算。

MindIE Motor部署EPD分离特性支持`infer_service_set`和`multi_deployment`模式下部署，支持`CPCD`和`CDP`调度模式。

### 工作原理

```text
                ┌─────────────────────────────────────────────────────────────────┐
                │                                                                 │
                │                             MindIE Motor                        │
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
│              │      Encoder Cache      │              │        KV Cache         │              │
│    Encode    │───────────────────────► │    Prefill   │───────────────────────► │    Decode    │
│   instance   │     Transfer Engine     │   instance   │     Transfer Engine     │   instance   │
│              │                         │              │                         │              │
└──────────────┘                         └──────────────┘                         └──────────────┘
```

一次多模态请求的处理流程如下：

1. **Encode阶段**：请求到达Coordinator后，Router先调度E实例执行视觉编码。仅当请求包含多模态内容（`messages`中含`image_url`或`video_url`）时才会调度E实例；纯文本请求直接进入预填充阶段。
2. **编码器缓存传输**：E实例完成编码后，通过Encoder Cache Transfer Engine（`ec-transfer-config`）将编码器缓存传输至P实例。
3. **Prefill/Decode阶段**：P实例完成预填充生成KV Cache后，通过KV Transfer Engine（`kv_transfer_config`）传输至D实例解码，该链路与PD分离部署完全一致。

在`CPCD`和`CDP`两种调度模式下都是先调度E实例，然后再按照之前的PD分离逻辑进行调度。

### 约束与限制

| 约束维度 | 要求 |
|----------|------|
| 硬件 | <ul><li>Atlas 800I A2推理服务器</li><li>Atlas 800I A3超节点服务器</li></ul>  |
| 部署场景 | <ul><li>支持`infer_service_set`和`multi_deployment`部署模式；</li><li>支持`CPCD`和`CDP`调度模式；</li><li>不支持`single_container`部署模式。</li></ul> |
| 引擎 | 仅支持vLLM引擎（`ec-transfer-config`为vLLM Ascend EPD分离特有配置） |
| 特性互斥 | 不支持与PD混部部署（`hybrid_instances_num`，union实例）组合使用；PD混部配置会拒绝P/D分离相关字段，且混部拓扑下Coordinator不会调度E实例执行编码。 |
| 软件依赖 | <ul><li>vLLM Ascend需支持EPD分离特性；</li><li>P/D实例间KV传输依赖Mooncake（MooncakeConnectorV1）；</li><li>使用`ECExampleConnector`时E实例与P实例需访问同一共享存储路径（shared_storage_path）。</li></ul> |
| 其他限制 | <ul><li>部署EPD分离使用的模型权重需支持多模态理解能力；</li><li>当前vLLM Ascend的EPD分离特性支持两种connector；</li><li>E实例与P实例的`ec_connector`、`ec_connector_extra_config`配置必须一致。</li></ul> |

## 特性使用

### 环境准备

- 已参考[MindIE Motor快速开始](../quick_start.md)完成基础部署。
- 部署EPD分离使用的模型权重需支持多模态理解能力，本文以Qwen3-VL-30B-A3B-Instruct模型为例进行说明。
- 当前vLLM Ascend的EPD分离特性支持两种connector，本文以`ECExampleConnector`为例进行说明。

### 使用样例

MindIE Motor部署EPD分离只需修改user_config.json配置文件后，通过deploy.py脚本即可完成服务部署，具体流程如下：

1. 修改user_config.json配置文件。

   以[MindIE Motor快速开始](../quick_start.md)中实例user_config.json为参考基线，适配EPD分离部署的配置。

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

   配置项说明如下：

   | 配置项 | 类型 | 取值范围 | 必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | e_instances_num | int | ≥0的整数 | 否（启用EPD分离时必填） | 未配置 | E实例的个数；未配置或为0时不部署E实例，即不启用EPD分离。 |
   | p_instances_num | int | [1,16] | 是 | 无 | P实例的个数。 |
   | d_instances_num | int | [1,16] | 是 | 无 | D实例的个数。|
   | single_e_instance_pod_num | int | ≥1 | 否 | 1 | 每个E实例占用的Pod个数。 |
   | single_p_instance_pod_num | int | ≥1 | 否 | 1 | 每个P实例占用的Pod个数。 |
   | single_d_instance_pod_num | int | ≥1 | 否 | 1 | 每个D实例占用的Pod个数。 |
   | e_pod_npu_num | int | ≥1 | 否 | 1 | 每个E实例的Pod占用的NPU卡数。 |
   | p_pod_npu_num | int | ≥1 | 否 | 1 | 每个P实例的Pod占用的NPU卡数。 |
   | d_pod_npu_num | int | ≥1 | 否 | 1 | 每个D实例的Pod占用的NPU卡数。 |
   | deploy_mode | string | infer_service_set、multi_deployment、single_container | 否 | infer_service_set | 部署模式，EPD分离支持`infer_service_set`和`multi_deployment`；通过`--update_instance_num`更新配置时不允许修改该字段。 |
   | ec_connector | string | vLLM Ascend支持的EC connector | 是（`ec-transfer-config`内） | 无 | connector类型，本文以`ECExampleConnector`为例，E实例与P实例需一致。 |
   | ec_role | string | ec_producer、ec_consumer | 是（`ec-transfer-config`内） | 无 | 角色配置，E实例为`ec_producer`，P实例为`ec_consumer`。 |
   | shared_storage_path | string | 合法的存储路径 | 是（`ec_connector_extra_config`内） | 无 | E实例与P实例共享的编码器缓存目录，两侧配置需一致且路径可访问。 |

   >[!NOTE] 说明
   >
   >- 在motor_deploy_config配置中，`e_instances_num`表示E实例的个数，`single_e_instance_pod_num`表示每个E实例占用的pod个数，`e_pod_npu_num`表示每个E实例的pod占用的NPU卡数。
   >- 增加motor_engine_encode_config配置，其中E实例的engine_config需要增加`ec-transfer-config`配置，并将`ec_role`配置为`ec_producer`。详细参考[《vLLM Ascend EPD分离特性说明》](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/epd_disaggregation.html)。
   >- 同时motor_engine_prefill_config中的engine_config需要增加`ec-transfer-config`配置，并将`ec_role`配置为`ec_consumer`。
   >- `infer_service_set`模式下`single_e_instance_pod_num`、`e_pod_npu_num`未配置时默认为1；`multi_deployment`模式下未配置时沿用引擎Deployment模板默认值，建议显式配置。

2. 部署服务。

   在examples/deployer目录下通过deploy.py脚本部署服务。支持指定配置目录或单独指定配置文件：

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

### 验证特性

1. 确认E/P/D实例均启动成功。

   ```bash
   kubectl get pod -n mindie-motor -owide
   ```

   除Controller、Coordinator外，`multi_deployment`模式下可以看到E实例Deployment生成的Pod（engine_type为vllm时命名为`vllm-e0`、`vllm-e1`，即`{引擎基础名}-e{实例序号}`），以及P/D实例Pod（`vllm-p0`、`vllm-d0`），均处于Running状态；`infer_service_set`模式下InferServiceSet的encode角色副本数与`e_instances_num`一致。

2. 发送多模态推理请求，验证请求经过E实例编码后再由P/D实例完成预填充与解码（将`{coordinator_ip}:{port}`替换为Coordinator服务地址与端口，参考[MindIE Motor快速开始](../quick_start.md)）。

   ```bash
   curl -X POST http://{coordinator_ip}:{port}/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "qwen3",
       "messages": [
         {
           "role": "user",
           "content": [
             {"type": "text", "text": "描述这张图片的内容"},
             {"type": "image_url", "image_url": {"url": "<图片base64编码>"}}
           ]
         }
       ],
       "max_tokens": 128
     }'
   ```

   返回HTTP 200，响应体包含`choices`字段；Coordinator日志中可看到该请求先在E实例完成编码（`PD_Encode`阶段）。

3. 发送纯文本请求，验证请求绕过E实例直接进入预填充阶段（请求体仅需包含`"content": "hello"`等纯文本），服务同样正常返回。

## 常见问题

### 多模态请求未经过E实例

**问题描述**

发送多模态请求后，E实例没有任何请求记录，请求直接由P/D实例处理。

**原因分析**

Coordinator仅对包含`image_url`或`video_url`内容的多模态请求调度E实例，且要求E、P、D实例均已注册就绪；任一条件不满足时请求会跳过编码阶段。

**解决步骤**

1. 检查请求是否为标准多模态格式（`messages`中`content`为包含`image_url`/`video_url`的列表）。
2. 检查E实例是否正常注册：`kubectl get pod -n mindie-motor`确认E实例Pod处于Running状态。
3. 查看Coordinator日志确认实例就绪状态。

### 服务未就绪（Service is not available）

**问题描述**

推理请求返回`{"detail":"Service is not available"}`。

**原因分析**

EPD分离拓扑要求E、P、D三类实例同时注册后服务才处于就绪状态，任一类实例缺失或未完成注册时服务不可用。

**解决步骤**

1. 确认`user_config.json`中`e_instances_num`、`p_instances_num`、`d_instances_num`配置正确且对应实例均已启动。
2. 查看未就绪实例的引擎日志：`kubectl logs <pod-name> -n mindie-motor`。

### 编码器缓存传输失败

**问题描述**

E实例编码完成后，P实例无法获取编码器缓存，推理失败。

**原因分析**

E实例与P实例的`ec-transfer-config`配置不一致，或共享存储路径不可访问。

**解决步骤**

1. 检查E实例（`ec_producer`）与P实例（`ec_consumer`）的`ec_connector`、`ec_connector_extra_config.shared_storage_path`配置是否完全一致。
2. 确认`shared_storage_path`对应目录在E实例与P实例所在节点均可访问，且具有读写权限。

### P实例与D实例之间无法传输KV Cache

**问题描述**

P实例与D实例之间无法传输KV Cache，推理失败。

**原因分析**

`kv_transfer_config`中`kv_role`配置错误，或`kv_port`不一致。

**解决步骤**

1. 检查`kv_transfer_config`中`kv_role`是否正确：P为`kv_producer`，D为`kv_consumer`。
2. 检查P实例与D实例的`kv_port`是否一致。
