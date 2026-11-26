# 云原生部署

该目录提供一套面向 `examples/deployer/deploy.py` 的云原生部署方式。

它的目标是让部署过程不再依赖人工登录宿主机，也不依赖宿主机上的本地配置文件，而是能够被云平台直接触发。

## 背景与必要性

项目原始部署方式默认由运维人员登录某个 Kubernetes 节点，手工执行 `examples/deployer/deploy.py`，并从本地目录读取配置文件。

这种方式适合开发调试和人工验证，但在云平台场景下会遇到明显问题：

- 集群通常是多租户的，同一集群内会并行部署多个推理服务，并通过 namespace 做隔离。
- 云平台用户不应被要求拥有节点登录权限。
- 部署请求通常来自平台控制面或 API，而不是人工在宿主机执行脚本。
- 交付物应该是容器镜像和 Kubernetes 清单，而不是依赖宿主机本地状态。
- 扩缩容、清理等动作也应该被表达为 Kubernetes Job，便于平台统一审计、重试和观测。

因此，这里把现有的部署逻辑封装成了适合容器执行的形式。对云平台而言，只需要两步：

1. 准备一个包含 `user_config.json` 和 `env.json` 的 ConfigMap。
2. 创建对应的 deploy / scale / cleanup Job。

底层真正执行部署的仍然是现有的 `examples/deployer/deploy.py`，这样可以保持与当前人工部署行为一致，同时把入口改造成云原生形态。

## 构建镜像

在仓库根目录执行：

```bash
docker build -f examples/cloud_native_deploy/Dockerfile -t mindie-motor-deployer:latest .
```

也可以使用仓库根目录的 Makefile：

```bash
make buildx-cloud-native-deployer TAG=latest
```

## 准备配置

编辑 `k8s/configmap-user-config.yaml`，写入实际使用的 `user_config.json` 和 `env.json`。

