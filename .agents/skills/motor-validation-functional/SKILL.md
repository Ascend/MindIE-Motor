---
name: motor-validation-functional
description: Explicit atomic workflow under motor-validation for focused checks with kubectl, HTTP, metrics, or tracing. Use for inference and deployed feature behavior, not readiness, performance, accuracy, stability, or reliability.
---

# Motor functional validation

读取 `references/case-catalog.md` 选择最小用例集，并在访问端点前读取
`references/coordinator-endpoints.md`。

1. 从当前原生 config 和 live K8s 解析 namespace、served model、已启用 feature、
   Coordinator Services 和实际端口；不从旧报告或固定名称猜值。
2. 执行前展示用例、请求、判据、目标和可能产生的负载。
3. 必要时使用受监控的临时 `kubectl port-forward`，退出时总是清理。
4. inference 请求发送到 Coordinator inference Service。默认端口 1025，但以当前
   config/Service 为准；禁止把业务请求发到 management port。
5. metrics 必须在一次受控请求后查询实际观测端点；tracing 注入唯一且 sampled 的
   W3C `traceparent`，再到当前 trace backend 查询对应 trace。
6. 保存精确请求、脱敏响应、状态码、相关 metrics/trace 和时间戳。

handler 缺失、feature 未开启、backend 不存在或证据不可访问时报告 BLOCKED/UNAVAILABLE，
不得算 PASS。本 Skill 不改配置，不宣称 performance、accuracy、stability 或
reliability 已通过。
