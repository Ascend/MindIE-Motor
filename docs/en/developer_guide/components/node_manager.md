# Motor Node Manager

## Function Description

The Node Manager is a management process deployed on inference nodes, responsible for connecting the Controller to the native vLLM/SGLang engine on the local node. The process entry point is `motor/node_manager/main.py`, and the core logic is in `motor/node_manager/node_manager.py` (the `NodeManager` class), which inherits from `motor/common/app/application.py` (the `Application` base class). Its main responsibilities are as follows:

1. Load node configuration, complete port allocation, and start the management-plane HTTP service.
2. Register the node with the Controller and receive instance startup commands delivered by the Controller.
3. Start, supervise, and stop the native engine process group directly by endpoint.
4. Poll the health interface of the native business port and report heartbeats to the Controller.
5. Handle graceful pause, hot configuration update, container snapshot restoration, and software fault reporting.

### Component Structure

| Object | Source Code | Responsibility |
|------|------|------|
| `Application` | `motor/common/app/application.py` | Base class: module management, hot configuration update propagation, daemon loop, and signal handling. It encapsulates the boilerplate shared by the Controller / NodeManager |
| `NodeManager` | `motor/node_manager/node_manager.py` | `Application` subclass: assembles modules and runs the daemon loop, checking the suicide flag on each tick |
| `NodeManagerConfig` | `motor/config/node_manager.py` | Loads, validates, and reloads node configuration, and derives the number of endpoints and ports |
| `NodeManagerAPI` | `motor/node_manager/api_server/node_manager_api.py` | Runs FastAPI/uvicorn in a background thread, providing startup, stop, and probe interfaces |
| `Daemon` | `motor/node_manager/core/daemon.py` | Service orchestrator: discovers and instantiates native Engine and KV-store services based on the configuration, maintains the process supervisor and the suicide arbitration thread, and holds EngineFtManager |
| `NativeEngineService` | `motor/node_manager/core/services/native_engine/service.py` | Native engine child process lifecycle management: constructs `LaunchContext`, starts/tracks/stops the vLLM/SGLang process group, orchestrates restarts (`restart`), and waits for `/health` readiness (`wait_ready`) |
| Startup acceleration adaptation | `motor/node_manager/core/services/native_engine/startup_acceleration.py` | Maps the Motor configuration to vLLM StartPlan/graph reuse environment variables and engine overrides, and performs the DFX pre-check of StartPlan candidate files before startup |
| `LocalService` | `motor/node_manager/core/services/memcache/lifecycle.py` | memcache backend lifecycle management: configuration preparation, child process startup (through `memcache/worker.py`), health check, and restart |
| `RegisterManager` | `motor/node_manager/core/register_manager.py` | Registration/re-registration, startup command validation, handling of ranktable, snapshot metadata, and persistence of engine restart parameters |
| `HeartbeatManager` | `motor/node_manager/core/heartbeat_manager.py` | Polls endpoint status, reports heartbeats, and maintains the pause/resume state; it only reports status facts, and the suicide decision is made in the Daemon |
| `EngineFtManager` | `motor/node_manager/core/engine_ft_manager.py` | Polls the engine FT status interface and reports software faults to the Controller; it is held by the Daemon and is paused/resumed during restarts |
| `ControllerApiClient` | `motor/node_manager/api_client/controller_api_client.py` | Invokes the Controller's registration, re-registration, heartbeat, and fault reporting interfaces |

`Daemon`, `RegisterManager`, and `HeartbeatManager` are all thread-safe singletons. HTTP routes and background threads share instances, endpoints, and process state through these singletons. `Application` and `NodeManager` are not singletons and are explicitly created by `main.py`.

## Lifecycle

### Startup

The startup process is driven by the `Application.run()` template method, and `main.py` is responsible only for configuration loading and entry invocation:

