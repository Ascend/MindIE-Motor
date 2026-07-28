# 配置参数热更新说明

在 MindIE Motor 中，部分配置参数可以在服务运行过程中动态修改，本文档对该过程进行说明。

---

## 允许热更新的配置字段

允许热更新的字段**主要与日志等级、实例运行数据查询、请求处理超时时间等内容相关**。

- **motor_controller_config**

    `logging_config.log_level`：Controller 日志等级，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR` 等。

    `observability_config.observability_enable`：是否打开对外查询接口，用来查当前推理实例的指标数据、告警信息等。

    `observability_config.metrics_ttl`：推理实例的指标数据多久刷新一次，单位为秒。

- **motor_coordinator_config**

    `logging_config.log_level`：Coordinator 日志等级，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR` 等。

    `exception_config.max_retry`：请求推理失败后，最多进行几次重试。

    `exception_config.retry_delay`：请求推理失败后，等待对应时间后重试，单位为秒。

    `exception_config.first_token_timeout`：等待首个 token 返回的超时时间，单位为秒。

    `exception_config.infer_timeout`：单次推理请求的总超时时间，单位为秒。

    `rate_limit_config.max_requests`：限流时间窗口内允许的最大请求数。热更新时会按旧速率结算令牌桶中已累积的令牌，再以新参数继续累积。

    `rate_limit_config.window_size`：限流统计的时间窗口长度，单位为秒。热更新后会与 `max_requests` 一起重新计算令牌桶的填充速率。

    `rate_limit_config.skip_paths`：不参与限流统计的路径列表，可自定义。热更新后立即对后续请求生效。

    `rate_limit_config.error_message`：触发限流时返回给客户端的提示文案。

    `rate_limit_config.error_status_code`：触发限流时返回的 HTTP 状态码，通常为 4xx（如 429）。

    `rate_limit_config.max_request_body_size`：请求体最大大小（MB），超过则直接拒绝并返回 413，不消耗限流令牌。`<= 0` 表示不限制，支持小数（如 `0.5` 表示 0.5MB，1MB = 1024\*1024 字节）。

    注意：`rate_limit_config.enable_rate_limit` `rate_limit_config.provider`、`rate_limit_config.scope`、`rate_limit_config.olc_config_path` 仅在服务启动时读取，不支持热更新。如需切换限流提供者（simple/olc）或修改 OLC 规则路径，请重启服务。

- **motor_nodemanger_config**

    `logging_config.log_level`：NodeManager 日志等级，可选 `DEBUG`、`INFO`、`WARNING`、`ERROR` 等

其余配置参数暂不支持热更新，可参照[配置参数说明](./config_reference.md)了解Motor全量配置参数的含义和配置方法。

---

## 操作方法

1. 在服务部署完成后，修改user_config.json配置文件

    ```bash
    vim user_config.json
    ```

2. 执行以下命令，第1步中修改的的配置参数将在已运行的服务中生效

    ```bash
    # --update_config代表热更新配置
    python deploy.py --config_dir <配置目录> --update_config
    ```
