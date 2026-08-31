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
     -t example.com/team/mindie-pymotor-deployer:latest .
   docker push example.com/team/mindie-pymotor-deployer:latest
   ```

2. 复制 `values.yaml` 并修改：

   ```bash
   cp examples/cloud_native_deploy/helm/values.yaml /tmp/pymotor-values.yaml
   ```

   必须按实际环境设置：

   - `image.repository` 和 `image.tag`：deployer 镜像。
   - `userConfig` JSON 字符串中的 `motor_deploy_config.image_name`：运行 Controller、Coordinator 和 Engine 的 Motor 镜像。
   - `job_id`、模型路径、实例数、NPU 数量和硬件类型。
   - 按需修改 `userConfig` JSON 字符串中的 `coordinator_infer_node_port` 和各组件 `*_node_selector`。

## 部署

```bash
helm upgrade --install pymotor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --create-namespace \
  --values /tmp/pymotor-values.yaml \
  --set operation=deploy
```

查看 Job 和日志：

```bash
kubectl get jobs -n mindie
kubectl logs job/pymotor-mindie-pymotor-deployer-deploy -n mindie
```

## 扩缩容

修改 `/tmp/pymotor-values.yaml` 中的实例数后执行：

```bash
helm upgrade pymotor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --values /tmp/pymotor-values.yaml \
  --set operation=scale
```

## 清理

先运行 cleanup Job 删除 deployer 动态创建的资源，再卸载 Helm release：

```bash
helm upgrade pymotor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --values /tmp/pymotor-values.yaml \
  --set operation=cleanup

helm uninstall pymotor --namespace mindie
```

> [!WARNING]注意
> 当前 cleanup 脚本会删除 `userConfig.motor_deploy_config.job_id` 对应 namespace 中的 Deployment 和 Service。请为每套服务使用独立 namespace，并在执行前确认 namespace。

## operation

| 取值 | 行为 |
|---|---|
| `deploy` | 执行完整部署。 |
| `scale` | 根据当前配置执行实例扩缩容。 |
| `cleanup` | 清理目标 namespace 中由部署流程使用的资源。 |

三个 Job 均使用 Helm `post-install,post-upgrade` hook，并通过 `before-hook-creation` 在重复执行前清理同名旧 Job。
