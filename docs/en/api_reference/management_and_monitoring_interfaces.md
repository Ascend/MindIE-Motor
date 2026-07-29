# Management and Monitoring APIs

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:07:43.674Z pushedAt=2026-06-27T03:23:42.458Z -->

## Startup Probe API

**API Function**

Allows probes to query the service startup status.

**API Format**

Request Type: **GET**  
URL: `http(s)://{CoordinatorIP}:{Management Port}/startup`

  >[!NOTE]NOTE
  >
  > - `{CoordinatorIP}`: The IP address or domain name of the machine where the Coordinator service is deployed. The value is taken from the configuration `api_config.coordinator_api_host` (default `127.0.0.1`). Refer to the value in `deployer/user_config.json` or the actual node IP at runtime.
  > - `{Management Port}`: Configuration item `api_config.coordinator_api_mgmt_port` (default `1026`).

**Request Parameters**  
None

**Usage Example**

```bash
curl -X GET "http://{CoordinatorIP}:{Management Port}/startup"
```

**Response Example**

```JSON
{ "status": "ok", "message": "Coordinator is starting up" }
```

## Liveness Probe API

**API Function**

Allows probes to query the service liveness status.

**API Format**

Request Type: **GET**  
URL: `http(s)://{CoordinatorIP}:{Management Port}/liveness`

  >[!NOTE]NOTE
  >
  > - `{CoordinatorIP}`: The IP address or domain name of the machine where the Coordinator service is deployed. The value is taken from the configuration `api_config.coordinator_api_host` (default `127.0.0.1`). Refer to the value in `deployer/user_config.json` or the actual node IP at runtime.
  > - `{Management Port}`: Configuration item `api_config.coordinator_api_mgmt_port` (default `1026`).

**Request Parameters**  
None

**Usage Example**

```bash
curl -X GET "http://{CoordinatorIP}:{Management Port}/liveness"
```

**Response Example**

- Response example:

```JSON
{ "status": "ok", "message": "Coordinator is alive" }
```

## Readiness Probe API

**API Function**

Queries whether the service is ready.

**API Format**

Request Type: **GET**  
URL: `http(s)://{CoordinatorIP}:{Management Port}/readiness`

  >[!NOTE]NOTE
  >
  > - `{CoordinatorIP}`: The IP address or domain name of the machine where the Coordinator service is deployed. The value is taken from the configuration `api_config.coordinator_api_host` (default `127.0.0.1`). Refer to the value in `deployer/user_config.json` or the actual node IP at runtime.
  > - `{Management Port}`: Configuration item `api_config.coordinator_api_mgmt_port` (default `1026`).

**Request Parameters**  
None

**Usage Example**

```bash
curl -X GET "http://{CoordinatorIP}:{Management Port}/readiness"
```

**Response Example**

```JSON
{ "status": "ok", "message": "Coordinator is ok", "ready": true }
```

>[!NOTE]NOTE
>If the active/standby mode is enabled and the current node is not the active node, `503` is returned with the message `Coordinator is not master`.

## Metrics Query API

**API Function**

Returns Prometheus-compatible monitoring metric text, supporting switching of metric aggregation granularity via the `type` parameter.

**API Format**

Request Type: **GET**  
URL: `http(s)://{CoordinatorIP}:{Management Port}/metrics?type={Metric Type}&role={Role Name}`

  >[!NOTE]NOTE
  >
  > - `{CoordinatorIP}`: The IP or domain name of the machine where the Coordinator service is deployed. The value is taken from the configuration `api_config.coordinator_api_host` (default `127.0.0.1`). Refer to the value in `deployer/user_config.json` or the actual runtime node IP.
  > - `{Management Port}`: Configuration item `api_config.coordinator_api_mgmt_port` (default `1026`).

**Request Parameters**

| Parameter | Type | Required/Optional | Default Value | Description |
|--------|------|------|--------|------|
| `type` | string | Optional | `full` | Metric aggregation type: `full` (full aggregation), `instance` (instance-level), `role` (aggregated by role) |
| `role` | string | Optional | None | When `type=role`, filters by the specified role: `prefill` or `decode`. If omitted, returns aggregated metrics across all roles. |

**`type` Value Description**