1. The module-level `set_process_title("NodeManager")` sets the process name.
2. `main()` loads `NodeManagerConfig`, configures logging, and performs port allocation.
3. `NodeManager(config)` is created and `run()` is called, which internally performs:
   a. `init_modules()` — dynamically registers modules based on `Daemon.has_engine`: `Daemon`, `NodeManagerAPI`, and `RegisterManager` and `HeartbeatManager`, which are registered only when an Engine exists.
   b. `_start_config_watcher()` — starts the configuration file watcher in non-snapshot mode; skipped in snapshot mode.
   c. `setup_signal_handlers()` — registers SIGINT / SIGTERM.
   d. `_daemon_loop()` — a select-based main loop that checks the suicide flag and stdin input every `daemon_loop_interval` seconds.
4. The initialization behavior of each module remains unchanged: `NodeManagerAPI.__init__` starts FastAPI in a background thread, `RegisterManager.__init__` starts the registration thread, and `Daemon.__init__` starts the process supervision thread.

The first registration is attempted up to 5 times, with retry intervals of 2, 4, 8, and 16 seconds. After consecutive failures, `RegisterManager` sends `SIGTERM` to the current process.

### Starting an Instance

After the Controller calls `POST /node-manager/start`, the processing flow is as follows:

1. Parse the request body into `StartCmdMsg`.
2. Verify that `job_name`, the number of endpoints, and the IP of each endpoint are consistent with the configuration of this node.
3. Save `instance_id`, endpoints, `node_rank`, and D2D peer information; if `RANKTABLE_PATH` is configured, write the instance ranktable to that file.
4. Prepare the snapshot runtime directory and metadata.
5. `Daemon.pull_engine()` starts one native engine process group (`vllm serve` / SGLang) for each endpoint, and starts the `EngineFtManager` held by `Daemon` (this takes effect only when the fault tolerance feature is enabled).
6. Update the endpoints in `HeartbeatManager`, and start the status polling and heartbeat threads.

When restoring from a host-side snapshot, step 5 does not start the engine again; instead, it updates the restoration metadata, endpoints, and restoration status.

### Stop and Rescheduling

- Upon receiving `SIGINT`, `SIGTERM`, or the standard input command `stop`, `Application._handle_signal()` sets `stop_event`; after the daemon loop exits, `shutdown()` is executed: the `stop()` of each module is called in the reverse order of registration, and then the configuration watcher is stopped.
- `Daemon.stop()` iterates over all services and calls `stop()`: `NativeEngineService.stop()` sends `SIGKILL` to the recorded native engine process group; `LocalService.stop()` sends `SIGKILL` to the memcache worker child process.
- The suicide decision is executed by an independent 3s arbitration thread in `Daemon`: when any endpoint remains `ABNORMAL` for 5 consecutive observation rounds (about 15s), the suicide flag is set (counting is paused within the freeze window of engine restart/death reporting). The daemon loop checks this flag on each tick; once triggered, `stop_event.set()` is called and `-1` is returned to trigger rescheduling.
- `exit_code` returns `-1` by default, consistent with the legacy behavior (-1 indicates rescheduling).

## Node Manager HTTP API

The Node Manager API listens on `api_config.pod_ip:api_config.node_manager_port` by default. HTTPS is used after `mgmt_tls_config.enable_tls` is enabled.

