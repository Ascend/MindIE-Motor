# Motor Node Manager

## Function Description

The Node Manager is a management process deployed on inference nodes, responsible for connecting the Controller to the Engine Server on the local node. The process entry point is `motor/node_manager/main.py`, and its main responsibilities are as follows:

1. Load node configuration, complete port allocation, and start the management-plane HTTP service.

2. Register the node with the Controller and receive instance startup commands delivered by the Controller.

3. Start, record, and stop the `engine_server` subprocess by endpoint.

4. Poll the Engine Server status and report heartbeats to the Controller.

5. Handle graceful pause, hot configuration update, container snapshot restoration, and software fault reporting.

### Component Structure

| Object | Source Code | Responsibility |
|------|------|------|
| `NodeManagerConfig` | `motor/config/node_manager.py` | Loads, validates, and reloads node configuration, and derives the number of endpoints and ports |
| `NodeManagerAPI` | `motor/node_manager/api_server/node_manager_api.py` | Runs FastAPI/uvicorn in a background thread, providing startup, shutdown, and probe interfaces |
| `Daemon` | `motor/node_manager/core/daemon.py` | Assembles the `engine_server` command, launches the child process, and maintains the PID |
| `EngineManager` | `motor/node_manager/core/engine_manager.py` | Handles registration/re-registration, validates startup commands, and processes ranktable, snapshot metadata, and fault reporting |
| `HeartbeatManager` | `motor/node_manager/core/heartbeat_manager.py` | Polls endpoint status, reports heartbeats, maintains pause/resume state, and triggers abnormal self-termination |
| `FaultReporter` | `motor/node_manager/core/fault_reporter.py` | Subscribes to Engine Server ZMQ software fault messages and forwards them to the Controller |
| `ControllerApiClient` | `motor/node_manager/api_client/controller_api_client.py` | Invokes the Controller's registration, re-registration, heartbeat, and fault reporting interfaces |
| `EngineServerApiClient` | `motor/node_manager/api_client/engine_server_api_client.py` | Invokes the Engine Server management plane's `GET /status` |

`Daemon`, `EngineManager`, and `HeartbeatManager` are all thread-safe singletons. HTTP routes and background threads share instances, endpoints, and process state through these singletons.

## Lifecycle

### Startup

The startup sequence of `main()` is as follows:

1. Register the `SIGINT` and `SIGTERM` signal handlers, and set the process name to `NodeManager`.

2. Load `NodeManagerConfig` from `Env.user_config_path or Env.config_path`.

3. Configure logging and perform Node Manager port allocation.

4. Create `NodeManagerAPI`, `Daemon`, `EngineManager`, and `HeartbeatManager` in sequence.

5. `NodeManagerAPI` starts in the `nm_api_server` background thread; after the FastAPI lifespan is ready, the API ready event is set.

6. The `engine_register` thread of `EngineManager` waits up to 30 seconds for the API ready event, and then registers with the Controller.

7. In non-snapshot mode, start the configuration file watcher; in snapshot mode, inotify is not used, so the watcher is disabled.

8. The main thread continuously checks the exit signal and `HeartbeatManager.should_suicide()`.

The first registration is attempted up to 5 times, with retry intervals of 2, 4, 8, and 16 seconds. After consecutive failures, `EngineManager` sends `SIGTERM` to the current process.

### Starting an Instance

After the Controller calls `POST /node-manager/start`, the processing flow is as follows:

1. Parse the request body into `StartCmdMsg`.

2. Verify that `job_name`, the number of endpoints, and the IP of each endpoint are consistent with the configuration of this node.

3. Save `instance_id`, endpoints, `node_rank`, and D2D peer information; if `RANKTABLE_PATH` is configured, write the instance ranktable to that file.

4. Prepare the snapshot runtime directory and metadata.

5. `Daemon.pull_engine()` starts an `engine_server` child process for each endpoint.

6. Update the endpoints in `HeartbeatManager`, and start the status polling and heartbeat threads.

7. Start `FaultReporter` in `EngineManager` (this takes effect only when the fault tolerance feature is enabled).

When restoring from a host-side snapshot, step 5 does not start the Engine Server again; instead, it updates the restoration metadata, endpoints, and restoration status.

