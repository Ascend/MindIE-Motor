# 手动扩缩容用户手册

## 适用范围

本手册适用于 MindIE Motor 的手动扩缩容流程，通过修改 `user_config.json` 中的实例数并执行相应命令完成扩缩容。

## 前置条件

- 已成功完成至少一次全量部署（集群内会存在 ConfigMap `motor-config`，其中含当前已部署的 user_config，作为基线）。
- 具备 `kubectl` 权限。

## 配置说明

扩缩容时仅允许修改以下字段：

- `motor_deploy_config.p_instances_num`（PD 分离）
- `motor_deploy_config.d_instances_num`（PD 分离）
- `motor_deploy_config.hybrid_instances_num`（PD 混部）

上述实例数须**大于 0 且不超过 16**，否则部署或扩缩容时会报错。

## 操作步骤

### 首次部署

在 `examples/deployer` 目录下执行全量部署：

```bash
cd examples/deployer
# PD 分离：方式一，指定配置目录（推荐）
python3 deploy.py --config_dir ../infer_engines/vllm

# PD 混部：方式一，指定配置目录（推荐）
python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid

# PD 分离：方式二，单独指定配置文件
python3 deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json

# PD 混部：方式二，单独指定配置文件
python3 deploy.py --user_config_path ../infer_engines/vllm/pd_hybrid/user_config.json --env_config_path ../infer_engines/vllm/pd_hybrid/env.json
```

完成后：

- 集群中会创建/更新 ConfigMap `motor-config`（内容来自当前输入的 `user_config.json`），作为后续扩缩容与刷新的基线。
- `output/deployment/` 下会生成各服务 YAML。

### 扩缩容

1. 修改 `user_config.json` 中的实例数：
   - PD 分离：`p_instances_num`、`d_instances_num`
   - PD 混部（CRD 默认）：`hybrid_instances_num`
2. 在 `examples/deployer` 目录下执行扩缩容命令：

```bash
cd examples/deployer
# PD 分离
python3 deploy.py --config_dir ../infer_engines/vllm --update_instance_num

# PD 混部
python3 deploy.py --config_dir ../infer_engines/vllm/pd_hybrid --update_instance_num
```

若使用单独指定配置文件方式部署，扩缩容时需同样指定 `--user_config_path` 和 `--env_config_path`。

说明：

- 基线来自集群 ConfigMap（motor-config），与当前输入对比，仅允许实例数变化。
- 扩容：仅对新增实例 index 执行 `kubectl apply`，已运行实例不会被重拉。
- 缩容：从 **index 大的实例开始** 依次删除，并同步删除 `output/deployment/` 下对应的 engine YAML 文件。
- 成功后 ConfigMap 会更新为当前输入的 `user_config.json`。

## 常见问题

### 报错：ConfigMap motor-config not found or has no user_config in cluster

表示尚未进行过全量部署，或对应 namespace 下没有 motor-config。请先在 `examples/deployer` 目录下执行全量部署，例如：

```bash
cd examples/deployer
python3 deploy.py --config_dir ../infer_engines/vllm
```

### 报错：user_config changes detected beyond instance numbers

表示除实例数外还修改了其他配置。请仅修改 `p_instances_num`/`d_instances_num`/`hybrid_instances_num`

PD 混部完整部署流程和配置说明请参考 [PD 混部服务部署](../deployment/k8s/pd_aggregation_deployment.md)。

## 注意事项

- 扩缩容只影响 engine 实例；controller/coordinator 不会在扩缩容路径中更新。
- 如需修改镜像、挂载路径等非实例数配置，请进行重新部署。
- 缩容会从高 index 开始删除实例，并删除 output 下对应 YAML 文件。
- 已部署配置的基线为集群内 ConfigMap（motor-config）中的 user_config。
- Prefix Cache特性默认开启，该特性会复用已计算好的KV Cache，用于提高推理性能。新扩容的实例没有KV Cache缓存，因此该实例的推理性能可能出现小幅度劣化并在一段时间后恢复。
