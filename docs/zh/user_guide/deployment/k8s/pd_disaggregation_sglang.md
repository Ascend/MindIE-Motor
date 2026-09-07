# SGLang PD 分离服务部署指导

本文档指导基于 MindIE Motor 的 **SGLang** 引擎完成 K8s **PD 分离**部署，并说明与 vLLM 的关键差异。

通用 PD 分离流程与 [PD 分离服务部署指导](./pd_disaggregation_deployment.md) 一致，本文只写 **SGLang 必改项与已验证典配用法**。

> [!NOTE]说明
>
> - 官方文档将 SGLang 标为 **已支持**：可通过 `engine_type: sglang` 部署；
> - 基于推理引擎的部分高级能力与 vLLM 覆盖范围可能不同；
> - 客户端只访问 **Coordinator** 北向接口，切换引擎无需改调用方式。

## 与 vLLM PD 分离的差异

| 项 | vLLM | SGLang |
| --- | --- | --- |
| 配置字段 | `engine_type: vllm` | `engine_type: sglang` |
| 引擎拉起 | `vllm serve` | `python3 -m sglang.launch_server` |
| PD 协调 | handoff / trigger（`kv_transfer_params` 等） | bootstrap（`bootstrap_host` / `bootstrap_port` / `bootstrap_room`） |
| 必配端口 | 业务口为主 | 业务口 + `disaggregation-bootstrap-port` |
| 传输 / 存储 | 按 Connector 配置 | 需配置 `disaggregation-transfer-backend: ascend`，Deployer 会额外拉起 **MF Store** |
| 健康探测 | 可选 Motor 虚推（`enable_virtual_inference`） | **原生生成式** `GET /health`，不创建 Motor 虚推 monitor |
| 配置生成 | 可用 vLLM→Motor 转换工具 | **暂无对等转换工具**；请基于 `examples/infer_engines/sglang/` 典配修改 |

## 950 系列服务器预检查

