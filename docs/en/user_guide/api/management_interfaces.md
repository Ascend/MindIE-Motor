# Management Interface

>[!NOTE]NOTE
>
> Kubernetes deployment should restrict the access scope of management ports through ClusterIP, NetworkPolicy, and similar mechanisms. For bare metal standalone deployment that needs to listen externally, it is recommended that the management plane API Key and `mgmt_tls_config` be enabled at the same time; the API Key is responsible for identity verification, and TLS is responsible for preventing eavesdropping.

## Management Plane API Key Authentication

After `mgmt_api_key_config.enable_api_key=true` is configured, the following privileged interfaces must carry the request header
`X-Motor-Management-Key: <key>`:

- `GET /instances`
- `POST /instances/refresh`
- `POST /precision/alarm_cleared`

`/startup`, `/liveness`, `/readiness`, and the root path remain unauthenticated, so Kubernetes probes do not need to be changed. A missing request header returns `401`, and an incorrect key returns `403`. The key is read from the single-line file specified by `mgmt_api_key_config.api_key_file` and should not be written directly into the JSON configuration. The API Key itself is not encrypted during transmission, so management plane TLS should also be enabled for cross-host access.

## Startup Probe Interface

**Interface Function**

Provides the probe with the service startup status.

**Interface Format**

Request type: **GET**
> URL: `http(s)://{IP}:{Port}/startup`

