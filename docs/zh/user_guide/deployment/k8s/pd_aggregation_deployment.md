# PD 混部服务部署

## 场景介绍

### PD 混部介绍

**PD 混部**将 Prefill 与 Decode 能力部署在同一类 Engine Server 实例中。部署时不再分别拉起 prefill、decode 两类角色，而是由 `union` 角色承载完整推理能力；Coordinator 以 `single_node` 调度模式将请求分发到可用的 union 实例。

与 [PD 分离部署](./pd_disaggregation_deployment.md) 相比，PD 混部减少了 P/D 角色拆分和 KV 跨角色传输配置，适用于快速验证、中小规模服务、资源规模较小或暂不需要独立规划 P/D 实例比例的场景。若业务需要针对 Prefill、Decode 两阶段分别规划资源、独立扩缩容或使用 PD 分离相关能力，建议使用 PD 分离部署。

### 部署入口与流程

部署流程围绕三个入口展开：

1. `user_config.json`：部署与业务的总配置，PD 混部重点配置 `hybrid_*` 字段、`motor_engine_union_config` 以及 Coordinator 的 `single_node` 调度模式。
2. `env.json`：各组件环境变量，PD 混部的 Engine Server 环境变量配置在 `motor_engine_union_env` 中。
3. 部署脚本 `deploy.py`：读取上述配置，生成 K8s YAML、更新启动脚本、创建 ConfigMap 并执行 `kubectl apply`。

**部署方式**：PD 混部默认使用 CRD 方式（`infer_service_set`），由 InferServiceSet 中的 `union` 角色拉起混部实例。若需沿用传统多 YAML Deployment，可在 `motor_deploy_config.deploy_mode` 中显式配置为 `multi_deployment`；但推荐优先使用默认 CRD 方式。

### 限制与约束

- Atlas 800I A2 推理服务器与 Atlas 800I A3 超节点服务器支持此特性。
- 模型支持范围同所选推理引擎（如 vLLM Ascend）。
- `hybrid_instances_num` 表示 union 实例数，扩缩容时仅允许修改该字段。
- `hybrid_pod_npu_num`、并行参数和模型路径需与实际硬件资源、模型权重路径保持一致。
- CRD 方式需集群预先安装 MindCluster InferServiceSet CRD 及对应 controller。

### 硬件环境

PD 混部部署支持的硬件环境如下所示。

**表 1**  PD 混部部署支持的硬件列表

| 类型 | 型号 | 内存 |
|------|------|------|
| 服务器 | Atlas 800I A2 推理服务器 | 32GB / 64GB |
| 服务器 | Atlas 800I A3 超节点服务器 | 64GB |

>[!NOTE]说明
>
>- 集群必须具备参数面互联：即服务器 NPU 卡对应的端口处在同一个 VLAN，可以通过 RoCE 互通。
>- 为保障业务稳定运行，用户应严格控制自建 Pod 的权限，避免高权限 Pod 修改 MindIE 内部参数而导致异常。

## 准备镜像

在部署 PD 混部服务前，需要在各计算节点上准备好可用的推理镜像。镜像要求与 PD 分离部署一致：需包含 MindIE Motor、所选推理引擎（如 vLLM）及其 Ascend 适配组件。镜像获取、离线导入和自定义构建方式请参考 [PD 分离服务部署](./pd_disaggregation_deployment.md) 中的“准备镜像”章节。

>[!NOTE]说明
>
>所有参与部署的 K8s 节点必须能够本地加载或拉取 `image_name` 指定的镜像，否则 Pod 可能因镜像不可用而处于 `ImagePullBackOff` 或 `ErrImagePull` 状态。

## 部署目录结构

请将本仓库中的 `examples` 目录上传至 K8s 集群的 master 节点。`examples` 可使用以下两种方式获取：

- **从本代码仓获取**：将仓库根目录下的 `examples` 目录上传至 master 服务器。

- **从容器镜像获取**：若无完整代码仓，但已拉取MindIE Motor推理镜像，可使用镜像内预置的示例目录，路径为 **`/tmp/motor/examples`**（目录结构与仓库中的 `examples` 一致）。在已拉取镜像的机器上执行（将 `IMAGE` 替换为实际镜像名或镜像 ID，可与 `user_config.json` 中 `motor_deploy_config.image_name` 保持一致）：

  ```bash
  IMAGE="<镜像名或镜像ID>"

  cid=$(docker create "$IMAGE")
  docker cp "$cid:/tmp/motor/examples" ./examples
  docker rm "$cid"
  ```

  将得到的 `examples` 目录上传至 master 服务器。

  若使用 Podman，将命令中的 `docker` 替换为 `podman` 即可。

**examples** 中与 PD 混部部署相关的主要目录结构如下：

