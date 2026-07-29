# Observability

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:08:08.272Z pushedAt=2026-06-27T06:49:11.436Z -->

## API Description

`observability` is used to query the aggregated O&M observation data from the Controller, including the model services, monitoring metrics, and alert information. The interface is disabled by default and takes effect only after configuring `observability_config.observability_enable=true`.

The `observability` query API uses an independent port:

- Service address: `api_config.controller_api_host`, which defaults to the Pod IP, or `127.0.0.1` if the Pod IP cannot be obtained.

- `observability` port: `api_config.observability_api_port`, default `1027`.

- Security protocol: `https` is used when `observability_tls_config.enable_tls=true`, otherwise `http` is used.

- Metric cache time: `observability_config.metrics_ttl`, default `5` seconds.

>[!NOTE]NOTE
>
> - `{ControllerIP}`: The IP address or domain name of the machine where the Controller service is deployed.
> - `{Observability port}`: Configuration item `api_config.observability_api_port`.
> - In active/standby mode, only the active Controller provides `observability` query capabilities externally; the standby Controller returns an internal error when receiving a query request.
> - When `observability_config.observability_enable=false`, the query interface returns an internal error with the error message `Observability is not enabled`.

## Model Service Query Interface

**Interface Function**

Queries the current model service list, returning basic model info, P/D instance lists, DP groups, and Pod–NPU associations. The list is aggregated from the Controller's internal active, initial, and inactive instance lists.

**Interface Format**

Request Type: **GET**

URL: `http(s)://{ControllerIP}:{Observability Port}/observability/inventory`

**Request Parameter**

None

**Usage Example**

```bash
curl -X GET "http://{ControllerIP}:{Observability Port}/observability/inventory"
```

**Response Example**

The following example references the normal use case in `tests/controller/observability/inventory/test_inventory_collector.py`: 2 Prefill instances, 1 Decode instance, model name `qwen3-8B`, model ID `model_123`.

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

**Output Description**

| Parameter | Type | Description |
| --- | --- | --- |
| code | integer | Response code. |
| message | string | Response message. |
| data | object | Model service data. |
| data.inferenceFrameworkType | string | Inference framework type, in the format `motor-{ENGINE_TYPE}`, where `ENGINE_TYPE` is from the environment variable and converted to lowercase. |
| data.modelID | string | Model identifier, from the environment variable `sys_id`. |
| data.modelName | string | Model name, from the current instance information. |
| data.modelState | integer | Model state: `1` indicates healthy, `2` indicates sub-healthy, `3` indicates abnormal. |
| data.modelType | string | Model type, currently consistent with `modelName`. |
| data.timestamp | integer | Collection time, unit: ms. |
| data.inventories.PInstanceList | array | Prefill instance list. |
| data.inventories.DInstanceList | array | Decode instance list. |
| data.inventories.DPGroupList | array | DP group list, containing DP, Pod, and NPU associations. |
| data.inventories.PDHybridList | array | PD hybrid instance list, currently defaulting to an empty array. |
| data.inventories.backupServerList | array | Backup service information list. |
| data.inventories.expertList | array | Expert information list. |
| data.inventories.serverIPList | array | List of server IPs involved in the service. |
| data.inventories.serverOfCoordinator | array | Server information where the Coordinator resides, currently defaulting to an empty array. |
| data.inventories.serverOfManagerMaster | array | Server information of the Controller master node, currently defaulting to an empty array. |
| data.inventories.serverOfManagerSlave | array | Server information of the Controller standby node, currently defaulting to an empty array. |
| PInstanceList[].InstanceStatus / DInstanceList[].InstanceStatus | string | Instance status: `running` indicates running, `init` indicates initializing, `error` indicates abnormal. |
| podAssociatedInfoList[].NPUID | string | NPU device ID. |
| podAssociatedInfoList[].NPUIP | string | NPU device IP. |

**Status Determination Description**

| Scenario | modelState | Description |
| --- | --- | --- |
| Active instances contain both Prefill and Decode, with no new instance names in initial/inactive | 1 | Healthy |
| Active instances contain both Prefill and Decode, but initial/inactive includes instance names not covered by active instances | 2 | Sub-healthy |
| Active instances lack either Prefill or Decode | 3 | Abnormal |

>[!NOTE]NOTE
>The response example only shows some Pod, NPU, and DP group content. The actual number returned depends on the number of runtime instances, Pods, Endpoints, and devices.

## Metric Query Interface

**Interface Function**

Queries the complete monitoring metrics aggregated by the Coordinator and returns Prometheus text. The metric collection results are cached according to `observability_config.metrics_ttl`. If the cache has not expired, the previous result is returned directly. After the cache expires, it re-fetches from the Coordinator. If the re-fetch fails and a cache exists, the old cache is returned; if no cache exists, an empty string is returned.

**Interface Format**

Request Type: **GET**  
URL: `http(s)://{ControllerIP}:{Observability Port}/observability/metrics`

**Request Parameter**

None

**Usage Example**

```bash
curl -X GET "http://{ControllerIP}:{Observability port}/observability/metrics"
```

**Response Example**

```JSON
{
  "code": 200,
  "message": "Success",
  "data": "# HELP vllm:request_success_total Count of successfully processed requests.\n# TYPE vllm:request_success_total counter\nvllm:request_success_total{engine=\"0\",finished_reason=\"stop\",model_name=\"/job/model/Qwen2.5-0.5B-Instruct\"} 1.0\n"
}
```