| Method | Path | Response | Description |
|------|------|----------|------|
| `POST` | `/node-manager/start` | `200 {}` | Validates the startup command and starts the native engine; performs restoration preparation during snapshot restoration |
| `POST` | `/node-manager/stop` | `200 {"message": "All engine processes stopped successfully."}` | Stops all native engine processes and then delays SIGTERM to itself (exit code `-1` → k8s restarts the Pod), that is, the "suicide" instruction delivered by the Controller, used for instance teardown and partial cross-machine disconnection coordination |
| `POST` | `/node-manager/engine-restart` | `200 {"message": ...}` | Controller-driven in-container engine restart: body `{"action": "restart"\|"abort", "instance_id"?}`. `restart` is fully delegated to `Daemon.restart_engine` (the Daemon parses the startup parameters, freezes suicide arbitration, pauses/resumes EngineFtManager, and kills and restarts all engines, while the KV store is not touched); `abort` = unfreezes suicide arbitration (falls back to container restart when the restart fails). Concurrency: 409; snapshot restoration in progress: 409; no startup record: 400; restart failure: 500 with unfreezing |
| `POST` | `/node-manager/pause` | `200 {"status":"ok", ...}` | Marks all endpoints as `PAUSED` and returns the `engine_metrics_targets` of non-headless native engines |
| `POST` | `/node-manager/resume` | `200 {"status":"ok", ...}` | Restores only `PAUSED` endpoints to `NORMAL` |
| `POST` | `/node-manager/fault-tolerance/status` | `200 {...}` | Queries the engine FT status of an explicit DP rank; can be called only by the Controller control plane |
| `POST` | `/node-manager/fault-tolerance/apply` | `200 {"status":"accepted"}` | Delivers `retry` or `scale_down`; can be called only by the Controller control plane |
| `POST` | `/node-manager/fault-tolerance/guard` | `200 {"status":"guarded"}` | Freezes suicide arbitration during a scale-in transaction; the lease is at most 300 seconds |
| `POST` | `/node-manager/fault-tolerance/finalize` | `200 {"status":...}` | Commits the local retired rank or releases the transaction guard |
| `GET` | `/node-manager/status` | `200 {"status": true/false}` | Returns whether all endpoints are `NORMAL`; when `relaxed=true` (engine restart polling), it is `true` as long as there is no `ABNORMAL`; it is `false` when there is no endpoint |
| `GET` | `/readiness` | `200` or `503` | Kubernetes Readiness Probe interface. Instance node Pods do not configure this probe by default; it is configured only in the default container snapshot application scenario to determine the steady-state point before executing container checkpoint. Returns `503` before the steady-state point is reached, and `200` after it is reached |

`/node-manager/pause` is used for PreStop graceful shutdown: the paused status causes readiness to fail and notifies the Controller through heartbeat; native health polling does not overwrite the manually set `PAUSED`. The `engine_metrics_targets` in the response uses the native business port and excludes headless members. If PreStop is canceled, `/node-manager/resume` can be called to restore scheduling.

`/node-manager/fault-tolerance/*` are stateful internal management interfaces, and `guard` temporarily freezes Pod self-healing.
Deployment must restrict access to the Controller only through Kubernetes NetworkPolicy, a service mesh, or an equivalent control-plane ACL, and must not expose these interfaces directly to the service network.
Currently, `mgmt_tls_config` provides server-side TLS, but it is not equivalent to client identity authentication.

`/readiness` is used only in the default snapshot application scenario, that is, MindCluster instance rescheduling, and is not a general health check interface of the Node Manager. MindCluster queries through this interface whether the instance node has reached the steady-state point. In the user-defined custom application scenario of container snapshots, `/node-manager/status` can be called to query the steady-state point; the interface returning `200 {"status": true}` indicates that the steady-state point has been reached.

### `StartCmdMsg` Request Fields

| Field | Type | Required | Description |
|------|------|------|------|
| `job_name` | string | Yes | Instance task name, which must be consistent with the local node configuration |
| `role` | string | Yes | Instance role, such as `prefill`, `decode`, or `union` |
| `instance_id` | int | Yes | Instance ID assigned by the Controller |
| `endpoints` | array | Yes | Endpoints managed by this node; each element contains `id`, `ip`, and `business_port`, and an SGLang PD endpoint may also contain `bootstrap_port`. Native engine health probing uses `business_port` |
| `master_dp_ip` | string | Yes | IP address of the data-parallel master node |
| `ranktable` | object/null | No | Instance-level ranktable, defaulting to `null` |
| `d2d_peer_ips` | array/null | No | D2D weight transfer peers, encoded by the Controller as `<endpoint_id>:<peer_ip>`, defaulting to `null` |
| `node_rank` | int | No | Node sequence number assigned by the Controller in registration order, defaulting to `0` |

