---
name: motor-validation-smoke
description: Explicit atomic workflow under motor-validation for the deployed Coordinator management readiness endpoint. Use for Motor 最小冒烟、部署后 readiness 或 Coordinator 就绪检查.
---

# Motor smoke

读取 `references/motor-readiness.md`，从当前原生配置和 K8s 对象解析 namespace 与
Coordinator management Service。

## 前置语义

- Pod `Ready` 只作参考，不通过 smoke；唯一 PASS 条件是 Coordinator
  `GET /readiness` 返回 HTTP 200 且 JSON `ready=true`。
- HTTP 200 + `ready=false` 表示仍在收敛，应继续轮询而不是立即 FAIL。

## 流程

1. 发现当前 Coordinator management Service 和端口；默认管理端口为 1026，但以
   当前 config/Service 为准。
2. 从能访问 Service 的执行环境直连，或启动受监控的临时 `kubectl port-forward`。
3. 默认每 15 秒轮询一次，最长 600 秒。每次记录 UTC 时间、HTTP 状态、响应体和
   已耗时；用户或当前配置给出更合适 deadline 时使用明确值。
4. 以下情况 FAIL：deadline 到期仍 `ready=false`；Service/转发等访问错误持续存在；
   HTTP 200 的响应无法解析为 JSON。
5. 无论结果如何都清理临时 port-forward。
6. FAIL 时先把轮询时间线、最终响应、Service 发现证据和清理结果交给
   `motor-diagnosis`，不得自动 restart、改配置或 redeploy。

`/health`、`/startup`、`/liveness`、TCP connect、Pod Ready 或 `/v1/models` 不替代
本检查。管理端口不承载 inference；推理请求使用 Coordinator inference Service。

报告 Service、访问方式、轮询次数、最终状态/响应、总等待时间和清理结果。