| Value | Content-Type | Response Format | Description |
|------|-------------|----------|------|
| `full` (default) | `text/plain` | Prometheus text | Global aggregation metric, where metrics from all instances/endpoints are aggregated into a single value and can be directly scraped by Prometheus |
| `instance` | `text/plain` | Prometheus text | Instance-level metrics, where `instance_id` and `role` labels are injected into the label of each metric to distinguish data from different instances |
| `role` (with role specified) | `text/plain` | Prometheus text | Aggregated metrics for a specified role (`prefill` / `decode`), with the `role` label injected into the labels |
| `role` (without role specified) | `text/plain` | Prometheus text | Aggregated metrics for all roles concatenated into a single Prometheus text, which can be directly scraped by Prometheus |

**Data Processing Description**

The Coordinator performs all data processing internally at the `/metrics` endpoint (instance-level label injection, role-level aggregation, Prometheus format serialization). Callers directly obtain the metric data in its final format without needing any secondary processing.

**Usage Example**

```bash
# Full aggregated metrics (default, identical to no-param behavior)
curl -X GET "http://{CoordinatorIP}:{Management Port}/metrics"
curl -X GET "http://{CoordinatorIP}:{Management Port}/metrics?type=full"

# Instance-level metrics (injected with instance_id and role labels)
curl -X GET "http://{CoordinatorIP}:{Management Port}/metrics?type=instance"

# Aggregated metrics for all roles (returns a dict, with role name as key and Prometheus text as value)
curl -X GET "http://{CoordinatorIP}:{Management Port}/metrics?type=role"

# Aggregated metrics for the prefill role only
curl -X GET "http://{CoordinatorIP}:{Management Port}/metrics?type=role&role=prefill"

# Aggregated metrics for the decode role only
curl -X GET "http://{CoordinatorIP}:{Management Port}/metrics?type=role&role=decode"
```

**Response Example (`type=full`, default)**

```text
# HELP python_gc_objects_collected_total Objects collected during gc
# TYPE python_gc_objects_collected_total counter
python_gc_objects_collected_total{generation="0"} 136662.0
python_gc_objects_collected_total{generation="1"} 18996.0
python_gc_objects_collected_total{generation="2"} 5696.0
# HELP python_gc_objects_uncollectable_total Uncollectable objects found during GC
# TYPE python_gc_objects_uncollectable_total counter
python_gc_objects_uncollectable_total{generation="0"} 0.0
python_gc_objects_uncollectable_total{generation="1"} 0.0
python_gc_objects_uncollectable_total{generation="2"} 0.0
# HELP python_gc_collections_total Number of times this generation was collected
# TYPE python_gc_collections_total counter
python_gc_collections_total{generation="0"} 6587.0
python_gc_collections_total{generation="1"} 596.0
python_gc_collections_total{generation="2"} 40.0
# HELP python_info Python platform information
# TYPE python_info gauge
python_info{implementation="CPython",major="3",minor="11",patchlevel="10",version="3.11.10"} 4.0
# HELP process_virtual_memory_bytes Virtual memory size in bytes.
# TYPE process_virtual_memory_bytes gauge
process_virtual_memory_bytes 46601515008.0
```

**Response Example (`type=instance`)**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{instance_id="0",role="prefill",model_name="Qwen2.5-7B-Instruct"} 12.0
vllm:num_requests_running{instance_id="1",role="prefill",model_name="Qwen2.5-7B-Instruct"} 8.0
vllm:num_requests_running{instance_id="2",role="decode",model_name="Qwen2.5-7B-Instruct"} 6.0
vllm:num_requests_running{instance_id="3",role="decode",model_name="Qwen2.5-7B-Instruct"} 4.0
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{instance_id="0",role="prefill",model_name="Qwen2.5-7B-Instruct"} 0.62
vllm:kv_cache_usage_perc{instance_id="1",role="prefill",model_name="Qwen2.5-7B-Instruct"} 0.45
vllm:kv_cache_usage_perc{instance_id="2",role="decode",model_name="Qwen2.5-7B-Instruct"} 0.72
vllm:kv_cache_usage_perc{instance_id="3",role="decode",model_name="Qwen2.5-7B-Instruct"} 0.55
```

**Response Example (`type=role&role=prefill`)**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{role="prefill",model_name="Qwen2.5-7B-Instruct"} 20.0
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{role="prefill",model_name="Qwen2.5-7B-Instruct"} 0.535
```

