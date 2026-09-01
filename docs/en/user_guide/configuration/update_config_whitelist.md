# Hot Update of Configuration Parameters

In MindIE Motor, some configuration parameters can be dynamically modified while the service is running. This document describes the process.

## Configurable Fields for Hot Update

The fields that support hot update are **mainly related to the log level, instance runtime data query, request processing timeout, and similar content**.

- **`motor_controller_config`**

    `logging_config.log_level`: Controller log level. The options include `DEBUG`, `INFO`, `WARNING`, and `ERROR`.

    `observability_config.observability_enable`: whether to enable the external query interface, which is used to query the metric data, alarm information, and so on of the current inference instance.

    `observability_config.metrics_ttl`: how often the metric data of the inference instance is refreshed, unit: second.

- **`motor_coordinator_config`**

    `logging_config.log_level`: Coordinator log level. The options include `DEBUG`, `INFO`, `WARNING`, and `ERROR`.

    `exception_config.max_retry`: the maximum number of retries after an inference request fails.

    `exception_config.retry_delay`: the waiting time before retrying after an inference request fails, unit: second.

    `exception_config.first_token_timeout`: timeout for waiting for the first token to be returned, unit: second.

    `exception_config.infer_timeout`: total timeout for a single inference request, unit: second.

    `rate_limit_config.max_requests`: maximum number of requests allowed within the rate limiting time window. During hot update, the tokens already accumulated in the token bucket are settled at the old rate, and then accumulation continues with the new parameters.

    `rate_limit_config.window_size`: length of the time window for rate limiting statistics, unit: second. After hot update, the token bucket refill rate is recalculated together with `max_requests`.

    `rate_limit_config.skip_paths`: list of paths that do not participate in rate limiting statistics, customizable. Takes effect on subsequent requests immediately after hot update.

    `rate_limit_config.error_message`: prompt text returned to the client when rate limiting is triggered.

    `rate_limit_config.error_status_code`: HTTP status code returned when rate limiting is triggered, usually 4xx (such as 429).

    `rate_limit_config.max_request_body_size`: maximum request body size (MB). If exceeded, the request is rejected directly and 413 is returned, without consuming rate limiting tokens. `<= 0` means no limit. Decimal values are supported (for example, `0.5` means 0.5 MB, and 1 MB = 1024\*1024 bytes).

    Note: `rate_limit_config.enable_rate_limit` `rate_limit_config.provider`, `rate_limit_config.scope`, and `rate_limit_config.olc_config_path` are read only when the service starts and do not support hot update. To switch the rate limiting provider (simple/olc) or modify the OLC rule path, restart the service.

- **`motor_nodemanger_config`**

    `logging_config.log_level`: NodeManager log level. Options include `DEBUG`, `INFO`, `WARNING`, and `ERROR`.

Other configuration parameters do not support hot update yet. For the meanings and configuration methods of all Motor configuration parameters, see [Configuration Parameter Description](./config_reference.md).

## Operation Procedure

1. After the service deployment is complete, modify the `user_config.json` configuration file.

    ```bash
    vim user_config.json
    ```

2. Run the following command. The configuration parameters modified in step 1 will take effect in the running service.

    ```bash
    # --update_config indicates hot update configuration.tes hot update configuration.
    python deploy.py --config_dir <configuration_directory> --update_config
    ```
