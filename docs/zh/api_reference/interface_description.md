# 接口说明

## 端口与协议

Coordinator 服务提供三类接口，分别使用独立端口：

- 推理端口：`api_config.coordinator_api_infer_port`（默认 1025），承载 `/v1/chat/completions`、`/v1/completions`、`/v1/messages`、`/v1/models`、`/v1/metaserver` 等推理业务接口
- 管理端口：`api_config.coordinator_api_mgmt_port`（默认 1026），承载 `/startup`、`/liveness`、`/readiness`、`/instances/refresh`、`/` 等管理探针接口
- Observability 端口：`api_config.coordinator_obs_port`（默认 1027），承载 `/metrics`、`/instance/metrics`（已弃用）、`/health` 等可观测性接口
- 安全协议：`infer_tls_config.tls_enable` / `mgmt_tls_config.tls_enable` 为 `true` 时，推理/管理端口使用 `https`

>[!NOTE]说明
>Metrics 指标通过 Observability 端口（默认 1027）的 `/metrics` 端点获取。Kubernetes 部署时，该端口通过 NodePort 对外暴露，可直接由 Prometheus 抓取。详见 [管理和监控接口](management_and_monitoring_interfaces.md)。

## 认证与限流

- API Key（可选）：对 `/v1/completions`、`/v1/chat/completions`、`/v1/messages`、`/v1/messages/count_tokens` 生效
  - Header 名称：`api_key_config.header_name`（默认 `Authorization`）
  - 前缀：`api_key_config.key_prefix`（默认 `Bearer`）
- 限流（可选）：`rate_limit_config.enable_rate_limit=true` 时启用，超限返回 `429`