The error codes of this API are as follows:

- Internal parsing exception of the start command: `400 Invalid start command payload`.
- Validation failure of `job_name`, the number of endpoints, or endpoint IPs: `422 Start command validation failed`.
- Failure to start the native engine: `500 Failed to start native engine`.
- Request JSON/Pydantic field parsing exceptions are converted to a generic `500` by the outer exception handling.
- `/readiness` returns `503` when the endpoint is not yet healthy or has not been started after snapshot restoration.

## Communication with External Components

### Controller

| Direction | Controller API | Behavior |
|------|-----------------|------|
| Node Manager → Controller | `POST /controller/register` | Reports the role, model, port, parallel configuration, ranktable, `nnodes`, and snapshot primary node flag |
| Node Manager → Controller | `POST /controller/reregister` | When the Controller restarts and returns `503` to heartbeats, re-registers with the instance and endpoint information |
| Node Manager → Controller | `POST /controller/heartbeat` | Reports the status of each endpoint according to `heartbeat_interval_seconds` |
| Node Manager → Controller | `POST /controller/report_software_fault` | Forwards software fault signals already available in the engine |

Heartbeats use a long-connection client with a single timeout of 5 seconds. When a TCP request fails, the connection is re-established, and retries are performed twice with backoff intervals of 1 second and 2 seconds.

### Native Engine Runtime State

`HeartbeatManager` reads the runtime state of `ProcessSupervisor` once per second. Non-headless endpoints use the native `business_port/health` and follow `infer_tls_config`; headless members do not forcibly construct an HTTP frontend, and only check process liveness and report `WAIT2START`. The Controller marks an instance as available only when all routable endpoints are `NORMAL` and all headless members have reported at least `WAIT2START`.

Status polling has the following protection logic:

- The state enters `STARTING` after the process starts; within `health_check_config.startup_timeout` (default 1800 seconds), the fact that the business port is not yet listening only indicates that the model is still loading.
- A headless process enters `RUNNING` and reports `WAIT2START` when it is alive, and process liveness is not used to pretend that an independent service is ready; when the process exits, `ABNORMAL` is still reported.
- After the native `/health` succeeds, the state enters `READY` and `NORMAL` is reported to the Controller; a single request timeout is retried only for the number of times specified by `health_collector_timeout_retry_attempts`, and the state enters `UNHEALTHY` if the process exits or is still unavailable after retries are exhausted outside the startup window.
- A generation marker is used when updating endpoints to prevent stale probe results from overwriting data newly delivered by the Controller.
- The manually set `PAUSED` status is not overwritten by polling results.

#### Virtual Inference

When virtual inference is enabled (see [Virtual Inference Health Probe](../../user_guide/features/sim_inference.md)), `NativeEngineService` maintains **a single** virtual inference monitor for each instance, initially binds it to DP0 **only for vLLM**, and migrates it to the new DP Master after the DP scale-in is committed (`TargetIdentity` is immutable and contains instance/endpoint/host/port/engine_type; `VirtualInferenceSpec` contains role/model/TLS/threshold/timeout). During pull, NodeManager retains the candidate specs of local endpoints; after receiving finalize with a new master rank, the node where the master resides installs a new monitor and other nodes clean up the old monitor. SGLang does not create a Motor virtual inference monitor/worker: it performs generative health checks in its own `GET /health` (`SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION=true` is forcibly set at startup), and NodeManager/`ProcessSupervisor` only perform the existing `/health` heartbeat. After the target identity matches and the native `/health` `READY` is observed for the first time, `runtime_state(endpoint, instance_id)` idempotently starts `VirtualInferenceWorker` (`motor/node_manager/core/services/native_engine/virtual_inference/`, executed directly by the Node Manager without depending on Engine Server):

