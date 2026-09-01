---
name: motor-diagnosis-startup
description: Motor deploy/startup diagnosis dispatcher after evidence preservation. Use for deploy.py failures, unready workloads, missing Services/endpoints, Coordinator readiness timeout, or curl connection failure, then route to the matching atomic diagnosis Skill.
---

# Motor startup diagnosis dispatcher

这是 `motor-diagnosis` 下的 deploy/startup 只读归因与分发流程。先由父 Skill 保存首次失败，
再根据故障发生位置调用一个原子 Skill；不得只根据最终 Pod phase 或一条日志决定根因。

## 入口

- `deploy.py` 或 `deploy.py --dry-run` 非零退出；
- 预期 workload 不存在或在有界等待内未 Ready；
- 必需 Service/Endpoint 缺失；
- Controller/Coordinator `/readiness` 未达到就绪条件；
- `curl` 在获得 HTTP 响应前发生 DNS、TCP、TLS 或 port-forward 失败。

保存 endpoint、context、namespace、失败阶段、UTC 时间窗、原命令、退出码、完整
stdout/stderr、预期结果、观察结果和已生成文件。某类对象尚未创建时，只收集该阶段
真实存在的证据。

## 分类框架

| Category | 典型证据 |
|---|---|
| environment | API/RBAC/admission/operator/scheduler/NPU/image/storage/network |
| deployer | 参数、traceback、模板/YAML 生成、apply 编排 |
| config | 原生 config 无效或 config→YAML→ConfigMap→Pod 漂移 |
| runtime-code | 进程已启动后 crash、hang、注册失败或运行时集成错误 |

失败阶段只是路由提示，不是根因。配置里的坏 image 值属于 config；合法 image 无法
从 registry 获取属于 environment；合法值在 YAML 生成时丢失属于 deployer。

## 原子 Skill 路由

| 判别证据 | 原子 Skill |
|---|---|
| K8s API/RBAC/admission、对象未创建、Pending、镜像、挂载、容器、probe 或 Service Endpoint 异常 | [`motor-diagnosis-k8s`](../motor-diagnosis-k8s/SKILL.md) |
| Controller/Coordinator 进程可访问，但 readiness 返回未就绪，或注册、心跳、实例刷新、P/D 拓扑未收敛 | [`motor-diagnosis-control-plane`](../motor-diagnosis-control-plane/SKILL.md) |
| 目标服务尚未返回 HTTP，表现为 DNS 失败、connection refused/reset、timeout、TLS handshake 或 port-forward 失败 | [`motor-diagnosis-connectivity`](../motor-diagnosis-connectivity/SKILL.md) |

先选最早能被证据证明的失败边界。`Readiness probe failed` 只是 K8s 观察到的结果：探针
连接失败路由连通性；探针收到 Controller/Coordinator 未就绪响应路由控制面；容器本身
未启动或反复退出路由 K8s。已收到推理接口 HTTP 4xx/5xx 时不使用连通性 Skill，转到
`motor-validation-functional` 或运行时代码分析。

## 停止规则与输出

证据链足以支持根因时停止；无法用只读证据区分时返回 `unknown`、缺失证据和最小
判别检查。输出失败阶段、主分类、可能的 contributing category、时间化证据链、
已排除项、置信度和下一步。

分类不授权 retry、restart、delete、repair、config edit、scale、namespace 创建或
故障注入。路由后读取并完整遵循对应原子 Skill；没有匹配项时继续用源码和只读事实
分析，并明确 capability gap。
