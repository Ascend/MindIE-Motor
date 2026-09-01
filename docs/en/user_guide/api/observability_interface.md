# Observability Interface

## Interface Description

The Observability interface is used to query the O&M observation data aggregated by the Controller, including the model service list and alarm information. The interface is disabled by default and takes effect only after `observability_config.observability_enable=true` is configured.

The Observability query interface uses an independent port:

- Service address: `api_config.controller_api_host`, which uses the Pod IP by default and falls back to `127.0.0.1` when the Pod IP cannot be obtained.

- Observability port: `api_config.observability_api_port`, with a default value of `1027`.

- Security protocol: `https` is used when `observability_tls_config.enable_tls=true`; otherwise, `http` is used.

>[!NOTE]NOTE
>
> - `{IP}`: the IP address or domain name of the machine where the Controller service is deployed.
> - `{Port}`: the configuration item `api_config.observability_api_port`.
> - In active/standby mode, only the active Controller provides the Observability query capability externally; the standby Controller returns an internal error when receiving a query request.
> - When `observability_config.observability_enable=false`, query interfaces return an internal error with the error message `Observability is not enabled.`.

## Model Service Inventory Query Interface

**Interface Function**

Queries the running inventory of the current model service, returning model basic information, P/D instance lists, DP groups, and Pod-to-NPU association information. The inventory data is aggregated from the active, initial, and inactive instance lists within the Controller.

**Interface Format**

Request type: **GET**
> URL: `http(s)://{IP}:{Port}/observability/inventory`

