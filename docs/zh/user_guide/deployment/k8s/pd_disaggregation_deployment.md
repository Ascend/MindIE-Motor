# PD分离服务部署

## 场景介绍

### PD分离介绍

**PD 分离**（Prefill & Decode 分离）将大语言模型推理的预填充（Prefill）与解码（Decode）两个阶段拆分到不同实例上运行，适用于对时延和吞吐要求较高的场景。通过 PD 分离可提高 NPU 利用率，减轻 Prefill 与 Decode 分时复用带来的相互干扰，在相同时延下提升整体吞吐。

两个推理阶段的含义如下：

- **Prefill 阶段**：对输入 prompt 执行一次完整前向传播，生成初始隐藏状态（Hidden States），**计算密集型**；每个新输入序列都需执行一次 Prefill。
- **Decode 阶段**：基于 Prefill 结果逐步生成后续 token，每步仅计算最新 token 的激活与 attention，单步计算量较小，但需反复执行直至生成结束，**访存密集型**（以 KV Cache 等内存访问为主）。

本仓库采用**多机 PD 分离**部署方案：通过 K8s Service 为 Coordinator 暴露推理入口，使用多个 Deployment 分别部署 Controller（单 Pod）、Coordinator（单 Pod）以及 Server（P 实例与 D 实例各若干 Pod）。Controller 负责集群与实例管理，Coordinator 接收用户请求并调度至 P/D 实例，由 P 实例与 D 实例协同完成一次完整推理。

**PD 分离的主要优势**

- **资源利用更优**：Prefill 为计算密集型、Decode 为访存密集型，特性不同，分离部署可更充分利用 NPU 的计算与带宽资源。
- **吞吐能力提升**：Prefill 处理新请求的同时，Decode 可持续处理已有请求的解码，整体处理能力更高。
- **时延更可控**：两阶段分离可减少排队与等待，尤其在高并发场景下有助于降低时延。

### 部署入口与流程

部署流程围绕三个入口展开：

1. `user_config.json`：部署与业务的总配置（实例数、镜像、模型、并行策略、TLS、Controller/Coordinator 等）。
2. `env.json`：各组件环境变量（如 CANN、HCCL、OMP 等），由 `deploy.py` 注入到 `boot.sh`。
3. 部署脚本 `deploy.py`：读取上述配置，生成 K8s YAML、更新 `boot.sh`、创建 ConfigMap 并执行 `kubectl apply`。

**部署方式**：当前默认采用 **CRD 方式**（基于 MindCluster 的 infer-operator）进行部署。该方式尚未完成 RAS 能力与池化能力的适配验证。若您需要 RAS（可靠性、可用性、可服务性）或 KV 池化能力，可在 `user_config.json` 的 `motor_deploy_config.deploy_mode` 中配置为 `multi_deployment`，切换为原有的**多 YAML Deployment 方式**（由 `deploy.py` 生成并 apply 多个 Deployment YAML），该方式已支持 RAS 与池化相关能力。

### 限制与约束

- Atlas 800I A2 推理服务器与 Atlas 800I A3 超节点服务器支持此特性。
- P 节点与 D 节点仅支持相同型号的机型。
- NPU 网口互联。
- 模型支持范围同 vllm-ascend。

### 硬件环境

PD 分离部署支持的硬件环境如下所示。

**表 1**  PD 分离部署支持的硬件列表

| 类型   | 型号                       | 内存     |
|--------|----------------------------|----------|
| 服务器 | Atlas 800I A2 推理服务器   | 32GB / 64GB |
| 服务器 | Atlas 800I A3 超节点服务器 | 64GB     |

>[!NOTE]说明
>
>- 集群必须具备参数面互联：即服务器 NPU 卡对应的端口处在同一个 VLAN，可以通过 RoCE 互通。
>- 为保障业务稳定运行，用户应严格控制自建 Pod 的权限，避免高权限 Pod 修改 MindIE 内部参数而导致异常。

## 准备镜像

在部署PD分离服务前，需要在各计算节点上准备好可用的推理镜像。推荐优先使用经过验证的预制镜像；若仅获取到基础（裸）镜像，则需要在镜像中自行安装vLLM、vllm-ascend以及本仓库MindIE-PyMotor，并重新制作镜像。

>[!NOTE]说明
>无论是使用预制镜像还是自制镜像，所有参与部署的K8s节点（包括运行 Controller、Coordinator、P实例和D实例的工作节点）都必须能够本地加载该镜像，否则Pod可能因镜像不可用而处于`ImagePullBackOff`或`ErrImagePull`状态。

### 使用推荐预制镜像

