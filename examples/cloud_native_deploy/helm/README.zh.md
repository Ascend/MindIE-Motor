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

## 清理

先运行 cleanup Job 删除 deployer 动态创建的资源，再卸载 Helm release：

```bash
helm upgrade motor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --values /tmp/motor-values.yaml \
  --set operation=cleanup

helm uninstall motor --namespace mindie
```

> [!WARNING]注意
> 当前 cleanup 脚本会删除 `userConfig.motor_deploy_config.job_id` 对应 namespace 中的 Deployment 和 Service。请为每套服务使用独立 namespace，并在执行前确认 namespace。

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

可选的 `scalingPolicy` values 用于为 InferServiceSet 的引擎角色（prefill/decode/union）渲染 HPA 形态的 `scalingPolicy` 字段。该配置会被注入 deployer 的 `user_config` 作为 `scaling_policy` 段，每个角色条目支持 `min_replicas`、`max_replicas`、`metric`（按角色默认 `motor_prefill_utilization` / `motor_decode_utilization`）和 `target`（默认 `0.8`）。未配置时不渲染 `scalingPolicy`，部署产物与现状一致。配置示例见 `values.yaml`，完整 HPA 对接流程（External Metrics Adaptor、推荐指标）见 `docs/zh/user_guide/features/auto_scaling.md`。

## 热更新 P/D scalingPolicy

update-scaling-policy 通过 Hook Job 修改现有服务的策略，不重新部署服务。
Job 从 userConfig.motor_deploy_config.job_id 获取部署命名空间，要求其中恰好存在一个
InferServiceSet。没有或有多个则报错，不猜测目标，无需填写 inferServiceSet。

只开启 P/D 扩容：

```bash
helm upgrade motor ./examples/cloud_native_deploy/helm \
  -n mindie-motor -f ./motor-values.yaml \
  --set operation=update-scaling-policy \
  --set policyUpdate.roles.prefill.spec.behavior.scaleUp.selectPolicy=Min \
  --set policyUpdate.roles.decode.spec.behavior.scaleUp.selectPolicy=Min \
  --timeout 3m
```

关闭时将 Min 改为 Disabled。不指定的角色和字段保持不变。
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

已有 `scalingPolicy` 时，上述命令只修改该字段，其他 Prefill 字段和 Decode 策略保持不变。
如果 Prefill 尚无 `scalingPolicy`，单字段更新会报错，必须先提供完整 HPA 策略创建。

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
角色尚无 scalingPolicy 时，必须提供完整 HPA 策略（type、minReplicas、maxReplicas、metrics、双向 behavior），否则报错；完整策略先经服务端校验再新增。
先做服务端 dry-run，再通过带 UID/角色/旧策略校验的 JSON Patch 原子写入，最后核对返回策略。

只支持已有 release 的 helm upgrade。Job 使用 deployer 镜像内的
`examples/deployer/deploy.py --update_scaling_policy /policy/policy.json`，复用现有 ServiceAccount。
集群外也可手动执行同一命令，此时使用当前 kubectl 上下文。
ConfigMap 仅传递 `policy.json`；后续只修改策略参数时可以复用同一镜像。
Job 不设固定活跃期限，通过 Helm 的 `--timeout` 为 Pod 调度、镜像拉取和策略更新预留时间；
每次 kubectl 请求仍保留 30 秒超时。
Helm 成功表示 InferServiceSet 写入完成，infer-operator 同步 HPA 仍有异步延迟。
本操作不修改原始部署模板，重新部署仍以部署配置为准；恢复策略需显式再次更新，不依赖 Helm rollback。

部署与手动扩缩容的额外 deploy.py 参数分别通过 `jobs.deploy.args`、`jobs.scale.args` 传入，例如 `--set 'jobs.scale.args[0]=--skip-check'`。不传额外参数时保留原有行为，不会自动跳过安全预检。
这两个参数默认均为空数组，只接受字符串数组；字符串、对象或含非字符串元素的数组会在 Helm 校验阶段报错。
