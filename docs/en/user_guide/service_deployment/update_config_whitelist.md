# `--update_config` Trustlist

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:06:13.428Z pushedAt=2026-07-03T02:36:33.058Z -->

This document describes the scope of configuration items that can be modified by `deploy.py --update_config`.

## 1. Usage Constraints

- `--update_config` only allows modification of fields within the trustlist.

- The deployment script compares the current `user_config.json` item by item with the deployed `motor-config` baseline configuration in the cluster.

- If there are changes to fields outside the trustlist, or if unsupported fields are added under a trustlisted configuration block, the script will directly report an error and refuse the update.

- `--update_config` only refreshes the ConfigMap and does not re-apply the Deployment.

## 2. Trustlist Scope

The configuration items currently allowed to be modified through `--update_config` are as follows.

### 2.1 `motor_controller_config`

- `logging_config.log_level`: Controller log level, options include `DEBUG`, `INFO`, `WARNING`, `ERROR`, etc.

- `observability_config.observability_enable`: whether to enable Controller observability

- `observability_config.metrics_ttl`: observability metric cache retention duration, unit: second

### 2.2 `motor_coordinator_config`

- `logging_config.log_level`: Coordinator log level, options include `DEBUG`, `INFO`, `WARNING`, `ERROR`, etc.

- `exception_config.max_retry`: maximum number of retries after a request fails

- `exception_config.retry_delay`: wait time before each retry, unit: second

- `exception_config.first_token_timeout`: timeout for waiting for the first token to be returned, unit: second

- `exception_config.infer_timeout`: total timeout for a single inference request, unit: second

- `timeout_config.request_timeout`: total timeout for a single HTTP request, unit: second

- `timeout_config.connection_timeout`: timeout for establishing a connection, unit: second

- `timeout_config.read_timeout`: timeout for read operations, unit: second

- `timeout_config.write_timeout`: timeout for write operations, unit: second

- `timeout_config.keep_alive_timeout`: HTTP connection keep-alive duration; the connection is closed if no activity occurs within this timeout, unit: second

### 2.3 `motor_nodemanger_config`

- `logging_config.log_level`: log level of NodeManager, options include `DEBUG`, `INFO`, `WARNING`, `ERROR`, etc.

## 3. Unsupported Modifications

Except for the fields listed above, no other configuration items can be modified through `--update_config`, including but not limited to:

- Deployment resource-related configurations

- Number of instances

- Model and engine configuration

- TLS configuration

- Master/Standby configuration

- Rate limiting and authentication configuration

To scale up or down, use `--update_instance_num`. To change other configurations, re-execute the deployment following the normal deployment process.