- The virtual inference module is responsible for vLLM virtual inference requests (`POST /v1/completions`), AI Cube utilization sampling, and consecutive failure counting; `ProcessSupervisor` is still responsible only for the process and `/health`.
- `runtime_state()` merges the two: when virtual inference marks the state abnormal, `READY` is degraded to `UNHEALTHY`, and `ABNORMAL` is reported as usual in heartbeats; after 5 consecutive times, suicide rescheduling is triggered.
- Virtual inference **only changes the state, and does not kill processes or restart the engine**; `stop()` and a pull without a valid target reliably clean up the monitor and its virtual inference thread and HTTP client. Startup rollback cleans up the monitor only when **the target process of the monitor is actually stopped in this round**; if the target engine is still alive (for example, start reports "different launch spec" or only non-DP0 endpoints are rolled back), the original monitor is retained to continue serving (`stop()` and `pull()` are serialized through `_pull_lock`).

## Native Engine Startup

`NativeEngineService` constructs `LaunchContext`, and the Native Engine Backend loads the configuration corresponding to the role and directly generates the native command:

```text
vllm serve <model> <native vLLM args...>

python3 -m sglang.launch_server <native SGLang args...>
```

Unified context and environment rules:

- In multi-endpoint mode, `ASCEND_RT_VISIBLE_DEVICES` is calculated for each process based on `local_world_size`; when the device number exceeds the end, allocation wraps around cyclically.
- Single-container ports, D2D peers, role, DP rank, and node rank are first written to `LaunchContext` and then mapped to native parameters by the corresponding Backend.
- When `POD_IP` exists in the environment and `VLLM_HOST_IP` is not set, `VLLM_HOST_IP=POD_IP` is set automatically.
- When `MOONCAKE_ASCEND_IPV6_EXPERIMENT=1`, `MC_USE_IPV6=1` is set by default.

The business port of an endpoint must be within `[1024, 65535]`, and the IP must be a valid IPv4 or IPv6 address; otherwise, startup fails.

### vLLM StartPlan and Graph Reuse Startup Acceleration

The `motor_nodemanger_config.vllm_startup_acceleration_config` corresponding to the role is parsed by `NodeManagerConfig` and converted into child process environment variables and engine configuration overrides before
`NativeEngineService.pull()` starts the endpoint:

| Motor Configuration | vLLM Child Process Environment Variable/Engine Override | Behavior |
|------------|---------------------------------|------|
| `enable_startup_plan=true` | `VLLM_ENABLE_STARTUP_PLAN=1` | Allows vLLM to skip memory profiling after a profile hit |
| `enable_startup_plan=false` | `VLLM_ENABLE_STARTUP_PLAN=0` | Explicitly disables the generation and loading of StartPlan |
| `enable_graph_reuse=true` | `VLLM_DISABLE_COMPILE_CACHE=0`, `enforce_eager=false`, `enable_npugraph_ex=true`; P/U uses `cudagraph_mode=FULL`, and D uses `FULL_DECODE_ONLY` | Enables the generation and reuse of the vLLM-Ascend backend compiled graph cache |
| `enable_graph_reuse=false` | `enable_npugraph_ex=false`, `enable_static_kernel=false` | Explicitly disables backend full graph reuse without modifying normal graph capture, `enforce_eager`, `cudagraph_mode`, or AOT behavior |
| All vLLM startups | `VLLM_CACHE_ROOT=<resolved-cache-root>` | StartPlan, AOT, and backend compilation caches share the same vLLM cache root; if it is empty, the existing `VLLM_CACHE_ROOT` and the default vLLM directory (`${XDG_CACHE_HOME}/vllm` or `${HOME}/.cache/vllm`) are used |

