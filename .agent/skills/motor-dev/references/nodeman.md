# NodeManager Module — Architecture & Implementation

## Architecture: Native Runtime Sidecar

One NodeManager process runs in each inference pod. It receives lifecycle commands from
Controller, starts one native vLLM or SGLang process per endpoint, supervises the complete
process group, probes the native `/health` endpoint, and reports endpoint status through
Controller heartbeats.

``` text
NodeManager (Application)
│
├── NodeManagerAPI (FastAPI thread)
│     POST /node-manager/start   — validate StartCmdMsg and launch native engines
│     POST /node-manager/stop    — stop native process groups
│     POST /node-manager/pause   — mark endpoints PAUSED for PreStop
│     POST /node-manager/resume  — restore PAUSED endpoints
│     GET  /node-manager/status  — report endpoint readiness
│     GET  /readiness            — Kubernetes readiness
│
├── Daemon
│     service registry for engine and optional KV-store services
│     5-second service monitor → health_check()
│
├── NativeEngineService
│     builds LaunchContext, selects Native Engine Backend, delegates lifecycle to ProcessSupervisor
│
├── ProcessSupervisor
│     subprocess.Popen(start_new_session=True)
│     owns RuntimeProcess records, process groups and native health probes
│
├── HeartbeatManager
│     polls ProcessSupervisor every second
│     reports Controller heartbeat at configured interval
│     preserves STARTING/STOPPING/PAUSED semantics and suicide threshold
│
└── FaultReporter
      optional GET {business_port}/fault_tolerance/status polling for engine software faults
```

`motor/node_manager/core/services/native_engine/` is the native engine service boundary. Its
engine backends are stateless converters: they turn a validated `LaunchContext` into an immutable
`LaunchSpec` containing a `CommandSpec` and a `ProbeSpec`. The Coordinator and Controller do not
depend on these node-local runtime states.

## Complete Lifecycle

### Phase 1: Startup and Registration

``` text
main.py
  → NodeManagerConfig.from_json()
  → port allocation / configuration validation
  → NodeManager(Application).run()
  → init_modules(): Daemon, NodeManagerAPI, EngineManager, HeartbeatManager
```

`EngineManager` and `HeartbeatManager` are initialized only when the active service registry
contains an engine. A KV-only pod (`kv_cache_store_config.mode="separated"`) starts the KV service
and NodeManager API without native engine registration or endpoint heartbeats.

`EngineManager` registers the pod with Controller in a background loop. It waits for the API-ready
event, posts `/controller/register`, and retries indefinitely with exponential backoff starting at
2 seconds and capped at 32 seconds. Registration failure does not terminate NodeManager; normal
shutdown and heartbeat-triggered recovery remain controlled by the application lifecycle.

### Phase 2: Start Command and Native Launch

Controller sends `POST /node-manager/start` with `StartCmdMsg`:

``` text
{
  instance_id, job_name, role,
  endpoints: [{id, ip, business_port, mgmt_port, dp_rank, headless, ...}],
  master_dp_ip, node_rank, d2d_peer_ips, ranktable
}
```

The API parses and validates the message, then `Daemon.pull_engine()` runs preparable KV-store
services before asking `NativeEngineService.pull()` to launch each endpoint.

For each endpoint, `NativeEngineService`:

1. Builds an immutable `LaunchContext` with role, rank, host, ports, distributed setup, D2D peers,
   environment, and the endpoint's `headless` flag.
2. Selects `VllmBackend` or `SGLangBackend` from the configured engine type.
3. Rebuilds and validates the role-specific `EndpointConfig` and native engine configuration.
4. Creates a `CommandSpec` and a `ProbeSpec`.
5. Calls `ProcessSupervisor.start()`.

The native commands are:

``` text
vllm serve <native vLLM arguments>
python3 -m sglang.launch_server <native SGLang arguments>
```

No `engine_server` process is inserted by the Native Runtime path.

Device pinning is applied by `NativeEngineService` when `enable_multi_endpoints` is enabled:
`ASCEND_RT_VISIBLE_DEVICES` contains the endpoint's calculated local device range. `POD_IP` is
used as the default `VLLM_HOST_IP` when it is not already set. When the Mooncake IPv6 experiment is
enabled, `MC_USE_IPV6=1` is supplied unless the environment already defines it.

### Native Engine Backend Contract

