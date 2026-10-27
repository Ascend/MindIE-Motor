# Service Rate Limiting

## Feature Introduction

Service rate limiting restricts the HTTP requests that enter per unit time at the Coordinator inference plane entry, preventing burst traffic from saturating the scheduling and inference instances. The configuration is located in `motor_coordinator_config.rate_limit_config` in `user_config.json` and is disabled by default.

After this feature is enabled, the inference process handles the requests entering the inference plane through middleware. Two built-in providers are available:

| Provider | Implementation |
|----------|------|
| simple | Global token bucket |
| olc | Overload control library, which matches rules by labels such as URL, Method, and IP |

The request body can also have a separate upper limit. If the limit is exceeded, HTTP `413` is returned, and no rate limiting token is consumed.

This capability is attached only to the Coordinator inference plane application and covers the following interfaces:

| Provider | Implementation |
|----------|------|
| POST | /v1/completions |
| POST | /v1/chat/completions |
| POST | /v1/responses |
| POST | /v1/messages |
| POST | /v1/messages/count_tokens |
| GET  | /v1/models |

The management plane, the Controller, the Node Manager, and inference engine processes are not subject to this rate limiting.

### How It Works

**Rate Limiting Mechanism (simple)**

simple implements rate limiting with a global token bucket. The parameters are as follows:

| Item | Description |
|----------|------|
| Bucket capacity | max_requests |
| Fill rate | max_requests / window_size (tokens/second) |
| Per request | Consumes 1 token |
| Initial state | Full bucket |

Decision rule: if enough tokens are available, the request is allowed; otherwise, the request is rejected immediately without queuing. Paths in `skip_paths` are matched by prefix (`startswith`). A matched path is allowed directly, consumes no token, and is not subject to the request body size check.

- The inference plane can start multiple Worker processes according to `inference_workers_config.num_workers`, with a default value of `4`. Each process holds its own token bucket, and the buckets are not shared. Congestion alarms are also reported by each process separately, and the Coordinator does not deduplicate them. Therefore, the total allowed throughput of `simple` rate limiting on a single Coordinator is approximately `max_requests × num_workers`, rather than the single `max_requests` in the configuration. When multiple Workers approach the quota at the same time, the Controller receives multiple congestion events.

- `scope` defaults to `global`. The current `simple` implementation always uses this single global bucket, and changing `scope` to another value does not enable rate limiting by IP or by user.

- When the rate limiting check fails, the request is allowed by default: if `is_allowed` throws an error, or the middleware fails to call the rate limiter, the inference plane is not entirely rejected due to an exception in the rate limiting module itself.

**Request Congestion Event**

After each decision, the simple rate limiter calculates the used quota used = max_requests - available (the capacity minus the currently available tokens), compares the ratio of the used quota to max_requests, and reports ReqCongestionEvent to the Controller.

| Condition | Behavior |
|------|------|
| Not reported yet, and used >= int(max_requests × 0.85) | Report once. The state is set only after the Controller accepts the report; if the report is not accepted, the state remains unreported. |
| Already reported, and used < int(max_requests × 0.75) | Report again. The state is cleared only after the Controller accepts the report; if the report is not accepted, the state remains reported. |

- The number in the additional information is the used quota. When the bucket is full and idle, `used` is very small, and no alarm is generated. After the state is set, it is not reported again until the used quota falls below the 75% threshold. Alarms are reported by each inference Worker process separately, consistent with the independent token buckets described in the previous section. The event has the following fixed parameters:

  - alarm_id=0xFC001005
  - Name: Coordinator Request Congestion Alarm
  - Level: MAJOR
  - reason_id=DEALING_WITH_CONGESTION

- `ControllerApiClient.report_alarms` does not throw an exception on failure. It returns `ok=false` (HTTP status other than 200 or a transport exception). If the rate limiter ignored this return value, the state would be set in advance, and no supplementary report would be sent after the Controller recovers. Currently, the state is toggled only when `ok` is returned. When the report is not accepted, the current request is not blocked, and a subsequent request retries the report at an interval of about 1 second, preventing a synchronous report for every request when the Controller is unreachable.

- When `max_requests` is 1, 2, 3, 4, 7, or 8, `int(max_requests × 0.85)` is equal to `int(max_requests × 0.75)`, so there is no hysteresis interval between triggering and recovery, and reports are repeated when the used quota fluctuates around that integer. When `max_requests` is 1, the recovery condition is `used < 0`, so the state is not cleared after being set. When `max_requests >= 10`, the two integer thresholds differ by at least 1.

