# PD分离服务部署（基于SGLang推理引擎）

本文皆在指导开发者基于MindIE Motor的SGLang推理引擎完成Kubernetes环境下的PD分离部署，并提供生成环境配置优化实践指导。若底层推理引擎为vLLM，请参考[PD分离服务部署（基于vLLM推理引擎）](./pd_disaggregation_deployment.md)。

## 部署要点

| 配置项 | 说明 |
| --- | --- |
| 配置字段 | engine_type: sglang |
| 引擎拉起 | python3 -m sglang.launch_server |
| PD协调 | bootstrap（bootstrap_host/bootstrap_port/bootstrap_room） |
| 必配端口 | 业务口，以及disaggregation-bootstrap-port |
| 传输/存储 | 配置disaggregation-transfer-backend: ascend，Deployer会额外拉起**MF Store**。 |
| 健康探测 | 原生生成式：GET /health |
| 配置文件生成 | 使用/model_configs/sglang目录下的典配文件，按实际环境修改 |

> [!NOTE] 说明
>客户端只访问**Coordinator**北向接口，切换引擎无需改调用方式。

## 环境预检查

使用Ascend 950PR&950DT系列产品部署推理服务时，需要准备**hixlep配置路径**。（其他硬件产品忽略此步骤）

请在每台服务器检查/lib/route.conf、/etc/hccl_rootinfo.json配置文件以及/etc/hixlep目录（UB链路拓扑结构）是否存在且正确；若不存在或不正确，请参考[hixlep配置文件生成文档](https://gitcode.com/cann/hixl/wiki/A5%20LocalCommRes%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md)生成对应内容，在生成/etc/hixlep时使用“D2D场景”即可。

## 约束与限制

| 维度 | 说明 |
| --- | --- |
| Anthropic接口/v1/messages | 依赖上游SGLang，仅/v1/messages/count_tokens可用。 |
| chat流式+重调度 | 受引擎return_token_ids限制，当前不可用。 |

## 获取启动脚本与镜像

### 环境要求

- 推理节点已完成[环境准备](../../environment_preparation.md)。
- 模型权重已放到集群可访问路径，并与weight_mount_path/model-path保持一致。
- P/D各至少具备典配所需的NPU数量，详情请参考[推荐典配](#推荐典配)。

### 准备镜像

通过以下方式获取镜像，并将镜像加载至K8s集群的所有节点：

基于Ascend SGLang基座镜像手动安装MindIE Motor，请参考[基于vllm-ascend/sglang镜像安装MindIE Motor](../../maintenance/build_motor_image_from_vllm_ascend.md#基于vllm-ascendsglang镜像安装mindie-motor)。

### 准备examples目录

从代码仓拷贝仓库中的/examples目录上传至K8s集群的master节点，examples目录结构如下所示：

```text
examples/
├── deployer/                         # deploy.py / delete.sh / show_log.sh
└── model_configs/
    └── sglang/
        ├── user_config.json          # 通用模板
        ├── env.json
        ├── models/
        │   └── glm5.1/A3/            # GLM 5.1 Atlas 800I A3超节点服务器 参考典配
        └── pd_hybrid/
            └── deepseek_v4_flash/A3/ # deepseek-v4-flash Atlas 800I A3超节点服务器 混部参考典配
```

>[!NOTE] 说明
>
>- 业务拓扑与deploy_mode`说明详情请参考[部署方式配置说明](./README.md)。
>- 部署工具用法详见examples/deployer/README.md。

## 准备配置文件

请直接选用仓库典配，并按实际环境修改后部署。

### 推荐典配

| 典型场景 | 目录 | 说明 |
| --- | --- | --- |
| <ul><li>GLM 5.1</li><li>Atlas 800I A3超节点服务器 1P1D</li></ul> | model_configs/sglang/models/glm5.1/A3/ | GLM 5.1模型的PD分离参考，更多典配请参考[SGLang GLM 5.1 Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/glm_5_1)。 |
| <ul><li>GLM 5.2</li><li>Atlas 800I A3超节点服务器 1P1D</li></ul> | model_configs/sglang/models/glm5.2/A3/ | GLM 5.2模型PD分离参考，更多典配请参考[SGLang GLM 5.2 Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/glm_5_2)。 |
| <ul><li>Qwen3-8B</li><li>Atlas 800I A2推理服务器</li></ul> | model_configs/sglang/models/qwen_8b/A2/ | 资源占用较小，适合打通流程，更多典配请参考[SGLang Qwen3-8B Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/qwen3_8b)。 |
| 更多模型 | model_configs/sglang/ | 根目录及子目录下的user_config.json与env.json配置文件。 |

每个典配目录需包含：

- user_config.json：部署拓扑、Motor组件配置、引擎参数。
- env.json：各角色环境变量。

### 配置user_config.json文件

配置典配中的user_config.json文件，需要确认的参数如下所示：

| 参数 | 模块 | 说明 |
| --- | --- | --- |
| image_name | motor_deploy_config | 与集群内可用镜像一致。 |
| job_id | motor_deploy_config | 即K8s的namespace名称。 |
| hardware_type | motor_deploy_config | 硬件类型，如：800I_A2或800I_A3。 |
| weight_mount_path | motor_deploy_config | 宿主机权重挂载根路径。 |
| p\_\*/d\_\*  | motor_deploy_config | 实例与卡数，与节点NPU、并行度匹配。 |
| *_node_selector | motor_deploy_config | 非必须，按集群hostname绑定Controller/Coordinator/P/D/MF Store。 |
| coordinator_infer_node_port等 | motor_deploy_config | 非必须，避免与现网 NodePort冲突。 |
| engine_type | P/D引擎段 | 引擎类型，必须为：sglang。 |
| model-path/served-model-name | engine_config | 权重路径与对外模型名。 |
| disaggregation-bootstrap-port |  engine_config（**Prefill**） | SGLang PD bootstrap端口；未配置则不生成bootstrap元数据。 |

### 配置示例

以下仅为Qwen3-8B示例，更多详细配置见：model_configs/sglang/models/{model_name}/{hardware_type}/目录。

```json
"motor_engine_prefill_config": {
  "engine_type": "sglang",
  "engine_config": {
    "served-model-name": "qwen3-8B",
    "model-path": "/mnt/weight/Qwen3-8B",
    "tp-size": 2,
    "pp-size": 1,
    "dp-size": 1,
    "mem-fraction-static": 0.7,
    "disaggregation-bootstrap-port": 9100,
    "disaggregation-transfer-backend": "ascend",
    "attention-backend": "ascend",
    "trust-remote-code": true
  }
},
"motor_engine_decode_config": {
  "engine_type": "sglang",
  "engine_config": {
    "served-model-name": "qwen3-8B",
    "model-path": "/mnt/weight/Qwen3-8B",
    "tp-size": 2,
    "pp-size": 1,
    "dp-size": 1,
    "mem-fraction-static": 0.7,
    "disaggregation-transfer-backend": "ascend",
    "attention-backend": "ascend",
    "trust-remote-code": true
  }
}
```

>[!NOTE] 说明：
>
>- engine_config配置模块由NodeManager组件透传为sglang.launch_server参数，字段名以所用SGLang版本CLI为准（支持kebab-case，部分也兼容underscore）。
>- Prefill侧disaggregation-bootstrap-port（或disaggregation_bootstrap_port）写入实例元数据的bootstrap_port，供Coordinator的SGLang Adapter做P/D对接，禁止与业务口endpoint_config.service_ports混用。
>- 首次打通建议scheduler_type: load_balance，reschedule_config.enable: false，高级特性请参考[特性配置](#特性配置)。
>- 全量字段说明请参考[user_config.json配置文件全量参数说明](../../configuration/config_reference.md)。

## 服务部署与验证

以下操作在K8s集群的master节点执行。以GLM 5.1模型、Atlas 800I A3超节点服务器为例。

### 启动服务

使用以下命令启动服务：

```bash
# <namespace>须与user_config.json中job_id保持一致
kubectl create namespace <namespace>

cd examples/deployer
python3 deploy.py --config_dir ../../model_configs/sglang/models/glm5.1/A3
```

>[!NOTE] 说明
>
>- 镜像与权重：确认各节点可正常拉取image_name，并且model-path在容器内可读。
>- NPU Pending：部署前检查目标节点资源是否被其他任务占满，Motor与纯SGLang等同机路径勿并行抢卡。
>- 重部署：先完整卸载并等待资源释放，再使用`deploy.py`，勿依赖`rollout restart`做配置切换。
>- PP场景：多PP场景时，需保证bootstrap和集合通信相关环境与典配一致，保证NodeManager可注入PP bootstrap相关环境变量。
>- 大模型权重加载可能需要数分钟，请等待P/D实例Ready后再重试。

### 查看状态

使用以下命令查看服务状态：

```bash
kubectl get pods -n <namespace> -o wide
```

| 运行时类型（示例） | 说明 |
| --- | --- |
| mindie-motor-controller-* | 服务管理 |
| mindie-motor-coordinator-* | 请求调度/业务入口 |
| mindie-motor-mf-store-*或mf-store | 服务KV Cache传输 |
| sglang-p* | Prefill实例 |
| sglang-d* | Decode实例 |
| kv-conductor-*（可选） | 服务KV亲和特性 |

>[!NOTE] 说明：
>Pod为Running不等于业务完全就绪，实际启动状态需结合日志与`/v1/models`确认。

### 查看日志

使用以下命令查看服务日志：

```bash
cd examples/deployer
# 编辑log_collect/log_config.ini：name_space改为job_id
vim log_collect/log_config.ini
bash show_log.sh
```

### 验证服务

发送以下推理请求验证服务是否正常：

```bash
# <node_ip>：暴露Coordinator Infer NodePort的节点IP
# <infer_port>：user_config中coordinator_infer_node_port
# <model>：engine_config中served-model-name
curl -s http://<node_ip>:<infer_port>/v1/models

curl -X POST http://<node_ip>:<infer_port>/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model>",
    "messages": [{"role": "user", "content": "你是谁？"}],
    "max_tokens": 360,
    "stream": true
  }'
```

- 若返回{"detail":"Service is not available"}：表示尚未就绪；先等P/D和MF Store Ready。检查Prefill是否因MF Store未就绪导致SMEM/传输初始化失败。
- 若返回流式JSON：推理正常。更多接口请参考[业务接口](../../api/service_interfaces.md)。

### 终止服务

推理完成后，如需终止服务，可使用以下命令：

```bash
bash delete.sh <namespace>
```

>[!NOTE] 说明：
>重部署前请确认旧Pod已退出、NPU已释放，再执行`deploy.py`。避免仅`kubectl rollout restart`或部分重建导致持续Pending。

## 特性配置

通用能力配置方式与[PD分离服务部署（基于vLLM推理引擎）](./pd_disaggregation_deployment.md#特性配置)相同，下面仅描述与SGLang相关的点。

### 虚拟推理健康探测

- NodeManager对业务口做`GET /health`就绪与心跳。
- SGLang使用原生生成式`/health`（闲时跑轻量生成、忙时可跳过），Motor不创建虚推Worker。
- 将enable_virtual_inference设置为false不会关闭SGLang生成式`/health`。
- 该特性详细文档请参考[虚拟推理健康探测](../../features/sim_inference.md)。

### ChunkPrefill

在P侧的engine_config配置模块中配置，示例例如：

```json
"chunked-prefill-size": 4096
```

该特性主要用于控制超长Prefill切块，减轻长短请求互相拖死。其取值需结合模型与显存验证，详见请参见[SGLang](https://github.com/sgl-project/sglang)。

### 不对等TP/PCP/MTP

不对等TP/PCP/MTP均属引擎原生能力，通过engine_config配置模块透传即可，例如：

- 不对等TP：P/D的tp-size可不同，非MLA模型不保证相关性能，详见请参见[SGLang](https://github.com/sgl-project/sglang)。
- PCP：Prefill侧配置，如enable-prefill-cp、cp-strategy、attn-cp-size，详见请参见[SGLang](https://github.com/sgl-project/sglang)。
- MTP：Decode侧配置，如speculative-algorithm及相关draft参数，详见请参见[SGLang](https://github.com/sgl-project/sglang)。

### 故障重调度

该特性默认关闭。开启后，completions流式/非流式与chat非流式场景已经支持。流式路径依赖引擎回传token ids做续流。SGLang当前对chat流式不支持return_token_ids与stream同时开启，导致chat流式重调度不可用。该特性详细文档请参考[请求重调度](../../features/fault_tolerance/rescheduler.md)。配置样例如下所示：

```json
"motor_coordinator_config": {
  "exception_config": {
    "reschedule_config": { "enable": true },
    "max_retry": 5,
    "transport_max_retry": 3,
    "retry_delay": 0.2
  }
}
```

### KV Cache亲和调度（可选）

开启KV Cache亲和调度，需检查是否同时配置以下参数：

- motor_coordinator_config.scheduler_config配置模块下需配置：scheduler_type: "kv_cache_affinity"；
- motor_engine_prefill_config.engine_config.kv-events-config配置模块下需配置："publisher": "zmq"，且不要关闭SGLang Radix（勿设disable-radix-cache）。
- kv_conductor_config配置模块需配置http_server_port。

```json
"motor_coordinator_config": {
  "scheduler_config": {
    "scheduler_type": "kv_cache_affinity",
    "kv_affinity": { "mode": "unified" }
  }
},
"motor_engine_prefill_config": {
  "engine_config": {
    "kv-events-config": {
      "publisher": "zmq",
      "enable_kv_cache_events": true,
      "endpoint": "tcp://*:5557",
      "replay_endpoint": "tcp://*:6667",
      "topic": "kv-events"
    }
  }
},
"kv_conductor_config": {
  "http_server_port": 13333,
  "block_size": 128,
  "engine_type": "sglang"
}
```

关闭该特性需要改回load_balance，并删除kv-events-config与kv_conductor_config配置模块。该特性详细文档请参考[KV Cache亲和性调度](../../features/kvcache_affinity.md)。
