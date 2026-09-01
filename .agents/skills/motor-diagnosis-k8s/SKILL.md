---
name: motor-diagnosis-k8s
description: Atomic read-only diagnosis for Motor Kubernetes deploy/startup failures. Use for API, RBAC, admission, missing workloads, Pending, scheduling, image pull, mount, container creation, CrashLoopBackOff, Kubernetes probe, Service selector, or Endpoint failures; not for an HTTP-reachable but unready Motor control plane.
---

# Motor Kubernetes startup diagnosis

这是 `motor-diagnosis-startup` 下的 K8s 原子诊断 Skill。目标是确定失败发生在 apply、
调度、镜像与存储、容器启动、probe 还是 Service Endpoint 阶段，并将根因归入
environment、deployer、config 或 runtime-code。首期只覆盖 Motor + vLLM + vLLM-Ascend。

## 入口与边界

适用于 K8s API/RBAC/admission 报错、资源未创建、Pod Pending、`ErrImagePull`、
`ImagePullBackOff`、`CreateContainerConfigError`、挂载失败、`CrashLoopBackOff`、probe
失败、Service selector 或 Endpoint 异常。

Controller/Coordinator 已能返回 HTTP、但 readiness 内容显示未就绪时，调用
[`motor-diagnosis-control-plane`](../motor-diagnosis-control-plane/SKILL.md)。仅在 HTTP
连接建立前失败时调用
[`motor-diagnosis-connectivity`](../motor-diagnosis-connectivity/SKILL.md)。

只执行读取命令；不 apply、patch、delete、rollout、scale、重启、修改配置或注入故障。
必须使用用户确认的 kube context、namespace 和 workload；不猜默认 namespace。

## 诊断流程

1. 记录原始 deploy 命令、退出码、stdout/stderr、UTC 时间窗和生成的 manifests。检查
   当前 context、API 可达性和目标 namespace 的只读权限；权限失败不能伪装成资源不存在。
2. 将预期 manifest 与 live 对象逐项核对。对象未创建时区分 YAML/render 错误、
   admission 拒绝、RBAC 拒绝和 apply 编排中断。
3. 对未 Ready Pod 读取 phase、conditions、containerStatuses、restart count、reason、
   exit code、lastState、调度节点及 Events。Events 是线索，必须与 Pod UID 和时间窗关联。
4. 按最早失败阶段深入：
   - Pending：scheduler reason、资源请求、node selector/affinity、taint/toleration、PVC、
     NPU device/plugin 与配额；
   - Waiting：镜像引用与 pull secret、ConfigMap/Secret/PVC 引用、volume mount、容器命令；
   - Terminated/重启：current/previous logs、exit code、OOMKilled、启动命令和挂载后的配置；
   - probe：probe 的 path、port、scheme、target container 与响应/连接错误；
   - Service：selector 与 Pod labels、port/targetPort、EndpointSlice addresses 和 Pod Ready。
5. 比较 `user_config.json`、rendered YAML、live workload/ConfigMap 和容器实际参数，确定
   值在哪一层首次偏离。只比较同一 revision 和本次 Pod UID。

```bash
kubectl --context "$CTX" auth can-i get pods -n "$NS"
kubectl --context "$CTX" get deploy,statefulset,pod,svc,endpoints,endpointslice -n "$NS" -o wide
kubectl --context "$CTX" get events -n "$NS" --sort-by=.lastTimestamp
kubectl --context "$CTX" describe pod -n "$NS" <pod>
kubectl --context "$CTX" logs -n "$NS" <pod> --all-containers --timestamps
kubectl --context "$CTX" logs -n "$NS" <pod> --all-containers --previous --timestamps
kubectl --context "$CTX" get pod -n "$NS" <pod> -o yaml
kubectl --context "$CTX" get svc,endpoints,endpointslice -n "$NS" -o yaml
```

某容器从未启动时没有 logs 是正常证据，不反复重试。`--previous` 无结果不能证明没有
崩溃；还要读取 lastState 和 restart count。避免导出 Secret 内容，对 registry、token
和无关租户字段脱敏。

## 归因规则

| 证据链 | 主分类 |
|---|---|
| API 不可达、RBAC/admission、节点/NPU/配额、registry、存储或集群网络异常 | environment |
| render 正确但 deployer 未 apply、对象顺序或等待逻辑错误 | deployer |
| image、selector、端口、资源、挂载或 probe 值从原始配置起就错误 | config |
| 容器成功启动后 Motor/vLLM/vLLM-Ascend 进程异常退出或 probe handler 代码异常 | runtime-code |

例如：非法镜像名来自配置是 config；合法镜像因 registry 认证失败是 environment；正确
镜像在 render 时丢失是 deployer。`CrashLoopBackOff`、`Pending` 和 `Unhealthy` 都只是
状态，不是根因。

## 输出

输出失败阶段、主分类、可能的 contributing category、时间化证据链、已排除项、置信度
和下一步。下一步只给最小判别或修复建议及其精确命令，未经授权不执行。证据不足时
返回 `unknown`，列出缺失证据，不用重试后的 Running 状态覆盖首次失败现场。
