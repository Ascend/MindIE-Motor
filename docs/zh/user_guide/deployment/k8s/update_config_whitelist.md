# `--update_config` 白名单说明

本文档说明 `deploy.py --update_config` 支持修改的配置项范围。

## 1. 使用约束

- `--update_config` 仅允许修改白名单内字段。
- 部署脚本会将当前 `user_config.json` 与集群中已部署的 `motor-config` 基线配置逐项比对。
- 若存在白名单外字段变更，或在白名单配置块下新增未支持字段，脚本会直接报错并拒绝更新。
- `--update_config` 仅刷新 ConfigMap，不会重新 apply Deployment。

## 2. 白名单范围

当前允许通过 `--update_config` 修改的配置项如下。

### 2.1 `motor_controller_config`

- `logging_config.log_level`：Controller 日志等级，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR` 等
- `observability_config.observability_enable`：是否开启 Controller 可观测能力
- `observability_config.metrics_ttl`：可观测指标缓存保留时长，单位为秒

### 2.2 `motor_coordinator_config`

- `logging_config.log_level`：Coordinator 日志等级，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR` 等
- `exception_config.max_retry`：请求失败后的最大重试次数
- `exception_config.retry_delay`：每次重试前的等待时间，单位为秒
- `exception_config.first_token_timeout`：等待首个 token 返回的超时时间，单位为秒
- `exception_config.infer_timeout`：单次推理请求的总超时时间，单位为秒
- `timeout_config.request_timeout`：单次 HTTP 请求的总超时时间，单位为秒
- `timeout_config.connection_timeout`：建立连接的超时时间，单位为秒
- `timeout_config.read_timeout`：读操作超时时间，单位为秒
- `timeout_config.write_timeout`：写操作超时时间，单位为秒
- `timeout_config.keep_alive_timeout`：HTTP 连接保活时长，超时无活动则关闭，单位为秒

### 2.3 `motor_nodemanger_config`

- `logging_config.log_level`：NodeManager 日志等级，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR` 等

## 3. 不支持的修改

除上述字段外，其他配置项均不支持通过 `--update_config` 修改，包括但不限于：

- 部署资源相关配置
- 实例数量
- 模型与引擎配置
- TLS 配置
- 主备配置
- 限流与鉴权相关配置

如需扩缩容，请使用 `--update_instance_num`；如需变更其他配置，请按正常部署流程重新执行部署。
