# Observability 接口

## 接口说明

Observability 接口用于查询 Controller 汇聚的运维观测数据，包括模型服务清单、监控指标和告警信息。接口默认关闭，需要配置 `observability_config.observability_enable=true` 后生效。

Observability 查询接口使用独立端口：

- 服务地址：`api_config.controller_api_host`，默认使用 Pod IP，未获取到时为 `127.0.0.1`。
- Observability 端口：`api_config.observability_api_port`，默认 `1027`。
- 安全协议：`observability_tls_config.enable_tls=true` 时使用 `https`，否则使用 `http`。
- 指标缓存时间：`observability_config.metrics_ttl`，默认 `5` 秒。

>[!NOTE]说明
>
> - `{ControllerIP}`：Controller 服务部署机器的 IP 或域名。
> - `{Observability端口}`：配置项 `api_config.observability_api_port`。
> - 主备模式下，仅主 Controller 对外提供 Observability 查询能力；备 Controller 收到查询请求时返回内部错误。
> - 当 `observability_config.observability_enable=false` 时，查询类接口返回内部错误，错误信息为 `Observability is not enabled.`。

---

## 模型服务清单查询接口

**接口功能**

查询当前模型服务的运行清单，返回模型基础信息、P/D 实例列表、DP 分组、Pod 与 NPU 关联信息等。清单数据由 Controller 内部的 active、initial、inactive 实例列表汇总得到。

**接口格式**

请求类型：**GET**
URL：`http(s)://{ControllerIP}:{Observability端口}/observability/inventory`

**请求参数**

无

**使用样例**

```bash
curl -X GET "http://{ControllerIP}:{Observability端口}/observability/inventory"
```

**响应示例**

以下示例参考 `tests/controller/observability/inventory/test_inventory_collector.py` 中的正常用例：2 个 Prefill 实例、1 个 Decode 实例，模型名为 `qwen3-8B`，模型 ID 为 `model_123`。

```JSON
{
  "code": 200,
  "message": "Success",
  "data": {
    "inventories": {
      "PInstanceList": [
        {
          "ID": "mindie-pymotor-p0-123456",
          "Name": "mindie-pymotor-p0-123456",
          "InstanceStatus": "running",
          "podInfoList": [
            {
              "podID": "192.168.222.211",
              "podName": "",
              "podAssociatedInfoList": [
                { "NPUID": "0", "NPUIP": "10.0.245.10" },
                { "NPUID": "1", "NPUIP": "10.0.245.11" }
              ]
            },
            {
              "podID": "192.168.222.212",
              "podName": "",
              "podAssociatedInfoList": [
                { "NPUID": "0", "NPUIP": "10.0.245.10" },
                { "NPUID": "1", "NPUIP": "10.0.245.11" }
              ]
            }
          ],
          "serverIPList": [],
          "serverList": []
        }
      ],
      "DInstanceList": [
        {
          "ID": "mindie-pymotor-d0-123456",
          "Name": "mindie-pymotor-d0-123456",
          "InstanceStatus": "running",
          "podInfoList": [
            {
              "podID": "192.168.222.213",
              "podName": "",
              "podAssociatedInfoList": [
                { "NPUID": "0", "NPUIP": "10.0.245.10" },
                { "NPUID": "1", "NPUIP": "10.0.245.11" }
              ]
            }
          ],
          "serverIPList": [],
          "serverList": []
        }
      ],
      "DPGroupList": [
        {
          "DPGroupID": 0,
          "DPGroupName": 0,
          "DPList": [
            {
              "DPID": 0,
              "DPName": "",
              "DPRole": "Central",
              "PDInstID": "mindie-pymotor-p0-123456",
              "podInfoList": [
                {
                  "podID": "192.168.222.211",
                  "podName": "",
                  "podAssociatedInfoList": [
                    { "NPUID": "0", "NPUIP": "10.0.245.10" },
                    { "NPUID": "1", "NPUIP": "10.0.245.11" }
                  ]
                }
              ],
              "serverList": [
                {
                  "serverID": "",
                  "serverIP": "192.168.222.211",
                  "serverName": "",
                  "NPUInfoList": [
                    { "NPUID": "0", "NPUIP": "10.0.245.10" },
                    { "NPUID": "1", "NPUIP": "10.0.245.11" }
                  ]
                }
              ]
            }
          ]
        }
      ],
      "PDHybridList": [],
      "backupServerList": [
        {
          "backupInfoList": [
            {
              "backupRole": "",
              "serverIp": ""
            }
          ]
        }
      ],
      "expertList": [
        {
          "DPIP": "",
          "ID": "",
          "Name": "",
          "podInfoList": [
            {
              "podID": "",
              "podName": "",
              "podAssociatedInfoList": [
                { "NPUID": "", "NPUIP": "" }
              ]
            }
          ],
          "serverIP": ""
        }
      ],
      "serverIPList": [],
      "serverOfCoordinator": [],
      "serverOfManagerMaster": [],
      "serverOfManagerSlave": []
    },
    "inferenceFrameworkType": "motor-vllm",
    "modelID": "model_123",
    "modelName": "qwen3-8B",
    "modelState": 1,
    "modelType": "qwen3-8B",
    "timestamp": 1698765432123
  }
}
```