仅在使用 950 系列服务器时：请在每台服务器检查 `/lib/route.conf`、`/etc/hccl_rootinfo.json` 以及 `/etc/hixlep` 是否存在且正确。若不存在或不正确，请参考 [hixlep 配置文件生成文档](https://gitcode.com/cann/hixl/wiki/A5%20LocalCommRes%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md)，在生成 `/etc/hixlep` 时使用「D2D 场景」。

## 获取启动脚本与镜像

### 环境要求

- 推理节点已完成 [环境准备](../../environment_preparation.md)。
- 模型权重已放到集群可访问路径，并与 `weight_mount_path` / `model-path` 一致。
- P/D 各至少具备典配所需的 NPU 数量（示例见下文「推荐典配」）。

### 准备镜像

将镜像加载到 K8s 集群**所有相关节点**：

- **方式一**：使用已安装 MindIE Motor 的 SGLang 镜像，保证镜像内可执行 `python3 -m motor` 与 `python3 -m sglang.launch_server`。（计划支持）
- **方式二**：基于 Ascend SGLang 基座镜像手动安装 MindIE Motor，参考 [基于 vllm-ascend/sglang 镜像安装 MindIE Motor](../../maintenance/build_motor_image_from_vllm_ascend.md#基于vllm-ascendsglang镜像安装mindie-motor)。

### 准备 examples

将 `examples` 放到 K8s master 节点：

- **方式一**：从代码仓拷贝仓库内目录 `examples/`；
- **方式二**：从镜像导出（路径一般为 `/tmp/motor/examples`）：（计划支持）

```bash
IMAGE="<镜像名或镜像ID>"
cid=$(docker create "$IMAGE")
docker cp "$cid:/tmp/motor/examples" ./examples
docker rm "$cid"
```

## 准备配置文件

SGLang 目前 **没有** vLLM 侧的脚本自动转换工具，请直接选用仓库典配，并按个人需求修改后部署。

### 推荐典配

| 典型场景 | 目录 | 说明 |
| --- | --- | --- |
| GLM 5.1 / A3 1P1D | `examples/infer_engines/sglang/models/glm5.1/A3/` | GLM 5.1 PD 分离参考；更多典配见 [SGLang GLM 5.1 Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/glm_5_1) |
| GLM 5.2 / A3 1P1D | `examples/infer_engines/sglang/models/glm5.2/A3/` | GLM 5.2 PD 分离参考；更多典配见 [SGLang GLM 5.2 Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/glm_5_2) |
| Qwen3-8B / A2 | `examples/infer_engines/sglang/models/qwen_8b/A2/` | 资源占用较小，适合打通流程；更多典配见 [SGLang Qwen3-8B Best Practice](https://docs.sglang.io/docs/hardware-platforms/ascend-npus/model-deployment/best-practices/qwen3_8b) |
| 更多模型 | `examples/infer_engines/sglang/` | 根目录及子目录下的 `user_config.json` / `env.json` |

每个典配目录需包含：

- `user_config.json`：部署拓扑、Motor 组件配置、引擎参数
- `env.json`：各角色环境变量

### 部署前必改项

打开典配中的 `user_config.json`，至少确认：

| 字段 | 位置 | 说明 |
| --- | --- | --- |
| `image_name` | `motor_deploy_config` | 与集群内可用镜像一致 |
| `job_id` | `motor_deploy_config` | 即 K8s namespace 名 |
| `hardware_type` | `motor_deploy_config` | 硬件类型，如 `800I_A2` / `800I_A3` |
| `weight_mount_path` | `motor_deploy_config` | 宿主机权重挂载根路径 |
| `p_*` / `d_*` 实例与卡数 | `motor_deploy_config` | 与节点 NPU、并行度匹配 |
| `*_node_selector` | `motor_deploy_config` | 非必须，按集群 hostname 绑定 Controller / Coordinator / P / D / MF Store |
| `coordinator_infer_node_port` 等 | `motor_deploy_config` | 非必须，避免与现网 NodePort 冲突 |
| `engine_type` | P/D 引擎段 | 引擎类型，必须为 `sglang` |
| `model-path` / `served-model-name` | `engine_config` | 权重路径与对外模型名 |
| `disaggregation-bootstrap-port` | **Prefill** `engine_config` | SGLang PD bootstrap 端口；未配置则不生成 bootstrap 元数据 |

### 最小引擎配置示例

以下仅为 `Qwen3-8B` 示例，更多详细配置见 `examples/infer_engines/sglang/models/{model_name}/{hardware_type}/`。

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

说明：

- `engine_config` 由 NodeManager 组件**透传**为 `sglang.launch_server` 参数，字段名以所用 SGLang 版本 CLI 为准（支持 kebab-case，部分也兼容 underscore）。
- Prefill 侧 `disaggregation-bootstrap-port`（或 `disaggregation_bootstrap_port`）写入实例元数据的 `bootstrap_port`，供 Coordinator 的 SGLang Adapter 做 P/D 对接，**不要**与业务口 `endpoint_config.service_ports` 混用。
- 首次打通建议 `scheduler_type: load_balance`，`reschedule_config.enable: false`，高级特性见下文。

全量字段说明见 [user_config 配置参考](../../configuration/config_reference.md)。

## 服务部署与验证

以下操作在 K8s master 节点执行。以 GLM 5.1 A3 典配为例。

### 拉起服务

```bash
# <namespace> 须与 user_config.json 中 job_id 一致
kubectl create namespace <namespace>

cd examples/deployer
python3 deploy.py --config_dir ../infer_engines/sglang/models/glm5.1/A3
```

大模型权重加载可能需要数分钟，请等待 P/D Ready 后再测试。

### 查看状态

```bash
kubectl get pods -n <namespace> -o wide
```

| 运行时类型（示例） | 说明 |
| --- | --- |
| `mindie-motor-controller-*` | 服务管理 |
| `mindie-motor-coordinator-*` | 请求调度 / 业务入口 |
| `mindie-motor-mf-store-*` 或 `mf-store` | 服务 KV Cache 传输 |
| `sglang-p*` | Prefill 实例 |
| `sglang-d*` | Decode 实例 |
| `kv-conductor-*`（可选） | 服务 KV 亲和特性 |

Pod `Running` 不等于业务完全就绪，实际拉起状态需结合日志与 `/v1/models` 确认。

### 查看日志

```bash
cd examples/deployer
# 编辑 log_collect/log_config.ini：name_space 改为 job_id
vim log_collect/log_config.ini
bash show_log.sh
```

### 验证服务

```bash
# <node_ip>：暴露 Coordinator Infer NodePort 的节点 IP
# <infer_port>：user_config 中 coordinator_infer_node_port
# <model>：engine_config 中 served-model-name
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

- 若返回 `{"detail":"Service is not available"}`：尚未就绪，稍后重试并查 P/D / MF Store 日志。
- 若返回流式 JSON：推理正常。更多接口见 [业务接口](../../api/service_interfaces.md)。

### 终止服务

```bash
cd examples/deployer
bash delete.sh <namespace>
```

重部署前请确认旧 Pod 已退出、NPU 已释放，再执行 `deploy.py`。避免仅 `kubectl rollout restart` 或部分重建导致持续 Pending。

## 特性配置指导

通用能力配置方式与 [PD 分离服务部署指导 · 特性配置](./pd_disaggregation_deployment.md#特性配置指导) 相同，下面仅列与 SGLang 相关的要点。

### 健康探测

- NodeManager 对业务口做 `GET /health` 就绪与心跳。
- SGLang 使用**原生生成式** `/health`（闲时跑轻量生成、忙时可跳过），Motor **不**创建虚推 Worker。
- `enable_virtual_inference` 仅影响 vLLM，设为 `false` **不会**关闭 SGLang 生成式 `/health`。
- 详见 [虚推健康探测](../../features/sim_inference.md)。

### ChunkPrefill

在 P 侧的 `engine_config` 中配置，例如：

```json
"chunked-prefill-size": 4096
```

- 用于控制超长 Prefill 切块，减轻长短请求互相拖死。
- 取值需结合模型与显存验证，详见 SGLang 官网。

### 不对等 TP / PCP / MTP

均属引擎原生能力，经 `engine_config` 透传即可，例如：

- 不对等 TP：P/D 的 `tp-size` 可不同，非 MLA 模型不保证相关性能，详见 SGLang 官网。
- PCP：Prefill 侧配置如 `enable-prefill-cp`、`cp-strategy`、`attn-cp-size`，详见 SGLang 官网。
- MTP：Decode 侧如 `speculative-algorithm` 及相关 draft 参数，详见 SGLang 官网。

### 故障重调度

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

- 默认关闭。开启后，`completions` 流式/非流式与 `chat` 非流式场景已经支持。
- **已知限制**：流式路径依赖引擎回传 token ids 做续流。SGLang 当前对 chat 流式不支持 `return_token_ids` 与 `stream` 同时开启，导致 chat 流式重调度不可用。
- 详见 [故障场景重调度](../../features/fault_tolerance/rescheduler.md)。

### KV Cache 亲和调度（可选）

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

若典配已包含或需开启亲和调度，检查是否同时具备：

- `motor_coordinator_config`：`scheduler_type: kv_cache_affinity`
- `motor_engine_prefill_config`：`kv-events-config`（ZMQ 发布），且**不要**关闭 SGLang Radix（勿设 `disable-radix-cache`）
- `kv_conductor_config`：配置 `http_server_port`

关闭时改回 `load_balance`，并去掉 `kv-events-config` 与 `kv_conductor_config`。详见 [KV Cache 亲和性调度](../../features/kvcache_affinity.md)。

### KV 池化（Mooncake / Memcache）

- SGLang 路径上 KV 池化能力覆盖与验收进度弱于 vLLM，正在积极适配中。
- 更多池化专项请参考 [KV 池化部署指南](../../features/kv_cache_store/README.md)，并以实际验证结论为准。

## 附录

### 运维技巧

- **Service is not available**：先等 P/D / MF Store Ready。检查 Prefill 是否因 MF Store 未就绪导致 SMEM / 传输初始化失败。
- **镜像与权重**：确认各节点可拉取 `image_name`，`model-path` 在容器内可读。
- **NPU Pending**：部署前检查目标节点 Ascend 资源是否被其他任务占满，Motor 与纯 SGLang 等同机路径勿并行抢卡。
- **重部署**：先完整卸载并等待资源释放，再 `deploy.py`，勿依赖 `rollout restart` 做配置切换。
- **PP 场景**：多 PP 时需保证 bootstrap 和集合通信相关环境与典配一致，保证 NodeManager 可注入 PP bootstrap 相关环境变量。

### 已知限制

| 项 | 说明 |
| --- | --- |
| Anthropic 接口 `/v1/messages` | 依赖上游 SGLang。仅 `/v1/messages/count_tokens` 可用 |
| chat 流式 + 重调度 | 受引擎 `return_token_ids` 限制，当前不可用 |
| 池化能力 | 以特性文档与实测为准，尚未与 vLLM 对齐 |
| 容器快照 | 以特性文档与实测为准，尚未与 vLLM 对齐 |

### examples 目录结构（SGLang）

```text
examples/
├── deployer/                         # deploy.py / delete.sh / show_log.sh
└── infer_engines/
    └── sglang/
        ├── user_config.json          # 通用模板
        ├── env.json
        ├── user_config_pd_hetero.json
        ├── models/
        │   ├── glm5.1/A3/            # GLM 5.1 A3 参考典配
        │   ├── glm5.2/A3/            # GLM 5.2 A3 参考典配
        │   └── qwen_8b/A3/           # Qwen3 8b A3 参考典配
        └── pd_hybrid/                # 混部相关示例
```

- 部署工具说明见 `examples/deployer/README.md`。
- 业务拓扑与 `deploy_mode` 说明见 [部署方式配置说明](./README.md)。