### Stop and Rescheduling

- Upon receiving `SIGINT`, `SIGTERM`, or the standard input command `stop`, the Node Manager stops the configuration watcher and stops modules in the reverse order of initialization.

- `Daemon.stop()` sends `SIGKILL` to the recorded Engine Server PIDs, and then clears the PID list.

- When any endpoint remains `ABNORMAL` for five consecutive heartbeat periods, `HeartbeatManager` sets the suicide flag. The main thread performs cleanup and returns `-1` to trigger rescheduling.

- The current normal exit path of `main()` also returns `-1`; the source code comments specify that `-1` indicates rescheduling and `0` indicates restart.

## Node Manager HTTP API

The Node Manager API listens on `api_config.pod_ip:api_config.node_manager_port` by default. HTTPS is used after `mgmt_tls_config.enable_tls` is enabled.

| Method | Path | Response | Description |
|------|------|----------|------|
| `POST` | `/node-manager/start` | `200 {}` | Validates the startup command and starts the Engine Server; performs restoration preparation during snapshot restoration |
| `POST` | `/node-manager/stop` | `200 {"message": "All engine processes stopped successfully."}` | Stops all Engine Server processes recorded by the current Node Manager |
| `POST` | `/node-manager/pause` | `200 {"status":"ok", ...}` | Marks all endpoints as `PAUSED` and returns the Engine Server management address |
| `POST` | `/node-manager/resume` | `200 {"status":"ok", ...}` | Restores only `PAUSED` endpoints to `NORMAL` |
| `GET` | `/node-manager/status` | `200 {"status": true/false}` | Returns whether all endpoints are `NORMAL`; returns `false` when there are no endpoints |
| `GET` | `/readiness` | `200` or `503` | Kubernetes Readiness Probe interface. Instance node Pods do not configure this probe by default; it is configured only in the default container snapshot application scenario to determine the steady-state point before executing container checkpoint. Returns `503` before the steady-state point is reached, and `200` after it is reached |

`/node-manager/pause` is used for PreStop graceful shutdown: the paused status causes readiness to fail and notifies the Controller through heartbeat reporting; status polling does not overwrite the manually set `PAUSED` with the Engine Server return value. If PreStop is canceled, `/node-manager/resume` can be called to restore scheduling.

`/readiness` is used only in the default snapshot application scenario, that is, MindCluster instance rescheduling, and is not a general health check interface of the Node Manager. MindCluster queries through this interface whether the instance node has reached the steady-state point. In the user-defined custom application scenario of container snapshots, `/node-manager/status` can be called to query the steady-state point; the interface returning `200 {"status": true}` indicates that the steady-state point has been reached.

### `StartCmdMsg` Request Fields

| Field | Type | Required | Description |
|------|------|------|------|
| `job_name` | string | Yes | Instance task name, which must be consistent with the local node configuration |
| `role` | string | Yes | Instance role, such as `prefill`, `decode`, or `union` |
| `instance_id` | int | Yes | Instance ID assigned by the Controller |
| `endpoints` | array | Yes | Endpoints managed by this node; each element contains `id`, `ip`, `business_port`, `mgmt_port`, etc. |
| `master_dp_ip` | string | Yes | IP address of the data-parallel master node |
| `ranktable` | object/null | No | Instance-level ranktable, defaulting to `null` |
| `d2d_peer_ips` | array/null | No | D2D weight transfer peers, encoded by the Controller as `<endpoint_id>:<peer_ip>`, defaulting to `null` |
| `node_rank` | int | No | Node sequence number assigned by the Controller in registration order, defaulting to `0` |

The error codes of this API are as follows:

- Internal parsing exception of the start command: `400 Invalid start command payload`.

- Validation failure of `job_name`, the number of endpoints, or endpoint IPs: `422 Start command validation failed`.

- Failure to start the Engine Server: `500 Failed to start engine server`.

- Request JSON/Pydantic field parsing exceptions are converted to a generic `500` by the outer exception handling.

- `/readiness` returns `503` when the endpoint is not yet healthy or has not been started after snapshot restoration.

## Communication with External Components

### Controller

