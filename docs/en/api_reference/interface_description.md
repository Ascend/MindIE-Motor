# Interface Description

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:07:36.599Z pushedAt=2026-06-27T02:42:10.947Z -->

## Ports and Protocols

The service provides inference and management interfaces, supporting separate port or combined port modes:

- Inference port: `api_config.coordinator_api_infer_port` (default `1025`)

- Management port: `api_config.coordinator_api_mgmt_port` (default `1026`)

- Security protocol: `https` is used when `infer_tls_config.tls_enable` / `mgmt_tls_config.tls_enable` is `true`

## Authentication and Rate Limiting

- API Key (optional): takes effect only for `/v1/completions` and `/v1/chat/completions`

  - Header name: `api_key_config.header_name` (default `Authorization`)

  - Prefix: `api_key_config.key_prefix` (default `Bearer`)

- Rate limiting (optional): enabled when `rate_limit_config.enable_rate_limit=true`. Exceeding the limit returns a `429` status code.