**输出说明**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| code | integer | 响应码。 |
| message | string | 响应消息。 |
| data | object | 模型服务清单数据。 |
| data.inferenceFrameworkType | string | 推理框架类型，格式为 `motor-{ENGINE_TYPE}`，其中 `ENGINE_TYPE` 来自环境变量并转为小写。 |
| data.modelID | string | 模型标识，来自环境变量 `sys_id`。 |
| data.modelName | string | 模型名称，来自当前实例信息。 |
| data.modelState | integer | 模型状态：`1` 表示健康，`2` 表示亚健康，`3` 表示异常。 |
| data.modelType | string | 模型类型，当前与 `modelName` 保持一致。 |
| data.timestamp | integer | 本次采集时间，单位为毫秒。 |
| data.inventories.PInstanceList | array | Prefill 实例列表。 |
| data.inventories.DInstanceList | array | Decode 实例列表。 |
| data.inventories.DPGroupList | array | DP 分组列表，包含 DP、Pod、NPU 关联关系。 |
| data.inventories.PDHybridList | array | PD 混合实例列表，当前默认为空数组。 |
| data.inventories.backupServerList | array | 备份服务信息列表。 |
| data.inventories.expertList | array | Expert 信息列表。 |
| data.inventories.serverIPList | array | 服务涉及的服务器 IP 列表。 |
| data.inventories.serverOfCoordinator | array | Coordinator 所在服务器信息，当前默认为空数组。 |
| data.inventories.serverOfManagerMaster | array | Controller 主节点服务器信息，当前默认为空数组。 |
| data.inventories.serverOfManagerSlave | array | Controller 备节点服务器信息，当前默认为空数组。 |
| PInstanceList[].InstanceStatus / DInstanceList[].InstanceStatus | string | 实例状态：`running` 表示运行中，`init` 表示初始化中，`error` 表示异常。 |
| podAssociatedInfoList[].NPUID | string | NPU 设备 ID。 |
| podAssociatedInfoList[].NPUIP | string | NPU 设备 IP。 |

**状态判断说明**

| 场景 | modelState | 说明 |
| --- | --- | --- |
| active 实例中同时存在 Prefill 和 Decode，且 initial/inactive 中没有新的实例名 | 1 | 健康。 |
| active 实例中同时存在 Prefill 和 Decode，但 initial/inactive 中存在 active 未覆盖的实例名 | 2 | 亚健康。 |
| active 实例中缺少 Prefill 或 Decode | 3 | 异常。 |

>[!NOTE]说明
>响应示例仅展示部分 Pod、NPU 与 DPGroup 内容。实际返回数量以运行时实例数、Pod 数、Endpoint 数和设备数为准。

