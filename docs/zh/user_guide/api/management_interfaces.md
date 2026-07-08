# 管理接口

>[!NOTE]说明
>
> 管理接口仅限Kubernetes集群内使用，不提供给集群外使用。

## 启动探针接口

**接口功能**

供探针查询服务启动状态。

**接口格式**

请求类型：**GET**
> URL：`http(s)://{IP}:{Port}/startup`

IP与端口参见[管理接口的IP/端口与配置](./README.md#管理接口的ip端口与配置)

**请求参数**
无

**使用样例**

```bash
curl -X GET "http://{IP}:{Port}/startup"
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
> URL：`http(s)://{IP}:{Port}/liveness`

IP与端口参见[管理接口的IP/端口与配置](./README.md#管理接口的ip端口与配置)

**请求参数**
无

**使用样例**

```bash
curl -X GET "http://{IP}:{Port}/liveness"
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
> URL：`http(s)://{IP}:{Port}/readiness`

IP与端口参见[管理接口的IP/端口与配置](./README.md#管理接口的ip端口与配置)

**请求参数**
无

**使用样例**

```bash
curl -X GET "http://{IP}:{Port}/readiness"
```

**响应示例**

```JSON
{ "status": "ok", "message": "Coordinator is ok", "ready": true }
```

>[!NOTE]说明
>若启用主备模式且当前节点非主节点，返回 `503`，并提示 `Coordinator is not master`。

---

## 实例刷新接口

**接口功能**

刷新Coordinator中的实例列表（add/del/set）。

**接口格式**

请求类型：**POST**
> URL：`http(s)://{IP}:{Port}/instances/refresh`

IP与端口参见[管理接口的IP/端口与配置](./README.md#管理接口的ip端口与配置)

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
curl -X POST "http://{IP}:{Port}/instances/refresh" \
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
> URL：`http(s)://{IP}:{Port}/`

IP与端口参见[管理接口的IP/端口与配置](./README.md#管理接口的ip端口与配置)

**请求参数**
无

**使用样例**

```bash
curl -X GET "http://{IP}:{Port}/"
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
  }
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
