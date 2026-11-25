# MindIE Motor 云原生部署 Helm Chart

该 Chart 将 `examples/cloud_native_deploy` 中的部署入口封装为 Helm 操作，支持部署、扩缩容和清理。

Chart 只管理以下资源：

- 包含 `user_config.json` 和 `env.json` 的 ConfigMap。
- deployer 使用的 ServiceAccount、ClusterRole 和 ClusterRoleBinding。
- 执行 `deploy.py` 的 deploy / scale / cleanup Job。

Controller、Coordinator、Engine、KV Pool 和 KV Conductor 等运行时资源仍由 deployer Job 动态生成。

## 准备

1. 构建并推送 deployer 镜像：

   ```bash
   docker build -f examples/cloud_native_deploy/Dockerfile \
     -t example.com/team/mindie-motor-deployer:latest .
   docker push example.com/team/mindie-motor-deployer:latest
   ```

2. 复制 `values.yaml` 并修改：

   ```bash
   cp examples/cloud_native_deploy/helm/values.yaml /tmp/motor-values.yaml
   ```

   必须按实际环境设置：

   - `image.repository` 和 `image.tag`：deployer 镜像。
   - `userConfig` JSON 字符串中的 `motor_deploy_config.image_name`：运行 Controller、Coordinator 和 Engine 的 Motor 镜像。
   - `job_id`、模型路径、实例数、NPU 数量和硬件类型。
   - 按需修改 `userConfig` JSON 字符串中的 `coordinator_infer_node_port` 和各组件 `*_node_selector`。

   生产部署与扩缩容前，还须在 `userConfig` 中配置推理 TLS 和管理面 API Key，具体字段见
   [安全预检说明](../../deployer/README.md#安全预检)。保留安全预检，移除 values / Job 中已保存的
   `--skip-check`；该参数仅限隔离的本地或测试环境验证，生产环境不得使用。

3. RBG（RoleBasedGroup）形态改用 `helm/values-rbg.yaml`。它只覆盖 `operation` 与 `userConfig`，
   镜像、`envConfig`、Job 等仍取 `values.yaml` 中的默认值；`userConfig` 与默认值的差异仅为
   `deploy_mode: rbg` 和 `rbg_config` 段：

   使用前需确认 RBG 实现及示例文件已合入，且 deployer 镜像包含对应实现。

   ```bash
   cp examples/cloud_native_deploy/helm/values-rbg.yaml /tmp/motor-rbg-values.yaml
   ```

   仍需按第 2 步设置 deployer 镜像、Motor 运行镜像、模型与集群参数，并按需覆盖 `envConfig`。
   部署前安装并运行 RBG v0.8.0 控制器及配套 CRD。默认组调度要求安装 Volcano，且 **RBG 控制器**
   设置 `--scheduler-name=volcano`，或在 RBG 自身的 Helm Chart 中设置
   `controller.features.gangScheduling.schedulerName=volcano`；控制器默认值为 `scheduler-plugins`。
   这与 Motor 的 `rbg_config.scheduler_name` 是两个配置，完整前置条件见
   [RBG 配置说明](../../deployer/lib/rbg/README.md#前置条件)。

   Volcano 和 RBG v0.8.0 控制器的安装命令、版本要求和验证步骤见
   [RBG 前置组件安装](../../deployer/lib/rbg/README.md#前置组件安装)。最小步骤如下：

   ```bash
   # Volcano：按 MindCluster Volcano 安装指导准备软件包和镜像，然后执行
   kubectl apply -f volcano-v{version}.yaml

   # RBG v0.8.0
   git clone https://github.com/sgl-project/rbg.git
   cd rbg && git checkout v0.8.0
   helm upgrade --install rbgs deploy/helm/rbgs \
     --create-namespace --namespace rbgs-system \
     --set controller.features.gangScheduling.schedulerName=volcano --wait
   ```

   更完整的安装包准备、单机 `useClusterInfoManager=false` 配置和验证命令见上述链接。

   同一 namespace 只部署一套 RBG。RAS（`ras_monitor`）当前按整实例重建处理；`kubectl delete pod`
   只补建单个 Pod，不能替代整实例重建。详见
   [ras_monitor 说明](../../features/fault_tolerance/ras_monitor/readme.md)。

   该 RBG 示例为 A3（`800I_A3`），P/D 均为每实例单 Pod、每 Pod 16 NPU。
   使用 A2/A5 或其他模型时，在 `userConfig` JSON 字符串中替换相应典配，并配置对应的 `envConfig`，
   具体字段见 [A2/A3/A5 配置调整](../../deployer/lib/rbg/README.md#a2a3a5-配置调整)。
   迁移前需检查 [调度规格限制](../../deployer/lib/rbg/README.md#调度规格限制)：`*-sp` 超节点亲和会被拒绝，
   默认组调度也不支持活动角色使用不同的硬件调度策略组合；该节同时说明 `scheduling_queue` 的注解适配差异。

   部署前须在 `userConfig` 中配置推理 TLS 与管理面 API Key，并保留安全预检，
   具体字段见 [安全预检说明](../../deployer/README.md#安全预检)。
   `--skip-check` 仅限隔离的本地或测试环境验证，生产环境不得使用；其余操作与 `values.yaml` 相同，下文命令中的
   `--values /tmp/motor-values.yaml` 换成 `/tmp/motor-rbg-values.yaml` 即可。

## 部署

```bash
helm upgrade --install motor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --create-namespace \
  --values /tmp/motor-values.yaml \
  --set operation=deploy
```

查看 Job 和日志：

```bash
kubectl get jobs -n mindie
kubectl logs job/motor-mindie-motor-deployer-deploy -n mindie
```

## 扩缩容

修改 `/tmp/motor-values.yaml` 中的实例数后执行：

```bash
helm upgrade motor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --values /tmp/motor-values.yaml \
  --set operation=scale
```

RBG 模式下该入口只修改实例数，目标角色须已解除 HPA/ScalingAdapter 管理；使用 RBG 示例时按上文
替换 values 文件，配置推理 TLS 与管理面 API Key 并保留安全预检。

## 清理

先运行 cleanup Job 删除 deployer 动态创建的资源，再卸载 Helm release：

```bash
# RBG 请将下面的 --values 改为 RBG values 文件，例如 /tmp/motor-rbg-values.yaml
helm upgrade motor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --values /tmp/motor-values.yaml \
  --set operation=cleanup

helm uninstall motor --namespace mindie
```

RBG 清理需使用 RBG values 文件（如 `/tmp/motor-rbg-values.yaml`）。Helm 的 cleanup Job 会读取
`userConfig.motor_deploy_config.deploy_mode` 并自动设置 `MOTOR_DEPLOY_MODE=rbg`；如果清理时使用
不含 `deploy_mode: rbg` 的默认 values，会走旧的非 RBG 清理流程。纯 kubectl 路径需在
`k8s/cleanup-job.yaml` 中设置 `MOTOR_DEPLOY_MODE: "rbg"`。

> [!WARNING]注意
> cleanup 以 `userConfig.motor_deploy_config.job_id` 为目标 namespace。RBG 模式先处理受管 HPA，
> 再删除 RBG 并等待后代退出，最后清理业务 Service、协调策略、运行时权限和 `motor-config`，保留数据 PVC
> 和外部资源；其他模式沿用 Deployment/Service 等资源的清理流程。请为每套服务使用独立 namespace
> （每个 namespace 只部署一套 RBG），并在 cleanup 完成后再卸载 release。

Chart 和默认部署镜像统一使用 `mindie-motor-deployer`。
升级已有 release 时保留原 release 名称；如需保留原资源名称，使用 `fullnameOverride` 指定原资源前缀。

## operation

| 取值 | 行为 |
|---|---|
| `deploy` | 执行完整部署。 |
| `scale` | 根据当前配置执行实例扩缩容。 |
| `cleanup` | 清理目标 namespace 中由部署流程使用的资源。 |

三个 Job 均使用 Helm `post-install,post-upgrade` hook，并通过 `before-hook-creation` 在重复执行前清理同名旧 Job。

## 弹性扩缩容（HPA）

非空的 `scalingPolicy` values 会注入 `userConfig` 为顶层 `scaling_policy`。每个角色支持
`min_replicas`、`max_replicas`、`metric`、`target`（默认 `0.8`）和 `target_type`；RBG 还支持 `behavior`。
Prefill/Decode 的默认指标分别为 `motor_prefill_utilization` / `motor_decode_utilization`。
InferServiceSet 模式将其渲染到引擎角色的 `scalingPolicy` 字段；RBG 模式生成各角色的 HPA，
其 `scaleTargetRef` 为 `RoleBasedGroupScalingAdapter/{rbg_name}-{role}`。

RBG 模式使用 `operation=deploy` 启用或调整 HPA：角色需具有正数实例，deployer 启用 Adapter、
等待绑定及 `scale` 初始化后再提交 HPA。HPA 调整完整实例数，不改变每实例 Pod 数。
后续提交省略 `scaling_policy` 通常保留线上 HPA；显式 `"scaling_policy": {}` 才表示关闭受管 HPA
并关闭、等待移除对应 Adapter。**顶层 Helm `scalingPolicy: {}` 不会注入关闭指令**；
关闭时应将顶层 `scalingPolicy` 留空或省略，并在 `userConfig` JSON 中显式写入 `"scaling_policy": {}`，
再以 `operation=deploy` 提交。手动扩缩容前需完成 HPA/Adapter 关闭。

配置示例见 `values.yaml`，RBG 的启停、协调策略配置见
[RBG 扩缩容说明](../../deployer/lib/rbg/README.md#扩缩容)和[协调策略](../../deployer/lib/rbg/README.md#协调策略)。
启用 HPA 前需完成指标采集与 External Metrics Adaptor 对接，见
[自动扩缩容指导](../../../docs/zh/user_guide/features/scaling_deployment/auto_scaling.md)。

## 热更新 P/D scalingPolicy

`update-scaling-policy` 通过 Hook Job 修改现有服务的 P/D HPA 策略，不重新部署引擎，也不修改
CoordinatedPolicy。Job 从 `userConfig.motor_deploy_config.job_id` 获取部署 namespace，
再读取线上 `motor-config` 判断部署形态：

- InferServiceSet 路径要求恰好存在一个 InferServiceSet；未配置 `scalingPolicy` 的角色可通过完整策略新增。
- RBG 路径要求恰好存在一个受管 RoleBasedGroup，并查找其已有的 P/D HPA；缺失时必须先通过
  `operation=deploy` 配置 `scalingPolicy` 创建，不能通过此更新入口新增。`policyUpdate.roles` 仅支持
  `prefill` 和 `decode`，不能修改 `scaleTargetRef`。

Chart 的 RBAC 和 `k8s/clusterrole.yaml` 需随 RBG 实现同步更新。旧版不含 RBG 和 HPA 规则，
不能仅补 RBG `list`；部署与策略更新前，应按 [部署账号 RBAC](../../deployer/lib/rbg/README.md#部署账号-rbac)
核对 RBG、CoordinatedPolicy、Adapter、HPA 及运行时资源的完整权限。

只开启 P/D 扩容：

```bash
helm upgrade motor ./examples/cloud_native_deploy/helm \
  -n mindie-motor -f ./motor-values.yaml \
  --set operation=update-scaling-policy \
  --set policyUpdate.roles.prefill.spec.behavior.scaleUp.selectPolicy=Min \
  --set policyUpdate.roles.decode.spec.behavior.scaleUp.selectPolicy=Min \
  --timeout 3m
```

关闭该方向的扩缩容时将 Min 改为 Disabled；这不会删除 HPA 或解除 Adapter 管理。
不指定的角色和字段保持不变。
多字段更新可在自己的 values.yaml 中填写 policyUpdate，例如：

```yaml
policyUpdate:
  roles:
    prefill:
      spec:
        maxReplicas: 2
        behavior:
          scaleUp:
            selectPolicy: Min
            stabilizationWindowSeconds: 60
            policies:
              - type: Pods
                value: 1
                periodSeconds: 300
    decode:
      spec:
        behavior:
          scaleDown:
            selectPolicy: Disabled
```

填写后执行同一 helm upgrade 命令，只需 --set operation=update-scaling-policy，不再需要上述两条开关参数。
不要 --reuse-values 继承之前更新的字段；本次文件中应只包含本次想更新的字段。

`helm upgrade` 会先合并 Chart 默认 values、`-f` 指定的 values 和命令行 `--set`。
Hook Job 使用合并后的 `policyUpdate.roles`：

- 只指定 `--set operation=update-scaling-policy` 时，更新 `-f` values 中已填写的
  `policyUpdate.roles`；如果未提供 `roles`，Helm 直接报错。
- 单字段更新可以只用 `--set` 指定完整字段路径。为了确保只更新该字段，
  `-f` values 中的 `policyUpdate` 应为空对象或不包含其他更新字段。

例如，只修改 Prefill 的扩容选择策略：

```bash
helm upgrade motor ./examples/cloud_native_deploy/helm \
  -n mindie-motor -f ./motor-values.yaml \
  --set operation=update-scaling-policy \
  --set policyUpdate.roles.prefill.spec.behavior.scaleUp.selectPolicy=Min \
  --timeout 3m
```

已有策略时，上述命令只修改该字段，其他 Prefill 字段和 Decode 策略保持不变。
InferServiceSet 的 Prefill 尚无 `scalingPolicy` 时，单字段更新会报错，需提供完整策略新增；
RBG 的 Prefill 尚无 HPA 时，需先走 `operation=deploy` 创建 HPA。

支持模板中 P/D 使用的全部 scalingPolicy 字段：

- type（仅支持 HPA，不切换策略类型）；
- spec.minReplicas、maxReplicas；
- spec.behavior.scaleUp/scaleDown 下的 selectPolicy、stabilizationWindowSeconds；
- policies 中每条规则的 type、value、periodSeconds；
- spec.metrics 中 External 指标的 metric.name、selector.matchLabels/matchExpressions，
  及 target.type、value/averageValue（数量值使用字符串，例如 "0.8"）。

对象按字段合并；metrics、policies、matchExpressions 等数组按整组替换，不按序号或名称猜测合并。
例如修改 D 的容量阈值时，metrics 必须同时列出需要保留的 KV 指标；未填写 metrics 则两者都不变。
不接受 null 删除字段。角色重复、参数错误、并发策略变化均报错退出，无重试或 fallback。
InferServiceSet 角色尚无 scalingPolicy 时，必须提供完整 HPA 策略（type、minReplicas、maxReplicas、
metrics、双向 behavior），否则报错；完整策略先经服务端校验再新增。RBG 路径仅修改已有 HPA 的 spec。
两条路径均先做服务端 dry-run，再提交 JSON Patch 并核对结果；InferServiceSet 路径校验 UID、角色与
旧策略，RBG 路径校验各 HPA 的 UID 和 resourceVersion。

只支持已有 release 的 helm upgrade。Job 使用 deployer 镜像内的
`examples/deployer/deploy.py --update_scaling_policy /policy/policy.json`，复用现有 ServiceAccount。
集群外也可手动执行同一命令，此时使用当前 kubectl 上下文。
ConfigMap 仅传递 `policy.json`；后续只修改策略参数时可以复用同一镜像。
Job 不设固定活跃期限，通过 Helm 的 `--timeout` 为 Pod 调度、镜像拉取和策略更新预留时间；
每次 kubectl 请求仍保留 30 秒超时。
策略更新成功在 InferServiceSet 路径表示其写入完成，infer-operator 同步 HPA 仍有异步延迟；
在 RBG 路径表示指定 HPA 已写入并核对，实际扩缩容由 HPA 与 RBG 控制器异步执行。
正常 `operation=deploy` Job 完成只表示资源已提交，不代表工作负载就绪或业务推理验证完成。
本操作不修改原始部署模板，重新部署仍以部署配置为准；恢复策略需显式再次更新，不依赖 Helm rollback。

部署与手动扩缩容的额外 deploy.py 参数分别通过 `jobs.deploy.args`、`jobs.scale.args` 传入。
不传额外参数时保留安全预检；生产须完成推理 TLS 与管理面 API Key 配置，并移除已保存在 values 或 Job 中的
`--skip-check`。该参数仅限隔离的本地或测试环境验证，生产环境不得使用。
这两个参数默认均为空数组，只接受字符串数组；字符串、对象或含非字符串元素的数组会在 Helm 校验阶段报错。