---

## 监控指标查询接口

**接口功能**

查询 Coordinator 汇聚后的完整监控指标，返回 Prometheus 文本。指标采集结果会按 `observability_config.metrics_ttl` 缓存；缓存未过期时直接返回上次结果，缓存过期后重新向 Coordinator 获取。若重新获取失败且已有缓存，则返回旧缓存；若没有缓存，则返回空字符串。

**接口格式**

请求类型：**GET**
URL：`http(s)://{ControllerIP}:{Observability端口}/observability/metrics`

**请求参数**

无

**使用样例**

```bash
curl -X GET "http://{ControllerIP}:{Observability端口}/observability/metrics"
```

**响应示例**

```JSON
{
  "code": 200,
  "message": "Success",
  "data": "# HELP vllm:request_success_total Count of successfully processed requests.\n# TYPE vllm:request_success_total counter\nvllm:request_success_total{engine=\"0\",finished_reason=\"stop\",model_name=\"/job/model/Qwen2.5-0.5B-Instruct\"} 1.0\n"
}
```

**输出说明**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| code | integer | 响应码。 |
| message | string | 响应消息。 |
| data | string | Prometheus 文本格式指标。若当前无可用指标，返回空字符串。 |

>[!NOTE]说明
>该接口返回的是标准响应结构，Prometheus 文本位于 `data` 字段中。

---

## 告警查询接口

**接口功能**

查询并返回指定来源的当前告警。告警被读取后会从内存告警列表中清除。

**接口格式**

请求类型：**GET**
URL：`http(s)://{ControllerIP}:{Observability端口}/observability/alarms`

**请求参数**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| source_id | string | 可选；告警来源标识。未传入时查询 `None` 对应的告警列表。 |

**使用样例**

```bash
curl -X GET "http://{ControllerIP}:{Observability端口}/observability/alarms?source_id={source_id}"
```

**响应示例**

```JSON
{
  "code": 200,
  "message": "Success",
  "data": {
    "total": 1,
    "alarms": [
      [
        {
          "category": 1,
          "cleared": 0,
          "clearCategory": 1,
          "occurUtc": 1698765432123,
          "occurTime": 1698765432123,
          "nativeMeDn": "service-001",
          "originSystem": "vllm",
          "originSystemName": "vllm",
          "originSystemType": "vllm",
          "location": "",
          "moi": "",
          "eventType": 1,
          "alarmId": "alarm_001",
          "alarmName": "Instance exception",
          "severity": 1,
          "probableCause": "",
          "reasonId": 0,
          "serviceAffectedType": 0,
          "additionalInformation": "instance heartbeat timeout, pod id=service-001"
        }
      ]
    ]
  }
}
```

**输出说明**

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| data.total | integer | 告警分组数量。 |
| data.alarms | array | 告警列表。每个元素为一组告警记录。 |
| category | integer | 告警类别：`1` 告警，`2` 清除，`3` 事件，`4` 级别变化，`5` 确认，`6` 取消确认，`7` 其他变化。 |
| cleared | integer | 清除状态：`0` 未清除，`1` 已清除。 |
| clearCategory | integer | 清除类型：`1` 自动清除，`2` 手动清除。 |
| occurUtc | integer | 告警 UTC 发生时间，单位为毫秒。 |
| occurTime | integer | 告警本地发生时间，单位为毫秒。 |
| nativeMeDn | string | 本地管理对象标识，默认来自环境变量 `SERVICE_ID`。 |
| originSystem | string | 告警源系统，默认来自环境变量 `ENGINE_TYPE`。 |
| originSystemName | string | 告警源系统名称，默认来自环境变量 `ENGINE_TYPE`。 |
| originSystemType | string | 告警源系统类型，默认来自环境变量 `ENGINE_TYPE`。 |
| location | string | 告警位置。 |
| moi | string | 管理对象实例。 |
| eventType | integer | 事件类型，例如 `1` 表示通信类事件。 |
| alarmId | string | 告警 ID。 |
| alarmName | string | 告警名称。 |
| severity | integer | 告警级别：`1` 紧急，`2` 重要，`3` 次要，`4` 警告。 |
| probableCause | string | 可能原因。 |
| reasonId | integer | 原因 ID。 |
| serviceAffectedType | integer | 服务影响状态：`0` 不影响，`1` 影响。 |
| additionalInformation | string | 附加信息，输出时会追加 `pod id={nativeMeDn}`。 |

