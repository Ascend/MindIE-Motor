# Engine Server 内部接口

>[!NOTE]说明
>
> Engine Server 内部接口挂载在 Engine Server 推理面，**不在** Coordinator 推理接口上提供服务。

## Engine Server 快照接口

Engine Server 快照接口用于容器快照场景下的推理引擎保存设备侧快照、设备解锁与设备侧快照恢复。接口挂载在 Engine Server **推理面（InferEndpoint）**，与 `/v1/chat/completions`、`/health` 等推理接口共用 `engine_server` 启动参数 `--port` 指定的业务端口。

典型调用顺序为：`suspend` →（可选）`device_unlock` → `resume`。具体时机由部署方决定。

Engine Server 快照接口使用推理端口：

- 基址：`http(s)://{EngineIP}:{推理端口}`
- 安全协议：`infer_tls_config.enable_tls` 为 `true` 时使用 `https`，否则使用 `http`。

IP与端口参见[内部接口的IP/端口](./README.md#内部接口的ip端口)

---

### 设备侧快照保存接口

**接口功能**

通知推理引擎将模型运行时权重落盘到指定路径，锁定设备并保存设备侧快照。

**接口格式**

请求类型：**POST**
> URL：`http(s)://{EngineIP}:{推理端口}/suspend?model_save_path={模型落盘路径}`

IP与端口参见[内部接口的IP/端口](./README.md#内部接口的ip端口)

**请求参数**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| `model_save_path` | string | 必选；Query 参数。模型权重等数据的落盘目录。 |

**使用样例**

```bash
curl -X POST "http://{EngineIP}:{推理端口}/suspend?model_save_path=/snapshot/weight"
```

**响应示例**

- 成功：HTTP `200`，响应体为空。
- 失败：缺少必填参数 `model_save_path` 时返回 `400`；当前引擎未实现 `suspend` / `resume` 时返回 `501`。

---

### 设备解锁接口

**接口功能**

在调用设备侧快照保存接口后，设备会处于锁定状态。本接口用于通知推理引擎解锁设备。

**接口格式**

请求类型：**POST**
> URL：`http(s)://{EngineIP}:{推理端口}/device_unlock`

IP与端口参见[内部接口的IP/端口](./README.md#内部接口的ip端口)

**请求参数**

无

**使用样例**

```bash
curl -X POST "http://{EngineIP}:{推理端口}/device_unlock"
```

**响应示例**

- 成功：HTTP `200`，响应体为空。
- 失败：当前引擎未实现 `device_unlock` 时返回 `501`。

---

### 设备侧快照恢复接口

**接口功能**

通知推理引擎恢复已保存的设备侧快照，从指定路径重新加载运行时模型权重并重建通信域等运行时状态。

**接口格式**

请求类型：**POST**
> URL：`http(s)://{EngineIP}:{推理端口}/resume?data_parallel_master_ip={DP主节点IP}&model_path={模型路径}`

IP与端口参见[内部接口的IP/端口](./README.md#内部接口的ip端口)

**请求参数**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| `data_parallel_master_ip` | string | 必选；Query 参数。数据并行（DP）主节点 IP。 |
| `model_path` | string | 必选；Query 参数。模型加载路径。 |

**使用样例**

```bash
curl -X POST "http://{EngineIP}:{推理端口}/resume?data_parallel_master_ip=10.0.0.1&model_path=/snapshot/weight"
```

**响应示例**

- 成功：HTTP `200`，响应体为空。
- 失败：缺少必填参数 `data_parallel_master_ip` 或 `model_path` 时返回 `400`；当前引擎未实现 `suspend` / `resume` 时返回 `501`。

---

## MetaServer转发接口

**接口功能**

仅在PD/CDP分离部署场景使用，用于D节点将请求转发至P节点。

**接口格式**

请求类型：**POST**
> URL：`http(s)://{EngineIP}:{推理端口}/v1/metaserver`

IP与端口参见[内部接口的IP/端口](./README.md#内部接口的ip端口)

**请求参数**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `model` | string | 必选；模型名称，透传至目标节点。 |
| `messages` | array | 与 `prompt` 二选一；Chat输入。 |
| `prompt` | string | 与 `messages` 二选一；Completion 输入。 |
| `stream` | boolean | 可选；是否流式返回，透传至目标节点。 |
| `kv_transfer_params` | object | 必选；转发控制参数。 |
| `kv_transfer_params.request_id` | string | 必选；请求标识，用于跨节点跟踪与关联。 |
| `kv_transfer_params.do_remote_decode` | boolean | 可选；是否在目标节点执行 Decode。 |
| `kv_transfer_params.do_remote_prefill` | boolean | 可选；是否在目标节点执行 Prefill。 |
| `kv_transfer_params.remote_engine_id` | string | 必选；目标节点引擎 ID。 |
| `kv_transfer_params.remote_host` | string | 必选；目标节点地址（IP 或域名）。 |
| `kv_transfer_params.remote_port` | string | 必选；目标节点端口。 |

**使用样例**

- CDP分离场景，D节点触发P节点Prefill：

  ```json
  curl -X POST "http://{EngineIP}:{推理端口}/v1/metaserver" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3",
    "messages": [
      { "role": "user", "content": "Hello!" }
    ],
    "stream": false,
    "kv_transfer_params": {
      "request_id": "req-id",
      "do_remote_decode": false,
      "do_remote_prefill": true,
      "remote_engine_id": "engine-p-0",
      "remote_host": "10.0.0.12",
      "remote_port": "1000"
    }
  }'
  ```

- PD分离场景，P节点触发D节点Decode：

  ```json
  curl -X POST "http://{EngineIP}:{推理端口}/v1/metaserver" \
    -H "Content-Type: application/json" \
    -d '{
      "model": "qwen3",
      "messages": [
        { "role": "user", "content": "Hello!" }
      ],
      "stream": false,
      "kv_transfer_params": {
        "request_id": "req-id",
        "do_remote_decode": true,
        "do_remote_prefill": false,
        "remote_engine_id": "engine-d-0",
        "remote_host": "10.0.0.21",
        "remote_port": "1001"
      }
    }'
  ```

**响应示例**

- CDP分离场景，透传P节点响应内容：

  ```JSON
  {
    "id": "chatcmpl-xxx12",
    "object": "chat.completion",
    "created": 1738828800,
    "model": "qwen3",
    "choices": [
      {
        "index": 0,
        "message": {
          "role": "assistant",
          "content": "Hello! How can I help you?"
        },
        "finish_reason": "stop"
      }
    ],
    "usage": {
      "prompt_tokens": 6,
      "completion_tokens": 7,
      "total_tokens": 13
    }
  }
  ```

- PD分离场景，透传D节点响应内容：

  ```JSON
  {
    "id": "chatcmpl-xxx",
    "object": "chat.completion",
    "created": 1738828800,
    "model": "qwen3",
    "choices": [
      {
        "index": 0,
        "message": {
          "role": "assistant",
          "content": "Hello! How can I help you?"
        },
        "finish_reason": "stop"
      }
    ],
    "usage": {
      "prompt_tokens": 8,
      "completion_tokens": 9,
      "total_tokens": 17
    }
  }
  ```

**输出说明**
该示例为非流式 `chat.completion`的输出说明：

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `id` | string | 响应 ID。 |
| `object` | string | 响应对象类型，示例为 `chat.completion`。 |
| `created` | integer | 响应创建时间（Unix 时间戳）。 |
| `model` | string | 实际使用的模型名称。 |
| `choices` | array | 生成结果列表。 |
| `choices[].index` | integer | 结果序号。 |
| `choices[].message.role` | string | 角色，示例为 `assistant`。 |
| `choices[].message.content` | string | 生成内容。 |
| `choices[].finish_reason` | string | 结束原因，如 `stop`、`length` 等。 |
| `usage` | object | Token 统计信息。 |
| `usage.prompt_tokens` | integer | 输入 Token 数量。 |
| `usage.completion_tokens` | integer | 输出 Token 数量。 |
| `usage.total_tokens` | integer | 总 Token 数量。 |
