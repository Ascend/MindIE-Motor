---
name: motor-diagnosis-control-plane
description: Atomic read-only diagnosis for an HTTP-reachable but unready Motor Controller or Coordinator. Use for readiness false or 503, standby not-master responses, instance registration, heartbeat, refresh, required-instance, or prefill/decode topology convergence failures; not for Pod creation or curl connection failures.
---

# Motor control-plane readiness diagnosis

这是 `motor-diagnosis-startup` 下的控制面原子诊断 Skill。目标是在 Controller、
Coordinator 进程已经启动且管理接口可访问后，定位 readiness、主备角色、实例注册、
心跳、刷新或 P/D 拓扑收敛失败。首期只覆盖 Motor + vLLM + vLLM-Ascend。

## 入口与边界

适用于 Controller/Coordinator `/readiness` 返回未就绪、HTTP 503、`ready=false`、
`Not master`，或日志显示 NodeManager 注册/重注册、心跳、实例刷新、required instances、
prefill/decode 配对未收敛。

Pod 未创建、容器未启动、镜像/挂载/调度失败时调用
[`motor-diagnosis-k8s`](../motor-diagnosis-k8s/SKILL.md)。DNS、TCP、TLS 或 port-forward
导致接口尚未返回 HTTP 时调用
[`motor-diagnosis-connectivity`](../motor-diagnosis-connectivity/SKILL.md)。

只执行读取命令；不触发注册、刷新、切主、重启、扩缩容、配置修改或故障注入。不得把
HA standby 的 `Not master` 单独判为故障，必须先确定访问目标和该副本预期角色。

## 诊断流程

1. 记录请求 URL、访问路径、UTC 时间、HTTP 状态和完整脱敏响应。确认访问的是 management
   Service 和实际 management port；不要把 inference port 的响应解释为 readiness。
2. 将响应中的 `ready`、message、reason、instance status 与同一时间窗的 Coordinator
   日志关联。读取 Pod 名称、UID、revision 和 HA role，避免混合不同副本证据。
3. 检查 Controller readiness 与主备角色。若目标是 standby，`Not master` 可为预期保护；
   若目标应为 master，再检查选主、租约/etcd 和角色切换证据。
4. 沿控制链逐段关联：NodeManager 启动 → Controller register/reregister → heartbeat →
   instance 状态 → Controller 向 Coordinator 推送/刷新 → Coordinator required-instance
   判定。用 job/instance ID、role、Pod IP 和时间戳关联日志。
5. 对 PD 分离分别核对 prefill、decode 的预期数量、注册数量、健康状态和 Coordinator
   可见拓扑。只有一侧实例、实例仍 INITIAL/INACTIVE、心跳过期或刷新缺失时，不把
   Coordinator 的 `ready=false` 本身当根因。
6. 比较当前 `user_config.json`、live ConfigMap/环境变量和进程启动日志中的 Controller、
   Coordinator 地址、端口、角色、实例数量及超时；确定值在哪一层首次偏离。

```bash
kubectl --context "$CTX" get pod,svc,endpoints,endpointslice -n "$NS" -o wide
kubectl --context "$CTX" get pod -n "$NS" <controller-pod> -o yaml
kubectl --context "$CTX" get pod -n "$NS" <coordinator-pod> -o yaml
kubectl --context "$CTX" logs -n "$NS" <controller-pod> --all-containers --timestamps
kubectl --context "$CTX" logs -n "$NS" <coordinator-pod> --all-containers --timestamps
kubectl --context "$CTX" logs -n "$NS" <engine-pod> --all-containers --timestamps
curl --silent --show-error --max-time 10 <coordinator-management-url>/readiness
```

优先使用当前部署已暴露的只读实例/状态接口；接口路径必须由相同 revision 的源码或服务
OpenAPI 确认，不猜测路径。接口不可用时用日志和配置继续定位，并明确缺失证据。

## 归因规则

| 证据链 | 主分类 |
|---|---|
| etcd/选主依赖、集群内控制链网络或基础服务不可用 | environment |
| deployer 未正确生成/传递控制面地址、角色、端口或依赖启动顺序 | deployer |
| 实例数量、P/D 角色、地址、端口、HA 或超时配置从原始配置起错误 | config |
| 注册、心跳、状态转换、刷新或 readiness 判定代码异常 | runtime-code |

HTTP 503 或 `ready=false` 是控制面状态，不自动等于 runtime-code。先找到最早未收敛的
环节；如果只是访问了 standby 或错误 Service，则主分类通常是 config/使用目标错误，
并把 HA 保护行为列为已验证事实。

## 输出

输出访问目标与角色、失败阶段、主分类、可能的 contributing category、实例拓扑摘要、
时间化证据链、已排除项、置信度和下一步。下一步只给最小判别或修复建议及精确命令，
未经授权不执行。无法区分注册、心跳或刷新环节时返回 `unknown` 并列出缺失证据。