---

## 对接 CCAE 前端平台

CCAE（Cluster Computing Autonomous Engine）是集群自智引擎系统。Motor 可通过 `examples/features/observability/ccae_reporter` 中的 CCAE Reporter 对接 CCAE，由 Reporter 采集 Motor 的告警、日志、实例清单和 metrics 信息并上报到 CCAE。

### 配置 CCAE 信息

在 `user_config.json` 中开启 Observability，并添加 CCAE 北向平台配置：

```json
{
  "motor_controller_config": {
    "observability_config": {
      "observability_enable": true,
      "metrics_ttl": 5
    },
    "api_config": {
      "observability_api_port": 1027
    }
  },
  "motor_deploy_config": {
    "tls_config": {
      "north_tls_config": {
        "enable_tls": true,
        "ca_file": "",
        "cert_file": "",
        "key_file": "",
        "passwd_file": ""
      }
    }
  },
  "north_config": {
    "name": "ccae_reporter",
    "ip": "xxx.xxx.xxx.xxx",
    "port": 31948
  }
}
```

配置说明如下：

| 参数 | 说明 |
| --- | --- |
| `motor_controller_config.observability_config.observability_enable` | 开启 Controller Observability 查询接口，CCAE Reporter 依赖该接口获取清单、指标和告警。 |
| `motor_controller_config.api_config.observability_api_port` | Observability 查询接口端口，默认 `1027`。 |
| `motor_deploy_config.tls_config.north_tls_config` | Reporter 访问 CCAE 北向接口和 Kafka 时使用的 TLS 配置。 |
| `north_config.name` | 北向 Reporter 名称，配置为 `ccae_reporter`。 |
| `north_config.ip` | CCAE 平台 IP。 |
| `north_config.port` | CCAE 平台北向 HTTP 端口。 |

修改配置后，可在 `examples/deployer` 目录更新配置：

```bash
cd examples/deployer
python deploy.py --config_dir ../infer_engines/vllm --update_config
```

也可以单独指定配置文件：

```bash
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json --update_config
```

>[!NOTE]说明
>CCAE 配置支持动态修改。Reporter 会监听 `user_config.json`，当检测到 `north_config` 和 `north_tls_config` 后开始对接 CCAE，无需重启 Motor 推理服务。

### 启动 CCAE Reporter

`examples/deployer/startup/roles/controller.sh` 和 `examples/deployer/startup/roles/coordinator.sh` 中已包含 Reporter 启动命令：

```bash
python3 -m ccae_reporter.run Controller &
python3 -m ccae_reporter.run Coordinator &
```

其中 Controller 侧 Reporter 会采集并上报告警、实例清单、metrics 和日志；Coordinator 侧 Reporter 仅上报心跳和日志，不上报告警与实例清单。

Reporter 的主要交互流程如下：

| 数据类型 | Reporter 访问 Motor 的接口 | Reporter 上报 CCAE 的接口 |
| --- | --- | --- |
| 心跳 | `/readiness` | `/rest/ccaeommgmt/v1/managers/mindie/register` |
| 告警 | `/observability/alarms?source_id={NORTH_PLATFORM}` | `/rest/ccaeommgmt/v1/managers/mindie/events` |
| 实例清单 | `/observability/inventory` | `/rest/ccaeommgmt/v1/managers/mindie/inventory` |
| 指标 | `/observability/metrics` | 随实例清单以 Base64 编码写入 `metrics.metric` 字段 |
| 日志 | 本地日志采集 | CCAE 返回的 Kafka topic |