```text
examples/
├── deployer/                  # 部署工具目录
│   ├── deploy.py              # 部署入口脚本
│   ├── delete.sh              # 卸载脚本
│   ├── show_log.sh            # 日志查看脚本
│   ├── yaml_template/         # K8s YAML 模板
│   ├── startup/               # 启动脚本
│   ├── log_collect/           # 日志采集
│   └── output_yamls/          # 生成的 YAML 输出目录
└── infer_engines/
    └── vllm/
        └── pd_hybrid/
            ├── user_config.json   # PD 混部用户配置示例
            ├── env.json           # PD 混部环境变量配置示例
            └── README.md          # 示例说明
```

- PD 混部示例配置位于 `examples/infer_engines/vllm/pd_hybrid/`。
- 部署工具使用方法详见 `examples/deployer/README.md`。

## 配置 `user_config.json`

PD 混部可直接参考 `examples/infer_engines/vllm/pd_hybrid/user_config.json`。该文件根节点包含 `version`、`motor_deploy_config`、`motor_controller_config`、`motor_coordinator_config` 和 `motor_engine_union_config`。

### motor_deploy_config（部署与资源）

`motor_deploy_config` 为部署与资源相关配置。

**配置示例**：

```json
"motor_deploy_config": {
  "deploy_mode": "infer_service_set",
  "hybrid_instances_num": 1,
  "single_hybrid_instance_pod_num": 1,
  "hybrid_pod_npu_num": 2,
  "image_name": "",
  "job_id": "mindie-motor",
  "hardware_type": "800I_A3",
  "weight_mount_path": "/mnt/weight/"
}
```

**配置项说明**：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| deploy_mode | string | 部署方式。PD 混部推荐使用 `infer_service_set`，不配置时默认也是该方式 |
| hybrid_instances_num | int | union 实例个数，≥1 且 ≤16 |
| single_hybrid_instance_pod_num | int | 单个 union 实例对应的 Pod 数，≥1 |
| hybrid_pod_npu_num | int | 单个 union Pod 占用的 NPU 卡数 |
| image_name | string | 推理镜像名，需包含 MindIE Motor 与推理引擎运行环境 |
| job_id | string | 部署任务名，同时作为 K8s 命名空间使用，如 `mindie-motor` |
| hardware_type | string | 硬件类型：`800I_A2` 或 `800I_A3` |
| weight_mount_path | string | 宿主机上模型权重挂载路径，容器内 `model` 需与此挂载路径一致 |

### motor_coordinator_config

PD 混部场景不再需要配置 Coordinator 调度模式。Coordinator 会根据运行中的 `union` 实例自动选择 `PDHybridRouter`。

**配置示例（默认负载均衡）**：

```json
"motor_coordinator_config": {}
```

**配置示例（KV Cache 亲和调度，可选）**：

```json
"motor_coordinator_config": {
  "scheduler_config": {
    "scheduler_type": "kv_cache_affinity",
    "kv_affinity_mode": "unified",
    "kv_affinity_load_weight": 1.0
  }
}
```

启用 KV Cache 亲和时，在 `examples/infer_engines/vllm/pd_hybrid/user_config.json` 中按上述示例修改 `motor_coordinator_config`、`motor_engine_union_config` 并增加 `kv_conductor_config` 即可，无需单独配置文件。KV Conductor 安装与部署说明见 [KV Cache 亲和部署](../../features/KV_cache_affinity.md)。

| 配置项 | 类型 | 说明 |
|--------|------|------|
| scheduler_config.scheduler_type | string | 调度类型。默认 `load_balance`；启用 KV 亲和时配置为 `kv_cache_affinity` |
| scheduler_config.kv_affinity_mode | string | KV 亲和子策略：`unified`（默认）或 `load_gated` |
| scheduler_config.kv_affinity_load_weight | float | unified 模式下负载权重，默认 `1.0` |
| scheduler_config.kv_affinity_overlap_credit | float | 缓存前缀对 prefill 成本的折扣系数，默认 `1.0` |
| scheduler_config.kv_affinity_prefill_load_scale | float | unified 模式下 prefill 成本权重，默认 `1.0` |
| scheduler_config.kv_affinity_load_gate_topn | int | load_gated 模式下保留的最小负载 endpoint 数量；`0` 时回退为 `2` |

### motor_engine_union_config（混部引擎）

`motor_engine_union_config` 用于配置混部 Engine Server。其结构与 PD 分离中的 `motor_engine_prefill_config` / `motor_engine_decode_config` 类似，但无需分别配置 P/D 两套引擎，也无需配置 `kv_transfer_config` 的 producer/consumer 角色。

**配置示例**：