| Type | Responsibility |
|------|----------------|
| `LaunchContext` | Immutable normalized input for one endpoint launch |
| `CommandSpec` | Immutable argv, environment and optional working directory |
| `ProbeSpec` | Native health path, request timeout, startup timeout, retry limit, TLS and headless mode |
| `LaunchSpec` | Pair of `CommandSpec` and `ProbeSpec` |
| `VllmBackend` | Builds `vllm serve`; Native P/D only accepts the supported HANDOFF profile |
| `SGLangBackend` | Builds `python3 -m sglang.launch_server`; encode role is rejected |

The backend configuration adapters flatten engine-specific JSON into native CLI arguments. They
must call the engine configuration converter and validator before returning a launch specification.
`None` values are omitted from CLI arguments. SGLang receives `enable-metrics=true` because
Coordinator metrics and PreStop drain depend on the native metrics endpoint.

For SGLang P/D roles, the backend sets `disaggregation-mode=prefill|decode`; union uses
`disaggregation-mode=null`. Multi-node configurations validate `nnodes` and require
`master_dp_ip`, which is formatted with the shared IPv4/IPv6 address helper.

### Probe and Runtime State

`ProbeSpec.max_attempts` is the configured
`health_check_config.health_collector_timeout_retry_attempts` value. It counts the first request
and retries only `requests` timeout failures (including exceptions wrapped by `SafeHTTPSClient`).
HTTP status failures, TLS errors, connection errors and other exceptions are not retried.

`ProcessSupervisor.state()` follows this state model:

``` text
start → STARTING
  ├─ headless process alive → RUNNING
  ├─ /health success         → READY
  ├─ process exits           → STOPPED
  └─ startup timeout expires while probe fails → UNHEALTHY
```

During `startup_timeout` a failed probe keeps `STARTING`; this prevents slow model loading from
being reported as a fault. A headless process is only proven alive, not independently ready. The
heartbeat layer maps `RUNNING` to `WAIT2START` and `READY` to `NORMAL`.

`ProcessSupervisor` protects state updates with a lock and performs HTTP probes outside the lock.
Each launch uses `start_new_session=True` on POSIX, caches the process-group ID, and treats the
process group as the lifecycle unit. A stopped or dead record is removed exactly once; dead
launchers trigger cleanup of the cached group so workers are not leaked.

### Phase 3: Heartbeat and Status Reporting

`HeartbeatManager` snapshots endpoint records under `_endpoint_lock`, probes each native endpoint
through `Daemon.get_engine_runtime_state()`, then commits results only if the endpoint generation
has not changed. HTTP probing is therefore not performed while holding the endpoint lock.

Status mapping:

| Native runtime state | Controller endpoint status |
|----------------------|----------------------------|
| `STARTING` / `STOPPING` | Preserve `INITIAL` or the last status |
| `RUNNING` | `WAIT2START` |
| `READY` | `NORMAL` |
| `UNHEALTHY` / `STOPPED` | `ABNORMAL` |
| manual `PAUSED` | Preserve `PAUSED` |

The heartbeat body is `HeartbeatMsg(job_name, ins_id, ip, status)` where `status` is keyed by
endpoint ID. Controller readiness requires routable endpoints to be `NORMAL`; headless members
must at least be alive and reported as `WAIT2START`.

`POST /node-manager/pause` marks all endpoints `PAUSED` and returns native metrics URLs for
non-headless endpoints. `resume` changes only `PAUSED` records back to `NORMAL`.

### Phase 4: Fault Detection and Recovery

`Daemon` calls each service's `health_check()` every 5 seconds. If `NativeEngineService` observes a dead
native launcher, it removes the record, cleans the process group, and requests one Pod-level
recovery through `SIGTERM` when `motor_restart_engine` is enabled. `_recovery_requested` prevents
duplicate recovery signals until a successful new pull resets it.

`HeartbeatManager` counts consecutive successful heartbeat reports containing `ABNORMAL` endpoints.
After five consecutive abnormal reports it sets the suicide flag. The main application tick sees
the flag, stops modules, and exits for platform rescheduling.

Controller HTTP 503 triggers the existing re-registration path. Heartbeat HTTP requests use a
shared long-lived client with bounded retry behavior in `ControllerApiClient`.

### Phase 5: Shutdown

Shutdown stops modules in reverse initialization order. `Daemon.stop()` asks each service to stop;
`NativeEngineService` marks records `STOPPING`, sends SIGTERM to each native process group, and waits up to
the configured grace period for the launcher. It then checks the cached PGID: a group still present after
the launcher exits is force-killed too, so surviving workers cannot retain NPU, port or memory resources.
Records are removed after cleanup so concurrent status reads cannot report a stopped process as a fresh endpoint.

### Snapshot Boundary