For the IP and port, see [IP/Port and Configuration of the Observability Interface](./README.md#ipport-and-configuration-of-the-observation-interface)

**Request Parameters**

None

**Usage Example**

```bash
curl -X GET "http://{IP}:{Port}/observability/inventory"
```

**Response Example**

The following example references the normal test case in `tests/controller/observability/inventory/test_inventory_collector.py`: 2 Prefill instances, 1 Decode instance, with the model name `qwen3-8B` and the model ID `model_123`.

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

**Output Description**

| Parameter | Type | Description |
| --- | --- | --- |
| code | integer | Response code. |
| message | string | Response message. |
| data | object | Model service inventory data. |
| data.inferenceFrameworkType | string | Inference framework type, in the format `motor-{ENGINE_TYPE}`, where `ENGINE_TYPE` comes from the environment variable and is converted to lowercase. |
| data.modelID | string | Model identifier, from the environment variable `sys_id`. |
| data.modelName | string | Model name, from the current instance information. |
| data.modelState | integer | Model state: `1` indicates healthy, `2` indicates sub-healthy, and `3` indicates abnormal. |
| data.modelType | string | Model type, currently consistent with `modelName`. |
| data.timestamp | integer | Collection time of this record, unit in milliseconds. |
| data.inventories.PInstanceList | array | Prefill instance list. |
| data.inventories.DInstanceList | array | Decode instance list. |
| data.inventories.DPGroupList | array | DP group list, containing the association relationships among DP, Pod, and NPU. |
| data.inventories.PDHybridList | array | PD hybrid instance list, currently an empty array by default. |
| data.inventories.backupServerList | array | Backup service information list. |
| data.inventories.expertList | array | Expert information list. |
| data.inventories.serverIPList | array | List of server IPs involved in the service. |
| data.inventories.serverOfCoordinator | array | Information about the server where the Coordinator resides, currently an empty array by default. |
| data.inventories.serverOfManagerMaster | array | Information about the server of the Controller master node, currently an empty array by default. |
| data.inventories.serverOfManagerSlave | array | Information about the server of the Controller standby node, currently an empty array by default. |
| PInstanceList[].InstanceStatus / DInstanceList[].InstanceStatus | string | Instance status: `running` indicates running, `init` indicates initializing, and `error` indicates abnormal. |
| podAssociatedInfoList[].NPUID | string | NPU device ID. |
| podAssociatedInfoList[].NPUIP | string | NPU device IP. |

**Status Determination Description**

| Scenario | modelState | Description |
| --- | --- | --- |
| Both Prefill and Decode exist in active instances, and no new instance names exist in initial/inactive. | 1 | Healthy |
| Both Prefill and Decode exist in active instances, but initial/inactive contains instance names not covered by active. | 2 | Sub-healthy |
| Prefill or Decode is missing in active instances. | 3 | Abnormal |

>[!NOTE]NOTE
>The response example shows only part of the Pod, NPU, and DPGroup content. The actual number returned depends on the number of runtime instances, Pods, Endpoints, and devices.

## (Deprecated) Metrics Query Interface

> [!WARNING] Deprecated
> The `GET /observability/metrics` interface is deprecated. It is a proxy that forwards to the Coordinator `/metrics` endpoint and is retained only for compatibility (it supports the `type` / `role` parameters and directly returns Prometheus text). **To obtain metrics, directly use the [`GET /metrics`](./metrics_interfaces.md#interface-format) interface on the Coordinator Observability port**, which supports richer aggregation views (`full` / `instance` / `role` / `dp` / `node`) and return formats (Prometheus / OpenTelemetry).

## Alarm Query Interface

**Interface Function**

Queries and returns the current alarms from the specified source. After an alarm is read, it is cleared from the in-memory alarm list.

**Interface Format**

Request type: **GET**
> URL: `http(s)://{IP}:{Port}/observability/alarms`

For the IP and port, see [IP/Port and Configuration of the Observability Interface](./README.md#ipport-and-configuration-of-the-observation-interface)

**Request Parameters**

| Parameter Name | Type | Description |
| --- | --- | --- |
| source_id | string | Optional; the alarm source identifier. If not passed, the alarm list corresponding to `None` is queried. |

**Usage Example**

```bash
curl -X GET "http://{IP}:{Port}/observability/alarms?source_id={source_id}"
```

**Response Example**

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

**Output Description**

| Parameter | Type | Description |
| --- | --- | --- |
| data.total | integer | Number of alarm groups. |
| data.alarms | array | Alarm list. Each element is a group of alarm records. |
| category | integer | Alarm category: `1` alarm, `2` clear, `3` event, `4` severity change, `5` acknowledge, `6` unacknowledge, `7` other change. |
| cleared | integer | Clear status: `0` not cleared, `1` cleared. |
| clearCategory | integer | Clear type: `1` automatic clear, `2` manual clear. |
| occurUtc | integer | UTC time when the alarm occurred, unit in milliseconds. |
| occurTime | integer | Local time when the alarm occurred, unit in milliseconds. |
| nativeMeDn | string | Local managed object identifier, default from the environment variable `SERVICE_ID`. |
| originSystem | string | Alarm source system, default from the environment variable `ENGINE_TYPE`. |
| originSystemName | string | Name of the alarm source system, default from the environment variable `ENGINE_TYPE`. |
| originSystemType | string | Type of the alarm source system, default from the environment variable `ENGINE_TYPE`. |
| location | string | Alarm location. |
| moi | string | Managed object instance. |
| eventType | integer | Event type, for example, `1` indicates a communication event. |
| alarmId | string | Alarm ID. |
| alarmName | string | Alarm name. |
| severity | integer | Alarm severity: `1` critical, `2` major, `3` minor, `4` warning. |
| probableCause | string | Probable cause. |
| reasonId | integer | Reason ID. |
| serviceAffectedType | integer | Service impact status: `0` not affected, `1` affected. |
| additionalInformation | string | Additional information. When output, `pod id={nativeMeDn}` is appended. |

## Connecting to the CCAE Frontend Platform

 Cluster Computing Autonomous Engine (CCAE) is a cluster autonomous engine system. Motor can connect to CCAE through the CCAE Reporter in `examples/features/observability/ccae_reporter`. The Reporter collects Motor's alarms, logs, instance list, and metrics information and reports them to CCAE.

### Configuring CCAE Information

Enable Observability in `user_config.json` and add the CCAE northbound platform configuration:

```json
{
  "motor_controller_config": {
    "observability_config": {
      "observability_enable": true
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

The configuration is described as follows:

| Parameter | Description |
| --- | --- |
| `motor_controller_config.observability_config.observability_enable` | Enables the Controller Observability query interface. The CCAE Reporter relies on this interface to obtain the instance list and alarms. Metrics are obtained by the Reporter directly from the Coordinator's `/metrics`. |
| `motor_controller_config.api_config.observability_api_port` | Port of the Observability query interface. The default value is `1027`. |
| `motor_deploy_config.tls_config.north_tls_config` | TLS configuration used by the Reporter to access the CCAE northbound interface and Kafka. |
| `north_config.name` | Name of the northbound Reporter. Set it to `ccae_reporter`. |
| `north_config.ip` | IP address of the CCAE platform. |
| `north_config.port` | Northbound HTTP port of the CCAE platform. |

After modifying the configuration, you can update the configuration in the `examples/deployer` directory:

```bash
cd examples/deployer
python deploy.py --config_dir ../infer_engines/vllm --update_config
```

You can also specify the configuration file separately:

```bash
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json --update_config
```

>[!NOTE]NOTE
>The CCAE configuration supports dynamic modification. The Reporter monitors `user_config.json` and starts connecting to CCAE after detecting `north_config` and `north_tls_config`, without restarting the Motor inference service.

### Starting CCAE Reporter

The Reporter startup commands are already included in `examples/deployer/startup/roles/controller.sh` and `examples/deployer/startup/roles/coordinator.sh`:

```bash
python3 -m ccae_reporter.run Controller &
python3 -m ccae_reporter.run Coordinator &
```

The Controller-side Reporter collects and reports alarms, the instance list, metrics, and logs. The Coordinator-side Reporter reports only heartbeats and logs, and does not report alarms or the instance list.

The main interaction flow of the Reporter is as follows:

| Data Type | Interface Used by Reporter to Access Motor | Interface Used by Reporter to Report to CCAE |
| --- | --- | --- |
| Heartbeat | `/readiness` | `/rest/ccaeommgmt/v1/managers/mindie/register` |
| Alarm | `/observability/alarms?source_id={NORTH_PLATFORM}` | `/rest/ccaeommgmt/v1/managers/mindie/events` |
| Instance list | `/observability/inventory` | `/rest/ccaeommgmt/v1/managers/mindie/inventory` |
| Metrics | Coordinator `/metrics` (see [Metrics Interface](./metrics_interfaces.md#interface-format)) | Written into the `metrics.metric` field in Base64 encoding along with the instance list |
| Precision control | `/controller/check_instance`, `/controller/terminate_instance` | `/rest/ccaeommgmt/v1/managers/mindie/precisioncontrol` |
| Logs | Local log collection | Kafka topic returned by CCAE |

When the Controller-side CCAE Reporter handles precision control, it reuses `/controller/terminate_instance`. In addition to the mandatory `instance_id` and `reason`, the request body can carry `p_instance_id` and `precision_alarm_clear=true`: `instance_id` corresponds to the D instance in the alarm, and `p_instance_id` corresponds to the P instance in the alarm. After terminating the P/D instance group, the Controller additionally clears the precision alarm of that instance group. `precision_alarm_clear` is an optional field and does not need to be carried in ordinary instance termination requests.
