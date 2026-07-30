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
docker build -f examples/cloud_native_deploy/Dockerfile -t mindie-pymotor-deployer:latest .
```

也可以使用仓库根目录的 Makefile：

```bash
make buildx-cloud-native-deployer TAG=latest
```

## 准备配置

编辑 `k8s/configmap-user-config.yaml`，写入实际使用的 `user_config.json` 和 `env.json`。

可选配置项：

- `motor_deploy_config.scheduling_queue`：指定 Volcano 队列。
- `motor_deploy_config.coordinator_service_name`：覆盖默认的对外推理 Service 名称。
- `motor_deploy_config.coordinator_infer_node_port`：覆盖对外推理 Service 的 NodePort。配置 `"-"` 时由 Kubernetes 自动分配，字段缺省时保留模板中的 `nodePort`，当前模板默认值为 `31015`。
- `motor_deploy_config.prefill_node_selector` 和 `motor_deploy_config.decode_node_selector`：分别控制 Prefill / Decode 的节点调度标签。
- `motor_deploy_config.controller_node_selector` 和 `motor_deploy_config.coordinator_node_selector`：分别控制 Controller / Coordinator 的节点调度标签。
- `motor_deploy_config.kv_pool_node_selector` 和 `motor_deploy_config.kv_conductor_node_selector`：分别控制 KV Pool / KV Conductor 的节点调度标签。

## 使用 Helm 部署

社区版本提供 `helm/` Chart，用于直接创建 ConfigMap、ServiceAccount、RBAC 和 deploy / scale / cleanup Job。运行时服务资源仍由 Job 中的 `examples/deployer/deploy.py` 生成。

先复制并修改默认配置，至少设置 deployer 镜像、Motor 运行镜像、模型路径和实际集群参数：

```bash
cp examples/cloud_native_deploy/helm/values.yaml /tmp/pymotor-values.yaml
```

执行部署：

```bash
helm upgrade --install pymotor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --create-namespace \
  --values /tmp/pymotor-values.yaml \
  --set operation=deploy
```

扩缩容时修改 values 中的 `p_instances_num` 和/或 `d_instances_num`，然后执行：

```bash
helm upgrade pymotor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --values /tmp/pymotor-values.yaml \
  --set operation=scale
```

清理运行时资源并卸载 Chart：

```bash
helm upgrade pymotor examples/cloud_native_deploy/helm \
  --namespace mindie \
  --values /tmp/pymotor-values.yaml \
  --set operation=cleanup

helm uninstall pymotor --namespace mindie
```

详细参数见 [Helm Chart 使用说明](helm/README.zh.md)。

## 部署前准备与执行

```bash
kubectl apply -f examples/cloud_native_deploy/k8s/serviceaccount.yaml
kubectl apply -f examples/cloud_native_deploy/k8s/clusterrole.yaml
kubectl apply -f examples/cloud_native_deploy/k8s/clusterrolebinding.yaml
kubectl apply -f examples/cloud_native_deploy/k8s/configmap-user-config.yaml
kubectl apply -f examples/cloud_native_deploy/k8s/job.yaml
```

查看日志：

```bash
kubectl logs job/mindie-pymotor-deploy -n mindie
```

## 扩缩容

修改 `k8s/configmap-user-config.yaml` 中的 `p_instances_num` 和/或 `d_instances_num`，重新 apply ConfigMap 后执行：

```bash
kubectl apply -f examples/cloud_native_deploy/k8s/scale-job.yaml
kubectl logs job/mindie-pymotor-scale -n mindie
```

## 清理

cleanup Job 只需要传入目标 namespace。

```bash
kubectl apply -f examples/cloud_native_deploy/k8s/cleanup-job.yaml
kubectl logs job/mindie-pymotor-cleanup -n mindie
```
