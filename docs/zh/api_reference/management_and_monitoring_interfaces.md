# 管理和监控接口

## 启动探针接口

**接口功能**

供探针查询服务启动状态。

**接口格式**

请求类型：**GET**
URL：`http(s)://{CoordinatorIP}:{管理端口}/startup`

  >[!NOTE]说明
  >
  > - `{CoordinatorIP}`：Coordinator 服务部署机器的 IP 或域名，取值来自配置 `api_config.coordinator_api_host`（默认 `127.0.0.1`），参考 `deployer/user_config.json` 取值或实际运行时节点IP。
  > - `{管理端口}`：配置项 `api_config.coordinator_api_mgmt_port`（默认 `1026`）。

**请求参数**
无

**使用样例**

```bash
curl -X GET "http://{CoordinatorIP}:{管理端口}/startup"
```

**响应示例**

```JSON
{ "status": "ok", "message": "Coordinator is starting up" }
```

---

## 存活探针接口

**接口功能**

供探针查询服务存活状态。

**接口格式**

请求类型：**GET**
URL：`http(s)://{CoordinatorIP}:{管理端口}/liveness`

  >[!NOTE]说明
  >
  > - `{CoordinatorIP}`：Coordinator 服务部署机器的 IP 或域名，取值来自配置 `api_config.coordinator_api_host`（默认 `127.0.0.1`），参考 `deployer/user_config.json` 取值或实际运行时节点IP。
  > - `{管理端口}`：配置项 `api_config.coordinator_api_mgmt_port`（默认 `1026`）。

**请求参数**
无

**使用样例**

```bash
curl -X GET "http://{CoordinatorIP}:{管理端口}/liveness"
```

**响应示例**

- 响应示例：

```JSON
{ "status": "ok", "message": "Coordinator is alive" }
```

---

## 就绪探针接口

**接口功能**

查询服务是否就绪。

**接口格式**

请求类型：**GET**
URL：`http(s)://{CoordinatorIP}:{管理端口}/readiness`

  >[!NOTE]说明
  >
  > - `{CoordinatorIP}`：Coordinator 服务部署机器的 IP 或域名，取值来自配置 `api_config.coordinator_api_host`（默认 `127.0.0.1`），参考 `deployer/user_config.json` 取值或实际运行时节点IP。
  > - `{管理端口}`：配置项 `api_config.coordinator_api_mgmt_port`（默认 `1026`）。

**请求参数**
无

**使用样例**

```bash
curl -X GET "http://{CoordinatorIP}:{管理端口}/readiness"
```

**响应示例**

```JSON
{ "status": "ok", "message": "Coordinator is ok", "ready": true }
```

>[!NOTE]说明
>若启用主备模式且当前节点非主节点，返回 `503`，并提示 `Coordinator is not master`。

---

## 指标查询接口

**接口功能**

返回Prometheus兼容的监控指标文本，支持通过 `type` 参数切换指标聚合粒度。

**接口格式**

请求类型：**GET**
URL：`http(s)://{CoordinatorIP}:{Observability端口}/metrics?type={指标类型}&role={角色名称}`

  >[!NOTE]说明
  >
  > - `{CoordinatorIP}`：Coordinator 服务部署机器的 IP 或域名，取值来自配置 `api_config.coordinator_api_host`（默认 `127.0.0.1`），参考 `deployer/user_config.json` 取值或实际运行时节点IP。
  > - `{Observability端口}`：配置项 `api_config.coordinator_obs_port`（默认 `1027`）。Kubernetes 部署时通过 NodePort 对外暴露，可直接被 Prometheus 抓取。

**请求参数**

| 参数名 | 类型 | 必选 | 默认值 | 说明 |
|--------|------|------|--------|------|
| `type` | string | 否 | `full` | 指标聚合类型：`full`（全量聚合）、`instance`（实例级）、`role`（按角色聚合） |
| `role` | string | 否 | 无 | 当 `type=role` 时，过滤指定角色：`prefill` 或 `decode`。不传时返回所有角色的聚合指标 |

**`type` 取值说明**

| 取值 | Content-Type | 返回格式 | 说明 |
|------|-------------|----------|------|
| `full`（默认） | `text/plain` | Prometheus text | 全局聚合指标，所有实例/端点的指标被聚合为单一值，可直接被 Prometheus 抓取 |
| `instance` | `text/plain` | Prometheus text | 实例级指标，每条指标的 label 中注入 `instance_id` 和 `role` 标签，可区分不同实例的数据 |
| `role`（指定 role） | `text/plain` | Prometheus text | 指定角色（`prefill` / `decode`）的聚合指标，label 中注入 `role` 标签 |
| `role`（不指定 role） | `text/plain` | Prometheus text | 所有角色的聚合指标拼接为单一 Prometheus 文本，可直接被 Prometheus 抓取 |

**数据加工说明**

Coordinator 在 `/metrics` 端点内部完成所有数据加工（实例级标签注入、角色级聚合、Prometheus 格式序列化），调用方直接获取最终格式的指标数据，无需再做二次加工。

**使用样例**