When StartPlan is enabled, NodeManager checks
`<cache_root>/startup_plan/startup_plan_*.json` only once in each `pull()`. The directory should be readable and writable, candidate files must not be symbolic links, and they should be non-empty,
no larger than 1 MiB (1048576 bytes), and valid JSON with an object as the root node.

The final generation, fingerprint matching, available memory verification, loading, and fallback of StartPlan are implemented by the matched vLLM/vLLM-Ascend; Motor is responsible only for
configuration injection and the pre-startup DFX check.

### Cross-Node PCP

The Node Manager always passes the `node_rank` and `master_dp_ip` assigned by the Controller to the Native Engine Backend.

For vLLM, cross-node PCP is enabled when the engine configuration contains `nnodes > 1` and `master_port` (compatible with `master-port`) is configured:

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
| `endpoint_config.base_port` | `10000` | Base address of the current control-plane port; business/management ports are still generated as even/odd numbers, and the management port will be removed after the remaining consumers are migrated |
| `basic_config.heartbeat_interval_seconds` | `3` | Interval for heartbeat reporting to the Controller |
| `basic_config.daemon_loop_interval` | `5.0` | Daemon loop check interval (seconds), which controls the suicide flag polling and stdin check frequency, and supports hot update |
| `basic_config.enable_multi_endpoints` | `true` | Whether to create multiple endpoints based on DP and device count |
| `basic_config.nnodes` | `1` | Number of cross-node instances derived from `engine_config.nnodes` |
| `kv_cache_store_config.mode` | `combined` | Deployment mode: `combined` means that the Engine and the KV-store are in the same Pod, and `separated` means that the KV-store is an independent Pod (no Engine is started, and no registration or heartbeat is performed) |
| `mgmt_tls_config.enable_tls` | `false` | Whether to enable TLS for management-plane communication between the Node Manager and the Controller |
| `health_check_config.startup_timeout` | `1800` | Startup window for native engine model loading; an unlistened `/health` within the window is not determined as a failure |
| `health_check_config.health_collector_timeout_retry_attempts` | `3` | Maximum number of attempts after a single native `/health` request timeout; retries occur only on timeout and include the first request |
| `fault_tolerance_config.enable_fault_tolerance` | `false` | Explicitly enables software fault polling; it is automatically enabled when the engine user config detects FT, with no configuration required |
| `fault_tolerance_config.poll_interval_sec` | `1.0` | Interval for polling the engine FT state (seconds) |
| `fault_tolerance_config.poll_timeout_sec` | `5.0` | HTTP timeout of a single poll (seconds) |
| `fault_tolerance_config.max_poll_failures` | `3` | Threshold of consecutive polling failures; after it is reached, the engine is reported as `dead` |
| `snapshot_config.enable_snapshot` | `false` | Whether to enable container snapshots; currently only the vLLM native engine is supported, and configuration validation fails when it is set to `true` for SGLang |
| `snapshot_config.snapshot_metadata_path` | Empty | Container snapshot metadata path; when empty, the default path `/snapshot/snapshot_metadata.json` is used |
| `vllm_startup_acceleration_config.enable_startup_plan` | `false` | Whether to enable StartPlan for the vLLM child process |
| `vllm_startup_acceleration_config.enable_graph_reuse` | `false` | Whether to enable full graph reuse of the vLLM-Ascend backend |
| `vllm_startup_acceleration_config.cache_root` | Empty | Optional absolute cache root directory; when not specified, the environment is inherited or the default vLLM directory is used. In production environments, explicitly configure a persistent path |
| `port_allocator_config.enable` | `true` | Whether to automatically check and adjust ports at startup |

`endpoint_num`, `service_ports`, `device_num`, `parallel_config`, `model_name`, `engine_type`, and `dispatch_capabilities` are mainly derived from the deployment configuration and engine configuration. `dispatch_capabilities` does not accept direct user overrides.

When `pod_ip` is empty, the API service determines the listening protocol family based on `POD_IP`: IPv6 uses `::`, and other cases use `0.0.0.0`. Only a `NodeManagerAPI` that is directly constructed without passing a configuration uses the internal fallback port `8080`; the normal startup process uses the configured port.