**Response Example (`type=role`, role not specified)**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{role="prefill",model_name="Qwen2.5-7B-Instruct"} 20.0
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{role="prefill",model_name="Qwen2.5-7B-Instruct"} 0.535
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{role="decode",model_name="Qwen2.5-7B-Instruct"} 10.0
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{role="decode",model_name="Qwen2.5-7B-Instruct"} 0.635
```

## Instance Refresh API

**API Function**

Refreshes the instance list in the Coordinator (`add`/`del`/`set`).

**API Format**

Request Type: **POST**  
URL: `http(s)://{CoordinatorIP}:{Management Port}/instances/refresh`

  >[!NOTE]NOTE
  >
  > - `{CoordinatorIP}`: The IP address or domain name of the machine where the Coordinator service is deployed. The value is taken from the configuration `api_config.coordinator_api_host` (default `127.0.0.1`). Refer to the value in `deployer/user_config.json` or the actual node IP at runtime.
  > - `{Management Port}`: Configuration item `api_config.coordinator_api_mgmt_port` (default `1026`).

Request Headers:

- Required: `Content-Type: application/json`

- Optional: None

**Request Parameters**

| Parameter | Type | Description |
|---|---|---|
| event | string | Required; event type: `add` / `del` / `set`. |
| instances | array | Required; list of instances. |

**Usage Example**

>[!NOTE]
>The request body must be in JSON format and must not exceed 10 MB in size.

```bash
curl -X POST "http://{CoordinatorIP}:{Management Port}/instances/refresh" \
  -H "Content-Type: application/json" \
  -d '{
    "event": "add",
    "instances": [
      {
        "job_name": "test-job",
        "model_name": "test-model",
        "id": 1,
        "role": "prefill",
        "endpoints": {
          "192.168.1.1": {
            "0": {
              "id": 0,
              "ip": "192.168.1.1",
              "business_port": "8080",
              "mgmt_port": "8081"
            }
          }
        }
      }
    ]
  }'
```

**Response Example**

```JSON
{
  "request_id": "refresh_request",
  "status": "success",
  "message": "Instance refresh completed",
  "data": {
    "timestamp": "2026-01-29T12:00:00+00:00",
    "event_type": "add",
    "instance_count": 1
  }
}
```

**Output Description**

| Parameter | Type | Description |
|---|---|---|
| request_id | string | Request identifier. |
| status | string | Request status. |
| message | string | Response message. |
| data | object | Response data. |
| data.timestamp | string | Event time. |
| data.event_type | string | Event type, corresponding to the request `event`. |
| data.instance_count | integer | Number of instances. |

## Root Path Service Information API

**API Function**

Returns Coordinator service information and API index.

**API Format**

Request Type: **GET**
URL: `http(s)://{CoordinatorIP}:{Management Port}/`

  >[!NOTE]NOTE
  >
  > - `{CoordinatorIP}`: The IP address or domain name of the machine where the Coordinator service is deployed. The value is taken from the configuration `api_config.coordinator_api_host` (default `127.0.0.1`). Refer to the value in `deployer/user_config.json` or the actual runtime node IP.
  > - `{Management Port}`: Configuration Item `api_config.coordinator_api_mgmt_port` (default `1026`).

**Request Parameters**  
None

**Usage Example**

```bash
curl -X GET "http://{CoordinatorIP}:{Management Port}/"
```

**Response Example**

```JSON
{
  "service": "Motor Coordinator Server",
  "version": "1.0.0",
  "description": "coordinator server, management and inference APIs",
  "docs": {
    "management": [
      "/startup",
      "/liveness",
      "/readiness",
      "/metrics",
      "/instances/refresh"
    ],
    "inference": [
      "/v1/models",
      "/v1/chat/completions",
      "/v1/completions",
      "/v1/metaserver"
    ]
  },
  "timestamp": "2026-01-29T12:00:00+00:00"
}
```

**Output Description**

| Parameter | Type | Description |
| --- | --- | --- |
| `service` | string | Service name. |
| `version` | string | Service version number. |
| `description` | string | Service description. |
| `docs` | object | API index information. |
| `docs.management` | array | List of management and monitoring APIs. |
| `docs.inference` | array | List of inference APIs. |
| `timestamp` | string | Service timestamp. |
