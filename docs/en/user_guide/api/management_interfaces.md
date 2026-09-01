# Management Interface

>[!NOTE]NOTE
>
> The management interface is available only within the Kubernetes cluster and is not provided for use outside the cluster.

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

```JSON
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

```JSON
{ "status": "ok", "timestamp": "2026-07-02T10:00:00+00:00" }
```

>[!NOTE]NOTE
>`/health` and `/metrics` are mounted on the Coordinator Observability port (`coordinator_obs_port`, default `1027`, K8s nodePort `31017`), and are **not** served on the management interface port (`coordinator_api_mgmt_port`, default `1026`).

## Instance Refresh Interface

**Interface Function**

Refreshes the instance list in the Coordinator (add/del/set).

**Interface Format**

Request type: **POST**
> URL: `http(s)://{IP}:{Port}/instances/refresh`

For the IP and port, see [IP/Port and Configuration of the Management Interface](./README.md#ipport-and-configuration-of-the-management-interface)

Request headers:

- Required: `Content-Type: application/json`

- Optional: None

**Request Parameters**

| parameter name | type | description |
|---|---|---|
| event | string | required; event type: `add` / `del` / `set`. |
| instances | array | required; instance list. |

**usage example**

>[!NOTE]NOTE
>The request body must be in JSON format and must not exceed 10 MB.

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

**response example**

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

**output description**

| parameter name | type | description |
|---|---|---|
| request_id | string | Request identifier. |
| status | string | Request status. |
| message | string | Response message. |
| data | object | Response data. |
| data.timestamp | string | Event time. |
| data.event_type | string | Event type, corresponding to the request `event`. |
| data.instance_count | integer | Number of instances. |

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

**Output Description**

| Parameter | Type | Description |
| --- | --- | --- |
| `service` | string | Service name. |
| `version` | string | Service version number. |
| `description` | string | Service description. |
| `endpoints` | object | Interface index information, keyed by `HTTP method path`, with the description as the value. |