### Configuration Hot Update

In non-snapshot mode, after the configuration watcher detects a file change, it processes it in a centralized manner through `Application.on_config_updated()`:

1. `_refresh_check_interval()` — refreshes the daemon loop interval from the configuration.
2. Iterate over all modules and call `update_config()`:
   - `HeartbeatManager` dynamically updates `heartbeat_interval_seconds`.
   - `Daemon` updates the configuration, and starts, stops, or rebuilds the `EngineFtManager` it holds based on changes to `enable_fault_tolerance` and endpoints.
   - `RegisterManager` updates the configuration.
3. Print the updated configuration summary `log_configuration_summary()`.
4. The API listening address, listening port, TLS, and device parameters cached by `Daemon` are not hot-restarted; after modification, the Node Manager must be restarted.

## Service Discovery and Backend Extension

The Node Manager uses a decorator-based service registration mechanism to manage Engine and KV-store services. The related code is located in `motor/node_manager/core/services/`.

### Directory Structure

```text
services/
  __init__.py
  protocols.py        ← DaemonService, PreparableService (interface contracts)
  registry.py         ← _ServiceRegistry (service discovery and registration center)
  native_engine/
    __init__.py
    service.py        ← NativeEngineService (native engine service orchestration)
    startup_acceleration.py ← vLLM startup acceleration environment variables, engine overrides, and StartPlan DFX pre-check
    models.py         ← LaunchContext, LaunchSpec, ProbeSpec, and RuntimeState
    supervisor.py     ← Common process group, health probing, and state management
    factory.py        ← Selects the Backend by engine_type
    config_factory.py ← Lazily loads engine configuration converters
    backends/
      base.py         ← NativeEngineBackend and IConfig interfaces
      vllm/
        backend.py    ← vLLM startup policy
        config.py     ← vLLM parameter conversion and validation
      sglang/
        backend.py    ← SGLang startup policy
        config.py     ← SGLang parameter conversion and validation
  memcache/
    __init__.py
    worker.py         ← memcache worker child process entry (DistributedObjectStore)
    lifecycle.py      ← daemon-side lifecycle management (@register_service)
```

### Service Registration

Each service is registered through the `@register_service` decorator, specifying the name, backend tag, and optional preparation priority:

```python
from motor.node_manager.core.services.registry import register_service

@register_service("engine", backend="engine")
class NativeEngineService:
    ...

@register_service("kv_store", backend="memcache", prepare_priority=10)
class LocalService:
    ...
```

`DaemonService` and `PreparableService` are Protocol interfaces (`motor/node_manager/core/services/protocols.py`) that define the `stop()`, `health_check()`, and `prepare()` contracts.

### Backend Activation

`Daemon.__init__` determines which services to activate based on `kv_cache_store_config`:

- `mode="combined"` (default): `services = "engine,<backend>"`, and the Engine and KV-store are in the same Pod.
- `mode="separated"`: `services = "<backend>"`, only the KV-store, with no Engine started, no registration with the Controller, and no heartbeat reporting.
- No `kv_cache_store_config`: `services = "engine"`, inference only.

`registry.discover(services)` parses the comma-separated service list and imports the `@register_service` of the corresponding module to trigger registration.

### Adding a Native Engine Backend

vLLM and SGLang are engine Backends of `NativeEngineService`, not independent Daemon services. To add a native engine:

1. Implement `backend.py` and `config.py` in `native_engine/backends/<engine>/`.
2. The Backend is responsible only for engine-specific configuration conversion, startup commands, role validation, and probe specifications.
3. Register the Backend in `native_engine/factory.py` and the configuration converter in `native_engine/config_factory.py`.
4. Process startup, process group cleanup, health probing, and the state machine continue to reuse the common `ProcessSupervisor`.

