# Deployer 部署工具

本目录包含 PD disaggregation 服务的部署脚本与配置模板，用于在集群中部署 Controller、Coordinator、Engine 等组件。

## 使用说明

本目录仅提供部署所需的脚本与示例配置。**完整的部署流程、环境要求、配置说明及故障排查请参考以下文档：**

👉 **[PD Disaggregation 完整部署指南](../../docs/zh/user_guide/deployment/k8s/pd_disaggregation_deployment.md)**

建议在正式部署前先阅读上述文档，按文档完成环境准备与配置后再使用本目录中的工具进行部署。

## deploy.py 使用方法

### 参数说明

Motor**服务部署**参数说明

| 参数 | 简写 | 说明 |
|------|------|------|
| `--config_dir` | `--dir` | 配置文件所在目录，目录下需包含 `user_config.json` 和 `env.json` |
| `--user_config_path` | `--config` | 用户配置文件路径，与 `--env` 必须同时指定 |
| `--env_config_path` | `--env` | 环境配置文件路径，与 `--config` 必须同时指定 |
| `--update_config` | - | 仅更新 ConfigMap，不重新部署 |
| `--update_instance_num` | - | 根据配置扩缩容实例数量 |
| `--dry-run` | - | 仅生成 YAML 文件，不执行 kubectl apply |
| `--auto_log_collect` | - | 部署完成后自动启动日志采集 |
| `--nostep` | - | 部署完成后不显示服务启动进度条 |

Motor**配置文件自动生成**参数说明

| 参数 | 简写 | 说明 |
|------|------|------|
| `--mode` | - | `deploy`（默认）或 `general_config`（从 vLLM 脚本生成配置） |
| `--deploy-scenario` | - | `general_config` 必填：`hybrid` / `separate` |
| `--hardware-type` | - | `general_config` 必填：`A2` / `A3` |
| `--weight-path` | - | `general_config` 可选：权重挂载路径 |
| `--image-name` | - | `general_config` 可选：镜像名称 |

### 使用方式

#### 方式零：交互式 TUI 模式

```bash
python deploy.py
```

**不带任何参数**启动 `deploy.py` 会进入交互式终端 UI（TUI），提供可视化的服务管理界面：

| 操作 | 按键 | 说明 |
|------|------|------|
| 部署服务 | `R` | 输入配置目录路径，执行部署 |
| 显示启动进度 | `P` | 打开/关闭内嵌进度条，实时查看各 Engine Pod 启动状态 |
| 日志采集 | `L` | 启动/重启日志采集 |
| 更新配置 | `U` | 更新集群 ConfigMap |
| 删除服务 | `D` | 输入 namespace 并确认后删除所有服务 |
| 退出 | `Q` | 退出 TUI |

**交互方式：**

- `↑` `↓` 或 vim 风格 `j` `k` 导航菜单
- `Enter` 选中当前高亮项
- 也可直接按菜单项的字母键（`[R]` `[P]` `[L]` `[U]` `[D]` `[Q]`）快速触发

> 已部署状态下，进度监控（`P`）会自动发现 Running 的 vLLM Pod，通过尾随 `kubectl logs` 解析启动日志，在菜单下方绘制每个 Pod 的实时进度条，并展示 Pod 就绪状态（`kubectl get pods`）。

#### 方式一：指定配置目录（推荐）

```bash
python deploy.py --config_dir ../infer_engines/vllm
```

程序会自动从指定目录下读取 `user_config.json` 和 `env.json`。

#### 方式二：单独指定配置文件

```bash
python deploy.py --config ../infer_engines/vllm/user_config.json --env ../infer_engines/vllm/env.json
```

#### 方式三：混合使用

```bash
python deploy.py --config_dir ../infer_engines/vllm --config /path/to/custom_user_config.json --env /path/to/custom_env.json
```

当同时指定 `--config_dir` 和 `--config`/`--env` 时，以 `--config` 和 `--env` 为准。

#### 方式四：基于vllm部署脚本生成Motor全量配置文件

使用方式请参阅[Motor配置自动生成指导](../infer_engines/vllm/models/README.md)。

### 其他操作

#### 更新配置

```bash
python deploy.py --config_dir ../infer_engines/vllm --update_config
```

仅更新集群中的 ConfigMap，不重新部署服务。

#### 扩缩容实例

```bash
python deploy.py --config_dir ../infer_engines/vllm --update_instance_num
```

根据 `user_config.json` 中的 `p_instances_num` 和 `d_instances_num` 进行实例扩缩容。

## 配置文件说明

配置文件位于 `examples/infer_engines/` 目录下，根据引擎类型和模型选择对应的配置：

```bash
examples/infer_engines/
├── vllm/                    # vLLM 引擎配置
│   ├── user_config.json     # 快速启动用户配置
│   ├── env.json             # 快速启动环境变量配置
│   └── models/              # 特定模型配置
│       └── deepseek/
│           └── v3_1/
│               ├── user_config.json
│               └── env_v3_1_A2_EP32.json
└── ...
```

### user_config.json

包含服务部署配置，主要字段：

- `motor_deploy_config`: 部署相关配置（实例数、镜像、部署模式等）
- `motor_controller_config`: Controller 组件配置
- `motor_coordinator_config`: Coordinator 组件配置
- `motor_engine_prefill_config`: Prefill 引擎配置
- `motor_engine_decode_config`: Decode 引擎配置
- `kv_cache_store_config`: KV 缓存池配置

### env.json

包含环境变量配置，主要字段：

- `motor_common_env`: 公共环境变量
- `motor_controller_env`: Controller 环境变量
- `motor_coordinator_env`: Coordinator 环境变量
- `motor_engine_prefill_env`: Prefill 引擎环境变量
- `motor_engine_decode_env`: Decode 引擎环境变量

## 参考示例

如需具体模型的拉起与配置示例，可参考仓库中的 **examples/infer_engines/** 目录：

👉 **[examples/infer_engines 目录](../infer_engines)**

该目录下提供多种场景的参考配置与脚本，便于按实际模型进行部署与调优。

## Motor 自动管理的 vLLM 原生参数

以下 vLLM 原生 CLI 参数由 PyMotor 在注册、组装、拉起过程中自动推导和注入，**无需在 `engine_config` 中手动指定**：

| 参数 | 自动管理方式 |
|------|-------------|
| `data-parallel-address` | Controller 根据组装结果确定 master DP 节点 IP，通过 `StartCmdMsg.master_dp_ip` → `--master-dp-ip` 传入 EngineServer |
| `data-parallel-rank` | 由 Endpoint ID 决定，NodeManager Daemon 以 `--dp-rank` 传入 EngineServer |
| `node-rank` | Controller 按 NodeManager 注册先后顺序分配（先注册 = 主节点 rank 0），通过 `StartCmdMsg.node_rank` → `--node-rank` 传入 EngineServer |
| `master-addr` | EngineServer 在检测到跨节点 PCP 模式（`nnodes > 1` 且 `master-port` 存在）时，自动将 `master-dp-ip` 作为 `--master-addr` 注入 vLLM |
| `headless` | EngineServer 在跨节点 PCP 模式下，对 `node-rank != 0` 的从节点自动追加 `--headless` |

> **注意**：跨节点 PCP 场景下，用户仅需在 `engine_config` 中配置 `nnodes` 和 `master-port`，其余参数由 Motor 自动处理。

CLI 参数与 `engine_config` 键名的完整映射关系详见：

👉 **[CLI 参数与 engine_config 映射指南](../../docs/zh/user_guide/operations/cli_to_engine_config_guide.md)**
