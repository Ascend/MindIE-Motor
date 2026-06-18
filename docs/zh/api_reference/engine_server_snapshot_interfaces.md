# Engine Server 快照接口

## 接口说明

Engine Server 快照接口用于容器快照场景下的推理引擎保存设备侧快照、设备解锁与设备侧快照恢复。接口挂载在 Engine Server **推理面（InferEndpoint）**，与 `/v1/chat/completions`、`/health` 等推理接口共用 `engine_server` 启动参数 `--port` 指定的业务端口。

典型调用顺序为：`suspend` →（可选）`device_unlock` → `resume`。具体时机由部署方决定。

Engine Server 快照接口使用推理端口：

- 基址：`http(s)://{EngineIP}:{推理端口}`
- 安全协议：`infer_tls_config.enable_tls` 为 `true` 时使用 `https`，否则使用 `http`。

>[!NOTE]说明
>
> - `{EngineIP}`：Engine Server 所在节点的 IP 或 `engine_server --host` 绑定的地址。
> - `{推理端口}`：`engine_server --port` 指定的端口。
> - 上述接口挂载在 Engine Server 推理面，**不在** Coordinator 推理端口上提供服务。

---

## 设备侧快照保存接口

**接口功能**

通知推理引擎将模型运行时权重落盘到指定路径，锁定设备并保存设备侧快照。

**接口格式**

请求类型：**POST**
URL：`http(s)://{EngineIP}:{推理端口}/suspend?model_save_path={模型落盘路径}`

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

## 设备解锁接口

**接口功能**

在调用设备侧快照保存接口后，设备会处于锁定状态。本接口用于通知推理引擎解锁设备。

**接口格式**

请求类型：**POST**
URL：`http(s)://{EngineIP}:{推理端口}/device_unlock`

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

## 设备侧快照恢复接口

**接口功能**

通知推理引擎恢复已保存的设备侧快照，从指定路径重新加载运行时模型权重并重建通信域等运行时状态。

**接口格式**

请求类型：**POST**
URL：`http(s)://{EngineIP}:{推理端口}/resume?data_parallel_master_ip={DP主节点IP}&model_path={模型路径}`

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
