# PD混部服务部署

## 场景介绍

### PD混部介绍

PD混部将Prefill与Decode实例部署在同一类原生引擎实例中。部署时不再分别拉起prefill、decode两类角色，而是由union角色承载完整推理能力；Coordinator以single_node调度模式将请求分发到可用的union实例。

与[PD服务分离部署](./pd_disaggregation_deployment.md)相比，PD混部减少了P/D角色拆分和KV跨角色传输配置，适用于快速验证、中小规模服务、资源规模较小或暂不需要独立规划P/D实例比例的场景。若业务需要针对Prefill、Decode两阶段分别规划资源、独立扩缩容或使用PD分离相关能力，建议使用PD分离服务部署。

### 部署入口与流程

部署流程围绕三个入口展开：

- user_config.json配置文件：部署与业务的总配置，PD混部重点配置hybrid_*、motor_engine_union_config字段以及Coordinator的single_node调度模式。
- env.json配置文件：各组件环境变量，PD混部的原生引擎环境变量配置在motor_engine_union_env字段中。
- deploy.py部署脚本：读取user_config.json和env.json配置文件，生成K8s YAML文件、更新启动脚本、创建ConfigMap并执行"kubectl apply"。

**部署方式**：

- （推荐）CRD方式（infer_service_set）：PD混部默认使用CRD方式（infer_service_set），由InferServiceSet中的union角色拉起混部实例。
- 多YAML Deployment方式：在motor_deploy_config.deploy_mode字段中显式配置为"multi_deployment"。

### 限制与约束

