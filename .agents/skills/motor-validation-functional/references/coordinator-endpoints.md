# Coordinator endpoint selection

从匹配 revision 的 `motor/config/coordinator.py`、
`motor/coordinator/api_server/`、当前 `user_config.json` 和 live Service 确认端口与路由。

| Endpoint class | 默认端口 | 用途 |
|---|---:|---|
| inference | 1025 | OpenAI-compatible inference 请求 |
| management | 1026 | readiness、管理和观测接口 |

默认值不是目标发现机制。必须用 config、Service port/targetPort、selector、Endpoint
和 Pod identity 证明实际地址。

Inference 用例优先使用部署明确支持的 OpenAI-compatible 路由。请求至少记录 URL、
method、headers（脱敏）、model、生成参数、stream 设置、HTTP 状态和响应。不要把
`/v1/models` 或 TCP connect 当作推理成功。

对临时 port-forward 使用有界后台任务，保存启动错误，并在成功、失败、取消和异常
路径清理进程。转发失败只说明访问通道失败，不能直接归因为 Motor 服务故障。
