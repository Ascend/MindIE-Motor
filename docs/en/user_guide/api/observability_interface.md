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

> [!NOTE] NOTE
> The `GET /observability/metrics` interface is deprecated. It is a proxy that forwards to the Coordinator `/metrics` endpoint and is retained only for compatibility (it supports the `type` / `role` parameters and directly returns Prometheus text). **To obtain metrics, directly use the [`GET /metrics`](./metrics_interfaces.md#interface-format) interface on the Coordinator Observability port**, which supports richer aggregation views (`full` / `instance` / `role` / `dp` / `node`) and return formats (Prometheus / OpenTelemetry).

## Alarm Query Interface

**Interface Function**

Queries and returns the unconsumed alarms, clear records, and event records from the specified source. After a successful query, the records returned this time are removed from the in-memory queue of the corresponding source.

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
| instanceId | string | ID of the instance associated with the alarm; an empty string when no specific instance is associated. |
| pInstanceId | string | ID of the Prefill instance associated with a precision anomaly alarm; an empty string when there is no Prefill instance or the alarm is not a precision anomaly alarm. |

### Alarm Reference

After Controller and Coordinator generate alarms or event records, the records are aggregated into the in-memory queue of Controller Observability; when CCAE Reporter is deployed, the Reporter queries this interface and reports the records to the CCAE northbound alarm interface.

```text
Controller / Coordinator
  -> Controller Observability
  -> GET /observability/alarms
  -> CCAE Reporter (optional)
  -> CCAE
```

>[!NOTE]NOTE
>
> - After a successful query, the interface removes the records returned this time from the queue of the corresponding `source_id`. This operation only indicates that the records have been consumed; it does not indicate that the fault has recovered, and it does not generate alarm clear records.
> - When an alarm recovers, MindIE Motor generates a clear record with `category=2` and `cleared=1` according to the clear mechanism of the specific alarm. An alarm occurrence record is usually `category=1` and `cleared=0`.
> - An event record has `category=3` and `cleared=1`. It is used to notify a state change that has occurred and does not maintain the active alarm state. Here, `cleared=1` does not mean that another alarm has been cleared.
> - `0xFC001001` currently retains only the data model, and `0xFC001006` currently retains the data model and the receiving and parsing capability; the main MindIE Motor flow does not actively generate these two types of records.

The following describes the interpretation, attributes, parameters, system impact, possible causes, handling steps, and clear method of each record. The alarm names, attributes, and triggering conditions are subject to the current implementation.

#### 0xFC001000 Controller Master To Slave Alarm

**Event Interpretation**

When an active/standby switchover occurs in Controller active/standby mode and the standby Controller obtains the active role and starts service modules, this event is reported. Currently, `alarmName` is `Controller Master To Slave Alarm`, and the actual trigger semantics is that the standby Controller is promoted to the active Controller.

**Event Attributes**

| Event ID | Event Severity | Event Type |
| --- | --- | --- |
| `0xFC001000` | Warning | Status change |

**Event Parameters**

| Category | Parameter Name | Parameter Description |
| --- | --- | --- |
| Location information, MOI, additional information | `service name` | Component name `Controller`. |
| Location information, MOI, additional information | `service ip` | `POD_IP` of the Pod where Controller resides. |
| Additional information | `pod id` | Environment variable `SERVICE_ID`, which is `Unknown` when not configured. |
| Cause | `reasonId=1` | The original active Controller is unavailable due to a fault. |

**System Impact**

`serviceAffectedType=0` for this record. The new active Controller starts service modules in place of the original active Controller to notify that an active/standby switchover has occurred.

**Possible Causes**

The node, process, or active lock lease renewal of the original active Controller is abnormal, which triggers the Controller active/standby switchover.

**Handling Steps**

1. Check the status of the node and process where the original active Controller resides.
2. Check the logs of the original active Controller and the active/standby election logs to confirm the switchover cause.
3. Confirm that all service modules of the new active Controller have started properly.

**Event Clear**

This record is a one-time state change event and does not need to be cleared; no `CLEAR` record is generated.

#### 0xFC001001 Service Level Degradation Alarm

**Alarm Interpretation**

Describes the scenario where a hardware or software fault reduces the number of available instances and degrades the service capability. MindIE Motor currently retains only the data model of this record, and the main flow has no active reporting point.

**Alarm Attributes**

| Alarm ID | Alarm Severity | Alarm Type |
| --- | --- | --- |
| `0xFC001001` | Major | Status change |

**Alarm Parameters**

| Category | Parameter Name | Parameter Description |
| --- | --- | --- |
| Cause | `reasonId=0` | Default value of the current data model. |
| Cause | `probableCause` | The reserved cause is that a hardware or software fault reduces the number of instances. |
| Location information, MOI, additional information | - | The current data model does not fill in scenario parameters; they are provided by the party that generates the actual record later. |

**System Impact**

If a subsequent flow generates this record, the number of available service instances is reduced, and the maximum throughput capability may decrease.

**Possible Causes**

A hardware or software fault reduces the number of Prefill, Decode, or hybrid instances.

**Handling Steps**

1. First confirm the party that generates the record and the MindIE Motor version; the current main flow does not actively generate this record.
2. Check the instance status in Controller and the logs of the corresponding NodeManager and inference instances.

**Alarm Clear**

MindIE Motor currently has no active reporting or clear flow for this record; if it is generated by another compatible component, the clear behavior is determined by the party that generates the record.

#### 0xFC001002 Model Instance Exception Alarm

**Alarm Interpretation**

- **Alarm reporting**: Controller reports this alarm when a model instance is abnormal due to an abnormal state, a heartbeat timeout that is confirmed by NodeManager probing, or the instance entering the paused state.
- **Alarm recovery**: When the corresponding instance enters the `ACTIVE` state again, Controller generates a clear record.

When an Endpoint heartbeat times out or its state is abnormal, the `0xFC001003` event may also occur.

**Alarm Attributes**

| Alarm ID | Alarm Severity | Alarm Type |
| --- | --- | --- |
| `0xFC001002` | Critical | Status change |

**Alarm Parameters**

| Category | Parameter Name | Parameter Description |
| --- | --- | --- |
| Location information, MOI, additional information | `servicename` | Component name `controller`. |
| Location information, MOI, additional information | `inst_type` | Currently, `p_inst_type or d_inst_type` is always output. |
| Location information, MOI, additional information | `inst_id` | `job_name` of the abnormal instance. |
| Location information, MOI, additional information | `service ip` | `POD_IP` of the Pod where Controller resides. |
| Additional information | `pod id` | Environment variable `SERVICE_ID`. |
| Response field | `instanceId` | `job_name` of the abnormal instance. |
| Cause | `reasonId=1` | A hardware or software fault causes the model instance to be abnormal. |

**System Impact**

The abnormal instance exits the schedulable state. When the capacity of the remaining instances is insufficient, the service throughput decreases or the inference service becomes unavailable.

**Possible Causes**

1. The instance Endpoint heartbeat times out or an abnormal state is reported.
2. A hardware or software fault occurs on the node, NodeManager, or inference process of the instance.
3. The instance is paused.

**Handling Steps**

1. Locate the abnormal instance based on `inst_id`, and check the status of the node where the instance resides and of NodeManager.
2. Check the logs of Controller, NodeManager, and the corresponding inference instance to confirm the cause of the heartbeat timeout or abnormality.
3. After the fault is rectified, confirm that the instance is registered again and enters the `ACTIVE` state.

**Alarm Clear**

When the instance enters the `ACTIVE` state again, a clear record with `category=2` and `cleared=1` is automatically generated, and manual clearing is not required.

#### 0xFC001003 Server Exception Alarm

**Event Interpretation**

Controller reports this event when it detects an instance Endpoint heartbeat timeout or an abnormal Endpoint state.

**Event Attributes**

| Event ID | Event Severity | Event Type |
| --- | --- | --- |
| `0xFC001003` | Major | Status change |

**Event Parameters**

| Category | Parameter Name | Parameter Description |
| --- | --- | --- |
| Location information, MOI, additional information | `service name` | Component name `Controller`. |
| Location information, MOI, additional information | `endpoint ip` | IP address of the abnormal Endpoint. |
| Location information, MOI, additional information | `endpoint ids` | List of Endpoint IDs detected as abnormal under the same IP. |
| Additional information | `pod id` | Environment variable `SERVICE_ID`. |
| Cause | `reasonId=1` | Endpoint heartbeat timeout. |
| Cause | `reasonId=2` | Abnormal Endpoint state. |

**System Impact**

`serviceAffectedType=1` for this record. The instance to which the abnormal Endpoint belongs may subsequently exit the schedulable state and trigger the `0xFC001002` alarm.

**Possible Causes**

1. The Endpoint does not respond or the heartbeat link is abnormal.
2. The Endpoint actively reports an abnormal state.
3. The inference process, NodeManager, network, or node hardware is abnormal.

**Handling Steps**

1. Distinguish between a heartbeat timeout and an abnormal Endpoint state based on `reasonId`.
2. Locate the inference process based on `endpoint ip` and `endpoint ids`, and check the node, network, and process status.
3. Check the logs of the corresponding inference instance, NodeManager, and Controller.

**Event Clear**

This record is a one-time abnormal event and does not need to be cleared; no `CLEAR` record is generated. The recovery of the instance-level alarm is reflected by the `0xFC001002` clear record.

#### 0xFC001004 Coordinator Service Exception Alarm

**Alarm Interpretation**

- **Alarm reporting**: Controller reports this alarm when it detects that no Prefill instance or no Decode instance is available.
- **Alarm recovery**: When both the Prefill and Decode roles have available instances again, Controller generates a clear record.

The current main flow uses `reasonId=1`. `reasonId=2` is a reserved cause for an abnormal state of Coordinator itself, and there is currently no active reporting point.

**Alarm Attributes**

| Alarm ID | Alarm Severity | Alarm Type |
| --- | --- | --- |
| `0xFC001004` | Critical | Service quality alarm |

**Alarm Parameters**

| Category | Parameter Name | Parameter Description |
| --- | --- | --- |
| Location information, MOI, additional information | `service name` | Component name `Coordinator`. |
| Location information, MOI, additional information | `service ip` | `POD_IP` of the Pod where Coordinator resides. |
| Additional information | `pod id` | Environment variable `SERVICE_ID`. |
| Cause | `reasonId=1` | No Prefill instance or Decode instance is available. |
| Cause | `reasonId=2` | Coordinator itself is in an abnormal state. This cause is not used by the current main flow. |

**System Impact**

`serviceAffectedType=1` for this record. When the available Prefill or Decode role is missing, PD disaggregation inference requests cannot be scheduled properly.

**Possible Causes**

1. All instances of a role are abnormal, paused, or removed.
2. A fault occurs on the node, NodeManager, or inference process of the instances.

**Handling Steps**

1. Query the instance status in Controller to confirm the role that lacks available instances.
2. Locate the abnormal instances and Endpoints based on the `0xFC001002` and `0xFC001003` records that occur at the same time.
3. Rectify the fault and confirm that both Prefill and Decode instances are restored to `ACTIVE`.

**Alarm Clear**

When both the Prefill and Decode roles have available instances again, a clear record with `category=2` and `cleared=1` is automatically generated.

#### 0xFC001005 Coordinator Request Congestion Alarm

**Event Interpretation**

- When the number of inference requests currently processed by Coordinator reaches 85% of `max_requests`, a congestion state event is reported.
- After the congestion state has been reported, when the number of requests drops below 75% of `max_requests`, a recovery state event is reported.

Both reports are `EVENT` records, and whether the state is congestion or recovery is distinguished by the number of requests and the threshold description in `additionalInformation`.

**Event Attributes**

| Event ID | Event Severity | Event Type |
| --- | --- | --- |
| `0xFC001005` | Major | Status change |

**Event Parameters**

| Category | Parameter Name | Parameter Description |
| --- | --- | --- |
| Location information, MOI | `service name` | Component name `Coordinator`. |
| Location information, MOI | `service ip` | `POD_IP` of the Pod where Coordinator resides. |
| Additional information | Current number of requests, `max_requests`, and threshold | Identifies whether this record indicates the congestion state at 85% or the recovery state below 75%. |
| Additional information | `pod id` | Environment variable `SERVICE_ID`. |
| Cause | `reasonId=1` | Congestion occurs in the requests being processed by Coordinator. |

**System Impact**

`serviceAffectedType=1` for this record. When the number of requests approaches the concurrency upper limit, new requests may be rate-limited, and the service latency may also increase.

**Possible Causes**

The current number of concurrent requests approaches the configured `max_requests`, or insufficient processing capability of downstream instances causes a request backlog.

**Handling Steps**

1. Check the current request volume, inference latency, and the load of each instance.
2. Confirm whether the `max_requests` configuration matches the deployment scale and capacity planning.
3. If requests remain congested, troubleshoot abnormal instances or scale out based on the actual capacity.

**Event Clear**

When the number of requests drops below 75% of `max_requests`, a recovery state event is reported, and no `CLEAR` record is generated.

#### 0xFC001006 Cluster Connection Exception Alarm

**Alarm Interpretation**

Describes exceptions such as registration, RankTable subscription, fault message subscription, or connection interruption between Controller and cluster services. MindIE Motor currently retains the data model and the receiving and parsing capability of this alarm, and the main flow has no active reporting point.

**Alarm Attributes**

| Alarm ID | Alarm Severity | Alarm Type |
| --- | --- | --- |
| `0xFC001006` | Critical | Status change |

**Alarm Parameters**

| Category | Parameter Name | Parameter Description |
| --- | --- | --- |
| Location information, MOI, additional information | `service name` | Component name `Controller`. |
| Location information, MOI, additional information | `service ip` | `POD_IP` of the Pod where Controller resides. |
| Location information, MOI, additional information | `cluster ip` | Environment variable `MINDX_SERVER_IP`; it is not output when not configured. |
| Additional information | `pod id` | Environment variable `SERVICE_ID`. |
| Cause | `reasonId=1` | Cluster service registration fails. |
| Cause | `reasonId=2` | RankTable subscription fails. |
| Cause | `reasonId=3` | Fault message subscription fails. |
| Cause | `reasonId=4` | The connection is interrupted. |

**System Impact**

If a compatible component generates this record, Controller cannot obtain the corresponding cluster data properly, and service flows that depend on this data may be affected.

**Possible Causes**

1. The cluster service address is incorrectly configured or the service is unavailable.
2. The network between Controller and the cluster service is abnormal.
3. Registration, RankTable subscription, or fault message subscription fails.

**Handling Steps**

1. First confirm the party that generates the record and the MindIE Motor version; the current main flow does not actively generate this record.
2. Check the corresponding registration, subscription, or connection link based on `reasonId`.
3. Check the cluster service status, address configuration, network connectivity, and related logs.

**Alarm Clear**

MindIE Motor currently does not actively generate this alarm, and the clear behavior is determined by the party that generates the record; Controller can receive and store the corresponding `CLEAR` records.

#### 0xFC001007 Scale P To D Recovery Event

**Event Interpretation**

After the ScaleP2D fault recovery policy successfully recovers a Decode instance, Controller reports this event.

**Event Attributes**

| Event ID | Event Severity | Event Type |
| --- | --- | --- |
| `0xFC001007` | Warning | Status change |

**Event Parameters**

| Category | Parameter Name | Parameter Description |
| --- | --- | --- |
| Location information, MOI, additional information | `service name` | Component name `Controller`. |
| Location information, MOI, additional information | `d instance id` | ID of the recovered Decode instance. |
| Location information, MOI, additional information | `d instance job name` | Job name of the recovered Decode instance. |
| Location information, MOI, additional information | `killed p instance ids` | List of Prefill instance IDs terminated during recovery. |
| Response field | `instanceId` | ID of the recovered Decode instance. |
| Cause | `reasonId=1` | The Decode instance has been recovered by the ScaleP2D policy. |

**System Impact**

`serviceAffectedType=0` for this record, indicating that the ScaleP2D recovery flow has completed successfully.

**Possible Causes**

A fault that can be handled by the ScaleP2D policy occurs on a Decode instance, and Controller completes recovery by adjusting Prefill/Decode resources.

**Handling Steps**

1. Confirm the status of the recovered Decode instance based on `d instance id` and `d instance job name`.
2. Confirm that the terminated Prefill instances have been handled as expected based on `killed p instance ids`.
3. If the service is still abnormal after recovery, check the Controller fault recovery logs and the logs of the related instances.

**Event Clear**

This record is a one-time recovery event and does not need to be cleared; no `CLEAR` record is generated.

#### 0xFC001008 Coordinator Master To Slave Alarm

**Event Interpretation**

When an active/standby switchover occurs in Coordinator active/standby mode and the standby Coordinator obtains the active role and starts the Inference process, this event is reported. Currently, `alarmName` is `Coordinator Master To Slave Alarm`, and the actual trigger semantics is that the standby Coordinator is promoted to the active Coordinator.

**Event Attributes**

| Event ID | Event Severity | Event Type |
| --- | --- | --- |
| `0xFC001008` | Warning | Status change |

**Event Parameters**

| Category | Parameter Name | Parameter Description |
| --- | --- | --- |
| Location information, MOI, additional information | `service name` | Component name `Coordinator`. |
| Location information, MOI, additional information | `service ip` | `POD_IP` of the Pod where Coordinator resides. |
| Additional information | `pod id` | Environment variable `SERVICE_ID`. |
| Cause | `reasonId=1` | The original active Coordinator is unavailable due to a fault. |

**System Impact**

`serviceAffectedType=0` for this record. The new active Coordinator starts the Inference process to notify that an active/standby switchover has occurred.

**Possible Causes**

The node, process, or active lock lease renewal of the original active Coordinator is abnormal, which triggers the Coordinator active/standby switchover.

**Handling Steps**

1. Check the status of the node and process where the original active Coordinator resides.
2. Check the logs of the original active Coordinator and the active/standby election logs to confirm the switchover cause.
3. Confirm that the Inference process of the new active Coordinator has started properly and can process requests.

**Event Clear**

This record is a one-time state change event and does not need to be cleared; no `CLEAR` record is generated.

#### 0xFC001009 Precision Anomaly Alarm

**Alarm Interpretation**

- **Alarm reporting**: Coordinator performs token-level sampling detection on Decode output; after consecutive anomalies of the same instance group reach `precision_issue_threshold`, a fixed question-and-answer probe is performed and a precision anomaly alarm is reported. The alarm is reported regardless of whether the probe succeeds or fails, and the number of probe failures is written into the additional information.
- **Alarm recovery**: A clear record is generated when Controller automatic recovery succeeds, CCAE precision control successfully terminates the instance group, or consecutive normal detections of the same instance group reach `precision_clear_threshold`.

**Alarm Attributes**

| Alarm ID | Alarm Severity | Alarm Type |
| --- | --- | --- |
| `0xFC001009` | Major | Processing error |

**Alarm Parameters**

| Category | Parameter Name | Parameter Description |
| --- | --- | --- |
| Location information | `service name` | Component name `Coordinator`. |
| Location information | `service ip` | `POD_IP` of the Pod where Coordinator resides. |
| MOI | `pId` | Prefill instance ID; this parameter is not included in PD hybrid deployment. |
| MOI | `instanceId` | Decode instance ID. The alarm and the clear record must use exactly the same MOI. |
| Additional information | `precision_issue_count` | Cumulative number of consecutive precision anomalies when the alarm is triggered. |
| Additional information | `probe_failure_count` | Number of failures of the fixed question-and-answer probe. |
| Additional information | `p_instance_id` | Prefill instance ID; `None` when there is no Prefill instance. |
| Additional information | `d_instance_id` | Decode instance ID. |
| Additional information | `pod id` | The first non-empty value among `SERVICE_ID`, `sys_id`, and the model ID. |
| Response field | `instanceId` | Decode instance ID. |
| Response field | `pInstanceId` | Prefill instance ID; an empty string when there is no Prefill instance. |
| Cause | `reasonId=0` | The current implementation retains the default value of `Record`. |

**System Impact**

`serviceAffectedType=1` for this record. The target instance group may continuously produce output quality anomalies such as repetition, garbled text, and rare characters; after automatic recovery is enabled, Controller terminates the corresponding Decode instance and the optional Prefill instance.

**Possible Causes**

The same Decode or PD instance group continuously produces token-level output quality anomalies and reaches the configured consecutive anomaly threshold.

**Handling Steps**

1. Locate the abnormal instance group based on `instanceId`, `pInstanceId`, and the MOI.
2. Check `precision_issue_count`, `probe_failure_count`, and the Coordinator precision detection logs.
3. Check the model, weights, quantization configuration, and run logs of the corresponding inference instance.
4. Confirm the Controller automatic recovery result, or perform recovery through CCAE precision control.

**Alarm Clear**

The clear record must reuse the MOI of the original alarm.

To report the preceding records to CCAE, you can deploy CCAE Reporter. For the configuration and startup method of the Reporter and its relationship with the northbound interfaces, see "Connecting to the CCAE Frontend Platform" below.

## Connecting to the CCAE Frontend Platform

 Cluster Computing Autonomous Engine (CCAE) is a cluster autonomous engine system. Motor can connect to CCAE through the CCAE Reporter in `examples/features/observability/ccae_reporter`. The Reporter collects Motor's alarms, logs, instance list, and metrics information and reports them to CCAE.

### Configuring CCAE Information

Enable Observability in `user_config.json` and add the CCAE northbound platform configuration:

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

The configuration is described as follows:

| Parameter | Description |
| --- | --- |
| `motor_controller_config.observability_config.observability_enable` | Enables the Controller Observability query interface. The CCAE Reporter relies on this interface to obtain the instance list and alarms. Metrics are obtained by the Reporter directly from the Coordinator's `/metrics`. |
| `motor_controller_config.api_config.observability_api_port` | Port of the Controller Observability query interface. The default value is `1027`. When omitted from the JSON, the Reporter falls back to this default value. |
| `motor_controller_config.api_config.controller_api_port` | Controller management/probe port. The default value is `1026`. When omitted from the JSON, the Reporter falls back to this default value. |
| `motor_coordinator_config.api_config.coordinator_obs_port` | Coordinator `/metrics` port. The default value is `1027`. When omitted from the JSON, the Reporter falls back to this default value. |
| `motor_deploy_config.tls_config.north_tls_config` | TLS configuration used by the Reporter to access the CCAE northbound interface and Kafka. |
| `north_config.name` | Name of the northbound Reporter. Set it to `ccae_reporter`. |
| `north_config.ip` | IP address of the CCAE platform. |
| `north_config.port` | Northbound HTTP port of the CCAE platform. |

After modifying the configuration, you can update the configuration in the `examples/deployer` directory:

```bash
cd examples/deployer
python deploy.py --config_dir ../../model_configs/vllm --update_config
```

You can also specify the configuration file separately:

```bash
python deploy.py --user_config_path ../../model_configs/vllm/user_config.json --env_config_path ../../model_configs/vllm/env.json --update_config
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
