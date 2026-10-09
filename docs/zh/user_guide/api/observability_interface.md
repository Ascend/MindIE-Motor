# 观测接口

## 接口说明

Observability 接口用于查询 Controller 汇聚的运维观测数据，包括模型服务清单和告警信息。接口默认关闭，需要配置 `observability_config.observability_enable=true` 后生效。

Observability 查询接口使用独立端口：

- 服务地址：`api_config.controller_api_host`，默认使用 Pod IP，未获取到时为 `127.0.0.1`。
- Observability 端口：`api_config.observability_api_port`，默认 `1027`。
- 安全协议：`observability_tls_config.enable_tls=true` 时使用 `https`，否则使用 `http`。

>[!NOTE]说明
>
> - `{IP}`：Controller 服务部署机器的 IP 或域名。
> - `{Port}`：配置项 `api_config.observability_api_port`。
> - 主备模式下，仅主 Controller 对外提供 Observability 查询能力；备 Controller 收到查询请求时返回内部错误。
> - 当 `observability_config.observability_enable=false` 时，查询类接口返回内部错误，错误信息为 `Observability is not enabled.`。

## 模型服务清单查询接口

**接口功能**

查询当前模型服务的运行清单，返回模型基础信息、P/D 实例列表、DP 分组、Pod 与 NPU 关联信息等。清单数据由 Controller 内部的 active、initial、inactive 实例列表汇总得到。

**接口格式**

请求类型：**GET**
> URL：`http(s)://{IP}:{Port}/observability/inventory`