**Output Description**

| Name | Type | Description |
| --- | --- | --- |
| code | integer | Response code. |
| message | string | Response message. |
| data | string | Prometheus text-format metrics. An empty string is returned if no metrics are currently available. |

>[!NOTE]NOTE
>The interface returns a standard response structure, with the Prometheus text located in the `data` field.

## Alert Query Interface

**Interface Function**

Queries and returns the current alerts from the specified source. Alerts are cleared from the memory alert list after being read.

**Interface Format**

Request Type: **GET**  
URL: `http(s)://{ControllerIP}:{Observability Port}/observability/alarms`

**Request Parameter**

| Name | Type | Description |
| --- | --- | --- |
| source_id | string | Optional; alert source identifier. When not passed, queries the alert list corresponding to `None`. |

**Usage Example**

```bash
curl -X GET "http://{ControllerIP}:{Observability Port}/observability/alarms?source_id={source_id}"
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
| data.total | integer | Number of alert groups. |
| data.alarms | array | Alert list. Each element is a group of alert records. |
| category | integer | Alert category: `1` alert, `2` clear, `3` event, `4` severity change, `5` acknowledge, `6` unacknowledge, `7` other change. |
| cleared | integer | Clear status: `0` not cleared, `1` cleared. |
| clearCategory | integer | Clear type: `1` automatic clear, `2` manual clear. |
| occurUtc | integer | Alert occurrence time in UTC. Unit: ms. |
| occurTime | integer | Alert local occurrence time. Unit: ms. |
| nativeMeDn | string | Local managed object identifier. Default from environment variable `SERVICE_ID`. |
| originSystem | string | Alert source system. Default from environment variable `ENGINE_TYPE`. |
| originSystemName | string | Alert source system name. Default from environment variable `ENGINE_TYPE`. |
| originSystemType | string | Alert source system type. Default from environment variable `ENGINE_TYPE`. |
| location | string | Alert location. |
| moi | string | Managed object instance. |
| eventType | integer | Event type, for example, `1` indicates a communication event. |
| alarmId | string | Alert ID. |
| alarmName | string | Alert name. |
| severity | integer | Alert severity: `1` critical, `2` major, `3` minor, `4` warning. |
| probableCause | string | Probable cause. |
| reasonId | integer | Reason ID. |
| serviceAffectedType | integer | Service impact status: `0` not affected, `1` affected. |
| additionalInformation | string | Additional information. When output, `pod id={nativeMeDn}` is appended. |

## Connecting to the CCAE Frontend Platform

Cluster Computing Autonomous Engine (CCAE) is a cluster autonomous intelligence engine system. Motor can connect to CCAE through the CCAE Reporter in `examples/features/observability/ccae_reporter`. The Reporter collects Motor's alerts, logs, instance lists, and metrics information and reports them to CCAE.

### Configuring CCAE Information

Enable `observability` in `user_config.json` and add the CCAE northbound platform configuration.

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

The configuration is described as follows:

| Parameter | Description |
| --- | --- |
| `motor_controller_config.observability_config.observability_enable` | Enables the controller observability query interface. The CCAE Reporter relies on this interface to obtain the lists, metrics, and alerts. |
| `motor_controller_config.api_config.observability_api_port` | Port for the observability query interface. Default: `1027`. |
| `motor_deploy_config.tls_config.north_tls_config` | TLS configuration used by the Reporter to access the CCAE northbound interface and Kafka. |
| `north_config.name` | Name of the northbound Reporter, configured as `ccae_reporter`. |
| `north_config.ip` | IP address of the CCAE platform. |
| `north_config.port` | Northbound HTTP port of the CCAE platform. |

After modifying the configuration, you can update the configuration in the `examples/deployer` directory:

```bash
cd examples/deployer
python deploy.py --config_dir ../infer_engines/vllm --update_config
```

You can also specify a configuration file separately:

```bash
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json --update_config
```

>[!NOTE]NOTE
>The CCAE configuration supports dynamic modification. The Reporter monitors `user_config.json` and starts connecting to CCAE when `north_config` and `north_tls_config` are detected, without restarting the Motor inference service.

### Starting the CCAE Reporter

The `examples/deployer/startup/roles/controller.sh` and `examples/deployer/startup/roles/coordinator.sh` files already contain the Reporter startup commands:

```bash
python3 -m ccae_reporter.run Controller &
python3 -m ccae_reporter.run Coordinator &
```

The Reporter on the Controller side collects and reports alerts, instance lists, metrics, and logs; the Reporter on the Coordinator side only reports heartbeats and logs, and does not report alerts or instance lists.

The main interaction flow of the Reporter is as follows:

| Data Type | Interface for Reporter to Access Motor | Interface for Reporter to Report to CCAE |
| --- | --- | --- |
| Heartbeat | `/readiness` | `/rest/ccaeommgmt/v1/managers/mindie/register` |
| Alert | `/observability/alarms?source_id={NORTH_PLATFORM}` | `/rest/ccaeommgmt/v1/managers/mindie/events` |
| Instance List | `/observability/inventory` | `/rest/ccaeommgmt/v1/managers/mindie/inventory` |
| Metric | `/observability/metrics` | Written to the `metrics.metric` field as a Base64-encoded string along with the instance list |
| Log | Local log collection | Kafka topic returned by CCAE |