通常建议使用官方或已在生产环境验证过的推理镜像（[镜像获取地址](https://www.hiascend.com/developer/ascendhub)），此类镜像中已预安装：

- vLLM及其Ascend适配组件（如vllm-ascend等）。
- MindIE-PyMotor及其运行所需的基础依赖。

对于生产环境无法直接访问镜像仓库、需要通过离线包（`.tar` 文件）导入的场景，可在各节点按容器运行时类型选择对应的加载方式。（tar获取地址同[镜像获取地址](https://www.hiascend.com/developer/ascendhub)）

**表 2**  不同运行时的镜像加载示例

| 运行时类型 | 加载镜像示例命令 |
|-----------|------------------|
| Docker    | `docker load -i mindie-motor-vllm-dev.tar` |
| containerd（使用 `ctr`） | `ctr -n k8s.io images import mindie-motor-vllm-dev.tar` |
| containerd（使用 `nerdctl`） | `nerdctl -n k8s.io load -i mindie-motor-vllm-dev.tar` |

> [!NOTE]说明
> 导入完成后可通过`docker images`、`ctr -n k8s.io images list`或`nerdctl -n k8s.io images`等命令确认镜像是否导入成功，镜像名与`image_name`中配置需保持完全一致。

### 基于裸镜像构建自定义镜像

若仅获得一个基础（裸）镜像（仅包含操作系统、CANN及Python等，未预装vLLM、vllm-ascend和MindIE-PyMotor），需在其中完成vLLM/vllm-ascend及本仓的安装后，将容器提交为镜像供部署使用。基础镜像选择、vLLM与vllm-ascend的安装及版本兼容要求等。

#### 在容器内安装并编译 MindIE-PyMotor

在已安装好vLLM、vllm-ascend的基础镜像上启动容器，将本仓库（MindIE-PyMotor）源码放入容器内（例如 `/home/PyMotor`），在源码根目录下依次执行：先使用 `requirements.txt` 安装依赖，再执行 `bash build.sh` 编译生成wheel包，最后安装本仓。

```bash
cd /home/PyMotor
pip install -r requirements.txt
bash build.sh
pip install dist/*.whl
```

#### 将容器提交为镜像

环境与 MindIE-PyMotor 安装完成后，在**运行该容器的宿主机**上根据当前使用的容器运行时，将容器提交为镜像。

- **Docker 运行时**：使用 `docker commit` 将当前容器保存为新镜像，再按需打标签或导出为 `.tar` 文件。

  ```bash
  docker commit <容器ID或名称> <镜像名>:<标签>
  # 可选：导出为离线包
  docker save -o mindie-motor-vllm-dev.tar <镜像名>:<标签>
  ```

- **containerd 运行时**：containerd 无直接等价于 `docker commit` 的“容器转镜像”命令，需先通过 Docker 或具备 commit 能力的工具在其它节点将容器保存为镜像并导出为 `.tar`，再在 containerd 节点使用 `ctr -n k8s.io images import` 或 `nerdctl -n k8s.io load -i` 导入；或在构建阶段通过 Dockerfile 等方式在镜像中完成 MindIE-PyMotor 的安装，再导出镜像供 containerd 使用。

### 在不同环境加载自定义镜像

在实际集群中，需要在所有运行 Controller、Coordinator、P/D Server Pod 的节点上执行镜像加载操作。加载方式与 2.1 节相同，可按运行时类型选择合适命令，例如：

- Docker 节点：

  ```bash
  docker load -i mindie-motor-vllm-dev.tar
  ```

- 使用 containerd 的节点（示例为 `ctr` 命令）：

  ```bash
  ctr -n k8s.io images import mindie-motor-vllm-dev.tar
  ```

## 部署目录结构

请将本仓库中的 `examples` 目录上传至 K8s 集群的 master 节点。`examples` 可使用以下两种方式获取：

- **从本代码仓获取**：将仓库根目录下的 `examples` 目录上传至 master 服务器。

- **从容器镜像获取**：若无完整代码仓，但已拉取PyMotor推理镜像，可使用镜像内预置的示例目录，路径为 **`/tmp/motor/examples`**（目录结构与仓库中的 `examples` 一致）。在已拉取镜像的机器上执行（将 `IMAGE` 替换为实际镜像名或镜像 ID，可与 `user_config.json` 中 `motor_deploy_config.image_name` 保持一致）：

  ```bash
  IMAGE="<镜像名或镜像ID>"

  cid=$(docker create "$IMAGE")
  docker cp "$cid:/tmp/motor/examples" ./examples
  docker rm "$cid"
  ```

  将得到的 `examples` 目录上传至 master 服务器。

  若使用 Podman，将命令中的 `docker` 替换为 `podman` 即可。

`examples` 中与 PD 分离部署相关的主要目录结构如下：

```text
examples/
├── deployer/                  # 部署工具目录
│   ├── deploy.py              # 部署入口脚本
│   ├── delete.sh              # 卸载脚本
│   ├── show_log.sh            # 日志查看脚本
│   ├── README.md              # 部署工具使用说明
│   ├── yaml_template/         # K8s YAML 模板
│   ├── startup/               # 启动脚本
│   │   ├── boot.sh            # 容器内启动脚本
│   │   ├── common.sh          # 公共环境变量设置
│   │   ├── hccl_tools.py      # 生成 ranktable
│   │   ├── mooncake_config.py # Mooncake 配置生成
│   │   └── roles/             # 各组件环境变量设置脚本
│   ├── probe/                 # 探针脚本
│   ├── log_collect/           # 日志采集
│   │   ├── log_config.ini     # 日志采集配置（使用 show_log 前须配置 name_space 等）
│   │   └── log_monitor.py     # 日志拉取脚本（由 show_log.sh 启动）
│   └── output_yamls/          # 生成的 YAML 输出目录
└── infer_engines/             # 各引擎配置示例
    └── vllm/                  # vLLM 引擎配置
        ├── user_config.json   # 快速启动用户配置
        ├── env.json           # 快速启动环境变量配置
        └── models/            # 特定模型配置
```

- 配置文件位于 `examples/infer_engines/` 目录下（如 `examples/infer_engines/vllm/user_config.json`），根据引擎类型和模型选择对应配置，以实际使用的为准。
- 部署工具使用方法详见 `examples/deployer/README.md`。

## 配置 `user_config.json`

在 `examples/infer_engines/vllm/user_config.json` 或 `examples/infer_engines/vllm/models/` 下对应模型配置（如 `examples/infer_engines/vllm/models/deepseek/v3_1/user_config.json`）中编辑，以实际使用的为准。该文件为 JSON 根结构：根节点包含 `version`（固定为 `"v2.0"`）及各模块配置对象。下文按模块说明 PD 分离场景下需要重点关注的配置项。

### motor_deploy_config（部署与资源）

`motor_deploy_config` 为部署与资源相关配置。

**配置示例**（1P1D，每 Pod 16 卡；`tls_config` 可选，结构见 4.6）：

```json
"motor_deploy_config": {
  "p_instances_num": 1,
  "d_instances_num": 1,
  "single_p_instance_pod_num": 1,
  "single_d_instance_pod_num": 1,
  "p_pod_npu_num": 16,
  "d_pod_npu_num": 16,
  "image_name": "",
  "job_id": "mindie-motor",
  "hardware_type": "800I_A3",
  "weight_mount_path": "/mnt/weight/",
  "tls_config": { ... }
}
```

**配置项说明**：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| p_instances_num | int | P 实例个数，≥1 |
| d_instances_num | int | D 实例个数，≥1 |
| single_p_instance_pod_num | int | 单个 P 实例对应的 Pod 数，≥1 |
| single_d_instance_pod_num | int | 单个 D 实例对应的 Pod 数，≥1 |
| p_pod_npu_num | int | 单个 P 实例 Pod 占用的 NPU 卡数 |
| d_pod_npu_num | int | 单个 D 实例 Pod 占用的 NPU 卡数 |
| image_name | string | 填写本文档 [2. 准备镜像](#准备镜像) 中准备/加载的推理镜像名（需包含 MindIE-PyMotor 与 vLLM 等运行环境） |
| job_id | string | 部署任务名，同时作为 K8s 命名空间使用，如 `mindie-motor` |
| hardware_type | string | 硬件类型：<br>A2: 800I_A2<br>A3: 800I_A3<br>A5: 850-Atlas-8p-8 |
| weight_mount_path | string | 宿主机上模型权重挂载路径，容器内 model_path 需与此挂载路径一致，如 `"/mnt/weight/"` |
| tls_config | object | 可选；TLS 相关配置，完整结构与示例见 4.6 |

### motor_controller_config / motor_coordinator_config

`deploy.py` 会将 `user_config.json` 中 Controller、Coordinator 的子配置合并到组件运行时配置：先采用代码默认值，再按此处配置项覆盖。支持在运行时通过修改组件所监控的配置文件实现动态生效。更多可配置项及全量参数说明请参考 [user_config 全量参数说明](./config_reference.md)。

**配置示例**（与仓内 `user_config.json` 对应结构一致）：

```json
"motor_controller_config": {
  "standby_config": {
    "enable_master_standby": false
  }
},
"motor_coordinator_config": {
  "standby_config": {
    "enable_master_standby": false
  },
  "request_limit": {
    "single_node_max_requests": 4096,
    "max_requests": 10000
  }
}
```

**配置项说明**：

**motor_controller_config**：仓内示例仅包含主备开关。

| 配置项 | 类型 | 说明 |
|--------|------|------|
| standby_config.enable_master_standby | bool | 是否开启 Controller 主备 |

**motor_coordinator_config**：包含主备开关与请求限流。

| 配置项 | 类型 | 说明 |
|--------|------|------|
| standby_config.enable_master_standby | bool | 是否开启 Coordinator 主备 |
| request_limit.single_node_max_requests | int | 单节点最大请求数 |
| request_limit.max_requests | int | 全局最大请求数 |

### motor_nodemanger_config

用于 NodeManager 组件的专项配置。仓内 `user_config.json` 中该对象为空 `{}`，PD 分离场景下一般无需配置。全量参数见 [user_config 全量参数说明](./config_reference.md#4-motor_nodemanger_config)。注意字段名为 `motor_nodemanger_config`（拼写保留仓内一致）。

### motor_engine_prefill_config / motor_engine_decode_config（P/D 引擎）

两者结构类似，均需指定 `engine_type` 和 `engine_config`，模型信息与并行策略直接配置在 `engine_config` 中。

**`engine_config` 与引擎原生配置等价**：其中的键名与所选引擎启动命令（如 vLLM 的 `vllm serve`、SGLang 的 `python -m sglang.launch_server`）的 CLI 参数一一对应——去掉 `--` 前缀后以 JSON 键写入即可（连字符 `-` 与下划线 `_` 两种形式均可，与引擎官方文档保持一致）。PyMotor 启动 Engine Server 时会将 `engine_config` 还原为引擎命令行参数，因此可直接复用引擎文档中的参数说明；原先 `model_config` 中的模型信息与 `parallel_config` 中的并行策略，现均直接写入 `engine_config`。

**从命令行迁移到 JSON**：若已有可运行的引擎启动命令，可将 `--key value` 逐条映射为 `"key": value` 写入 `engine_config`。例如 vLLM：

| 命令行参数 | `engine_config` 键值 |
|------------|----------------------|
| `--model /mnt/weight/qwen3_8B` | `"model": "/mnt/weight/qwen3_8B"` |
| `--served-model-name qwen3-8B` | `"served_model_name": "qwen3-8B"` |
| `--tensor-parallel-size 2` | `"tensor_parallel_size": 2` |
| `--enforce-eager` | `"enforce-eager": true` |

也可使用 [engine_config 命令行转换工具](../../operations/cli_to_engine_config_guide.md) 批量完成上述映射。

#### 配置示例（Qwen3-8B，MooncakeLayerwiseConnector）

```json
"motor_engine_prefill_config": {
  "engine_type": "vllm",
  "engine_config": {
    "served_model_name": "qwen3-8B",
    "model": "/mnt/weight/qwen3_8B",
    "gpu_memory_utilization": 0.9,
    "data_parallel_size": 2,
    "tensor_parallel_size": 2,
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
      "kv_port": "20001",
      "engine_id": "0",
      "kv_rank": 0,
      "kv_connector_extra_config": {}
    }
  },
  "health_check_config": {
    "enable_virtual_inference": false,
    "npu_usage_threshold": 3,
    "max_failure_count": 6,
    "health_collector_timeout": 2
  }
},
"motor_engine_decode_config": {
  "engine_type": "vllm",
  "engine_config": {
    "served_model_name": "qwen3-8B",
    "model": "/mnt/weight/qwen3_8B",
    "gpu_memory_utilization": 0.9,
    "data_parallel_size": 2,
    "tensor_parallel_size": 2,
    "pipeline_parallel_size": 1,
    "enable_expert_parallel": false,
    "data_parallel_rpc_port": 9000,
    "max_model_len": 2048,
    "kv_transfer_config": { ... }
  },
  "health_check_config": {
    "enable_virtual_inference": false,
    "npu_usage_threshold": 3,
    "max_failure_count": 6,
    "health_collector_timeout": 2
  }
}
```

下文对上述配置项逐一说明。

#### 根节点配置项

| 配置项 | 类型 | 说明 |
|--------|------|------|
| engine_type | string | 引擎类型，如 `vllm` |
| engine_config | object | 引擎相关配置，含模型信息、并行策略、KV 传输与引擎原生参数 |
| dispatch_profile | string | 可选；P/D 协同语义（`handoff` / `trigger`）。`kv_connector` 不在白名单时必填，见 [kv_connector 识别白名单与 dispatch_profile](#kv_connector-识别白名单与-dispatch_profile) |
| health_check_config | object | 可选；虚推（虚拟推理）健康探测配置，见下表及 [配置参考](./config_reference.md#62-health_check_config虚推健康探测) |

#### health_check_config 配置项（虚推健康探测）

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable_virtual_inference | bool | `false` | 虚推总开关，设为 `true` 启用周期性健康探测 |
| npu_usage_threshold | int | `3` | AICore 使用率阈值（%），用于异常判定与间隔恢复 |
| max_failure_count | int | `6` | 连续虚推失败次数上限，达到后 `/status` 返回 abnormal |
| health_collector_timeout | int | `2` | 推理面 `/health` 探测超时（秒） |

**启用虚推**：将 P/D 配置中的 `enable_virtual_inference` 设为 `true`。`npu_usage_threshold` 建议按业务负载调整（默认 3% 表示仅在 NPU 近乎空闲时判定虚推失败）；`max_failure_count` 控制触发 abnormal 的容忍度。机制细节见 [Engine Server 组件文档](../../../developer_guide/components/engine_server.md#虚推虚拟推理健康探测)。

#### engine_config 配置项（模型与并行策略）

模型信息与并行策略直接配置在 `engine_config` 下，使用引擎原生参数名：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| served_model_name | string | 模型名称，如 qwen3-8B |
| model | string | 容器内模型权重路径，需与 weight_mount_path 挂载后一致，如 /mnt/weight/qwen3_8B |
| gpu_memory_utilization | float | NPU 内存使用占比上限，0～1，如 0.9 |
| data_parallel_size | int | 数据并行大小 |
| tensor_parallel_size | int | 张量并行大小 |
| pipeline_parallel_size | int | 流水并行大小 |
| enable_expert_parallel | bool | 是否启用 EP |
| data_parallel_rpc_port | int | DP 侧 RPC 端口 |

#### engine_config 其它配置项

**engine_config** 下除模型与并行策略外，还涵盖：**kv_transfer_config**（KV 传输，其内可含 **kv_connector_extra_config** 等）、以及引擎原生参数。除下文单独说明的 `kv_transfer_config` 结构外，其余项（含 `kv_connector_extra_config` 中子字段及其它键）均按所选用引擎（如 vLLM）的原生参数直接填写即可，参见对应引擎文档。

| 配置项 | 类型 | 说明 |
|--------|------|------|
| kv_transfer_config | object | KV 传输配置，PD 协同关键，见下表 |
| 其它键 | - | 引擎原生参数（如 vLLM 的 max_model_len、enforce-eager 等），按引擎文档直接填写 |

#### kv_transfer_config 配置项

| 配置项 | 类型 | 说明 |
|--------|------|------|
| kv_connector | string | KV 连接器类型，如 `MooncakeLayerwiseConnector` |
| kv_buffer_device | string | KV 缓冲区设备，如 `npu` |
| kv_role | string | KV 角色，prefill 为 `kv_producer`，decode 为 `kv_consumer` |
| kv_parallel_size | int | KV 并行大小 |
| kv_port | string | KV 端口 |
| engine_id | string | 引擎 ID |
| kv_rank | int | KV rank |
| kv_connector_extra_config | object | 额外配置，见下表 |

#### kv_connector_extra_config 配置项

| 配置项 | 类型 | 说明 |
|--------|------|------|
| prefill | object | prefill 侧额外配置，如 dp_size、tp_size |
| decode | object | decode 侧额外配置，如 dp_size、tp_size |

prefill / decode 子字段：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| dp_size | int | 数据并行大小 |
| tp_size | int | 张量并行大小 |

> **说明**
>
>- `kv_connector_extra_config` 中 prefill/decode 的 dp_size、tp_size 一般无需手动填写，Motor 在拉起服务时会根据 `data_parallel_size` / `tensor_parallel_size` 自动刷新。
>- 若需使用 KV 池化等能力，请改用 MultiConnector，并参考 [KV 池化部署指南](../../features/KV_pool.md) 修改 `user_config.json`，并与 `deploy.py` 配合使用。

#### kv_connector 识别白名单与 dispatch_profile

NodeManager 根据 `engine_config.kv_transfer_config.kv_connector`（大小写不敏感）自动推导 P/D 协同 capability。vLLM 当前内置识别：

| `kv_connector` | 推导出的 capability | 协同行为 |
|----------------|---------------------|----------|
| `MooncakeConnectorV1` | `prefill_handoff_decode` | Prefill 完成后将结果交给 Decode |
| `MooncakeHybridConnector` | `prefill_handoff_decode` | 同上 |
| `NixlConnector` | `prefill_handoff_decode` | 同上 |
| `MooncakeLayerwiseConnector` | `concurrent_engine_sync` | P/D 并发执行，由引擎同步 KV |
| `MultiConnector` | 取 `kv_connector_extra_config.connectors[0]` 递归判定 | 取决于传输层 connector |

- **`MultiConnector` 只看 `connectors[0]`（传输层）**。KV 池/存储类 connector（如 `AscendStoreConnector`）作为 `connectors[1]` 不参与 capability 判定，**无需**出现在白名单中。
- 不在上表内、且 `connectors[0]` 也无法识别的 connector 会被判为 `unknown`，**不产生 capability**；Coordinator 不会强行配对，路由返回 503（fail-closed）。

完整说明见 [PD 分离特性说明](../../../design/pd_disaggregation.md#vllm-connector-识别白名单)。

**不在白名单时**：在 `motor_engine_prefill_config` / `motor_engine_decode_config` **顶层**（与 `engine_type` 同级）显式声明 `dispatch_profile`；**不可**直接填写 `dispatch_capabilities`（会被忽略）。P/D 两端取值须一致：

| `dispatch_profile` | 推导出的 capability |
|--------------------|---------------------|
| `handoff` | `prefill_handoff_decode` |
| `trigger` | `concurrent_engine_sync` |

**配置示例**：

```json
"motor_engine_prefill_config": {
  "engine_type": "vllm",
  "dispatch_profile": "handoff",
  "engine_config": {
    "kv_transfer_config": {
      "kv_connector": "YourCustomConnector",
      "kv_role": "kv_producer"
    }
  }
},
"motor_engine_decode_config": {
  "engine_type": "vllm",
  "dispatch_profile": "handoff",
  "engine_config": {
    "kv_transfer_config": {
      "kv_connector": "YourCustomConnector",
      "kv_role": "kv_consumer"
    }
  }
}
```

字段说明见 [user_config 全量参数说明](./config_reference.md#61-dispatch_profilepd-协同语义)。

### kv_cache_pool_config（可选）

仅在启用 KV Cache 池化时需要配置，详细内容可参考 [KV 池化部署指南](../../features/KV_pool.md)；若仅使用 PD 分离且未开启池化，可保留仓内默认结构即可。

**配置示例**（与仓内 `user_config.json` 一致）

```json
"kv_cache_pool_config": {
  "metadata_server": "P2PHANDSHAKE",
  "protocol": "ascend",
  "device_name": "",
  "alloc_in_same_node": true,
  "global_segment_size": "1GB"
}
```

**配置项说明**

| 配置项 | 类型 | 说明 |
|--------|------|------|
| metadata_server | string | 元数据服务类型，如 `"P2PHANDSHAKE"` |
| protocol | string | 协议，如 `"ascend"` |
| device_name | string | 设备名，可为空字符串 |
| alloc_in_same_node | bool | 是否在同节点分配 |
| global_segment_size | string | 全局分段大小，如 `"1GB"` |

### tls_config（可选）

`motor_deploy_config.tls_config` 与仓内 `user_config.json` 结构一致，下含四类 TLS 配置对象，每类结构相同。如需生成证书，可参考 [examples/enable_tls/README.md](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/features/http/enable_tls/README.md) 中的生成证书部分。

**配置示例**（完整结构，填入 `motor_deploy_config` 中即可）：

```json
"tls_config": {
  "infer_tls_config": {
    "tls_enable": false,
    "ca_file": "/usr/local/Ascend/pyMotor/conf/security/infer/ca.pem",
    "cert_file": "/usr/local/Ascend/pyMotor/conf/security/infer/cert.pem",
    "key_file": "/usr/local/Ascend/pyMotor/conf/security/infer/nopass.cert.key.pem",
    "passwd_file": "/usr/local/Ascend/pyMotor/conf/security/infer/key_pwd.txt",
    "tls_crl": ""
  },
  "mgmt_tls_config": {
    "tls_enable": false,
    "ca_file": "/usr/local/Ascend/pyMotor/conf/security/mgmt/ca.pem",
    "cert_file": "/usr/local/Ascend/pyMotor/conf/security/mgmt/cert.pem",
    "key_file": "/usr/local/Ascend/pyMotor/conf/security/mgmt/nopass.cert.key.pem",
    "passwd_file": "/usr/local/Ascend/pyMotor/conf/security/mgmt/key_pwd.txt",
    "tls_crl": ""
  },
  "etcd_tls_config": {
    "tls_enable": false,
    "ca_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/ca.pem",
    "cert_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/cert.pem",
    "key_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/nopass.cert.key.pem",
    "passwd_file": "/usr/local/Ascend/pyMotor/conf/security/etcd/key_pwd.txt",
    "tls_crl": ""
  },
  "grpc_tls_config": {
    "tls_enable": false,
    "ca_file": "/usr/local/Ascend/pyMotor/conf/security/clusterd/ca.pem",
    "cert_file": "/usr/local/Ascend/pyMotor/conf/security/clusterd/cert.pem",
    "key_file": "/usr/local/Ascend/pyMotor/conf/security/clusterd/nopass.cert.key.pem",
    "passwd_file": "/usr/local/Ascend/pyMotor/conf/security/clusterd/key_pwd.txt",
    "tls_crl": ""
  }
}
```

**配置项说明**：

- **infer_tls_config**：推理面 TLS
- **mgmt_tls_config**：管控面 TLS
- **etcd_tls_config**：etcd TLS
- **grpc_tls_config**：集群通信 gRPC TLS（证书路径通常为 `.../security/clusterd/`）

每类 TLS 配置对象的字段如下：

| 配置项 | 类型 | 说明 |
|--------|------|------|
| tls_enable | bool | 是否开启 TLS；为 false 时表示关闭，无需准备证书 |
| ca_file | string | CA 证书路径 |
| cert_file | string | 服务端证书路径 |
| key_file | string | 私钥路径 |
| passwd_file | string | 私钥密码文件路径 |
| tls_crl | string | 证书吊销列表路径 |

生产环境建议开启 TLS 以保障通信安全。

## 配置 `env.json`

`env.json` 用于为各组件注入环境变量，其路径由 `user_config.json` 中的 `motor_deploy_config.env_path` 指定（如 `./conf/env.json`）。`deploy.py` 会读取该文件，并将对应段落写入 `boot_helper/boot.sh` 中的 `set_common_env`、`set_controller_env`、`set_coordinator_env`、`set_prefill_env`、`set_decode_env`、`set_kv_pool_env` 等函数，供容器启动时 source 执行。

**配置示例**（典型结构）：

```json
{
  "version": "2.0.0",
  "motor_common_env": {
    "CANN_INSTALL_PATH": "/usr/local/Ascend"
  },
  "motor_controller_env": {},
  "motor_coordinator_env": {},
  "motor_engine_prefill_env": {
    "HCCL_BUFFSIZE": 200,
    "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
    "HCCL_OP_EXPANSION_MODE": "AIV",
    "OMP_PROC_BIND": "false",
    "OMP_NUM_THREADS": 100,
    "ASCEND_BUFFER_POOL": "4:8"
  },
  "motor_engine_decode_env": {
    "HCCL_BUFFSIZE": 200,
    "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
    "HCCL_OP_EXPANSION_MODE": "AIV",
    "OMP_PROC_BIND": "false",
    "OMP_NUM_THREADS": 100,
    "ASCEND_BUFFER_POOL": "4:8"
  },
  "motor_kv_cache_pool_env": {}
}
```

**配置项说明**：

- **motor_common_env**：所有组件共用（如 CANN 安装路径）。
- **motor_engine_prefill_env** / **motor_engine_decode_env**：P/D 实例的 NPU、HCCL、OMP 等环境变量，可按机型与模型进行调优。

常用环境变量（P/D 引擎，与上述配置示例对应）如下：

| 变量名 | 说明 | 默认取值 |
|--------|------|----------|
| CANN_INSTALL_PATH | CANN 安装路径（motor_common_env） | /usr/local/Ascend |
| HCCL_BUFFSIZE | HCCL 缓冲区大小 | 200 |
| PYTORCH_NPU_ALLOC_CONF | NPU 显存分配策略 | expandable_segments:True |
| HCCL_OP_EXPANSION_MODE | 通信算法编排展开位置 | AIV |
| OMP_PROC_BIND | OpenMP 线程绑定 | false |
| OMP_NUM_THREADS | OpenMP 线程数 | 100 |
| ASCEND_BUFFER_POOL | 缓冲区池配置 | 4:8 |

修改后保存即可，无需手动修改 `boot.sh`；下次执行 `deploy.py` 时会重新生成并注入上述环境变量。

## 执行部署（`deploy.py`）

### 安全与权限说明

- 部署脚本建议由 **K8s 集群管理员** 执行，以避免脚本或配置被篡改引发任意命令执行或容器逃逸风险。
- 须严格管控 MindIE 相关 ConfigMap（如 motor-config）的写、更新与删除权限；建议安装目录权限设为 750、文件权限设为 640，并配合 Namespace 与 RBAC 进行约束。
- 修改 deployment 模板时，请使用安全镜像（非 root、安全 Pod 上下文），并挂载安全路径（避免软链接、系统危险路径及业务敏感路径）。

>[!NOTE]说明
>当请求发送速度高于处理速度时，Coordinator 会缓存未处理的请求，导致内存占用上升，可能因达到内存上限而被终止。此时需适当增大 `examples/deployer/yaml_template/coordinator_template.yaml`（multi_deployment 场景）或 `examples/deployer/yaml_template/infer_service_template.yaml`（CRD 场景）中 `requests` 和 `limits` 下的 `memory` 参数。
>
>- `requests.memory`：Coordinator 运行所需最小内存。
>- `limits.memory`：Coordinator 可用内存上限。
>
>为保证 Coordinator 稳定获得上述内存，建议将两者设为相同值。按请求积压规模建议档位如下：
>
>- 约 1 万条积压：`4Gi`
>- 约 2 万条积压：`8Gi`
>- 约 4 万条积压：`16Gi`
>- 约 9 万条积压：`24Gi`

### 前置条件

- 已安装Kubernetes、MindCluster、NPU驱动和固件和镜像和权重路径的配置。
- 已创建与 `job_id` 同名的命名空间，例如：

  ```bash
  kubectl create namespace mindie-motor
  ```

- 宿主机上模型权重已放在 `user_config.json` 的 `weight_mount_path` 指定路径（如 `/mnt/weight/`）。

### 部署命令

在 **examples/deployer** 目录下执行，支持两种指定配置的方式：

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

主要参数：

- `--config_dir` 或 `--dir`：配置文件所在目录，目录下需包含 `user_config.json` 和 `env.json`（推荐，如 `../infer_engines/vllm`）
- `--user_config_path` 或 `--config`：用户配置文件路径，需与 `--env` 同时指定
- `--env_config_path` 或 `--env`：环境配置文件路径，需与 `--config` 同时指定
- `--update_config`：仅刷新 ConfigMap（motor-config），不重新 apply Deployment
- `--update_instance_num`：根据配置扩缩容实例数量
- `--auto_log_collect`：部署完成后自动启动日志收集（`name_space` 会自动设置为 `job_id`）

`--update_config` 仅支持修改**白名单内**的配置项；脚本会将当前 `user_config.json` 与集群中已部署的 `motor-config` 基线配置进行逐项比对，若存在非白名单字段变更将直接报错并拒绝更新。当前白名单范围主要包括以下几类：

- 各模块日志等级：`motor_controller_config.logging_config.log_level`、`motor_coordinator_config.logging_config.log_level`、`motor_nodemanger_config.logging_config.log_level`
- Controller 可观测配置：`motor_controller_config.observability_config`
- Coordinator 异常处理配置：`motor_coordinator_config.exception_config`
- Coordinator 超时配置：`motor_coordinator_config.timeout_config`

具体允许修改到哪些字段，以及白名单配置块下哪些新增字段会被拦截，请参考 [`--update_config` 白名单说明](./update_config_whitelist.md)。

除白名单外，其他配置项（包括部署资源、实例数量、模型配置、TLS、主备、限流等）均不支持通过 `--update_config` 修改。如需扩缩容，请使用 `--update_instance_num`，如需变更其他配置，请按正常部署流程重新执行部署。

示例：

```bash
# 使用配置目录（推荐）
python3 deploy.py --config_dir ../infer_engines/vllm

# 单独指定配置文件（--user_config_path 与 --env_config_path 必须同时指定）
python3 deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json

# 仅更新配置
python3 deploy.py --config_dir ../infer_engines/vllm --update_config

# 扩缩容实例
python3 deploy.py --config_dir ../infer_engines/vllm --update_instance_num
```

更多使用方法详见 `examples/deployer/README.md`。

`deploy.py` 会依次执行以下步骤：

1. 读取 `user_config.json` 和 `env.json`。
2. 根据 `motor_deploy_config`，生成 Controller、Coordinator 及各 P/D 的 Deployment YAML 到 `output_yamls/`。
3. 将 `env.json` 中的环境变量写入 `startup/` 目录下各脚本中的对应函数。
4. 对生成的 YAML 执行 `kubectl apply -f ... -n <job_id>`， 拉起任务pod。

### 查看集群状态与日志

查看 Pod 列表（将 `<job_id>` 换为实际命名空间，如 mindie-motor）：

```bash
kubectl get pods -n <job_id>
```

回显中各 Pod/Deployment 的命名可能随模板与 `engine_type` 变化，不建议仅依赖固定前缀判断角色。可按以下方式识别：

- **Controller / Coordinator**：查看 `output/deployment/` 生成的 YAML（或 `kubectl get deployments -n <job_id>`），以实际 Deployment/Service 名称为准。
- **P/D Server**：当前 `engine_type` 支持 `vllm`、`mindie-llm`、`sglang` 三种类型。以 `engine_type=vllm` 为例，`deploy.py` 会生成形如 `vllm-p0`、`vllm-d0` 的 Deployment（index 递增）；其余类型同理，Deployment 基础名随 `engine_type` 变化。

Pod 状态为 Running 仅表示已成功调度并启动，是否业务就绪仍需结合日志进一步确认。

**查看日志与排查问题**：

可以通过以下三种方式查看集群日志或进行排查：

> **提示**：如果在执行 `deploy.py` 时使用了 `--auto_log_collect` 参数，部署完成后日志采集已在后台自动启动，无需再手动执行 `show_log.sh`。

**方式一：推荐使用 `show_log.sh` 工具进行统一采集**

1. **配置 `log_config.ini`（必做）**：`show_log.sh` 会在 `log_collect` 目录下启动 `log_monitor.py`，该脚本从同目录的 `examples/deployer/log_collect/log_config.ini` 读取 `[LogSetting]` 配置。仓库中 **`name_space` 默认为空**，运行前**必须**将其填写为与实际部署 workload 所在的 **Kubernetes 命名空间** 一致（须与本文中查看 Pod、执行 `kubectl` 时使用的 **`<job_id>`** 相同）。若未填写，`show_log.sh` 会在**当前终端**（stderr）输出英文错误并**立即退出**，不会启动 `log_monitor.py`（详见《常见问题》第 6 节）。若已填写但与真实命名空间不一致，`kubectl get pods` / `kubectl logs` 会指向错误命名空间，导致采集不到目标 Pod 日志或内容为空。同文件中还可按需调整日志输出目录 `out_path`、单文件上限 `max_log_size`、轮转备份数 `backup_count`（单位与含义以脚本为准）。

2. **执行采集脚本**：在 `examples/deployer` 目录下执行 `show_log.sh` 获取/查看日志：

   ```bash
   cd examples/deployer
   bash show_log.sh
   ```

3. **关于采集的日志文件**：
   - **存储目录**：日志会收集在 `log_config.ini` 配置的 `out_path` 下，以**当前系统本地时间**生成时间戳文件夹（例如 `20260328_102430`）。
   - **文件命名规则**：单个 Pod 的日志文件命名格式为 `<pod_name>_<node_name>_<retry_count>.log`，直接体现了该 Pod 所在的 Kubernetes 节点名称（例如 `vllm-0-controller-0-xxxx_node01_0.log`），方便跨节点问题排查。
   - **日志轮转（自动拆分）机制**：当采集日志大小超过 `log_config.ini` 中配置的 `max_log_size`（默认 10MB）时，触发轮转机制，将旧文件重命名并追加 `.1`、`.2` 等后缀（例如 `xxx_0.log.1`、`xxx_0.log.2` 代表更早的日志段），并生成一个新的 `.log` 继续记录。它们均属于同一个 Pod 单次生命周期的日志流。轮转保存的备份数受 `backup_count` 限制。

**方式二：兜底方案（查看单 Pod 日志）**

如果您只想快速查看某个特定 Pod 的标准输出，可以直接使用 kubectl 命令：

```bash
kubectl logs <pod_name> -n <job_id>
```

*例如：`kubectl logs mindie-server-p0-xxx -n mindie-motor`*

**方式三：进入容器内部排查**

如果需要进入容器内部查看内部状态或执行调试命令，可执行：

```bash
kubectl exec -it <pod_name> -n <job_id> -- bash
```

**确认 P/D 与 Pod 的对应关系**：

结合 Pod 列表中的 IP 与名称，即可区分哪些 Pod 对应 P 实例、哪些对应 D 实例。确认各组件无报错且推理服务就绪后，可按下一节发送推理请求进行验证。

## 发送推理请求

服务就绪后，可通过发送推理请求测试服务是否拉起正常，以 `/v1/chat/completions` 接口为例（更多API接口可参考[业务接口](../../../api_reference/service_interface.md)。推理入口为 Coordinator 对外暴露的端口（默认 31015）。请将 `<IP>` 替换为实际访问地址（如 Coordinator Service 的 NodePort/LoadBalancer 或宿主机 IP）。若已开启 TLS（见 4.6），请使用 `https` 并配置客户端证书。

```bash
curl -X POST http://<IP>:31015/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3",
    "prompt": "who are you?",
    "max_tokens": 36,
    "stream": true
  }'
```

若返回 `{"detail":"Service is not available"}`，表示服务尚未就绪，可稍后重试。若返回流式 JSON，则说明推理正常。

>[!NOTE]说明
>HTTP 协议存在安全风险，建议您使用 HTTPS 安全协议。开启证书与 TLS 配置请参考 [4.6 tls_config（可选）](#tls_config可选)。

## 卸载

在 **examples/deployer** 目录下执行 `delete.sh`，将删除当前 `job_id` 对应命名空间下的 `K8S ConfigMap` 以及 `output_yamls` 中已 apply 的 YAML，并清理 `startup/` 中由 `deploy.py` 注入的环境变量函数。

```bash
cd examples/deployer
bash delete.sh <命名空间>
```

例如：`bash delete.sh mindie-motor`

>[!NOTE]说明
>
>- 命名空间请根据实际创建的名称替换（如 `job_id` 对应的命名空间）。
>- `delete.sh` 会删除命名空间下的 motor-config ConfigMap 以及 `output_yamls` 下已 apply 的 YAML，并清理 `startup/` 中由 `deploy.py` 注入的各 `set_*_env` 函数。卸载脚本**必须在 examples/deployer 目录下**执行，否则无法正确找到 `output_yamls` 路径而报错。

## 故障排查与注意事项

- **服务未就绪**：若推理接口返回 `{"detail":"Service is not available"}`，多为 P/D 或 Coordinator 尚未完全就绪，可等待一段时间后重试，并查看各 Pod 日志确认无启动错误。
- **镜像与权重**：确保 `image_name` 在集群内可正常拉取；`weight_mount_path` 在宿主机上存在。
- **部署失败**：若部署失败，可先按第 8 节卸载集群，排查并修改配置后重新部署。
- **加载权重超时**：当前依赖`vllm v0.13.0`版本，该版本权重加载超时时间不能通过环境变量或者配置修改，导致加载权重超过10分钟会报`timeout`，并不影响程序运行，`vllm v0.14.0`版本会修复这个问题。
- **实例重调度约束**：*实例重调度*能力依赖mindcluster，如果P/D实例有多个POD，直接删除其中一个POD，不会进入mindcluster的故障处理流程，所以不会触发*实例重调度*
- **Prefix Cache特性（默认开启）对性能测试的影响**：Prefix Cache用于在多条请求存在相同prompt前缀时，复用已计算好的KV Cache，从而提升服务的推理性能。若期望获取推理服务的基线性能数据（非前缀缓存加速），应在配置中关闭Prefix Cache特性，可在user_config.json配置文件的"engine_config"中增加 "no-enable-prefix-caching": true（vLLM推理引擎） 或 "disable_radix_cache": true （SGLang推理引擎）字段。