IP与端口参见[观测接口的IP/端口与配置](./README.md#观测接口的ip端口与配置)

**请求参数**

无

**使用样例**

```bash
curl -X GET "http://{IP}:{Port}/observability/inventory"
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
          "ID": "mindie-motor-p0-123456",
          "Name": "mindie-motor-p0-123456",
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
          "ID": "mindie-motor-d0-123456",
          "Name": "mindie-motor-d0-123456",
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
              "PDInstID": "mindie-motor-p0-123456",
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
| active 实例中同时存在 Prefill 和 Decode，且 initial/inactive 中没有新的实例名 | 1 | 健康 |
| active 实例中同时存在 Prefill 和 Decode，但 initial/inactive 中存在 active 未覆盖的实例名 | 2 | 亚健康 |
| active 实例中缺少 Prefill 或 Decode | 3 | 异常 |

>[!NOTE]说明
>响应示例仅展示部分 Pod、NPU 与 DPGroup 内容。实际返回数量以运行时实例数、Pod 数、Endpoint 数和设备数为准。

## 监控指标查询接口（已弃用）

> [!NOTE] 说明
> `GET /observability/metrics` 接口已弃用，该接口是转发到 Coordinator `/metrics` 的代理，仅作兼容保留（支持 `type` / `role` 参数，直接返回 Prometheus 文本）。**获取指标请直接使用 Coordinator Observability 端口的 [`GET /metrics`](./metrics_interfaces.md#接口格式) 接口**，支持更丰富的聚合视图（`full` / `instance` / `role` / `dp` / `node`）与返回格式（Prometheus / OpenTelemetry）。

## 告警查询接口

**接口功能**

查询并返回指定来源尚未消费的告警、清除和事件记录。查询成功后，本次返回的记录会从对应来源的内存队列中移除。

**接口格式**

请求类型：**GET**
> URL：`http(s)://{IP}:{Port}/observability/alarms`

IP与端口参见[观测接口的IP/端口与配置](./README.md#观测接口的ip端口与配置)

**请求参数**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| source_id | string | 可选；告警来源标识。未传入时查询 `None` 对应的告警列表。 |

**使用样例**

```bash
curl -X GET "http://{IP}:{Port}/observability/alarms?source_id={source_id}"
```

**响应示例**

```json
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
          "location": "servicename = controller,inst_type=p_inst_type or d_inst_type,inst_id=mindie-motor-d0-123456, service ip=192.168.1.10",
          "moi": "servicename = controller,inst_type=p_inst_type or d_inst_type,inst_id=mindie-motor-d0-123456, service ip=192.168.1.10",
          "eventType": 15,
          "alarmId": "0xFC001002",
          "alarmName": "Model Instance Exception Alarm",
          "severity": 1,
          "probableCause": "1:Hardware and software failures caused instance anomalies",
          "reasonId": 1,
          "serviceAffectedType": 1,
          "additionalInformation": "servicename = controller,inst_type=p_inst_type or d_inst_type,inst_id=mindie-motor-d0-123456, service ip=192.168.1.10, pod id=service-001",
          "instanceId": "mindie-motor-d0-123456",
          "pInstanceId": ""
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
| instanceId | string | 告警关联的实例 ID；不关联具体实例时为空字符串。 |
| pInstanceId | string | 精度异常告警关联的 Prefill 实例 ID；无 Prefill 实例或非精度异常告警时为空字符串。 |

### 告警参考

Controller 和 Coordinator 产生告警或事件记录后，将记录汇聚到 Controller Observability 的内存队列；部署 CCAE Reporter 时，由 Reporter 查询该接口，并将记录上报到 CCAE 北向告警接口。

```text
Controller / Coordinator
  -> Controller Observability
  -> GET /observability/alarms
  -> CCAE Reporter（可选）
  -> CCAE
```

>[!NOTE]说明
>
> - 查询成功后，接口会移除对应 `source_id` 队列中本次返回的记录。该操作仅表示记录已被消费，不表示故障已经恢复，也不会生成告警清除记录。
> - 告警恢复时，MindIE Motor 根据具体告警的清除机制生成 `category=2`、`cleared=1` 的清除记录。告警产生记录通常为 `category=1`、`cleared=0`。
> - 事件记录为 `category=3`、`cleared=1`，用于通知已经发生的状态变化，不维护活动告警状态。其中 `cleared=1` 不表示清除了另一条告警。
> - `0xFC001001` 当前仅保留数据模型，`0xFC001006` 当前保留数据模型和接收解析能力；MindIE Motor 主流程不主动生成这两类记录。

以下按记录分别说明解释、属性、参数、系统影响、可能原因、处理步骤和清除方式。告警名称、属性和触发条件均以当前实现为准。

#### 0xFC001000 Controller Master To Slave Alarm

**事件解释**

Controller 主备模式发生主备切换，备 Controller 获取主角色并启动业务模块时，上报此事件。当前 `alarmName` 为 `Controller Master To Slave Alarm`，实际触发语义为备 Controller 升为主 Controller。

**事件属性**

| 事件 ID | 事件级别 | 事件类型 |
| --- | --- | --- |
| `0xFC001000` | 警告 | 状态改变 |

**事件参数**

| 类别 | 参数名称 | 参数含义 |
| --- | --- | --- |
| 定位信息、MOI、附加信息 | `service name` | 组件名称 `Controller`。 |
| 定位信息、MOI、附加信息 | `service ip` | Controller 所在 Pod 的 `POD_IP`。 |
| 附加信息 | `pod id` | 环境变量 `SERVICE_ID`，未配置时为 `Unknown`。 |
| 原因 | `reasonId=1` | 原主 Controller 因故障不可用。 |

**对系统的影响**

该记录的 `serviceAffectedType=0`。新主 Controller 接替原主 Controller 启动业务模块，用于通知发生了主备切换。

**可能原因**

原主 Controller 所在节点、进程或主锁续租异常，触发 Controller 主备切换。

**处理步骤**

1. 检查原主 Controller 所在节点和进程状态。
2. 查看原主 Controller 日志及主备选举相关日志，确认切换原因。
3. 确认新主 Controller 的业务模块均已正常启动。

**事件清除**

该记录是单次状态变化事件，无需清除，不生成 `CLEAR` 记录。

#### 0xFC001001 Service Level Degradation Alarm

**告警解释**

用于描述硬件或软件故障导致可用实例数量减少、服务能力下降的场景。MindIE Motor 当前仅保留该记录的数据模型，主流程没有主动上报点。

**告警属性**

| 告警 ID | 告警级别 | 告警类型 |
| --- | --- | --- |
| `0xFC001001` | 重要 | 状态改变 |

**告警参数**

| 类别 | 参数名称 | 参数含义 |
| --- | --- | --- |
| 原因 | `reasonId=0` | 当前数据模型默认值。 |
| 原因 | `probableCause` | 预留原因为硬件或软件故障导致实例数量减少。 |
| 定位信息、MOI、附加信息 | - | 当前数据模型未填充场景参数，由后续实际记录产生方提供。 |

**对系统的影响**

如果后续流程产生该记录，表示服务可用实例数量减少，最大吞吐能力可能下降。

**可能原因**

硬件或软件故障导致 Prefill、Decode 或混合实例数量减少。

**处理步骤**

1. 先确认记录产生方和 MindIE Motor 版本；当前主流程不会主动产生该记录。
2. 检查 Controller 中的实例状态以及对应 NodeManager、推理实例日志。

**告警清除**

MindIE Motor 当前没有该记录的主动上报和清除流程；如由其他兼容组件产生，清除行为由记录产生方决定。

#### 0xFC001002 Model Instance Exception Alarm

**告警解释**

- **告警上报**：模型实例因状态异常、心跳超时并经 NodeManager 探测确认异常，或者实例进入暂停状态时，Controller 上报此告警。
- **告警恢复**：对应实例重新进入 `ACTIVE` 状态时，Controller 生成清除记录。

Endpoint 心跳超时或状态异常时还可能伴随 `0xFC001003` 事件。

**告警属性**

| 告警 ID | 告警级别 | 告警类型 |
| --- | --- | --- |
| `0xFC001002` | 紧急 | 状态改变 |

**告警参数**

| 类别 | 参数名称 | 参数含义 |
| --- | --- | --- |
| 定位信息、MOI、附加信息 | `servicename` | 组件名称 `controller`。 |
| 定位信息、MOI、附加信息 | `inst_type` | 当前固定输出 `p_inst_type or d_inst_type`。 |
| 定位信息、MOI、附加信息 | `inst_id` | 异常实例的 `job_name`。 |
| 定位信息、MOI、附加信息 | `service ip` | Controller 所在 Pod 的 `POD_IP`。 |
| 附加信息 | `pod id` | 环境变量 `SERVICE_ID`。 |
| 响应字段 | `instanceId` | 异常实例的 `job_name`。 |
| 原因 | `reasonId=1` | 硬件或软件故障导致模型实例异常。 |

**对系统的影响**

异常实例会退出可调度状态。剩余实例容量不足时，服务吞吐能力下降或推理服务不可用。

**可能原因**

1. 实例 Endpoint 心跳超时或上报异常状态。
2. 实例所在节点、NodeManager 或推理进程发生软硬件故障。
3. 实例被暂停。

**处理步骤**

1. 根据 `inst_id` 定位异常实例，检查实例所在节点和 NodeManager 状态。
2. 查看 Controller、NodeManager 和对应推理实例日志，确认心跳超时或异常原因。
3. 修复故障后确认实例重新注册并进入 `ACTIVE` 状态。

**告警清除**

实例重新进入 `ACTIVE` 状态时自动生成 `category=2`、`cleared=1` 的清除记录，无需手工清除。

#### 0xFC001003 Server Exception Alarm

**事件解释**

Controller 检测到实例 Endpoint 心跳超时或 Endpoint 状态异常时，上报此事件。

**事件属性**

| 事件 ID | 事件级别 | 事件类型 |
| --- | --- | --- |
| `0xFC001003` | 重要 | 状态改变 |

**事件参数**

| 类别 | 参数名称 | 参数含义 |
| --- | --- | --- |
| 定位信息、MOI、附加信息 | `service name` | 组件名称 `Controller`。 |
| 定位信息、MOI、附加信息 | `endpoint ip` | 异常 Endpoint 的 IP 地址。 |
| 定位信息、MOI、附加信息 | `endpoint ids` | 同一 IP 下检测到异常的 Endpoint ID 列表。 |
| 附加信息 | `pod id` | 环境变量 `SERVICE_ID`。 |
| 原因 | `reasonId=1` | Endpoint 心跳超时。 |
| 原因 | `reasonId=2` | Endpoint 状态异常。 |

**对系统的影响**

该记录的 `serviceAffectedType=1`。异常 Endpoint 所属实例可能随后退出可调度状态，并触发 `0xFC001002` 告警。

**可能原因**

1. Endpoint 无响应或心跳链路异常。
2. Endpoint 主动上报异常状态。
3. 推理进程、NodeManager、网络或节点硬件异常。

**处理步骤**

1. 根据 `reasonId` 区分心跳超时和 Endpoint 状态异常。
2. 根据 `endpoint ip` 和 `endpoint ids` 定位推理进程，检查节点、网络和进程状态。
3. 查看对应推理实例、NodeManager 和 Controller 日志。

**事件清除**

该记录是单次异常事件，无需清除，不生成 `CLEAR` 记录。实例级告警的恢复情况通过 `0xFC001002` 清除记录体现。

#### 0xFC001004 Coordinator Service Exception Alarm

**告警解释**

- **告警上报**：Controller 检测到当前没有可用的 Prefill 实例或没有可用的 Decode 实例时，上报此告警。
- **告警恢复**：Prefill 和 Decode 两类角色均重新具备可用实例时，Controller 生成清除记录。

当前主流程使用 `reasonId=1`。`reasonId=2` 为 Coordinator 自身状态异常的预留原因，当前没有主动上报点。

**告警属性**

| 告警 ID | 告警级别 | 告警类型 |
| --- | --- | --- |
| `0xFC001004` | 紧急 | 业务质量告警 |

**告警参数**

| 类别 | 参数名称 | 参数含义 |
| --- | --- | --- |
| 定位信息、MOI、附加信息 | `service name` | 组件名称 `Coordinator`。 |
| 定位信息、MOI、附加信息 | `service ip` | Coordinator 所在 Pod 的 `POD_IP`。 |
| 附加信息 | `pod id` | 环境变量 `SERVICE_ID`。 |
| 原因 | `reasonId=1` | 没有可用的 Prefill 实例或 Decode 实例。 |
| 原因 | `reasonId=2` | Coordinator 自身状态异常，当前主流程未使用。 |

**对系统的影响**

该记录的 `serviceAffectedType=1`。缺少可用的 Prefill 或 Decode 角色时，PD 分离推理请求无法正常调度。

**可能原因**

1. 某一角色的实例全部异常、暂停或被移除。
2. 实例所在节点、NodeManager 或推理进程发生故障。

**处理步骤**

1. 查询 Controller 中的实例状态，确认缺少可用实例的角色。
2. 根据同时出现的 `0xFC001002`、`0xFC001003` 记录定位异常实例和 Endpoint。
3. 修复故障并确认 Prefill、Decode 两类实例均恢复为 `ACTIVE`。

**告警清除**

Prefill 和 Decode 两类角色均重新具备可用实例时自动生成 `category=2`、`cleared=1` 的清除记录。

#### 0xFC001005 Coordinator Request Congestion Alarm

**事件解释**

- Coordinator 当前处理的推理请求数达到 `max_requests` 的 85% 时，上报拥塞状态事件。
- 已上报拥塞状态后，请求数下降到 `max_requests` 的 75% 以下时，上报恢复状态事件。

两次上报均为 `EVENT` 记录，是否为拥塞或恢复状态通过 `additionalInformation` 中的请求数及阈值描述区分。

**事件属性**

| 事件 ID | 事件级别 | 事件类型 |
| --- | --- | --- |
| `0xFC001005` | 重要 | 状态改变 |

**事件参数**

| 类别 | 参数名称 | 参数含义 |
| --- | --- | --- |
| 定位信息、MOI | `service name` | 组件名称 `Coordinator`。 |
| 定位信息、MOI | `service ip` | Coordinator 所在 Pod 的 `POD_IP`。 |
| 附加信息 | 当前请求数、`max_requests` 和阈值 | 标识本次记录为达到 85% 的拥塞状态或低于 75% 的恢复状态。 |
| 附加信息 | `pod id` | 环境变量 `SERVICE_ID`。 |
| 原因 | `reasonId=1` | Coordinator 正在处理的请求发生拥塞。 |

**对系统的影响**

该记录的 `serviceAffectedType=1`。请求接近并发上限时，新请求可能受到限流，业务时延也可能升高。

**可能原因**

当前并发请求数接近配置的 `max_requests`，或者下游实例处理能力不足导致请求积压。

**处理步骤**

1. 检查当前请求量、推理时延以及各实例负载。
2. 确认 `max_requests` 配置是否符合部署规模和容量规划。
3. 请求持续拥塞时，排查异常实例或按实际容量扩容。

**事件清除**

请求数下降到 `max_requests` 的 75% 以下时上报恢复状态事件，不生成 `CLEAR` 记录。

#### 0xFC001006 Cluster Connection Exception Alarm

**告警解释**

用于描述 Controller 与集群服务之间注册、RankTable 订阅、故障消息订阅或连接中断等异常。MindIE Motor 当前保留该告警的数据模型和接收解析能力，主流程没有主动上报点。

**告警属性**

| 告警 ID | 告警级别 | 告警类型 |
| --- | --- | --- |
| `0xFC001006` | 紧急 | 状态改变 |

**告警参数**

| 类别 | 参数名称 | 参数含义 |
| --- | --- | --- |
| 定位信息、MOI、附加信息 | `service name` | 组件名称 `Controller`。 |
| 定位信息、MOI、附加信息 | `service ip` | Controller 所在 Pod 的 `POD_IP`。 |
| 定位信息、MOI、附加信息 | `cluster ip` | 环境变量 `MINDX_SERVER_IP`；未配置时不输出。 |
| 附加信息 | `pod id` | 环境变量 `SERVICE_ID`。 |
| 原因 | `reasonId=1` | 集群服务注册失败。 |
| 原因 | `reasonId=2` | RankTable 订阅失败。 |
| 原因 | `reasonId=3` | 故障消息订阅失败。 |
| 原因 | `reasonId=4` | 连接中断。 |

**对系统的影响**

如果兼容组件产生该记录，表示 Controller 无法正常获取相应集群数据，依赖这些数据的业务流程可能受影响。

**可能原因**

1. 集群服务地址配置错误或服务不可用。
2. Controller 与集群服务之间网络异常。
3. 注册、RankTable 订阅或故障消息订阅失败。

**处理步骤**

1. 先确认记录产生方和 MindIE Motor 版本；当前主流程不会主动产生该记录。
2. 根据 `reasonId` 检查对应的注册、订阅或连接链路。
3. 检查集群服务状态、地址配置、网络连通性和相关日志。

**告警清除**

MindIE Motor 当前不主动产生该告警，清除行为由记录产生方决定；Controller 可以接收并转存对应的 `CLEAR` 记录。

#### 0xFC001007 Scale P To D Recovery Event

**事件解释**

ScaleP2D 故障恢复策略成功恢复 Decode 实例后，Controller 上报此事件。

**事件属性**

| 事件 ID | 事件级别 | 事件类型 |
| --- | --- | --- |
| `0xFC001007` | 警告 | 状态改变 |

**事件参数**

| 类别 | 参数名称 | 参数含义 |
| --- | --- | --- |
| 定位信息、MOI、附加信息 | `service name` | 组件名称 `Controller`。 |
| 定位信息、MOI、附加信息 | `d instance id` | 已恢复的 Decode 实例 ID。 |
| 定位信息、MOI、附加信息 | `d instance job name` | 已恢复的 Decode 实例 Job 名称。 |
| 定位信息、MOI、附加信息 | `killed p instance ids` | 恢复过程中终止的 Prefill 实例 ID 列表。 |
| 响应字段 | `instanceId` | 已恢复的 Decode 实例 ID。 |
| 原因 | `reasonId=1` | Decode 实例已由 ScaleP2D 策略恢复。 |

**对系统的影响**

该记录的 `serviceAffectedType=0`，表示 ScaleP2D 恢复流程已经成功完成。

**可能原因**

Decode 实例发生可由 ScaleP2D 策略处理的故障，Controller 通过调整 Prefill/Decode 资源完成恢复。

**处理步骤**

1. 根据 `d instance id` 和 `d instance job name` 确认恢复后的 Decode 实例状态。
2. 根据 `killed p instance ids` 确认被终止的 Prefill 实例已按预期处理。
3. 若恢复后业务仍异常，查看 Controller 故障恢复日志及相关实例日志。

**事件清除**

该记录是单次恢复事件，无需清除，不生成 `CLEAR` 记录。

#### 0xFC001008 Coordinator Master To Slave Alarm

**事件解释**

Coordinator 主备模式发生主备切换，备 Coordinator 获取主角色并启动 Inference 进程时，上报此事件。当前 `alarmName` 为 `Coordinator Master To Slave Alarm`，实际触发语义为备 Coordinator 升为主 Coordinator。

**事件属性**

| 事件 ID | 事件级别 | 事件类型 |
| --- | --- | --- |
| `0xFC001008` | 警告 | 状态改变 |

**事件参数**

| 类别 | 参数名称 | 参数含义 |
| --- | --- | --- |
| 定位信息、MOI、附加信息 | `service name` | 组件名称 `Coordinator`。 |
| 定位信息、MOI、附加信息 | `service ip` | Coordinator 所在 Pod 的 `POD_IP`。 |
| 附加信息 | `pod id` | 环境变量 `SERVICE_ID`。 |
| 原因 | `reasonId=1` | 原主 Coordinator 因故障不可用。 |

**对系统的影响**

该记录的 `serviceAffectedType=0`。新主 Coordinator 启动 Inference 进程，用于通知发生了主备切换。

**可能原因**

原主 Coordinator 所在节点、进程或主锁续租异常，触发 Coordinator 主备切换。

**处理步骤**

1. 检查原主 Coordinator 所在节点和进程状态。
2. 查看原主 Coordinator 日志及主备选举相关日志，确认切换原因。
3. 确认新主 Coordinator 的 Inference 进程已正常启动并可处理请求。

**事件清除**

该记录是单次状态变化事件，无需清除，不生成 `CLEAR` 记录。

#### 0xFC001009 Precision Anomaly Alarm

**告警解释**

- **告警上报**：Coordinator 对 Decode 输出进行 token 级采样检测；同一实例组连续异常达到 `precision_issue_threshold` 后执行固定问答拨测，并上报精度异常告警。无论拨测成功或失败都会上报告警，拨测失败次数写入附加信息。
- **告警恢复**：Controller 自动恢复成功、CCAE 精度控制成功终止实例组，或者同一实例组连续正常检测达到 `precision_clear_threshold` 时，生成清除记录。

**告警属性**

| 告警 ID | 告警级别 | 告警类型 |
| --- | --- | --- |
| `0xFC001009` | 重要 | 处理错误 |

**告警参数**

| 类别 | 参数名称 | 参数含义 |
| --- | --- | --- |
| 定位信息 | `service name` | 组件名称 `Coordinator`。 |
| 定位信息 | `service ip` | Coordinator 所在 Pod 的 `POD_IP`。 |
| MOI | `pId` | Prefill 实例 ID；PD 混合部署时不包含该参数。 |
| MOI | `instanceId` | Decode 实例 ID。告警与清除记录必须使用完全相同的 MOI。 |
| 附加信息 | `precision_issue_count` | 触发告警时累计的连续精度异常次数。 |
| 附加信息 | `probe_failure_count` | 固定问答拨测失败次数。 |
| 附加信息 | `p_instance_id` | Prefill 实例 ID；无 Prefill 实例时为 `None`。 |
| 附加信息 | `d_instance_id` | Decode 实例 ID。 |
| 附加信息 | `pod id` | 依次取非空的 `SERVICE_ID`、`sys_id` 或模型 ID。 |
| 响应字段 | `instanceId` | Decode 实例 ID。 |
| 响应字段 | `pInstanceId` | Prefill 实例 ID；无 Prefill 实例时为空字符串。 |
| 原因 | `reasonId=0` | 当前实现保留 `Record` 的默认值。 |

**对系统的影响**

该记录的 `serviceAffectedType=1`。目标实例组可能持续产生重复、乱码、生僻字等输出质量异常；开启自动恢复后，Controller 会终止对应 Decode 实例及可选的 Prefill 实例。

**可能原因**

同一 Decode 或 PD 实例组连续产生 token 级输出质量异常，并达到配置的连续异常阈值。

**处理步骤**

1. 根据 `instanceId`、`pInstanceId` 和 MOI 定位异常实例组。
2. 检查 `precision_issue_count`、`probe_failure_count` 以及 Coordinator 精度检测日志。
3. 检查对应推理实例的模型、权重、量化配置和运行日志。
4. 确认 Controller 自动恢复结果，或者通过 CCAE 精度控制执行恢复。

**告警清除**

清除记录必须复用原告警的 MOI。自动恢复、CCAE 精度控制以及连续正常检测三种清除路径详见[精度检测故障恢复特性](../../design/fault_tolerance/precision_detection.md#告警清除)。

如需将上述记录上报到 CCAE，可部署 CCAE Reporter。Reporter 的配置、启动方式和北向接口关系参见下文“对接 CCAE 前端平台”。

## 对接 CCAE 前端平台

CCAE（Cluster Computing Autonomous Engine）是集群自智引擎系统。Motor 可通过 `examples/features/observability/ccae_reporter` 中的 CCAE Reporter 对接 CCAE，由 Reporter 采集 Motor 的告警、日志、实例清单和 metrics 信息并上报到 CCAE。

### 配置 CCAE 信息

在 `user_config.json` 中开启 Observability，并添加 CCAE 北向平台配置：

```json
{
  "motor_controller_config": {
    "observability_config": {
      "observability_enable": true
    }
  },
  "motor_deploy_config": {
    "tls_config": {
      "north_tls_config": {
        "enable_tls": false,
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
| `motor_controller_config.observability_config.observability_enable` | 开启 Controller Observability 查询接口，CCAE Reporter 依赖该接口获取清单和告警；指标由 Reporter 直接从 Coordinator 的 `/metrics` 获取。 |
| `motor_controller_config.api_config.observability_api_port` | Controller Observability 查询接口端口，默认 `1027`。JSON 省略时 Reporter 回落该默认值。 |
| `motor_controller_config.api_config.controller_api_port` | Controller 管理/probe 端口，默认 `1026`。JSON 省略时 Reporter 回落该默认值。 |
| `motor_coordinator_config.api_config.coordinator_obs_port` | Coordinator `/metrics` 端口，默认 `1027`。JSON 省略时 Reporter 回落该默认值。 |
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
| 指标 | Coordinator `/metrics`（参见[指标接口](./metrics_interfaces.md#接口格式)） | 随实例清单以 Base64 编码写入 `metrics.metric` 字段 |
| 精度控制 | `/controller/check_instance`、`/controller/terminate_instance` | `/rest/ccaeommgmt/v1/managers/mindie/precisioncontrol` |
| 日志 | 本地日志采集 | CCAE 返回的 Kafka topic |

Controller 侧 CCAE Reporter 处理 precision control 时复用 `/controller/terminate_instance`。请求体除必填 `instance_id`、`reason` 外，可携带 `p_instance_id` 和 `precision_alarm_clear=true`：`instance_id` 对应告警中的 D 实例，`p_instance_id` 对应告警中的 P 实例；Controller 在终止 P/D 实例组后附加清除该实例组的精度告警。`precision_alarm_clear` 为可选字段，普通实例终止请求无需携带。
