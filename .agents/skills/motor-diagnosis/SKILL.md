---
name: motor-diagnosis
description: Motor diagnosis entry point for deploy、startup or runtime failures, log collection, evidence preservation, and root-cause routing. Use this parent Skill before selecting an atomic Motor diagnosis workflow.
---

# Motor diagnosis dispatcher and evidence collection

这是 Motor 故障诊断的统一入口。先保存首次失败现场，再根据故障阶段路由；不得用重试
覆盖原始证据，也不得把诊断请求视为修复授权。

## 路由

| 现象 | Skill / 处理 |
|---|---|
| `deploy.py` 失败、workload 未 Ready、Service/Endpoint 缺失、readiness 超时 | [`motor-diagnosis-startup`](../motor-diagnosis-startup/SKILL.md) |
| 已启动服务的未知异常或通用日志采证 | 本 Skill 的通用采证流程 |
| Pod 创建、调度、拉镜像、挂载、容器或 K8s probe 异常 | [`motor-diagnosis-k8s`](../motor-diagnosis-k8s/SKILL.md) |
| Controller/Coordinator 已启动但 readiness、注册、心跳或拓扑未收敛 | [`motor-diagnosis-control-plane`](../motor-diagnosis-control-plane/SKILL.md) |
| `curl` 无法建立 HTTP 连接、DNS/TCP/TLS 或 port-forward 异常 | [`motor-diagnosis-connectivity`](../motor-diagnosis-connectivity/SKILL.md) |

路由到原子 Skill 前先执行下面的最小采证，并读取、完整遵循对应 `SKILL.md`。当前没有
匹配原子 Skill 时，基于源码和只读事实继续分析并明确 capability gap。

使用用户指定或从当前原生配置中确认的 kube context 与 namespace。没有明确目标时
停止补充信息，不猜 namespace，不要求 workspace run ID。

## 采证

```bash
kubectl --context "$CTX" get all -n "$NS" -o wide
kubectl --context "$CTX" get events -n "$NS" --sort-by=.lastTimestamp
kubectl --context "$CTX" describe pod -n "$NS" <pod>
kubectl --context "$CTX" logs -n "$NS" <pod> --all-containers --timestamps
kubectl --context "$CTX" logs -n "$NS" <pod> --all-containers --previous --timestamps
```

按故障范围补充生成的 manifests、原生 deploy 命令及完整 stdout/stderr、Service 和
Endpoint、容器状态、Pod UID、restart count、镜像/包版本，以及 deployer
`--auto_log_collect` 产物。所有证据记录来源命令和 UTC 时间。

先保存首次失败，再考虑任何重试。日志按时间、Pod UID、instance/job ID 和请求 ID
关联；单条 error、最终 Running 状态或重启后恢复都不能独立证明根因。

## 输出与边界

- 保存到用户指定目录；未指定时保留在会话中，不发明仓库内 artifact 路径。
- 对 secret、token、registry credential 和无关租户信息做脱敏。
- 通用采证本身不强制归因。deploy/startup 失败把证据交给
  `motor-diagnosis-startup`。
- 不 restart、delete、repair、edit config、scale 或注入故障。
- 输出已收集证据、缺失证据和最小下一项只读检查；没有证据链时明确写“未定位到根因”。
