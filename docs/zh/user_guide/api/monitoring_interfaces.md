# 监控接口

## 健康状态查询接口

**接口功能**

查询服务的健康状态。

**接口格式**

请求类型：**GET**
> URL：`http(s)://{IP}:{Port}/health`

IP与端口参见[监控接口的IP/端口与配置](./README.md#监控接口的ip端口与配置)

**请求参数**

无

**使用样例**

```bash
curl -X GET "http://{IP}:{Port}/health"
```

**响应示例**

```JSON
{ "status": "ok", "timestamp": "2026-07-02T10:00:00Z" }
```

---

## 指标查询接口

**接口功能**

返回Prometheus兼容的监控指标文本，支持通过 `type` 参数切换指标聚合粒度。

**接口格式**

请求类型：**GET**
> URL：`http(s)://{IP}:{Port}/metrics?type={指标类型}&role={角色名称}`

IP与端口参见[监控接口的IP/端口与配置](./README.md#监控接口的ip端口与配置)

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
curl -X GET "http://{IP}:{Port}/metrics"
curl -X GET "http://{IP}:{Port}/metrics?type=full"

# 实例级指标（注入 instance_id 和 role 标签）
curl -X GET "http://{IP}:{Port}/metrics?type=instance"

# 所有角色的聚合指标（返回 dict，key 为角色名，value 为 Prometheus 文本）
curl -X GET "http://{IP}:{Port}/metrics?type=role"

# 仅 Prefill 角色的聚合指标
curl -X GET "http://{IP}:{Port}/metrics?type=role&role=prefill"

# 仅 Decode 角色的聚合指标
curl -X GET "http://{IP}:{Port}/metrics?type=role&role=decode"
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
> URL：`http(s)://{IP}:{Port}/instance/metrics`

IP与端口参见[监控接口的IP/端口与配置](./README.md#监控接口的ip端口与配置)

**响应示例**

```text
# /instance/metrics is deprecated. Use GET /metrics?type=instance instead.
```