| Direction | Controller API | Behavior |
|------|-----------------|------|
| Node Manager → Controller | `POST /controller/register` | Reports the role, model, port, parallel configuration, ranktable, `nnodes`, and snapshot primary node flag |
| Node Manager → Controller | `POST /controller/reregister` | When the Controller restarts and returns `503` to heartbeats, re-registers with the instance and endpoint information |
| Node Manager → Controller | `POST /controller/heartbeat` | Reports the status of each endpoint according to `heartbeat_interval_seconds` |
| Node Manager → Controller | `POST /controller/report_software_fault` | Forwards software faults from the Engine Server |

Heartbeats use a long-connection client with a single timeout of 5 seconds. When a TCP request fails, the connection is re-established, and retries are performed twice with backoff intervals of 1 second and 2 seconds.

### Engine Server

When `HeartbeatManager` starts, it first waits for the management port of each endpoint to become connectable, waiting up to 60 seconds; after that, it calls the Engine Server's `GET /status` once per second, with a single request timeout of 5 seconds.

Status polling has the following protection logic:

- During the 120-second grace period after the Engine Server starts, the original status is retained when `ABNORMAL` is detected.

- A generation marker is used when updating endpoints to prevent stale probe results from overwriting data newly delivered by the Controller.

- The manually set `PAUSED` status is not overwritten by polling results.

- When the Engine Server returns an unknown status or an invalid response, it is handled as `ABNORMAL`.

## Engine Server Startup Parameters

`Daemon.pull_engine()` performs the following for each endpoint:

```text
engine_server \
  --dp-rank <endpoint.id> \
  --instance-id <instance_id> \
  --role <prefill|decode|union> \
  --host <endpoint.ip> \
  --port <endpoint.business_port> \
  --mgmt-port <endpoint.mgmt_port> \
  --master-dp-ip <master_dp_ip> \
  --node-rank <node_rank> \
  --config-path <USER_CONFIG_PATH>
```

Other parameters and environment variables:

- In multi-endpoint mode, `ASCEND_RT_VISIBLE_DEVICES` is calculated for each process based on `local_world_size`; when the device number exceeds the end, allocation wraps around cyclically.

- In single-container mode, `--kv-port` and `--dp-rpc-port` are appended; when the configuration exists, `--lookup-rpc-port` is also appended.

- When snapshots are enabled, `--snapshot-metadata` is appended.

- D2D peers are filtered by endpoint ID, separated by commas, and passed through `--d2d-peer-ips`.

- When `POD_IP` exists in the environment and `VLLM_HOST_IP` is not set, `VLLM_HOST_IP=POD_IP` is set automatically.

- When `MOONCAKE_ASCEND_IPV6_EXPERIMENT=1`, `MC_USE_IPV6=1` is set by default.

The service port of an endpoint must be within `[1024, 65535]`, and the IP must be a valid IPv4 or IPv6 address; otherwise, startup fails.

### Cross-Node PCP

The Node Manager always passes the `node_rank` assigned by the Controller to the Engine Server as `--node-rank`, and passes `master_dp_ip` as `--master-dp-ip`.

For vLLM, the Engine Server enables cross-node PCP when the engine configuration contains `nnodes > 1` and `master_port` (compatible with `master-port`) is configured:

- `master_addr` uses `master_dp_ip`.

- `node_rank == 0` is the master node.

- When `node_rank != 0`, a headless follower is enabled, and only worker processes are started.

The Node Manager derives the per-node `local_world_size` from `engine_config.nnodes`. When `pcp_size` is divisible by `nnodes`, each node uses `pcp_size / nnodes` PCP ranks to calculate the number of visible devices. `nnodes` and `master_port` themselves come from the engine configuration and are not command-line parameters appended by `Daemon`.

## Configuration Description

The configuration file path preferentially uses `USER_CONFIG_PATH`; otherwise, `CONFIG_PATH` is used. In the role-based user configuration, the Node Manager configuration is located in **`motor_nodemanger_config`** within the corresponding engine block. The `nodemanger` in this key name is the current compatibility format; do not rewrite it as `node_manager`.