For the IP and port, see [IP/Port and Configuration of the Management Interface](./README.md#ipport-and-configuration-of-the-management-interface)

**Request Parameter**
None

**Usage Example**

```bash
curl -X GET "http://{IP}:{Port}/startup"
```

**Response Example**

```JSON
{ "status": "ok", "message": "Coordinator is starting up" }
```

## Liveness Probe Interface

**Interface Function**

Allows the probe to query the liveness status of the service.

**Interface Format**

Request type: **GET**
> URL: `http(s)://{IP}:{Port}/liveness`

For the IP and port, see [IP/Port and Configuration of the Management Interface](./README.md#ipport-and-configuration-of-the-management-interface)

**Request Parameter**
None

**Usage Example**

```bash
curl -X GET "http://{IP}:{Port}/liveness"
```

**Response Example**

- Response example:

```JSON
{ "status": "ok", "message": "Coordinator is alive" }
```

## Readiness Probe Interface

**Interface Function**

Queries whether the service is ready.

**Interface Format**

Request type: **GET**
> URL: `http(s)://{IP}:{Port}/readiness`

For the IP and port, see [IP/Port and Configuration of the Management Interface](./README.md#ipport-and-configuration-of-the-management-interface)

**Request Parameter**
None

**Usage Example**

```bash
curl -X GET "http://{IP}:{Port}/readiness"
```

**Response Example**

```json
{ "status": "ok", "message": "Coordinator is ok", "ready": true }
```

>[!NOTE]NOTE
>If active/standby mode is enabled and the current node is not the master node, `503` is returned with the message `Coordinator is not master`.

## Health Status Query Interface

**Interface Function**

Queries the health status of the service.

**Interface Format**

Request type: **GET**
> URL: `http(s)://{IP}:{Port}/health`

For the IP and port, see [IP/Port and Configuration of the Metrics Interface](./README.md#ipport-and-configuration-of-the-metrics-interface)

**Request Parameter**

None

**Usage Example**

```bash
curl -X GET "http://{IP}:{Port}/health"
```

**Response Example**

```json
{ "status": "ok", "timestamp": "2026-07-02T10:00:00+00:00" }
```

>[!NOTE]NOTE
>`/health` and `/metrics` are mounted on the Coordinator Observability port (`coordinator_obs_port`, default `1027`, K8s nodePort `31017`), and are **not** served on the management interface port (`coordinator_api_mgmt_port`, default `1026`).

## Instance Query Interface

**Interface Function**

Queries the instances currently registered with Coordinator (including available / unavailable / paused).
`status` comes from the push of Controller (or of independent registration), and the circuit breaker does not overwrite it; `pool` is the internal pool of Coordinator;
`circuit_breaker` is a snapshot of the circuit breaker state machine; `healthy` is `true` only when `pool=available`, `status=active`, and the circuit breaker is
`closed`.

**Interface Format**

Request type: **GET**
> URL: `http(s)://{IP}:{Port}/instances`

The external service of the instance query is located on the Coordinator Observability port: inside a container or on a bare metal machine, `coordinator_obs_port` (`1027`) is used by default; outside a Kubernetes cluster, the NodePort of the `mindie-motor-coordinator-obs` Service (default `31017`) is used. The original management port
`coordinator_api_mgmt_port` (default `1026`) still retains this interface, and is used only for compatible calls within the cluster.

**Request Parameter**
None

**Usage Example**

```bash
curl -X GET "http://{server IP}:31017/instances" \
  -H "X-Motor-Management-Key: <key>"
```

Bare metal or port passthrough deployment can use `http(s)://{server IP}:1027/instances`. When
`mgmt_api_key_config` is not enabled, the preceding request header does not need to be carried.

Standalone deployment can also use:

```bash
python3 -m motor.coordinator.register list
```

**Response Example**

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
      "pool": "available",
      "healthy": true,
      "circuit_breaker": {
        "state": "closed",
        "trip_count": 0,
        "failure_count": 0,
        "current_timeout": 0.0
      },
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
      "pool": "available",
      "healthy": true,
      "circuit_breaker": {
        "state": "closed",
        "trip_count": 0,
        "failure_count": 0,
        "current_timeout": 0.0
      },
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

**Output Description**

| Parameter | Type | Description |
| --- | --- | --- |
| `count` | int | Number of currently registered instances. |
| `instances` | array | List of instance summaries, sorted by `role` and `id`. |
| `instances[].id` | int | Instance ID. For standalone deployment registration, it is derived from `role` + the sorted complete endpoint group. |
| `instances[].role` | string | Instance role: `prefill` / `decode` / `union`. |
| `instances[].job_name` | string | Instance job name. |
| `instances[].model_name` | string | Model name. |
| `instances[].status` | string | Instance status pushed by Controller, such as `active` / `inactive` / `paused`. The circuit breaker does not overwrite it. |
| `instances[].pool` | string | Coordinator internal pool: `available` / `unavailable` / `paused`; `unknown` when no ownership can be found. |
| `instances[].healthy` | bool | `true` only when `pool=available`, `status=active`, and the circuit breaker is `closed`. |
| `instances[].circuit_breaker` | object | Snapshot of the circuit breaker state machine. |
| `instances[].circuit_breaker.state` | string | `closed` (not tripped) / `open` (tripped). |
| `instances[].circuit_breaker.trip_count` | int | Cumulative number of trips. |
| `instances[].circuit_breaker.failure_count` | int | Current number of consecutive failures. |
| `instances[].circuit_breaker.current_timeout` | float | Current circuit breaker timeout in seconds. |
| `instances[].endpoints` | array | Business endpoints under the instance. |
| `instances[].endpoints[].business_port` | string | Engine HTTP port. |

## Instance Refresh Interface

**Interface Function**

Refreshes the instance list in the Coordinator (add/del/set).

**Interface Format**

Request type: **POST**
> URL: `http(s)://{IP}:{Port}/instances/refresh`

For the IP and port, see [IP/Port and Configuration of the Management Interface](./README.md#ipport-and-configuration-of-the-management-interface)

Request headers:

- Required: `Content-Type: application/json`

- Mandatory when the management plane API Key is enabled: `X-Motor-Management-Key: <key>`

**Request Parameters**

| parameter name | type | description |
|---|---|---|
| event | string | required; event type: `add` / `del` / `set`. |
| instances | array | required; instance list. |

**usage example**

>[!NOTE]NOTE
>The request body must be in JSON format and must not exceed 10 MB.
>[!WARNING]Version Upgrade
> The `Endpoint` registration protocol has removed the unused `mgmt_port` and strictly rejects unknown fields. Controller, Coordinator, and NodeManager must be upgraded together, and mixed operation of new and old components is not supported.

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

**response example**

```json
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

**output description**

`bootstrap_port` in `instances[].endpoints` is an optional field, used only for native SGLang PD bootstrap
interconnection.

| parameter name | type | description |
|---|---|---|
| request_id | string | Request identifier. |
| status | string | Request status. |
| message | string | Response message. |
| data | object | Response data. |
| data.timestamp | string | Event time. |
| data.event_type | string | Event type, corresponding to the request `event`. |
| data.instance_count | integer | Number of instances. |

### External Deployer Compact Instance Events

Coordinator standalone deployment reuses the same URL and supports the compact protocol of `event=set/add/del`. The original Controller
full protocol remains unchanged. A request is parsed according to the compact protocol when any of the following conditions is met: the top level contains `model_name`,
`dispatch_capabilities`, or `engine_type`; or `instances[].endpoints` is
a `[{"address": "host:port"}]` array and the instance does not contain `job_name`. If none of the preceding forms is matched, the request is parsed according to the Controller
full protocol. An error is returned when the two instance forms are mixed in the same request.

Top-level fields of the compact request:

| Parameter Name | Type | Mandatory | Description |
|---|---|---|---|
| `event` | string | Yes | `set` replaces all instances, `add` adds instances to management, and `del` removes instances from management. |
| `model_name` | string | No (mandatory when multiple models exist) | Can be omitted for a single model, in which case Coordinator automatically discovers it from `/v1/models` of the first reachable engine; it must be explicitly specified when the engine returns multiple models. If `motor_coordinator_config.aigw.id` is configured for Coordinator, the value must be consistent with it (case-insensitive). |
| `dispatch_capabilities` | string | No | Only `prefill_handoff_decode` (default, handoff) and `concurrent_engine_sync` (trigger/sync) are allowed. Other values such as `decode_colocation` are rejected. |
| `engine_type` | string | No | `vllm` by default; the current standalone compact protocol supports only `vllm`. |
| `instances` | array | Yes | Instances to be processed fully or incrementally. |
| `instances[].id` | integer | Yes | Globally unique instance ID, which must be greater than 0. |
| `instances[].role` | string | Yes | Only `prefill` or `decode` is supported. |
| `instances[].endpoints` | array | Yes | At least one business Endpoint. |
| `endpoints[].id` | integer | Yes | DP index within the instance, an integer ≥ 0; it must be unique within the same instance. |
| `endpoints[].address` | string | Yes | Engine `host:port`; use `[addr]:port` for IPv6. |

```JSON
{
  "event": "set",
  "model_name": "qwen3-8b",
  "instances": [
    {
      "id": 1,
      "role": "prefill",
      "endpoints": [
        {"id": 0, "address": "10.0.0.11:8000"}
      ]
    },
    {
      "id": 2,
      "role": "prefill",
      "endpoints": [
        {"id": 0, "address": "10.0.0.12:8000"}
      ]
    },
    {
      "id": 3,
      "role": "decode",
      "endpoints": [
        {"id": 0, "address": "10.0.0.21:8000"}
      ]
    }
  ]
}
```

Coordinator generates internal fields such as `job_name`, `status`, and `parallel_config`. When
`model_name` is omitted, it is automatically discovered through the engine `/v1/models`; when `dispatch_capabilities` and
`engine_type` are omitted, they default to `prefill_handoff_decode` and `vllm`, respectively. `add/del` only change the
Coordinator management state and do not manage the P/D lifecycle; after a restart, the External Deployer must
replay a complete `set`. To prevent a reused instance ID from mistakenly deleting an already updated instance, `del` must carry the
`role` and Endpoint physical identity (which is `address` in the external protocol) consistent with those at registration; if they are inconsistent, the interface returns
HTTP 409.

## Precision Alarm State Clearing Interface

**Interface Function**

Clears the precision alarm state of the specified P/D instance group in the Coordinator scheduler. This interface is called by Controller/O&M orchestration after the
precision alarm has been handled, and is not responsible for terminating instances.

**Interface Format**

Request type: **POST**
> URL: `http(s)://{IP}:{Port}/precision/alarm_cleared`

For the IP and port, see [IP/Port and Configuration of the Management Interface](./README.md#ipport-and-configuration-of-the-management-interface)

Request headers:

- Required: `Content-Type: application/json`
- Mandatory when the management plane API Key is enabled: `X-Motor-Management-Key: <key>`

**Request Parameters**

| Parameter Name | Type | Mandatory | Description |
|---|---|---|---|
| `d_instance_id` | integer | Yes | Decode instance ID. |
| `p_instance_id` | integer | No | Prefill instance ID; if it is not specified, clearing is performed based on the Decode instance only. |

**Usage Example**

```bash
curl -X POST "http://{IP}:{Port}/precision/alarm_cleared" \
  -H "Content-Type: application/json" \
  -H "X-Motor-Management-Key: <key>" \
  -d '{"d_instance_id": 2, "p_instance_id": 1}'
```

**Response Example**

```json
{
  "request_id": "precision_alarm_cleared",
  "status": "success",
  "message": "Precision alarm state cleared",
  "data": {"dismissed": true}
}
```

## Root Path Service Information Interface

**Interface Function**

Returns the Coordinator service information and interface index.

**Interface Format**

Request type: **GET**
> URL: `http(s)://{IP}:{Port}/`

For the IP and port, see [IP/Port and Configuration of the Management Interface](./README.md#ipport-and-configuration-of-the-management-interface)

**Request Parameter**
None

**Usage Example**

```bash
curl -X GET "http://{IP}:{Port}/"
```

**Response Example**

```json
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

**Output Description**

| Parameter | Type | Description |
| --- | --- | --- |
| `service` | string | Service name. |
| `version` | string | Service version number. |
| `description` | string | Service description. |
| `endpoints` | object | Interface index information, keyed by `HTTP method path`, with the description as the value. |