生产部署与扩缩容前，须在 `user_config.json` 中配置推理 TLS 和管理面 API Key，字段位置见
[安全预检说明](../deployer/README.md#安全预检)。默认保留安全预检，并移除 Helm values 或 Job 中
已保存的 `--skip-check`。该参数仅限隔离的本地或测试环境验证，生产环境不得使用。

可选配置项：

- `motor_deploy_config.scheduling_queue`：指定 Volcano 队列。Motor 将其写入 RBG 注解
  `rbg.workloads.x-k8s.io/group-gang-scheduling-volcano-queue`，与 RBG v0.8.0 读取的键一致，
  启用组调度时作用于 RBG 组队列。使用前需确认队列存在且 `scheduler_name` 为 `volcano`；RBG 模式下
  更改队列需要清理后重新部署。
- `motor_deploy_config.coordinator_service_name`：覆盖默认的对外推理 Service 名称。
- `motor_deploy_config.coordinator_infer_node_port`：覆盖对外推理 Service 的 NodePort。配置 `"-"` 时由 Kubernetes 自动分配，字段缺省时保留模板中的 `nodePort`，当前模板默认值为 `31015`。
- `motor_deploy_config.encode_node_selector`、`motor_deploy_config.prefill_node_selector` 和 `motor_deploy_config.decode_node_selector`：分别控制 Encode / Prefill / Decode 的节点调度标签。
- `motor_deploy_config.controller_node_selector` 和 `motor_deploy_config.coordinator_node_selector`：分别控制 Controller / Coordinator 的节点调度标签。
- `motor_deploy_config.kv_pool_node_selector` 和 `motor_deploy_config.kv_conductor_node_selector`：分别控制 KV Pool / KV Conductor 的节点调度标签。

## 使用 Helm 部署

社区版本提供 `helm/` Chart，用于直接创建 ConfigMap、ServiceAccount、RBAC 和 deploy / scale / cleanup Job。运行时服务资源仍由 Job 中的 `examples/deployer/deploy.py` 生成。

先复制并修改默认配置，至少设置 deployer 镜像、Motor 运行镜像、模型路径和实际集群参数：

```bash
cp examples/cloud_native_deploy/helm/values.yaml /tmp/motor-values.yaml
```

执行部署：

```bash
helm upgrade --install motor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --create-namespace \
  --values /tmp/motor-values.yaml \
  --set operation=deploy
```

扩缩容时修改 values 中的 `p_instances_num` 和/或 `d_instances_num`，然后执行：

```bash
helm upgrade motor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --values /tmp/motor-values.yaml \
  --set operation=scale
```

清理运行时资源并卸载 Chart：

```bash
helm upgrade motor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --values /tmp/motor-values.yaml \
  --set operation=cleanup

helm uninstall motor --namespace mindie
```

详细参数见 [Helm Chart 使用说明](helm/README.zh.md)。

## 使用 RBG 部署

使用前需确认 RBG 实现及示例文件已合入，且 deployer 镜像基于包含该实现的代码版本构建。

仓库提供 RBG（RoleBasedGroup）形态的示例配置：

- Helm：`helm/values-rbg.yaml`。它只覆盖 `operation` 与 `userConfig`，镜像、`envConfig`、Job 等仍取 Chart
  默认值；其中 `userConfig` 与默认示例的差异仅为 `deploy_mode` 为 `rbg` 并增加了 `rbg_config` 段。
- 纯 kubectl：`k8s/configmap-user-config-rbg.yaml`，内容与上面的 `userConfig` 一致，用于替换默认的
  `k8s/configmap-user-config.yaml`。

部署前需安装并运行 RBG v0.8.0 控制器及配套 CRD。默认启用组调度，需安装 Volcano，且 RBG 控制器
使用 `--scheduler-name=volcano`（RBG Helm 配置为 `controller.features.gangScheduling.schedulerName=volcano`）。
控制器默认选择 `scheduler-plugins`，仅设置 Motor 的 `rbg_config.scheduler_name=volcano` 不会修改控制器配置。
完整前置条件见 [RBG 配置说明](../deployer/lib/rbg/README.md#前置条件)。

Volcano 和 RBG v0.8.0 控制器的安装命令、版本要求和验证步骤见
[RBG 前置组件安装](../deployer/lib/rbg/README.md#前置组件安装)。以下命令从 Motor 源码根目录执行，
`RBG_SOURCE_DIR` 使用尚未存在的绝对目录，安装过程不切换当前目录：

```bash
# Volcano：按 MindCluster Volcano 安装指导准备软件包和镜像，然后执行
kubectl apply -f volcano-v{version}.yaml

# RBG v0.8.0
RBG_SOURCE_DIR=/tmp/rbg-v0.8.0
git clone --branch v0.8.0 --single-branch https://github.com/sgl-project/rbg.git "$RBG_SOURCE_DIR"
helm upgrade --install rbgs "$RBG_SOURCE_DIR/deploy/helm/rbgs" \
  --create-namespace --namespace rbgs-system \
  --set controller.features.gangScheduling.schedulerName=volcano --wait
```

更完整的安装包准备、单机 `useClusterInfoManager=false` 配置和验证命令见上述链接。

同一 namespace 只部署一套 RBG。实例联动重建由 RBG 的 `restartPolicy` 实现；RAS（`ras_monitor`）
仍通过旧 `delete.sh` 删除并重新部署整套服务，不具备 RBG 清理流程的数据 PVC 保留语义，
不推荐作为 RBG 的实例恢复入口。`kubectl delete pod` 只补建单个 Pod，也不能替代整实例重建。
详见 [ras_monitor 说明](../features/fault_tolerance/ras_monitor/readme.md)。

复制 RBG values 后，仍需设置 deployer 镜像、`userConfig` 中的 Motor 运行镜像、模型路径、`job_id`、
实例数、NPU 数量、硬件类型和节点标签，并按实际环境填写 `envConfig`。示例中的空运行镜像不能直接用于部署。

RBG 示例使用 `hardware_type: "800I_A3"`，P/D 均为每实例单 Pod、每 Pod 16 NPU。
该 A3 示例不是 A5 DeepSeek-V4.1-Flash 配置。使用 A5 时应采用
`model_configs/vllm/models/deepseek_v4.1_flash/A5/`：`hardware_type: Ascend950`、P/D 1/1、
每实例单 Pod、每 Pod 8 NPU、P/D 的 DP×TP×PP 为 8×1×1、KV memcache inprocess、P/D
`HCCL_OP_EXPANSION_MODE=CCU_SCHED`，不能只把 A3 示例的 `hardware_type` 改成 A5。
使用 A2/A5 或其他模型时，应将内嵌 JSON 换为对应模型、硬件的配置，再按
[A2/A3/A5 配置调整](../deployer/lib/rbg/README.md#a2a3a5-配置调整)与
[A5 参数表](../deployer/lib/rbg/README.md#a5-deepseek-v41-flash-部署参数)修改 RBG 字段。
Helm 修改 `userConfig` / `envConfig`；纯 kubectl 修改 `k8s/configmap-user-config-rbg.yaml` 中
`data` 下的 `user_config.json` / `env.json`。当前适配器不支持 `*-sp` 超节点亲和，默认组调度也不支持
活动角色使用不同的硬件调度策略组合。MindCluster 支持 A3/A5 超节点调度，上述限制属于当前 Motor
RBG 适配器。A3 的 `sp-block` 定义逻辑超节点块，A5 默认 `chip8-node8` 则是节点内互联策略；
P/D 注解一致不能证明硬件调度已经生效。相同的真实超节点成员 selector 可以限制节点范围，
但不能补齐调度接入。迁移前需检查 [调度规格限制](../deployer/lib/rbg/README.md#调度规格限制)，
并核对 A2 多 Pod 实例的 Gang 计数和 A3 MultiConnector 配置问题。
RBG 还不支持 P2D 和容器快照。[完整 rbg_config 示例](../deployer/lib/rbg/README.md#完整-rbg_config-示例)
说明各字段、默认值和更新限制；资源提交后还需按 [部署后验收](../deployer/lib/rbg/README.md#部署后验收)
确认工作负载就绪和实际推理成功。

部署前须配置推理 TLS 与管理面 API Key，并保留部署和扩缩容 Job 的安全预检：

```bash
cp examples/cloud_native_deploy/helm/values-rbg.yaml /tmp/motor-rbg-values.yaml
# 先按实际环境编辑 /tmp/motor-rbg-values.yaml，再执行部署

helm upgrade --install motor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --create-namespace \
  --values /tmp/motor-rbg-values.yaml \
  --set operation=deploy
```

扩缩容沿用 `--set operation=scale`，默认保留安全预检；
此入口只修改实例数，目标角色须已解除 HPA/ScalingAdapter 管理。
HPA 的启用、保留与关闭见 [RBG 扩缩容说明](../deployer/lib/rbg/README.md#扩缩容)。
清理使用 `--set operation=cleanup`，先处理 HPA，再删除 RBG、等待后代退出，最后清理业务附件与运行配置；
保留数据 PVC 和外部资源，完成后再卸载 Helm release。Helm 的 cleanup Job 会读取
`userConfig.motor_deploy_config.deploy_mode`，在 RBG 模式下自动设置 `MOTOR_DEPLOY_MODE=rbg`，
因此清理时需使用 RBG values 文件（或保留 `deploy_mode: rbg` 的 values）。走纯 kubectl 路径时，需在
`k8s/cleanup-job.yaml` 中设置 `MOTOR_DEPLOY_MODE: "rbg"`；同时在 ConfigMap 中配置
相同的安全字段，生产环境的 `k8s/job.yaml` 与 `k8s/scale-job.yaml` 不携带 `--skip-check`。
`k8s/clusterrole.yaml` 或 Helm RBAC 应与 deployer 镜像使用同一配套 RBG 实现版本，并按
[部署账号 RBAC](../deployer/lib/rbg/README.md#部署账号-rbac)核对完整权限；不能假定所用分支已包含
RBG 和 HPA 规则，也不能仅补 RBG `list`。部署及策略更新前需检查 RBG、CoordinatedPolicy、Adapter、
HPA 和运行时资源的全部所需权限。

RBG 的配置字段、前提条件与限制见 [Deployer 说明](../deployer/README.md) 的「RBG（RoleBasedGroup）模式」。

## 部署前准备与执行

以下命令使用默认配置；RBG 模式将其中的 ConfigMap 文件改为 `configmap-user-config-rbg.yaml`，
并完成上文的镜像、配置和 Job 参数设置。

```bash
kubectl apply -f examples/cloud_native_deploy/k8s/serviceaccount.yaml
kubectl apply -f examples/cloud_native_deploy/k8s/clusterrole.yaml
kubectl apply -f examples/cloud_native_deploy/k8s/clusterrolebinding.yaml
kubectl apply -f examples/cloud_native_deploy/k8s/configmap-user-config.yaml
kubectl apply -f examples/cloud_native_deploy/k8s/job.yaml
```

查看日志：

```bash
kubectl logs job/mindie-motor-deploy -n mindie
```

## 扩缩容

修改所用 ConfigMap（RBG 为 `k8s/configmap-user-config-rbg.yaml`）中的 `p_instances_num` 和/或
`d_instances_num`，重新 apply ConfigMap 后执行；RBG 目标角色须已解除 HPA/ScalingAdapter 管理：

```bash
kubectl apply -f examples/cloud_native_deploy/k8s/scale-job.yaml
kubectl logs job/mindie-motor-scale -n mindie
```

## 清理

cleanup Job 需要目标 namespace。RBG 部署在 apply 前需编辑 `k8s/cleanup-job.yaml`，把
`MOTOR_DEPLOY_MODE` 设置为 `"rbg"`；默认空值会走旧的非 RBG 清理流程。

```yaml
env:
  - name: NAMESPACE
    value: mindie
  - name: MOTOR_DEPLOY_MODE
    value: "rbg"
```

```bash
kubectl apply -f examples/cloud_native_deploy/k8s/cleanup-job.yaml
kubectl logs job/mindie-motor-cleanup -n mindie
```