```bash
# 全量聚合指标（默认，行为与不带参数时完全一致）
curl -X GET "http://{CoordinatorIP}:{Observability端口}/metrics"
curl -X GET "http://{CoordinatorIP}:{Observability端口}/metrics?type=full"

# 实例级指标（注入 instance_id 和 role 标签）
curl -X GET "http://{CoordinatorIP}:{Observability端口}/metrics?type=instance"

# 所有角色的聚合指标（返回 dict，key 为角色名，value 为 Prometheus 文本）
curl -X GET "http://{CoordinatorIP}:{Observability端口}/metrics?type=role"

# 仅 Prefill 角色的聚合指标
curl -X GET "http://{CoordinatorIP}:{Observability端口}/metrics?type=role&role=prefill"

# 仅 Decode 角色的聚合指标
curl -X GET "http://{CoordinatorIP}:{Observability端口}/metrics?type=role&role=decode"
```

**响应示例（`type=full`，默认）**

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

**响应示例（`type=instance`）**

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

**响应示例（`type=role&role=prefill`）**

```text
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{role="prefill",model_name="Qwen2.5-7B-Instruct"} 20.0
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{role="prefill",model_name="Qwen2.5-7B-Instruct"} 0.535
```

**响应示例（`type=role`，不指定 role）**

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

---

## 实例指标查询接口（已弃用）

> [!WARNING] 已弃用
> `GET /instance/metrics` 接口已弃用，请使用 `GET /metrics?type=instance` 代替。调用本接口将返回 HTTP 410 Gone。

**接口格式**

请求类型：**GET**
URL：`http(s)://{CoordinatorIP}:{Observability端口}/instance/metrics`

**响应示例**

```text
# /instance/metrics is deprecated. Use GET /metrics?type=instance instead.
```

---

## 实例刷新接口

**接口功能**

刷新Coordinator中的实例列表（add/del/set）。

**接口格式**

请求类型：**POST**
URL：`http(s)://{CoordinatorIP}:{管理端口}/instances/refresh`

  >[!NOTE]说明
  >
  > - `{CoordinatorIP}`：Coordinator 服务部署机器的 IP 或域名，取值来自配置 `api_config.coordinator_api_host`（默认 `127.0.0.1`），参考 `deployer/user_config.json` 取值或实际运行时节点IP。
  > - `{管理端口}`：配置项 `api_config.coordinator_api_mgmt_port`（默认 `1026`）。

请求头：

- 必选：`Content-Type: application/json`
- 可选：无

**请求参数**

| 参数名 | 类型 | 说明 |
|---|---|---|
| event | string | 必选；事件类型：`add` / `del` / `set`。 |
| instances | array | 必选；实例列表。 |

**使用样例**

>[!NOTE]说明
>请求体必须为JSON格式，且大小不得超过10MB。

```bash
curl -X POST "http://{CoordinatorIP}:{管理端口}/instances/refresh" \
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

**响应示例**

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

**输出说明**

| 参数名 | 类型 | 说明 |
|---|---|---|
| request_id | string | 请求标识。 |
| status | string | 请求状态。 |
| message | string | 响应消息。 |
| data | object | 响应数据。 |
| data.timestamp | string | 事件时间。 |
| data.event_type | string | 事件类型，与请求`event`对应。 |
| data.instance_count | integer | 实例数量。 |

---

## 根路径服务信息接口

**接口功能**

返回Coordinator服务信息与接口索引。

**接口格式**

请求类型：**GET**
URL：`http(s)://{CoordinatorIP}:{管理端口}/`

  >[!NOTE]说明
  >
  > - `{CoordinatorIP}`：Coordinator 服务部署机器的 IP 或域名，取值来自配置 `api_config.coordinator_api_host`（默认 `127.0.0.1`），参考 `deployer/user_config.json` 取值或实际运行时节点IP。
  > - `{管理端口}`：配置项 `api_config.coordinator_api_mgmt_port`（默认 `1026`）。

**请求参数**
无

**使用样例**

```bash
curl -X GET "http://{CoordinatorIP}:{管理端口}/"
```

**响应示例**

```JSON
{
  "service": "Motor Coordinator Management Server",
  "version": "1.0.0",
  "description": "Management plane: liveness, startup, readiness, instance refresh",
  "endpoints": {
    "GET /liveness": "liveness check",
    "GET /startup": "startup probe",
    "GET /readiness": "readiness check",
    "POST /instances/refresh": "refresh instances"
  },
  "timestamp": "2026-01-29T12:00:00+00:00"
}
```

**输出说明**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `service` | string | 服务名称。 |
| `version` | string | 服务版本号。 |
| `description` | string | 服务描述。 |
| `endpoints` | object | 接口索引信息，以 `HTTP方法 路径` 为键，说明为值。 |
| `timestamp` | string | 服务时间戳。 |

>[!NOTE]说明
>Metrics 可观测性端点（`/metrics`、`/instance/metrics`、`/health`）由 Observability 端口（默认 1027）独立提供服务，不在管理端口返回。详见 [Observability 接口](observability_interface.md)。