- Atlas 800I A2推理服务器与Atlas 800I A3超节点服务器支持此特性。
- 模型支持范围同所选推理引擎，详情请参考[模型支持列表](https://www.hiascend.com/developer/models)。
- hybrid_instances_num表示union实例数，扩缩容时仅允许修改该字段。
- hybrid_pod_npu_num、并行参数和模型路径需与实际硬件资源、模型权重路径保持一致。
- CRD方式需集群预先安装MindCluster InferServiceSet CRD及对应controller。

### 硬件环境

PD混部部署支持的硬件环境如下所示。

**表 1** PD混部部署支持的硬件列表

| 类型 | 型号 | 内存 |
|------|------|------|
| 服务器 | Atlas 800I A2推理服务器 | 32GB/64GB |
| 服务器 | Atlas 800I A3超节点服务器 | 64GB |

>[!NOTE] 说明
>
>- 集群必须具备参数面互联，即服务器NPU卡对应的端口处在同一个VLAN，可以通过RoCE互通。
>- 为保障业务稳定运行，用户应严格控制自建Pod的权限，避免高权限Pod修改MindIE内部参数而导致异常。

## 准备镜像

部署PD混部服务前，需要在各计算节点上准备好可用的推理镜像。

- 方式一：下载官方完整的MindIE Motor镜像

  进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，在搜索框输入`motor`，单击“mindie-motor”卡片，在“镜像下载”页签根据设备型号下载对应的MindIE Motor镜像。
- 方式二：在已有镜像中安装MindIE Motor

  基础镜像已安装CANN、vLLM、vLLM Ascend等组件，请参考[基于vllm-ascend/sglang镜像安装MindIE Motor](../../maintenance/build_motor_image_from_vllm_ascend.md#基于vllm-ascendsglang镜像安装mindie-motor) 额外安装MindIE Motor。

获取镜像后，请使用以下命令将镜像加载至服务器：

   ```bash
   docker load -i xxxx.tar
   ```

待镜像导入后，请使用以下命令查看Docker镜像是否存在：

   ```bash
   docker images
   ```

>[!NOTE] 说明
>
>所有参与部署的K8s节点必须能够本地加载或拉取`image_name`指定的镜像，否则Pod可能因镜像不可用而处于ImagePullBackOff或ErrImagePull状态。

## 部署目录结构

请将本仓库中的examples目录上传至K8s集群的master节点。examples可使用以下两种方式获取：

- 从本代码仓获取：将仓库根目录下的examples目录上传至master服务器。

- 从容器镜像获取：若无完整代码仓，但已拉取MindIE Motor推理镜像，可使用镜像内预置的示例目录，路径为：/tmp/motor/examples（目录结构与仓库中的examples一致）。

  在已拉取镜像的机器上执行以下命令（将`IMAGE`替换为实际镜像名或镜像ID，可与user_config.json配置文件中motor_deploy_config.image_name字段值保持一致。）：

  ```bash
  # 自定义一个存镜像名或镜像ID的变量IMAGE
  IMAGE="<镜像名或镜像ID>"
  # 创建一个未启动的临时容器，并记录容器ID
  cid=$(docker create "$IMAGE")
  # 把容器中/tmp/motor/examples目录复制到宿主机的当前目录
  docker cp "$cid:/tmp/motor/examples" ./examples
  # 删除以上创建的临时容器$cid，清理资源，避免残留无用容器
  docker rm "$cid"
  ```

  >[!NOTE] 说明
  >若使用Podman命令，将以上命令中的`docker`替换为`podman`即可。

examples目录中与PD混部部署相关的主要文件目录结构如下：

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
└── model_configs/
    └── vllm/
        └── pd_hybrid/
            ├── user_config.json   # PD 混部用户配置示例
            ├── env.json           # PD 混部环境变量配置示例
            └── README.md          # 示例说明
```

- PD混部示例配置请参考[PD混部部署示例](../../../../../model_configs/vllm/pd_hybrid/README.md)。
- 部署工具使用方法详请参考[Deployer部署工具](../../../../../examples/deployer/README.md)。

## 配置文件

### 配置user_config.json文件

PD混部可直接参考代码仓model_configs/vllm/pd_hybrid/user_config.json文件进行配置。

**motor_deploy_config**

motor_deploy_config字段下的参数主要用于部署与资源相关配置，配置示例如下所示：

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

**表 2** motor_deploy_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------|
| deploy_mode | string | 部署方式，未配置是默认为：infer_service_set。<br>PD混部推荐使用infer_service_set方式。 |
| hybrid_instances_num | int | union实例个数，取值范围：[1,16]。 |
| single_hybrid_instance_pod_num | int | 单个union实例对应的Pod数，取值范围：大于等于1。 |
| hybrid_pod_npu_num | int | 单个union Pod占用的NPU卡数。 |
| image_name | string | 推理镜像名，需包含MindIE Motor与推理引擎运行环境。 |
| job_id | string | 部署任务名，同时作为K8s命名空间使用，如：mindie-motor。|
| hardware_type | string | 硬件类型，取值为800I_A2或800I_A3。 |
| weight_mount_path | string | 宿主机上模型权重挂载路径，容器内model需与此挂载路径一致。 |

**motor_coordinator_config**

PD混部场景不需要配置Coordinator调度模式。Coordinator会根据运行中的union实例自动选择PDHybridRouter。

**配置示例（默认负载均衡）**：

```json
"motor_coordinator_config": {}
```

**配置示例（KV Cache亲和调度，可选）**：

启用KV Cache亲和调度时，在 model_configs/vllm/pd_hybrid/user_config.json配置文件中按以下示例修改motor_coordinator_config、motor_engine_union_config字段，并增加 kv_conductor_config字段即可。详情请参考[KV Cache 亲和性调度](../../features/kvcache_affinity.md)。

```json
"motor_coordinator_config": {
  "scheduler_config": {
    "scheduler_type": "kv_cache_affinity",
    "kv_affinity": {
      "mode": "unified",
      "load_weight": 1.0
    }
  }
}
```

**表 3** KV Cache亲和调度参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------|
| scheduler_config.scheduler_type | string | 调度类型，默认值为load_balance。<br>启用 KV Cache亲和调度时配置为kv_cache_affinity。|
| scheduler_config.kv_affinity.mode | string | KV亲和子策略。<ul><li>unified（默认值）</li><li>load_gated</li></ul>|
| scheduler_config.kv_affinity.load_weight | float | unified模式下负载权重，默认值为：1.0。 |
| scheduler_config.kv_affinity.overlap_credit | float | 缓存前缀对prefill成本的折扣系数，默认值为：1.0。 |
| scheduler_config.kv_affinity.prefill_load_scale | float | unified模式下prefill成本权重，默认值为：1.0。 |
| scheduler_config.kv_affinity.load_gate_topn | int | load_gated模式下保留的最小负载endpoint数量；0时回退为2。 |
| scheduler_config.kv_affinity.w_npu / w_cpu / w_disk | float | 互斥介质命中块权重，默认值为：1.0/1.0/0.0。 |
| scheduler_config.kv_affinity.hit_rate_threshold | float | 亲和性命中率门槛，取值范围[0, 1]，默认值为：0，表示关闭。<br>大于0时最大前缀命中率须高于该阈值才走亲和，否则回退负载均衡。 |

**motor_engine_union_config**

motor_engine_union_config字段用于配置PD混部原生引擎。其结构与PD分离中的 motor_engine_prefill_config/motor_engine_decode_config字段类似，但无需分别配置P/D两套引擎，也无需配置kv_transfer_config字段的producer/consumer角色，配置示例如下所示。

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

**表 4** motor_engine_union_config字段参数说明

| 配置项 | 类型 | 说明 |
|--------|------|------|
| engine_type | string | 引擎类型，如：vllm。 |
| engine_config | object | 引擎相关配置，含模型信息、并行策略和引擎原生参数。 |
| engine_config.served_model_name | string | 对外服务的模型名称。 |
| engine_config.model | string | 容器内模型权重路径，需与`weight_mount_path`挂载后一致。 |
| engine_config.gpu_memory_utilization | float | NPU内存使用占比上限，取值范围：[0,1]。 |
| engine_config.data_parallel_size | int | 数据并行大小。 |
| engine_config.tensor_parallel_size | int | 张量并行大小。 |
| engine_config.pipeline_parallel_size | int | 流水并行大小。 |
| engine_config.enable_expert_parallel | bool | 是否启用EP。 |
| engine_config.data_parallel_rpc_port | int | DP侧RPC端口。 |
| engine_config.max_model_len | int | 最大模型上下文长度。 |
| engine_config.kv-events-config | object | 启用KV Cache亲和时配置KV事件发布（请参考[KV Cache 亲和部署](../../features/kvcache_affinity.md)）。 |
| engine_config.enable-prefix-caching | bool | 启用KV Cache亲和时建议开启前缀缓存。 |
| 其它键 | - | 引擎原生参数，按所选引擎文档直接填写。 |

>[!NOTE] 说明
>启用KV Cache亲和调度时，Coordinator会从motor_engine_union_config.engine_config.kv-events-config字段中自动合并prefill_kv_event_config，无需再配置 motor_engine_prefill_config字段。同时需在user_config.json配置文件根节点中配置 kv_conductor_config字段（其中至少包含http_server_port参数）。

### 配置env.json文件

PD混部可直接参考代码仓model_configs/vllm/pd_hybrid/env.json文件进行配置，PD混部原生引擎的环境变量配置在motor_engine_union_env字段中，配置示例如下所示。

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

**表 5** env.json文件关键参数说明

| 配置项 | 说明 |
|--------|------|
| motor_common_env | 所有组件共用环境变量，如CANN安装路径、日志根目录。 |
| motor_engine_union_env | union实例的NPU、HCCL、OMP等环境变量，可按机型与模型进行调优。 |

修改后保存即可，无需手动修改启动脚本；下次执行deploy.py时会重新生成并注入上述环境变量。

## 部署服务

### 安全与权限说明

- 部署脚本建议由K8s集群管理员执行，以避免脚本或配置被篡改引发任意命令执行或容器逃逸风险。
- 须严格管控MindIE相关ConfigMap（如：motor-config）的写、更新与删除权限。
- 修改YAML模板时，请使用安全镜像和安全挂载路径，避免软链接、系统危险路径及业务敏感路径。

### 前置条件

- 已按照[环境准备](../../environment_preparation.md)章节完成Kubernetes、MindCluster、NPU驱动和固件。
- 已创建与`job_id`同名的命名空间，示例如下：

  ```bash
  kubectl create namespace mindie-motor
  ```

- 宿主机上模型权重已放在weight_mount_path参数指定路径（如：/mnt/weight/）。
- 各计算节点已准备好image_name指定的推理镜像。

### 部署命令

在/examples/deployer目录下执行以下命令，支持两种指定配置的方式。

- 方式一：指定配置目录（推荐）

  ```bash
  cd examples/deployer
  python3 deploy.py --config_dir ../../model_configs/vllm/pd_hybrid
  ```

  >[!NOTE] 说明
  >如需仅检查YAML生成，可在命令中增加`--dry-run`参数，示例如下：
  >
  >```bash
  > python3 deploy.py --config_dir ../../model_configs/vllm/pd_hybrid --dry-run
  >```
  >
  >`deploy.py`部署脚本会依次执行以下步骤：
  >
  >1. 读取user_config.json和env.json配置文件。
  >2. 根据motor_deploy_config生成Controller、Coordinator和union实例的K8s资源。
  >3. 将motor_engine_union_env等环境变量写入启动脚本。
  >4. 创建或更新motor-config ConfigMap。
  >5. 执行kubectl apply启动服务。

- 方式二：单独指定配置文件路径

  ```bash
  cd examples/deployer
  python3 deploy.py \
    --user_config_path ../../model_configs/vllm/pd_hybrid/user_config.json \
    --env_config_path ../../model_configs/vllm/pd_hybrid/env.json
  ```

>[!NOTE] 说明
>若部署失败，可先卸载集群，排查并修改配置后重新部署。

### 查看集群状态与日志

- **查看Pod列表**：

  ```bash
  kubectl get pods -n <job_id>
  ```

  在CRD默认方式下，InferServiceSet会拉起controller、coordinator和union角色对应的Pod。Pod状态为Running仅表示已成功调度并启动，是否业务就绪仍需结合日志进一步确认。

- **查看日志**：

  1. 配置log_collect/log_config.ini，设置name_space属性为实际的命名空间，此处为mindie-motor。

  2. 在/examples/deployer目录下执行以下命令获取/查看日志路径。

      ```bash
      cd examples/deployer
      bash show_log.sh
      ```

      日志生成路径：examples/deployer/log_collect/log/\<YYYYMMDD_hhmmss>。

      单个Pod的日志文件命名格式为\<pod_name>\_\<node_name>\_\<retry_count>.log，例如： vllm-0-controller-0-xxxx_node01_0.log。
  3. 使用以下命令查看日志内容。

      ```bash
      tail -f log_collect/log/<YYYYMMDD_hhmmss>/<pod_name>_<node_name>_<retry_count>.log
      ```

- **查看单个Pod日志**：

  ```bash
  kubectl logs <pod_name> -n <job_id>
  ```

- **进入容器内部查看日志**：

  ```bash
  kubectl exec -it <pod_name> -n <job_id> -- bash
  ```

## 发送推理请求

服务就绪后，可通过/v1/chat/completions接口测试服务是否拉起正常。推理入口为Coordinator对外暴露的端口（默认为31015）。请将`<IP>`替换为实际访问地址，将`model`替换为user_config.json配置文件中motor_engine_union_config字段下`served_model_name`参数配置的模型名称。

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

若返回{"detail":"Service is not available"}，表示服务尚未就绪，可稍后重试并查看Pod日志。若返回流式JSON，则说明推理正常。

>[!NOTE] 说明
>
>HTTP协议存在安全风险，生产环境建议开启HTTPS。

## 手动扩缩容

PD混部扩缩容时仅修改user_config.json配置文件中motor_deploy_config字段下hybrid_instances_num参数，然后在examples/deployer目录执行以下命令：

```bash
python3 deploy.py --config_dir ../../model_configs/vllm/pd_hybrid --update_instance_num
```

>[!NOTE] 说明
>
>- hybrid_instances_num参数的取值范围必须为：(0,16]。
>- 扩缩容基线来自集群ConfigMap motor-config。
>- 只能修改user_config.json配置文件中motor_deploy_config字段下hybrid_instances_num参数，不允许同时修改其他参数。
>- CRD默认方式下，脚本更新infer_service.yaml中union角色的replicas后执行apply，由 CRD controller完成扩缩容。

手动扩缩容更多详细说明请参考[手动扩缩容](../../features/manual_scaling.md)。

## 卸载

在/examples/deployer目录下执行以下命令，删除当前job_id对应命名空间下的K8s ConfigMap以及已apply的YAML文件，并清理启动脚本中由deploy.py注入的环境变量函数。

```bash
bash delete.sh <命名空间>
```

例如：

```bash
bash delete.sh mindie-motor
```

>[!NOTE] 说明
>命名空间请根据实际创建的名称替换。卸载脚本必须在/examples/deployer目录下执行，否则无法正确找到output_yamls路径而报错。

## 故障排查与注意事项

- **镜像与权重**：确保image_name在集群内可正常拉取；weight_mount_path在宿主机上存在，且engine_config.model指向容器内正确路径。
- **实例角色错误**：PD混部需确保部署产生union角色实例；Coordinator会自动识别，无需额外调度模式配置。
- **Prefix Cache 特性对性能测试的影响**：Prefix Cache默认开启，若期望获取推理服务的基线性能数据，可在vLLM的engine_config中增加"no-enable-prefix-caching": true。