**Request Body Size**

`max_request_body_size` is in MB (1 MB = 1024×1024 bytes) and allows decimal values, for example `0.5`. The startup validation rejects negative numbers. At runtime, a value less than or equal to 0 indicates no limit; when the value is greater than 0, the body size check is performed before tokens are consumed.

| Scenario | Handling |
|------|------|
| A valid `Content-Length` is present | Compare against this header. If the limit is exceeded, return `413` directly without pre-reading the body. |
| No valid `Content-Length` (for example, chunked) | Read the actual bytes first. If the limit is exceeded, return `413`; otherwise, replay the read body to the downstream. |

When `Content-Length` is present and the limit is exceeded, the response body is:

```json
{
  "error": "request_body_too_large",
  "message": "Request body size (52428800 bytes) exceeds maximum"
}
```

When `Content-Length` is absent and the actual bytes exceed the limit, `message` is:

```text
`Request body size exceeds maximum (<maximum bytes> bytes)`.
```

**Rejection Response and Response Headers**

A request rejected by rate limiting does not enter scheduling. The default HTTP status code is error_status_code (429), and the response body is:

```json
{
  "error": "rate_limit_exceeded",
  "message": "too many requests, please try again later",
  "details": {
    "available": 0,
    "limit": 1000,
    "window_size": 60
  }
}
```

simple adds the following headers to both allowed and rejected responses:

| Response Header | Description |
|------|------|
| X-RateLimit-Remaining | Number of remaining tokens |
| X-RateLimit-Limit | max_requests |
| X-RateLimit-Window | window_size |

**Overload Control (provider: olc)**

When `provider` is `olc` and `enable_rate_limit` is `true`, the startup validation requires `olc_config_path` to point to an existing directory. The directory must contain OLC rule files (such as `overload-config.properties` and `olc.json`). The startup validation checks only whether the directory exists and does not check whether these files are in the directory. If the files are missing, creating the OLC middleware fails and falls back to the token bucket.

The Coordinator writes this path to the environment variable `OLC_CONFIG_PATH` and mounts `OlcFastAPIAdapter`. The label extractor provides the following labels:

| Label | Source |
|------|------|
| URL | Request path |
| Method | HTTP method |
| IP | Client address, which is `unknown` if it cannot be obtained |

