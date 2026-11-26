# PD混部服务部署（基于vLLM推理引擎）

本文档介绍PD混部服务部署（基于vLLM推理引擎），将Prefill与Decode实例部署在同一类原生引擎实例中、由Coordinator以single_node调度模式分发请求，涵盖镜像准备、user_config.json与env.json配置、部署命令、状态与日志查看、扩缩容及卸载，适用于需要快速验证或中小规模部署PD混部服务的用户。

## 场景介绍

### PD混部介绍

PD混部将Prefill与Decode实例部署在同一类原生引擎实例中。部署时不再分别拉起prefill、decode两类角色，而是由union角色承载完整推理能力；Coordinator以single_node调度模式将请求分发到可用的union实例。

与[PD服务分离部署](../README.md#k8s-pd-disaggregation)相比，PD混部减少了P/D角色拆分和KV跨角色传输配置，适用于快速验证、中小规模服务、资源规模较小或暂不需要独立规划P/D实例比例的场景。若业务需要针对Prefill、Decode两阶段分别规划资源、独立扩缩容或使用PD分离相关能力，建议使用PD分离服务部署。

### 部署入口与流程

部署流程围绕三个入口展开：

- user_config.json配置文件：部署与业务的总配置，PD混部重点配置hybrid_*、motor_engine_union_config字段以及Coordinator的single_node调度模式。
- env.json配置文件：各组件环境变量，PD混部的原生引擎环境变量配置在motor_engine_union_env字段中。
- deploy.py部署脚本：读取user_config.json和env.json配置文件，生成K8s YAML文件、更新启动脚本、创建ConfigMap并执行"kubectl apply"。

**部署方式**：

- （推荐）CRD方式（infer_service_set）：PD混部默认使用CRD方式（infer_service_set），由InferServiceSet中的union角色拉起混部实例。
- RBG方式（rbg）：由RoleBasedGroup中的union角色拉起混部实例，并支持通过RoleBasedGroupScalingAdapter对接HPA。
- 多YAML Deployment方式：在motor_deploy_config.deploy_mode字段中显式配置为"multi_deployment"。

### 限制与约束

- Atlas 800I A2推理服务器与Atlas 800I A3超节点服务器支持此特性。
- 模型支持范围同所选推理引擎，详情请参考[模型支持列表](https://www.hiascend.com/developer/models)。
- hybrid_instances_num表示union实例数，扩缩容时仅允许修改该字段。
- hybrid_pod_npu_num、并行参数和模型路径需与实际硬件资源、模型权重路径保持一致。
- CRD方式需集群预先安装MindCluster InferServiceSet CRD及对应controller。
- RBG方式需集群预先安装RoleBasedGroup、RoleInstanceSet、RoleInstance、RoleBasedGroupScalingAdapter和CoordinatedPolicy CRD及对应controller，具体准备参见[RBG部署说明](../../../../../examples/deployer/lib/rbg/README.md)。
- RBG方式不支持ScaleP2D和容器快照；自动扩缩容与union角色的手动扩缩容不能同时使用。完整限制参见[RBG部署说明](../../../../../examples/deployer/lib/rbg/README.md)。

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

  基础镜像已安装CANN、vLLM、vLLM Ascend等组件，请参考[基于vllm-ascend/sglang镜像安装MindIE Motor](../../maintenance/build_motor_image_from_vllm_ascend.md#自行制作mindie-motor镜像) 额外安装MindIE Motor。

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
- 部署工具使用方法详情请参考[Deployer部署工具](../../../../../examples/deployer/README.md)。

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
| deploy_mode | string | 部署方式，未配置时默认为：infer_service_set。<br>PD混部推荐使用infer_service_set方式。 |
| hybrid_instances_num | int | union实例个数。InferServiceSet / multi_deployment取值范围为[1,16]；RBG初次部署允许为0，手动扩缩容后至少为1。 |
| single_hybrid_instance_pod_num | int | 单个union实例对应的Pod数，取值范围：大于等于1。 |
| hybrid_pod_npu_num | int | 单个union Pod占用的NPU卡数。 |
| image_name | string | 推理镜像名，需包含MindIE Motor与推理引擎运行环境。 |
| job_id | string | 部署任务名，同时作为K8s命名空间使用，如：mindie-motor。|
| hardware_type | string | 硬件类型，取值为800I_A2、800I_A3或Ascend950。 |
| weight_mount_path | string | 宿主机上模型权重挂载路径，容器内model需与此挂载路径一致。 |

#### 使用RBG方式部署

使用RBG方式时，将`deploy_mode`改为`rbg`，并增加`rbg_config`。其余PD混部配置保持不变：

```json
"motor_deploy_config": {
  "deploy_mode": "rbg",
  "job_id": "mindie-motor",
  "rbg_config": {
    "name": "vllm",
    "logical_service_name": "vllm",
    "gang_scheduling": true,
    "scheduler_name": "volcano",
    "restart_policy_by_role": {
      "union": "RecreateRoleInstanceOnPodRestart"
    },
    "coordinated_policy": {
      "policies": [
        {
          "name": "engine-scaling",
          "roles": ["union"],
          "strategy": {
            "scaling": {
              "maxSkew": "100%",
              "progression": "OrderReady"
            }
          }
        }
      ]
    }
  }
}
```

`rbg_config`字段说明（完整配置参见[配置参数说明](../../configuration/config_reference.md#motor_deploy_config)）：

| 字段 | 含义 |
|------|------|
| `name` | RoleBasedGroup资源名；未配置时使用`logical_service_name`，两者均未配置时最终使用`vllm`。 |
| `logical_service_name` | 业务Service的逻辑名称，默认`vllm`。这两个名称均须为不超过63字符的Kubernetes DNS label。 |
| `restart_policy_by_role` | 按引擎Role（此处为`union`）设置Pod重启时是否重建整个RoleInstance；可选`None`或`RecreateRoleInstanceOnPodRestart`。开启DP域缩容的Role必须使用`None`。 |
| `gang_scheduling` | 是否启用组级Gang Scheduling；首次部署默认`true`，启用时须使用Volcano。 |
| `scheduler_name` | Pod调度器名称；首次部署默认`volcano`。仅在关闭Gang Scheduling且没有昇腾硬件调度注解时可用`default-scheduler`。 |
| `coordinated_policy` | 可选的Role协调扩缩容/滚动更新策略；首次部署省略时生成默认同名CoordinatedPolicy。HPA自动扩缩容另由顶层`scaling_policy`配置。 |

这里只叠加RBG专属字段。必须保留原配置中的`hardware_type`、`hybrid_pod_npu_num`、`single_hybrid_instance_pod_num`及引擎并行参数，不能统一改成示例值。省略`gang_scheduling`和`scheduler_name`时，首次部署默认启用Volcano Gang Scheduling。

使用A3（`800I_A3`）或A5（`Ascend950`）时，RBG会生成`sp-block`硬件调度注解，必须使用`scheduler_name: volcano`。当前不支持选择超节点亲和调度策略，Union角色也不提供自定义`nodeSelector`配置入口。A2可使用默认Volcano；如集群不使用Gang Scheduling，可显式配置`gang_scheduling: false`和`scheduler_name: default-scheduler`。

`rbg_config.name`和`logical_service_name`必须是长度不超过63的Kubernetes DNS label，未配置时默认使用`vllm`。`hybrid_instances_num`映射为union Role的`replicas`，`single_hybrid_instance_pod_num`表示每个RoleInstance包含的Pod数量。

**motor_coordinator_config**

PD混部场景不需要配置Coordinator调度模式。Coordinator会根据运行中的union实例自动选择PDHybridRouter。

**配置示例（默认负载均衡）**：

```json
"motor_coordinator_config": {}
```

**配置示例（KV Cache亲和调度，可选）**：

启用KV Cache亲和调度时，在 model_configs/vllm/pd_hybrid/user_config.json配置文件中按以下示例修改motor_coordinator_config、motor_engine_union_config字段，并增加 kv_conductor_config字段即可。详情请参考[KV Cache亲和性调度](../../features/inference_scheduling/kvcache_affinity.md)。

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
| engine_config.kv-events-config | object | 启用KV Cache亲和时配置KV事件发布（请参考[KV Cache 亲和部署](../../features/inference_scheduling/kvcache_affinity.md)）。 |
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
- 使用RBG后端时，已安装`RoleBasedGroup`、`RoleInstanceSet`、`RoleInstance`、`RoleBasedGroupScalingAdapter`和`CoordinatedPolicy` CRD及对应controller；启用默认Gang Scheduling时还须安装Volcano。具体预先安装准备参见[RBG部署说明](../../../../../examples/deployer/lib/rbg/README.md)。可执行以下命令确认资源已注册：

  ```bash
  kubectl api-resources | grep -E 'rolebasedgroup|roleinstanceset|roleinstance|coordinatedpolic'
  ```

- 已创建与`job_id`同名的命名空间，示例如下：

  ```bash
  kubectl create namespace mindie-motor
  ```

- 宿主机上模型权重已放在weight_mount_path参数指定路径（如：/mnt/weight/）。
- 各计算节点已准备好image_name指定的推理镜像。

### 部署命令

在 `examples/deployer` 目录下可按以下任一种方式启动服务。所有部署后端（含 RBG）在启动和 `--dry-run` 预览前均会执行[部署前安全预检](README.md#deployment-security-precheck)。仅在隔离测试环境需要沿用现有配置时，可在原命令末尾追加 `--skip-check`。

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
  >正常部署时，`deploy.py` 会依次执行以下步骤：
  >
  >1. 读取user_config.json和env.json配置文件。
  >2. 根据motor_deploy_config生成Controller、Coordinator和union实例的K8s资源；`deploy_mode=rbg`时生成RoleBasedGroup、业务Service、CoordinatedPolicy及配套资源。
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

  RBG方式下还应检查RoleBasedGroup、RoleInstance、Service和协调策略：

  ```bash
  kubectl get rolebasedgroup,roleinstance,coordinatedpolicy -n <job_id>
  kubectl get pods,svc -n <job_id> -o wide
  kubectl describe rolebasedgroup vllm -n <job_id>
  ```

  若配置了顶层`scaling_policy`，还应检查HPA及ScalingAdapter：

  ```bash
  kubectl get rolebasedgroupscalingadapter,hpa -n <job_id>
  ```

- **查看容器输出**

  `show_log.sh` 和 `kubectl logs` 查看 Pod 容器的标准输出和标准错误。

  1. 配置log_collect/log_config.ini，设置name_space属性为实际的命名空间，此处为mindie-motor。

  2. 在examples/deployer目录下执行以下命令，持续采集容器输出。

      ```bash
      cd examples/deployer
      bash show_log.sh
      ```

      容器输出采集路径：examples/deployer/log_collect/log/\<YYYYMMDD_hhmmss>。

      单个Pod的采集文件命名格式为\<pod_name>\_\<node_name>\_\<retry_count>.log，例如：vllm-0-controller-0-xxxx_node01_0.log。

  3. 使用以下命令查看采集内容。

      ```bash
      tail -f log_collect/log/<YYYYMMDD_hhmmss>/<pod_name>_<node_name>_<retry_count>.log
      ```

  也可以直接查看指定 Pod 的容器输出：

  ```bash
  kubectl logs <pod_name> -n <job_id>
  ```

- **查看组件日志、诊断文件和缓存**

  使用已有 `plog-path` hostPath 日志卷的标准模板部署时，计算 Pod 内各组件写入的日志文件、诊断文件和缓存默认位于其所在宿主机的 `<宿主机日志根>/<模型名>/<service_id>/<Pod名>/<容器名>/`。宿主机日志根默认与 `env.json` 中的 `motor_common_env.MOTOR_LOG_ROOT_PATH` 相同，未配置时为 `/root/ascend/log`；`multi_deployment` 可通过 `logging_config.host_log_dir` 指定宿主机日志根。`service_id` 在环境生成阶段按 `<job_id>_<北京时间YYYYMMDDhhmmss>` 生成。挂载适用范围、组件子目录及控制面目录见[日志与诊断文件目录](../../maintenance/log_directory.md)。

  也可以进入对应容器，在 `<MOTOR_LOG_ROOT_PATH>/<模型名>/<service_id>/<Pod名>/<容器名>/` 下查看文件：

  ```bash
  kubectl exec -it <pod_name> -n <job_id> -- bash
  ```

## 发送推理请求

服务就绪后，可通过/v1/chat/completions接口测试服务是否拉起正常。推理入口为Coordinator对外暴露的端口（默认为31015）。请将`<IP>`替换为实际访问地址，将`model`替换为user_config.json配置文件中motor_engine_union_config字段下`served_model_name`参数配置的模型名称。

启用推理 TLS 时，请按[HTTPS 验证方式](README.md#verify-inference-tls)使用 HTTPS 地址和 `curl --cacert`。以下 HTTP 示例适用于推理 TLS 关闭的隔离测试环境。

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

若命令提示安全预检未通过，请参见[部署前安全预检](README.md#deployment-security-precheck)完成安全配置。仅在隔离测试环境需要沿用现有配置时，可在原命令末尾追加 `--skip-check`。

>[!NOTE] 说明
>
>- hybrid_instances_num参数的取值范围必须为：(0,16]。
>- 扩缩容基线来自集群ConfigMap motor-config。
>- 只能修改user_config.json配置文件中motor_deploy_config字段下hybrid_instances_num参数，不允许同时修改其他参数。
>- CRD默认方式下，脚本更新infer_service.yaml中union角色的replicas后执行apply，由 CRD controller完成扩缩容。
>- RBG方式下，脚本更新在线RoleBasedGroup中union角色的`replicas`。详细机制、验证方法和故障处理参见[手动扩缩容](../../features/scaling_deployment/manual_scaling.md)。

手动扩缩容更多详细说明请参考[手动扩缩容](../../features/scaling_deployment/manual_scaling.md)。

RBG方式需要自动扩缩容时，在`user_config.json`顶层的`scaling_policy`中配置union角色的HPA策略。union角色没有默认指标，必须显式配置`metric`。完整配置与停用方法参见[自动弹性扩缩容](../../features/scaling_deployment/auto_scaling.md#rbg自动扩缩容)。部署后使用以下命令检查资源和外部指标：

```bash
kubectl get rolebasedgroupscalingadapter,hpa -n <job_id>
kubectl describe hpa -n <job_id>
kubectl get --raw '/apis/external.metrics.k8s.io/v1beta1'
```

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