The Native Runtime path currently does not support container snapshot suspend/resume or native
engine restore. `snapshot_config.enable_snapshot=true` is rejected during NodeManager config
validation, and `snapshot_metadata_path` is not consumed by Native Runtime.

Legacy snapshot helpers and EngineServer compatibility code remain in the repository for the
transition period, but they are not a supported Native Runtime launch path. Do not add new native
runtime behavior that depends on `engine_server` management endpoints or snapshot metadata until a
separate runtime contract is defined.

## Key Files

| File | Role |
|------|------|
| `motor/node_manager/main.py` | Thin process entrypoint: config, port allocation and `NodeManager.run()` |
| `motor/node_manager/node_manager.py` | Application lifecycle, module initialization, suicide handling and shutdown |
| `motor/node_manager/api_server/node_manager_api.py` | FastAPI start/stop/pause/resume/status/readiness APIs |
| `motor/node_manager/core/daemon.py` | Service discovery, preparable-service ordering and process monitor |
| `motor/node_manager/core/services/native_engine/service.py` | LaunchContext creation, device pinning, native launch and recovery request |
| `motor/node_manager/core/services/native_engine/models.py` | LaunchContext, CommandSpec, ProbeSpec, LaunchSpec and RuntimeState |
| `motor/node_manager/core/services/native_engine/factory.py` | Selects the stateless vLLM/SGLang backend by engine type |
| `motor/node_manager/core/services/native_engine/config_factory.py` | Lazily loads engine-specific CLI configuration adapters |
| `motor/node_manager/core/services/native_engine/backends/` | vLLM/SGLang command construction, configuration conversion and validation |
| `motor/node_manager/core/services/native_engine/supervisor.py` | Process groups, bounded native health probes and runtime state ownership |
| `motor/node_manager/core/services/registry.py` | Service registration and backend discovery |
| `motor/node_manager/core/services/memcache/` | Optional KV-store service implementation |
| `motor/node_manager/core/heartbeat_manager.py` | Native state polling, status mapping, heartbeat and suicide threshold |
| `motor/node_manager/core/engine_manager.py` | Controller registration, StartCmdMsg validation, ranktable and legacy transition hooks |
| `motor/node_manager/api_client/controller_api_client.py` | Controller register, reregister and heartbeat HTTP client |
| `motor/node_manager/core/fault_reporter.py` | Optional native engine software-fault polling |
| `motor/config/node_manager.py` | NodeManager schema, endpoint derivation, ports and snapshot validation |

## Port and Address Rules

- Inference service ports and management ports are allocated from the NodeManager port allocator;
  native runtime health probes use the endpoint's `business_port`.
- Ports are validated before launch. The shared address helpers bracket IPv6 literals when building
  URLs and distributed addresses.
- `master_dp_ip`, D2D peer addresses and endpoint hosts are passed through the common address
  formatting utilities; do not concatenate IPv6 host and port strings manually.
- In multi-endpoint mode, device visibility is calculated from `local_world_size`; single-container
  offsets are applied after local device selection.

## Development Rules

- Keep native process lifecycle behind `Daemon` → `NativeEngineService` → `ProcessSupervisor`; do not call
  `subprocess.Popen` from API handlers or native engine backends.
- Native engine backends remain stateless and return immutable launch specifications.
- Add a regression test for every new state transition, CLI mapping, probe policy or cleanup path.
- Keep health probing outside endpoint locks; commit results under lock with generation checks.
- Health retries must remain bounded and timeout-only. Do not retry explicit HTTP rejection, TLS or
  configuration errors.
- Preserve the distinction between process liveness (`RUNNING`/`WAIT2START`) and service readiness
  (`READY`/`NORMAL`).
- New engine backends should add a backend/config package and tests without coupling `Daemon` to the
  engine-specific CLI.
- Native Runtime snapshot support is intentionally disabled until its lifecycle contract is defined.

## Testing

```bash
# Focused backend and supervisor tests
bash tests/run_tests.sh --serial tests/node_manager/core/services/native_engine/

# NodeManager module tests
bash tests/run_tests.sh tests/node_manager/
```

Important test areas:

- `tests/node_manager/core/services/native_engine/test_supervisor.py`: process-group ownership, state races, bounded
  timeout retries, headless liveness and cleanup.
- `tests/node_manager/core/services/native_engine/test_backends.py`: vLLM/SGLang command construction and ProbeSpec
  mapping.
- `tests/node_manager/core/test_heartbeat_manager.py`: generation-safe status mapping and heartbeat
  behavior.
- `tests/node_manager/core/services/native_engine/test_service.py`: recovery latch and process-death handling.