Common configurations are as follows. For complete fields, see [Configuration Reference](../../user_guide/configuration/config_reference.md#motor_nodemanger_config).

| Configuration Item | Default Value | Description |
|--------|--------|------|
| `api_config.pod_ip` | `Env.pod_ip` or `127.0.0.1` | Registration address and API listening address |
| `api_config.node_manager_port` | `1026` | Node Manager management port |
| `endpoint_config.base_port` | `10000` | Engine Server port base; service/management ports are generated as even/odd numbers |
| `basic_config.heartbeat_interval_seconds` | `3` | Interval for heartbeat reporting to the Controller |
| `basic_config.enable_multi_endpoints` | `true` | Whether to create multiple endpoints based on DP and device count |
| `basic_config.nnodes` | `1` | Number of cross-node instances derived from `engine_config.nnodes` |
| `mgmt_tls_config.enable_tls` | `false` | Whether to enable TLS for management-plane communication among Node Manager, Controller, and Engine Server |
| `fault_tolerance_config.enable_fault_tolerance` | `false` | Whether to start the software fault subscription thread |
| `fault_tolerance_config.zmq_pub_port` | `0` | ZMQ PUB base port; each endpoint uses `base_port + endpoint.id` |
| `snapshot_config.enable_snapshot` | `false` | Whether to enable the container snapshot process |
| `snapshot_config.snapshot_metadata_path` | Empty | Custom snapshot metadata path; the user must create and mount this file in advance. When empty, the default snapshot application scenario is used, that is, MindCluster instance rescheduling |
| `port_allocator_config.enable` | `true` | Whether to automatically check and adjust ports at startup |

`endpoint_num`, `service_ports`, `mgmt_ports`, `device_num`, `parallel_config`, `model_name`, `engine_type`, and `dispatch_capabilities` are mainly derived from the deployment configuration and engine configuration. `dispatch_capabilities` does not accept direct user overrides.

When `pod_ip` is empty, the API service determines the listening protocol family based on `POD_IP`: IPv6 uses `::`, and other cases use `0.0.0.0`. Only a `NodeManagerAPI` that is directly constructed without passing a configuration uses the internal fallback port `8080`; the normal startup process uses the configured port.

### Configuration Hot Update

In non-snapshot mode, after the configuration watcher detects a file change, it invokes the `update_config()` of each module:

- `HeartbeatManager` dynamically updates `heartbeat_interval_seconds`.

- `EngineManager` updates the configuration, and starts, stops, or rebuilds `FaultReporter` based on changes to `enable_fault_tolerance`, endpoint, Pod IP, or `zmq_pub_port`.

- The API listening address, listening port, TLS, and device parameters cached by `Daemon` are not hot-restarted; after modification, the Node Manager must be restarted.

## Software Fault Reporting

After `fault_tolerance_config.enable_fault_tolerance` is enabled, `FaultReporter` connects a ZMQ SUB socket for each endpoint and subscribes to the topic `vllm_fault`. The port is:

```text
fault_tolerance_config.zmq_pub_port + endpoint.id
```

The status mapping in the message is as follows:

- `healthy`: Records the status and does not report a fault.

- `dead`: Reports `EngineDeadError`.

- `unhealthy`: Reports `EngineUnhealthyError`.

The same non-healthy status of the same Engine is marked as reported only after it is successfully sent to the Controller; if the sending fails, subsequent messages can still be retried. After a ZMQ error occurs, wait for 5 seconds and re-establish the subscription.

## Container Snapshot

After `snapshot_config.enable_snapshot` is enabled:

- The Node Manager does not support hot configuration updates and does not start the configuration file watcher.

- When the configuration is empty, the default snapshot application scenario is entered, that is, MindCluster instance rescheduling: the container snapshot image is created by the MindCluster; the MindCluster mounts the snapshot metadata through a ConfigMap, and the Node Manager copies the mounted file to the default writable path `/snapshot/snapshot_metadata.json` and then passes it to the Engine Server for use.

- In the custom path scenario, the user must create and mount the snapshot metadata file in advance; the framework reads or updates the file and passes its path to Engine Server, and is not responsible for creating or mounting the file.

- During the snapshot creation phase, after Engine Server completes suspend, its management-plane status changes from `INIT` to `NORMAL`. When all Engine Servers on this node have completed suspend, it indicates that the instance node container has reached the steady-state point: in the default snapshot application scenario, this is determined by `/readiness` returning `200`; in the user custom application scenario, this is determined by `/node-manager/status` returning `200 {"status": true}`.

- After the steady-state point is queried, checkpoint is performed on the instance node container, and the container Host snapshot image is saved.

- An instance in the process of container snapshot image checkpoint cannot provide services. When the Node Manager status is normal but the checkpoint has not yet completed, heartbeat reporting to the Controller is paused.

- After snapshot restoration, `job_name` and `namespace` are first restored from the metadata, the Pod IP and Controller DNS are refreshed, and then registration is performed again.

- When the Controller calls `/node-manager/start` again, the Node Manager only prepares the `model_load_path` and `data_parallel_master_ip` metadata required for the snapshot restoration phase, but does not recreate the Engine Server process.

- After snapshot restoration and before the start command is received, readiness remains not ready.

### Snapshot Metadata Fields

The snapshot metadata file must be a JSON object, and the values of the following fields are all strings. In custom application scenarios, users must prepare the metadata in advance according to the stage at which each field is used.

| Field | Usage Stage | Preparation Requirements | Description |
|------|----------|----------|------|
| `model_save_path` | Snapshot creation | Must be prepared before creating a container snapshot | The on-disk path of the runtime weights inside the container when a Device snapshot is saved. It must be a host-mounted path. |
| `model_load_path` | Snapshot restoration | Must be prepared before restoring from a container snapshot | The loading path of the runtime weights inside the container when a Device snapshot is restored. It must be a host-mounted path. |
| `job_name` | Snapshot restoration | Must be prepared before restoring from a container snapshot | The job name used to update the Node Manager during registration after restoration. |
| `namespace` | Snapshot restoration | Must be prepared when the Controller uses the in-cluster `.svc.cluster.local` DNS | Used during registration after restoration to update the Controller DNS to the namespace to which the snapshot belongs. It can be left unconfigured in non-cluster DNS scenarios. |
| `data_parallel_master_ip` | Snapshot restoration | May not be preconfigured; it is delivered by the Controller | The value in the file is used first. If it is not configured, the Node Manager writes the `master_dp_ip` delivered by the Controller. |
| `checkpoint` | Snapshot creation | Written after the Host-side checkpoint is completed | The user or MindCluster updates it to `"done"`, based on which the framework unlocks the Device and resumes the business of the cold-start instance. |

Therefore, in custom application scenarios, before restoring from a container snapshot, at least `model_load_path` and `job_name` must be prepared; when the in-cluster Controller DNS is used, `namespace` must also be prepared. Other unknown fields in the metadata are not used by the Node Manager.

## Usage Examples

Local startup entry point:

```bash
export USER_CONFIG_PATH=/path/to/user_config.json
export ROLE=prefill
python -m motor.node_manager.main
```

In actual deployment, the service is usually started through the image entry script, and the Controller automatically completes registration and instance delivery. You can use the following request to check the status:

```bash
curl http://127.0.0.1:1026/node-manager/status
curl -i http://127.0.0.1:1026/readiness
```

When TLS on the management plane is enabled, change the protocol to `https` and access it according to the certificate configuration.

## Error Reporting and Troubleshooting

- Log containing `Registration failed after maximum retries`: Check the Controller DNS, port, TLS configuration, and network connectivity. The Node Manager then receives `SIGTERM`.

- Log containing `Start command validation failed`: Check whether the `job_name`, the number of endpoints, and the endpoint IPs delivered by the Controller are consistent with the Node Manager configuration.

- Log containing `Invalid endpoint parameters`: Check the endpoint IP and service port. The service port must be within `[1024, 65535]`.

- Log containing `Engine process exited immediately`: The Engine Server exits immediately after `Popen`. Continue to check the Engine Server logs, configuration path, and startup parameters.

- `/readiness` returns `503`: No endpoint exists, a non-`NORMAL` endpoint exists, the state is `PAUSED`, or the startup command has not been received after snapshot restoration.

- Consecutive occurrence of `Consecutive abnormal heartbeat count: 5/5`: The Node Manager cleans up the Engine Server and exits with `-1` to trigger rescheduling.

The related unit tests are located in `tests/node_manager/`; the graceful pause process tests are located in `tests/e2e/test_prestop_e2e.py`.