Rules such as quota and concurrency are determined by the OLC configuration. See the repository example `examples/features/http/overload_control` and the [OLC documentation](https://gitcode.com/openFuyao/olc-python).

When creating the OLC middleware fails, the Coordinator records the following error log:

```text
Using simple rate limit, Failed to create olc limit middleware
```

It then falls back to the built-in token bucket, and the service can still start normally.

**Hot Update**

After the simple rate limiter has been created at startup (enable_rate_limit=true and provider is simple, or the fallback has been performed after olc creation failed), a hot update immediately modifies the following fields:

| Label | Source |
|------|------|
| skip_paths, error_message, error_status_code, max_request_body_size | Take effect immediately. |
| enable_rate_limit | Written into the runtime switch. After it is changed to `false`, subsequent requests are allowed directly, and the request body size check is no longer performed; after it is changed back to `true`, the existing token bucket is used again. |
| max_requests, window_size | Settle the tokens in the bucket at the old rate first, and then update the capacity and the fill rate. If the new capacity is smaller, the excess tokens are truncated; if the capacity is larger, the difference is immediately added to the bucket. |

Configuration reloading requires both `max_requests` and `window_size` to be greater than 0. A value equal to 0 or a negative value does not enter the token bucket. The token bucket function itself rejects `max_requests < 0` and `window_size <= 0` and keeps the old parameters.

The following changes cannot be completed by a hot update and require restarting the Coordinator:

- `enable_rate_limit=false` at startup, with the rate limiting middleware to be installed later

- Switching `provider`, or modifying `olc_config_path`

- Modifying `scope` (the current implementation does not read this field for bucketing either)

For the full list of fields, see [Hot Update of Configuration Parameters](../configuration/update_config_whitelist.md).

### Core Capabilities

- **Global rate limiting**: `simple` uses a token bucket to limit the number of requests of a single inference Worker process within a time window. Requests that exceed the limit are rejected immediately without queuing.

- **Request body protection**: requests that exceed `max_request_body_size` return HTTP `413` and do not consume tokens.

- **Path exemptions**: `skip_paths` skips rate limiting and the request body check by prefix. By default, probes, documents, and `/v1/metaserver` can be exempted.

- **Overload control**: `olc` matches external rules by URL, Method, and IP; if loading fails, it falls back to the built-in token bucket.

- **Congestion alarms**: an alarm is reported to the Controller once when the used quota reaches 85% of the capacity, and reported again with the state cleared when the used quota falls below 75%.

- **Hot update**: after the `simple` rate limiter has been created at startup, the quota, exempt paths, error response, and runtime switch can be adjusted without a restart.

### Constraints and Limitations

| Dimension | Description |
|------|------|
| Deployment scenario | <ul><li>PD disaggregation service deployment</li><li>PD aggregation service deployment</li><li>Standalone Coordinator deployment</li></ul>Applies only to the Coordinator inference plane |
| Engine | Independent of the inference engine type |
| Feature mutual exclusion | None |
| Software dependency | When `provider=olc`, [OLC](https://gitcode.com/openFuyao/olc-python) (`olc-python` v0.1.0) must be installed |
| Other constraints | <ul><li>`simple` counts independently for each inference Worker process, and the total allowed throughput is approximately `max_requests × num_workers`.</li><li>Congestion alarms are also reported by each Worker process separately and are not merged into one alarm within the Coordinator.</li><li>When `max_requests` is 1, 2, 3, 4, 7, or 8, the rounded 85% and 75% thresholds are the same, so alarms may be reported repeatedly near the boundary; when it is 1, the alarm state is not cleared after being set.</li><li>`scope` does not change the bucketing method, which is currently a fixed in-process global bucket</li><li>If rate limiting is not enabled at startup, the middleware cannot be installed by a hot update.</li><li>A successfully loaded `olc` cannot switch the provider or the rule directory by a hot update.</li></ul> |

## Using the Feature

### Usage Scenarios

- The request rate at the Coordinator inference entry needs to be limited to prevent burst traffic from saturating the scheduling and inference instances.

- Oversized request bodies need to be rejected to prevent an excessively large body from occupying memory.

- External overload rules need to be applied by URL, method, or client address instead of a single global quota.

### Usage Example

**Enabling Service Rate Limiting**

Add `rate_limit_config` to `motor_coordinator_config`. The following is the minimal configuration of the built-in token bucket, which indicates about 1000 requests per 60 seconds (about 16.7 QPS on average), counted separately for each inference Worker process:

```json
{
  "motor_coordinator_config": {
    "rate_limit_config": {
      "enable_rate_limit": true,
      "provider": "simple",
      "max_requests": 1000,
      "window_size": 60
    }
  }
}
```

Example of all fields:

```json
{
  "motor_coordinator_config": {
    "rate_limit_config": {
      "enable_rate_limit": true,
      "provider": "simple",
      "max_requests": 1000,
      "window_size": 60,
      "scope": "global",
      "skip_paths": [
        "/liveness",
        "/readiness",
        "/metrics",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
        "/startup",
        "/v1/metaserver"
      ],
      "error_message": "too many requests, please try again later",
      "error_status_code": 429,
      "max_request_body_size": 0,
      "olc_config_path": ""
    }
  }
}
```

To use OLC, install the library first, and then point `olc_config_path` to the rule directory:

```bash
pip install git+https://gitcode.com/openFuyao/olc-python.git@v0.1.0
```

```json
{
  "motor_coordinator_config": {
    "rate_limit_config": {
      "enable_rate_limit": true,
      "provider": "olc",
      "olc_config_path": "examples/features/http/overload_control/config"
    }
  }
}
```

| Configuration Item | Type | Default Value | Description |
|--------|------|--------|------|
| enable_rate_limit | bool | `false` | Whether to install the rate limiting middleware at startup |
| provider | string | `simple` | `simple` or `olc`. Falls back to `simple` when `olc` creation fails |
| max_requests | int | `1000` | Token bucket capacity, which must be a positive number. Used only by `simple` |
| window_size | int | `60` | Time window (seconds), which must be a positive number. Fill rate = `max_requests / window_size` |
| scope | string | `global` | The current `simple` uses a fixed global bucket, and changing this field does not change the bucketing method |
| skip_paths | array | See the example above | Path prefixes that are not subject to rate limiting and the request body check |
| error_message | string | `too many requests, please try again later` | Rejection message |
| error_status_code | int | `429` | Rejection status code. The startup validation requires `100`–`599` |
| max_request_body_size | float | `0` | Request body upper limit (MB). `0` means no limit; negative numbers fail the startup validation; at runtime, `<= 0` means no check |
| olc_config_path | string | `""` | Mandatory when `provider=olc` and rate limiting is enabled, and must be an existing directory |

The field definitions are also described in the **`rate_limit_config` field** of [Configuration Parameter Description](../configuration/config_reference.md#motor_coordinator_config). For the minimal switch in Kubernetes PD disaggregation deployment, see [Service Rate Limiting](../deployment/k8s/pd_disaggregation_deployment.md#service-rate-limiting).

**Disabling Service Rate Limiting**

Delete `rate_limit_config`, or set `enable_rate_limit` to `false` and restart the service. If the `simple` middleware has been installed at startup, you can also change `enable_rate_limit` to `false` by a hot update.

### Verifying the Feature

1. Restart the Coordinator with the `simple` configuration above.

2. Send a request to the inference plane that does not match `skip_paths`. The allowed response should contain `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Window`.

3. Continuously saturate `max_requests` on a Worker process. After the limit is exceeded, HTTP `429` should be returned, with `rate_limit_exceeded` in the `error` field of the response body.

4. Set `max_request_body_size` to a small positive number and send a request that exceeds the limit. HTTP `413` should be returned, and the request does not consume tokens.

5. Default exempt paths such as `/liveness`, `/readiness`, and `/metrics` should continue to be allowed.

## FAQs

### Configuration Changes Do Not Take Effect

**Symptom**

After `rate_limit_config` is modified, the rate limiting behavior does not change without restarting the service.

**Cause Analysis**

A hot update takes effect only for a `simple` rate limiter that has been created at startup. If `enable_rate_limit=false` at startup, or a successfully loaded `olc` is used all along, modifying the configuration at runtime does not install or switch the middleware.

**Solution**

1. Confirm that `enable_rate_limit` is `true` in the startup configuration and that the current provider is `simple` (or that `olc` has fallen back to `simple`).

2. If rate limiting was not enabled at startup, or `provider` / `olc_config_path` needs to be switched, restart the Coordinator.

### Actual Throughput Is Significantly Higher Than max_requests

**Symptom**

Although `max_requests` is configured, the request volume actually handled by the cluster is significantly higher than this value.

**Cause Analysis**

`simple` maintains an independent token bucket for each inference Worker process. When `inference_workers_config.num_workers` is greater than 1, the total allowed throughput is approximately `max_requests × num_workers`.

**Solution**

Convert the threshold based on the number of Worker processes, or reduce `max_requests`.

### Global Rate Limiting Still Applies with a Per-IP or Per-User scope

**Symptom**

Although `scope` is set to `per_ip` or `per_user`, rate limiting still takes effect globally.

**Cause Analysis**

The current `simple` does not read `scope` for bucketing and always uses an in-process global token bucket.

**Solution**

If rate limiting by URL, method, or client address is required, use `provider: olc` and configure the corresponding rules.

### OLC Fallback Appears in the Log

**Symptom**

`Using simple rate limit, Failed to create olc limit middleware` appears in the log.

**Cause Analysis**

OLC is not installed, or the rules in the rule directory fail to load. The Coordinator has fallen back to the built-in token bucket.

**Solution**

1. Install OLC: `pip install git+https://gitcode.com/openFuyao/olc-python.git@v0.1.0`.

2. Confirm that the directory specified by `olc_config_path` exists and contains `overload-config.properties` and `olc.json`.

3. Restart the Coordinator.

### Startup Errors About olc_config_path or error_status_code

**Symptom**

The startup fails, and the error contains `olc_config_path is required`, `does not exist`, `provider must be 'simple' or 'olc'`, or `error_status_code must be in range 100-599`.

**Cause Analysis**

When `provider` is `olc` and rate limiting is enabled, `olc_config_path` is mandatory and must be an existing directory. `provider` can only be `simple` or `olc`. `error_status_code` must be within the range of `100`–`599`.

**Solution**

Correct the corresponding fields based on the error, and then restart the service.

### Client Receives 429

**Symptom**

The client receives HTTP `429`, and `error` in the response body is `rate_limit_exceeded`.

**Cause Analysis**

The tokens of the current process are used up.

**Solution**

Check `details.available` and the `X-RateLimit-*` response headers. Increase `max_requests`, or reduce the client request rate.

### Client Receives 413

**Symptom**

The client receives HTTP `413`, and `error` in the response body is `request_body_too_large`.

**Cause Analysis**

The request body exceeds `max_request_body_size` (MB). This rejection does not consume tokens. The default value `0` means no limit.

**Solution**

Increase `max_request_body_size`, or reduce the request body.