```json
"motor_engine_union_config": {
  "engine_type": "vllm",
  "engine_config": {
    "served_model_name": "qwen3-8B",
    "model": "/mnt/weight/qwen3_8B",
    "gpu_memory_utilization": 0.9,
    "data_parallel_size": 1,
    "tensor_parallel_size": 1,
    "pipeline_parallel_size": 1,
    "enable_expert_parallel": false,
    "data_parallel_rpc_port": 9000,
    "enforce-eager": true,
    "max_model_len": 2048
  }
}
```

**配置项说明**：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| engine_type | string | 引擎类型，如 `vllm` |
| engine_config | object | 引擎相关配置，含模型信息、并行策略和引擎原生参数 |
| engine_config.served_model_name | string | 对外服务的模型名称 |
| engine_config.model | string | 容器内模型权重路径，需与 `weight_mount_path` 挂载后一致 |
| engine_config.gpu_memory_utilization | float | NPU 内存使用占比上限，0～1 |
| engine_config.data_parallel_size | int | 数据并行大小 |
| engine_config.tensor_parallel_size | int | 张量并行大小 |
| engine_config.pipeline_parallel_size | int | 流水并行大小 |
| engine_config.enable_expert_parallel | bool | 是否启用 EP |
| engine_config.data_parallel_rpc_port | int | DP 侧 RPC 端口 |
| engine_config.max_model_len | int | 最大模型上下文长度 |
| engine_config.kv-events-config | object | 启用 KV Cache 亲和时配置 KV 事件发布（见 [KV Cache 亲和部署](../../features/KV_cache_affinity.md)） |
| engine_config.enable-prefix-caching | bool | 启用 KV Cache 亲和时建议开启前缀缓存 |
| 其它键 | - | 引擎原生参数，按所选引擎文档直接填写 |

启用 KV Cache 亲和时，Coordinator 会从 `motor_engine_union_config.engine_config.kv-events-config` **自动合并** `prefill_kv_event_config`（无需再配置 `motor_engine_prefill_config`）。同时需在 `user_config.json` 根节点配置 `kv_conductor_config`（至少包含 `http_server_port`）。

## 配置 `env.json`

PD 混部可直接参考 `examples/infer_engines/vllm/pd_hybrid/env.json`。混部 Engine Server 的环境变量配置在 `motor_engine_union_env` 中。

**配置示例**：

```json
{
  "version": "2.0.0",
  "motor_common_env": {
    "CANN_INSTALL_PATH": "/usr/local/Ascend",
    "MOTOR_LOG_ROOT_PATH": "/root/ascend/log"
  },
  "motor_engine_union_env": {
    "HCCL_BUFFSIZE": 200,
    "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
    "HCCL_OP_EXPANSION_MODE": "AIV",
    "OMP_PROC_BIND": "false",
    "OMP_NUM_THREADS": 100,
    "ASCEND_BUFFER_POOL": "0:0"
  }
}
```

| 配置项 | 说明 |
|--------|------|
| motor_common_env | 所有组件共用环境变量，如 CANN 安装路径、日志根目录 |
| motor_engine_union_env | union 实例的 NPU、HCCL、OMP 等环境变量，可按机型与模型进行调优 |

修改后保存即可，无需手动修改启动脚本；下次执行 `deploy.py` 时会重新生成并注入上述环境变量。

## 执行部署（`deploy.py`）

### 安全与权限说明

- 部署脚本建议由 K8s 集群管理员执行，以避免脚本或配置被篡改引发任意命令执行或容器逃逸风险。
- 须严格管控 MindIE 相关 ConfigMap（如 `motor-config`）的写、更新与删除权限。
- 修改 YAML 模板时，请使用安全镜像和安全挂载路径，避免软链接、系统危险路径及业务敏感路径。

### 前置条件

- 已安装 Kubernetes、MindCluster、NPU 驱动和固件。
- 已创建与 `job_id` 同名的命名空间，例如：

  ```bash
  kubectl create namespace mindie-motor
  ```

- 宿主机上模型权重已放在 `weight_mount_path` 指定路径（如 `/mnt/weight/`）。
- 各计算节点已准备好 `image_name` 指定的推理镜像。

### 部署命令

在 `examples/deployer` 目录下执行，支持两种指定配置的方式。

**方式一：指定配置目录（推荐）**：

```bash
cd examples/deployer
python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid
```

**方式二：单独指定配置文件路径**：

```bash
cd examples/deployer
python3 deploy.py \
  --user_config_path ../infer_engines/vllm/pd_hybrid/user_config.json \
  --env_config_path ../infer_engines/vllm/pd_hybrid/env.json
```

如需仅检查 YAML 生成，可增加 `--dry-run`：

```bash
python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid --dry-run
```

`deploy.py` 会依次执行以下步骤：

1. 读取 `user_config.json` 和 `env.json`。
2. 根据 `motor_deploy_config` 生成 Controller、Coordinator 和 union 实例的 K8s 资源。
3. 将 `motor_engine_union_env` 等环境变量写入启动脚本。
4. 创建或更新 `motor-config` ConfigMap。
5. 执行 `kubectl apply` 拉起服务。