Do not call `subprocess.Popen` directly in the engine Backend, and do not duplicate the stop and probe state machine for each engine.

### Adding a Backend

Adding a KV backend requires only two steps, with no modification to existing code:

```python
# 1. Create a service module and register it
from motor.node_manager.core.services.registry import register_service, SERVICE_KV_STORE

@register_service(SERVICE_KV_STORE, backend="new_backend", prepare_priority=10)
class NewBackendService:
    ...

# 2. Register the module discovery path
registry.add_discovery_path("new_backend", "path.to.new_backend_module")
```

## Software Fault Reporting

`EngineFtManager` is enabled when `fault_tolerance_config.enable_fault_tolerance` is explicitly enabled, or when the normalized
`basic_config.ft_capability.enabled` is `true`. The latter is derived from
`enable-fault-tolerance` / `enable_fault_tolerance` in the engine configuration. After it is enabled, a background thread polls the FT status interface of each endpoint at the
`poll_interval_sec` interval:

```text
GET http://{endpoint.ip}:{endpoint.business_port}/fault_tolerance/status
```

Response format (provided by the vLLM FaultTolerance framework, see vllm-project/vllm#44428):

```json
{
  "schema_version": 1,
  "total_engines": 1,
  "engines": [{"id": 0, "status": "healthy|dead|unhealthy", "fault_info": "..."}]
}
```

The statuses are mapped as follows:

- `healthy`: Records the status and does not report a fault.
- `dead`: Reports `EngineDeadError`.
- `unhealthy`: Reports `EngineUnhealthyError`; when the response carries `fault_info`, it is used as the `exception_type` (such as `RuntimeError`).

The same non-healthy status of the same Engine is marked as reported only after it is successfully sent to the Controller; if the sending fails, subsequent polls can still retry. When the engine fails to be polled for `max_poll_failures` consecutive times (connection refused/timeout), `EngineDeadError` is reported as `dead` (the exception message indicates the number of unreachable polls), and counting restarts after the engine becomes pollable again.

## Container Snapshot

Currently, the Node Manager starts the native vLLM/SGLang directly; the saving and restoration of NPU memory snapshots are completed by the engine in a closed loop,
and only vLLM images that provide the snapshot capability are supported. When `snapshot_config.enable_snapshot=true` is configured and the engine type is
SGLang, configuration validation explicitly fails.

After container snapshots are enabled, the Node Manager performs only framework-side orchestration: it prepares metadata such as `model_save_path` / `model_load_path` /
`data_parallel_master_ip`, refreshes `job_name`, the Pod IP, and the Controller DNS after snapshot restoration,
and detects whether saving/restoration is complete through the engine readiness state and the Host-side `checkpoint` marker. The Node Manager does not call the
engine's `/suspend`, `/device_unlock`, or `/resume`.

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

- The log continuously shows `Registration attempt N failed`: Check the Controller DNS, port, TLS configuration, and network connectivity. The Node Manager keeps retrying registration and does not exit because of this.
- The log shows `Start command validation failed`: Check whether the `job_name`, the number of endpoints, and the endpoint IPs delivered by the Controller are consistent with the Node Manager configuration.
- The log shows `Invalid endpoint parameters`: Check the endpoint IP and business port; the business port must be within `[1024, 65535]`.
- The log shows `Engine process exited immediately`: The native engine exits immediately after `Popen`. Continue to check the native engine logs, configuration path, and startup parameters.
- `/readiness` returns `503`: There is no endpoint, a non-`NORMAL` endpoint exists, the state is `PAUSED`, or the startup command has not been received after snapshot restoration.
- `Consecutive abnormal heartbeat count: 5/5` occurs consecutively: The Node Manager cleans up the native engine process group and exits with `-1` to trigger Pod-level rescheduling.

The related unit tests are located in `tests/node_manager/`; the graceful pause process tests are located in `tests/e2e/test_prestop_e2e.py`.
