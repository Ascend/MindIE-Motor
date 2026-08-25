# 管理接口

>[!NOTE]说明
>
> Kubernetes 部署应通过 ClusterIP、NetworkPolicy 等限制管理端口的访问范围。裸机独立部署如需对外监听，建议同时启用管理面 API Key 和 `mgmt_tls_config`；API Key 负责身份校验，TLS 负责防窃听。

## 管理面 API Key 鉴权

配置 `mgmt_api_key_config.enable_api_key=true` 后，下列特权接口必须携带请求头
`X-Motor-Management-Key: <key>`：

- `GET /instances`
- `POST /instances/refresh`
- `POST /precision/alarm_cleared`

`/startup`、`/liveness`、`/readiness` 和根路径保持免鉴权，Kubernetes 探针无需改动。缺少请求头返回 `401`，密钥错误返回 `403`。密钥从 `mgmt_api_key_config.api_key_file` 指定的单行文件读取，不应直接写入 JSON 配置。API Key 本身不加密传输，跨主机访问应同时开启管理面 TLS。

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

## 健康状态查询接口

**接口功能**

查询服务的健康状态。

**接口格式**

请求类型：**GET**
> URL：`http(s)://{IP}:{Port}/health`

IP与端口参见[指标接口的IP/端口与配置](./README.md#指标接口的ip端口与配置)

**请求参数**

无

**使用样例**

```bash
curl -X GET "http://{IP}:{Port}/health"
```

**响应示例**

```JSON
{ "status": "ok", "timestamp": "2026-07-02T10:00:00+00:00" }
```

>[!NOTE]说明
>`/health` 与 `/metrics` 同挂 Coordinator Observability 端口（`coordinator_obs_port`，默认 `1027`，K8s nodePort `31017`），**不在**管理接口端口（`coordinator_api_mgmt_port`，默认 `1026`）上提供服务。

---

## 实例查询接口

**接口功能**

查询 Coordinator 当前登记的实例（含 available / unavailable / paused）。

**接口格式**

请求类型：**GET**
> URL：`http(s)://{IP}:{Port}/instances`

IP与端口参见[管理接口的IP/端口与配置](./README.md#管理接口的ip端口与配置)

**请求参数**
无

**使用样例**

```bash
curl -X GET "http://{IP}:{Port}/instances" \
  -H "X-Motor-Management-Key: <key>"
```

独立部署也可用：

```bash
python3 -m motor.coordinator.register list
```

**响应示例**

```JSON
{
  "count": 2,
  "instances": [
    {
      "id": 7,
      "role": "decode",
      "job_name": "Qwen3-8B-decode-10.10.0.12-8000",
      "model_name": "Qwen3-8B",
      "status": "active",
      "endpoints": [
        {
          "id": 0,
          "ip": "10.10.0.12",
          "business_port": "8000",
          "headless": false
        }
      ]
    },
    {
      "id": 42,
      "role": "prefill",
      "job_name": "Qwen3-8B-prefill-10.10.0.11-8000",
      "model_name": "Qwen3-8B",
      "status": "active",
      "endpoints": [
        {
          "id": 0,
          "ip": "10.10.0.11",
          "business_port": "8000",
          "headless": false
        }
      ]
    }
  ]
}
```

**输出说明**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `count` | int | 当前登记的实例数量。 |
| `instances` | array | 实例摘要列表，按 `role`、`id` 排序。 |
| `instances[].id` | int | 实例 ID。独立部署注册时由 `role` + 排序后的完整 endpoint 组派生。 |
| `instances[].role` | string | 实例角色：`prefill` / `decode` / `union`。 |
| `instances[].job_name` | string | 实例作业名。 |
| `instances[].model_name` | string | 模型名。 |
| `instances[].status` | string | 实例状态，如 `active` / `inactive` / `paused`。 |
| `instances[].endpoints` | array | 该实例下的业务 endpoint。 |
| `instances[].endpoints[].business_port` | string | 引擎 HTTP 端口。 |

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
- 启用管理面 API Key 时必选：`X-Motor-Management-Key: <key>`

**请求参数**

| 参数名 | 类型 | 说明 |
|---|---|---|
| event | string | 必选；事件类型：`add` / `del` / `set`。 |
| instances | array | 必选；实例列表。 |

**使用样例**

>[!NOTE]说明
>请求体必须为JSON格式，且大小不得超过10MB。
>[!WARNING]版本升级
> `Endpoint` 注册协议已删除不再使用的 `mgmt_port`，并严格拒绝未知字段。Controller、Coordinator 和 NodeManager 必须同步升级，不支持新旧组件混合运行。

```bash
curl -X POST "http://{IP}:{Port}/instances/refresh" \
  -H "Content-Type: application/json" \
  -H "X-Motor-Management-Key: <key>" \
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
              "bootstrap_port": 21000
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

`instances[].endpoints` 中的 `bootstrap_port` 为可选字段，仅用于 SGLang PD 原生 bootstrap
对接。

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

## 精度告警状态清理接口

**接口功能**

清理 Coordinator 调度器中指定 P/D 实例组的精度告警状态。该接口供 Controller/运维编排在
精度告警已处理后调用，不负责终止实例。

**接口格式**

请求类型：**POST**
> URL：`http(s)://{IP}:{Port}/precision/alarm_cleared`

IP 与端口参见[管理接口的IP/端口与配置](./README.md#管理接口的ip端口与配置)

请求头：

- 必选：`Content-Type: application/json`
- 启用管理面 API Key 时必选：`X-Motor-Management-Key: <key>`

**请求参数**

| 参数名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `d_instance_id` | integer | 是 | Decode 实例 ID。 |
| `p_instance_id` | integer | 否 | Prefill 实例 ID；不传表示仅按 Decode 实例清理。 |

**使用样例**

```bash
curl -X POST "http://{IP}:{Port}/precision/alarm_cleared" \
  -H "Content-Type: application/json" \
  -H "X-Motor-Management-Key: <key>" \
  -d '{"d_instance_id": 2, "p_instance_id": 1}'
```

**响应示例**

```json
{
  "request_id": "precision_alarm_cleared",
  "status": "success",
  "message": "Precision alarm state cleared",
  "data": {"dismissed": true}
}
```

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
  "description": "Management plane: liveness, startup, readiness, metrics, instance list/refresh",
  "endpoints": {
    "GET /liveness": "liveness check",
    "GET /startup": "startup probe",
    "GET /readiness": "readiness check",
    "GET /instances": "list registered instances",
    "POST /instances/refresh": "refresh instances",
    "POST /precision/alarm_cleared": "clear precision alarm scheduler state"
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