### 查看集群状态与日志

- 查看 Pod 列表：

  ```bash
  kubectl get pods -n <job_id>
  ```

  在 CRD 默认方式下，InferServiceSet 会拉起 controller、coordinator 和 union 角色对应的 Pod。Pod 状态为 Running 仅表示已成功调度并启动，是否业务就绪仍需结合日志进一步确认。

- 查看日志可使用 `show_log.sh`：

  1. 配置 `log_collect/log_config.ini`，设置 `name_sapce` 属性为实际的命名空间，此处为 `mindie-motor`。

  2. 在 `examples/deployer` 目录下执行 `show_log.sh` 获取/查看日志。

      ```bash
      cd examples/deployer
      bash show_log.sh
      ```

      日志将生成在 `examples/deployer/log_collect/log/<YYYYMMDD_hhmmss>` 目录下。

      单个 Pod 的日志文件命名格式为 `<pod_name>_<node_name>_<retry_count>.log`，例如 `vllm-0-controller-0-xxxx_node01_0.log`。
  3. 可以使用 `tail` 命令查看日志。

      ```bash
      cd examples/deployer
      tail -f log_collect/log/<YYYYMMDD_hhmmss>/<pod_name>_<node_name>_<retry_count>.log
      ```

- 直接查看单个 Pod 日志：

  ```bash
  kubectl logs <pod_name> -n <job_id>
  ```

- 进入容器内部排查，可执行：

  ```bash
  kubectl exec -it <pod_name> -n <job_id> -- bash
  ```

## 发送推理请求

服务就绪后，可通过 `/v1/chat/completions` 接口测试服务是否拉起正常。推理入口为 Coordinator 对外暴露的端口（默认 31015）。请将 `<IP>` 替换为实际访问地址，将 `model` 替换为 `served_model_name` 中配置的模型名称。

```bash
curl -X POST http://<IP>:31015/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-8B",
    "messages": [
      {
        "role": "user",
        "content": "who are you?"
      }
    ],
    "max_tokens": 36,
    "stream": true
  }'
```

若返回 `{"detail":"Service is not available"}`，表示服务尚未就绪，可稍后重试并查看 Pod 日志。若返回流式 JSON，则说明推理正常。

>[!NOTE]说明
>
>HTTP 协议存在安全风险，生产环境建议开启 HTTPS。TLS 配置可参考 [PD 分离服务部署](./pd_disaggregation_deployment.md) 中的 `tls_config` 章节。

## 手动扩缩容

PD 混部扩缩容时仅修改 `motor_deploy_config.hybrid_instances_num`，然后执行：

```bash
cd examples/deployer
python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid --update_instance_num
```

说明：

- `hybrid_instances_num` 须大于 0 且不超过 16。
- 扩缩容基线来自集群 ConfigMap `motor-config`。
- 除 `hybrid_instances_num` 外，不允许同时修改其他配置项。
- CRD 默认方式下，脚本更新 `infer_service.yaml` 中 union 角色的 replicas 后执行 apply，由 CRD controller 完成扩缩容。

更多说明请参考 [手动扩缩容用户手册](../../features/manual_instance_scaling.md)。

## 卸载

在 `examples/deployer` 目录下执行 `delete.sh`，删除当前 `job_id` 对应命名空间下的 K8s ConfigMap 以及已 apply 的 YAML，并清理启动脚本中由 `deploy.py` 注入的环境变量函数。

```bash
cd examples/deployer
bash delete.sh <命名空间>
```

例如：

```bash
bash delete.sh mindie-motor
```

>[!NOTE]说明
>
>命名空间请根据实际创建的名称替换。卸载脚本必须在 `examples/deployer` 目录下执行，否则无法正确找到 `output_yamls` 路径而报错。

## 故障排查与注意事项

- **服务未就绪**：若推理接口返回 `{"detail":"Service is not available"}`，多为 union 实例或 Coordinator 尚未完全就绪，可等待一段时间后重试，并查看 Pod 日志确认无启动错误。
- **镜像与权重**：确保 `image_name` 在集群内可正常拉取；`weight_mount_path` 在宿主机上存在，且 `engine_config.model` 指向容器内正确路径。
- **实例数配置错误**：`hybrid_instances_num` 必须大于 0 且不超过 16；扩缩容时仅允许修改该字段。
- **实例角色错误**：PD 混部需确保部署产生 `union` 角色实例；Coordinator 会自动识别，无需额外调度模式配置。
- **部署失败**：若部署失败，可先卸载集群，排查并修改配置后重新部署。
- **Prefix Cache 特性对性能测试的影响**：Prefix Cache 默认开启，若期望获取推理服务的基线性能数据，可在 vLLM 的 `engine_config` 中增加 `"no-enable-prefix-caching": true`。
