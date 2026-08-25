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
│     POST /node-manager/start   — spawn engines with StartCmdMsg
│     POST /node-manager/stop    — kill engines + delayed SIGTERM self (exit -1 →
│                                  k8s pod restart) — the "suicide" instruction
│     POST /node-manager/engine-restart — relaunch engines in place, no pod restart
│                                  body {"action": "restart"|"abort", "instance_id"?}
│                                  (thin route: restart delegates the whole
│                                   relaunch to Daemon.restart_engine, maps
│                                   its errors to 400/409/500;
│                                   abort   = unfreeze suicide → heartbeat fallback)
│     POST /node-manager/pause   — pause endpoints (upgrade flow)
│     POST /node-manager/resume  — resume endpoints
│     GET  /node-manager/status  — current engine states (relaxed=true for relaunch poll)
│     GET  /readiness            — k8s readiness probe
│     (TLS via mgmt_tls_config)
│
├── RegisterManager (ThreadSafeSingleton)
│     Registration protocol: POST /controller/register (with retry)
│     Ranktable file writing: saves ranktable JSON for engine RPC
│     Persists master_dp_ip/role/endpoints for engine relaunch (get_restart_params)
│
├── Daemon (ThreadSafeSingleton)
│     Service registry orchestration (core/services/registry.py):
│       "engine" services  → NativeEngineService (vllm serve / SGLang, device pinning)
│       "kv-store" backends → memcache/lifecycle services
│     Device pinning via ASCEND_RT_VISIBLE_DEVICES env var
│     SIGKILL on stop (no graceful shutdown — engines are stateless)
│     5s process monitor thread → svc.health_check() for every service
│       (PID death → freeze suicide + report ENGINE_DEAD to Controller)
│     3s suicide-arbitration thread (single pod-rescheduling decision point):
│       5 consecutive ABNORMAL observations after the grace period → suicide flag;
│       freeze window (deadline-based) suspends counting; only endpoints that
│       were NORMAL before are reported as dead (cold-start guard)
│     restart_engine(instance_id): owns the whole relaunch — serializes
│       (in-progress flag, EngineRestartInProgressError on overlap), resolves
│       launch params from RegisterManager (EngineRestartParamError on
│       missing/mismatch), freezes suicide (unfreezes on failure), pauses the
│       FaultReporter for the relaunch window and resumes after (fresh engines
│       restart their startup grace)
│     FaultReporter (owned here, third monitoring source): started in
│       pull_engine, stopped in stop(), (re)configured in update_config
│
├── NativeEngineService
│     builds LaunchContext, selects Native Engine Backend, delegates lifecycle to ProcessSupervisor
│     owns a single per-instance VirtualInferenceWorker bound to the eligible vLLM DP0 target
│     (worker started after the target's first /health READY)
│
├── ProcessSupervisor
│     subprocess.Popen(start_new_session=True)
│     owns RuntimeProcess records, process groups and native health probes
│
├── VirtualInferenceWorker (single, per-instance vLLM DP0 target)
│     POST /v1/completions virtual requests + npu-smi AI Cube usage sampling + failure counting
│     reaching max_failure_count only marks the endpoint abnormal — never kills the process
│
├── HeartbeatManager (ThreadSafeSingleton)
│     Two daemon threads:
│       _engine_status_thread     — poll ProcessSupervisor / native /health every 1s
│       _heartbeat_report_thread  — POST /controller/heartbeat every interval
│     Endpoint-state facts only (status polling + heartbeat reporting) —
│       arbitration lives in the Daemon, not here
│     No engine-readiness logic: status probing waits for the Daemon's
│       engine-ready handoff (native /health on business_port), injected at start()
│
└── FaultReporter
      HTTP poll GET {business_port}/fault_tolerance/status per engine
      (vLLM FT REST API) → report_software_fault to Controller
      pause()/resume(): suspended by the Daemon across an engine relaunch —
      resume() clears poll state so re-pulled engines get a fresh startup grace
```

`motor/node_manager/core/services/native_engine/` is the native engine service boundary. Its
engine backends are stateless converters: they turn a validated `LaunchContext` into an immutable
`LaunchSpec` containing a `CommandSpec` and a `ProbeSpec`. The Coordinator and Controller do not
depend on these node-local runtime states.

### Virtual Inference (虚推)

Virtual inference probes engine liveness beyond `/health` (which can pass while the engine is unable to infer, e.g. NPU hang or driver fault). It is implemented under `motor/node_manager/core/services/native_engine/virtual_inference/` and runs inside the NodeManager process — it never depends on `motor.engine_server`.

**Split by engine type:**

| Engine | Motor role | Health path |
|--------|------------|-------------|
| vLLM | DP0 instance-level `VirtualInferenceWorker` (completions + AI Cube) | Motor probes `POST /v1/completions`; `ProcessSupervisor` still uses `GET /health` |
| SGLang | **Never** creates a Motor monitor/worker/requester | SGLang runs its own generative check inside `GET /health`; Motor only heartbeats `/health` |

| File | Role |
|------|------|
| `capabilities.py` | `should_enable_vllm_virtual_inference(...)` — vLLM-only feature gate; `is_error_ascend_global_log_level(raw)` — ERROR-level gate on final engine env (no IntFlag / Protocol / policy registry) |
| `spec.py` | Immutable `TargetIdentity` (instance id / endpoint id / host / port / engine type) + immutable `VirtualInferenceSpec` (identity + role, model, dispatch profile, TLS, thresholds, timeout) — the only inputs the worker consumes |
| `requesters.py` | `VllmCompletionsRequester` (POST /v1/completions, PD-aware) plus request-id helpers; no requester Protocol / factory / SGLang requester |
| `worker.py` | `VirtualInferenceWorker`: constructs `VllmCompletionsRequester(spec)` directly; warmup + periodic loop, AI Cube sampling thread, consecutive failure counting, thread/HTTP-client cleanup |

vLLM enablement gates (all must hold, decided at `pull()` time before building desired spec):

1. `engine_type == "vllm"` (non-vLLM → desired spec `None`, cleared via normal `reconcile(None)`)
2. `should_enable_vllm_virtual_inference(...)`: `enable_virtual_inference == true`, `dp_rank == 0`, `headless == false`, `0 < npu_usage_threshold <= 100` (ineligible → silent `None`, **no** log-level warning)
3. Only for otherwise-eligible targets: final engine launch env `launch_spec.command.env["ASCEND_GLOBAL_LOG_LEVEL"]` is ERROR: unset / `None` / `""` / whitespace → treat as ERROR (allow); `str(...).strip() == "3"` allow; any other explicit value → desired `None`, warning, engine process kept, old monitor cleared via `reconcile(None)`. Uses final backend-prepared env, **not** NodeManager `os.environ` and **not** deploy rewriting `user_config`. SGLang generative `/health` is unaffected.

vLLM virtual request:

| Engine | Requester | Request | PD body |
|--------|-----------|---------|---------|
| vLLM | `VllmCompletionsRequester` | POST `/v1/completions` `{model, prompt:"1", max_tokens:1}`, `X-Request-Id: {ts}_virtual` | decode+trigger only (`kv_transfer_params.do_virtual`) |

SGLang launch env switch (`SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION`): `SGLangBackend.prepare()` always pins the variable to `"true"` in the command env (overriding any container-level value, including `"false"`). This pin is **independent of** `enable_virtual_inference`: setting that flag to `false` only disables Motor's vLLM proactive virtual inference and must never turn off SGLang's native generative `/health`. Motor never calls `/health_generate` and never builds an SGLang virtual worker; NodeManager / `ProcessSupervisor` only request `GET /health`, using the existing `health_collector_timeout` / retries / `startup_timeout` (not `virtual_inference_timeout`).

Periodic vLLM request client timeout comes from `health_check_config.virtual_inference_timeout` (default 5.0, must be positive, **vLLM Motor virtual inference only**; retained for config compatibility) and flows through `VirtualInferenceSpec.request_timeout_seconds` into `httpx.Timeout` per loop iteration. The first warmup request after READY keeps a fixed 180s timeout (`_VIRTUAL_WARMUP_TIMEOUT_SEC`), independent from `virtual_inference_timeout`. SGLang ignores `virtual_inference_timeout` and uses `health_collector_timeout` for `/health`.

The backend loads and validates the deploy config in `prepare()` and returns it on `LaunchSpec.deploy_config`; `NativeEngineService._build_virtual_spec()` reads `health_check_config` from it — no second config load.

Lifecycle:

- The service holds **at most one** monitor (`_virtual_monitor: VirtualInferenceWorker | None`), bound to the eligible vLLM DP0 target. Identity/spec are read from the same `worker.spec` snapshot; CAS install/detach compares worker object identity with `is`.
- On pull, an immutable `VirtualInferenceSpec` is built for the vLLM DP0 endpoint only (non-vLLM → `None`); the monitor is reconciled (`_reconcile_virtual_monitor`) after the whole pull succeeds — never half-installed. Candidate CAS failure must stop the candidate and must not overwrite or stop the newer current.
- Reconciliation is idempotent on the full spec (identity + role/model/profile/TLS/timeout/threshold): an identical re-pull keeps the worker; an identity or config change replaces it (stop old, install new). Installation uses compare-and-swap so a stale reconcile cannot overwrite a newer monitor.
- The virtual loop is **not** started at pull time. `NativeEngineService.runtime_state(endpoint, instance_id)` matches the probe against the monitor's `TargetIdentity` (from `worker.spec`); on the first `READY` observation it calls `worker.start()` (idempotent — a `_started` latch prevents duplicate threads), then re-checks identity under the lock so a replaced monitor cannot leak stale abnormal state.
- `worker.start()` verifies once (cached) that the HDK supports `npu-smi info watch -s u` (AI Cube Usage); unsupported HDK disables the worker.
- The loop: 180s-warmup first virtual request (fixed, legacy behavior), then 5s interval (20s when AI Cube peak >= 80%). Periodic requests use `health_check_config.virtual_inference_timeout` (default 5s, vLLM only) — the warmup timeout is independent from it. Consecutive failures are counted only when AI Cube usage is available and below `npu_usage_threshold`; reaching `max_failure_count` sets the abnormal flag. Unexpected monitor-loop exceptions are logged and backed off only — they do **not** mark the engine abnormal.
- `runtime_state()` merges the worker: `READY + abnormal → UNHEALTHY`. The heartbeat layer maps `UNHEALTHY` to `ABNORMAL` as usual — virtual inference never kills or restarts the engine process (no `SIGTERM`/recovery from the worker).
- `stop()` and a pull with no eligible target reliably detach and stop the monitor (join threads, close the HTTP client on its event loop, clear the registration). A failed pull only detaches the monitor when its target endpoint was actually rolled back (its process stopped) this pull; if the target engine is still alive (e.g. start raised "different launch spec", or only a non-DP0 endpoint was rolled back) the monitor is kept. `stop()` is serialized with `pull()` via `_pull_lock`.

## Complete Lifecycle

### Phase 1: Startup and Registration

``` text

1. main.py is a thin wrapper (41 lines): load config → port setup → NodeManager(config).run()

   The real orchestration lives in NodeManager(Application) (motor/node_manager/node_manager.py)
   and the Application base class (motor/common/app/application.py): signal handlers,
   daemon tick loop, module init/start, graceful shutdown. There is no separate
   suicide_procedure() function — suicide is driven by the tick loop (see Phase 4).

2. NodeManager.init_modules() — registration order:
   Daemon → NodeManagerAPI → [RegisterManager → HeartbeatManager]

   RegisterManager/HeartbeatManager are registered ONLY when daemon.has_engine
   is True. A KV-only pod (kv_cache_store_config mode="separated") registers
   just Daemon + NodeManagerAPI.

3. RegisterManager._register() [background thread]

   Loop:
     wait_until_api_ready(timeout=30.0)      # NodeManagerAPI must be serving
     POST /controller/register (with instance metadata, capabilities)
     → 200: registration accepted, break
     → non-200: retry with exponential backoff (2, 4, 8, 16, 32s, max 5 retries)
     → max retries exceeded: os.kill(SIGTERM) — pod restart by k8s

4. Main loop (Application.run()) blocks until stop_event
   - SIGTERM/SIGINT → cleanup and exit
   - stdin EOF → cleanup and exit
   - each daemon tick also checks the HeartbeatManager suicide flag

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
  endpoints: [{id, ip, business_port, bootstrap_port, dp_rank, headless, ...}],
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

The backend configuration adapters initialize and flatten engine-specific JSON into native CLI
arguments. Final argument validation is delegated to the native engine process; NodeManager does
not import engine parser implementations. `None` values are omitted from CLI arguments. SGLang
receives `enable-metrics=true` because Coordinator metrics and PreStop drain depend on the native
metrics endpoint.

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
  ├─ /snapshot/health 200    → READY
  ├─ /snapshot/health 202    → RUNNING
  ├─ process exits           → STOPPED
  └─ startup timeout expires while probe fails → UNHEALTHY
```

During `startup_timeout` a failed probe keeps `STARTING`; this prevents slow model loading from
being reported as a fault. A headless process is only proven alive, not independently ready. The
heartbeat layer maps `RUNNING` to `WAIT2START` and `READY` to `NORMAL`.

Related Daemon / Heartbeat arbitration context:

``` text
Grace period: starts when endpoints are set and ends after the first endpoint
  reaches NORMAL (the per-endpoint NORMAL-history guard continues to protect
  other endpoints that are still loading)

Daemon suicide arbitration (loop paced by heartbeat_interval_seconds,
threshold 5 ≈ 15s of continuous ABNORMAL with the default 3s interval):
  - endpoint generation change → counter reset (endpoints were (re)set)
  - freeze window (engine relaunch / successful death report) → counting
    suspended; deadline-based freeze, so a lost abort message still expires
  - has_abnormal_endpoints → counter += 1; else reset + clear report dedup
  - counter >= 5 → _should_suicide = True
    → NodeManager._on_daemon_tick() detects the flag → stop_event.set()
    → main loop exits → shutdown() gracefully stops all modules
      (Daemon.stop SIGKILLs engine subprocesses — no os._exit(-1))
    → run() returns exit code -1
    → Kubernetes restarts the pod → fresh registration

Engine death detection (two signal sources, both report to Controller via
report_software_fault and freeze suicide ONLY on a successful report —
dedup by PID / endpoint id):
  1. process monitor (5s): engine PID death (EngineDeadError)
  2. arbitration: ABNORMAL endpoint that was NORMAL before — covers
     native engine process alive but /health failed (e.g. vLLM EngineCore crash)
  Cold-start guard: an endpoint never NORMAL yet is still loading — no report.
  Report failure (Controller unreachable) → NO freeze: the arbitration keeps
  counting and the container-restart fallback stays live (freezing on a
  failed report would leave the pod permanently dead-ended).

Controller-dispatched suicide — POST /node-manager/stop:
  Daemon.stop() (kill engines) + delayed SIGTERM self → graceful shutdown →
  exit -1 → k8s restarts the pod. Used for instance teardown and for
  partial-loss coordination (a surviving NodeManager exits so the whole
  cross-machine instance restarts together).

HTTP 503 from Controller:
  → Controller restarted → HeartbeatManager._reregister()
  → POST /controller/reregister (ReregisterMsg) — single attempt, no backoff;
    a later heartbeat exception triggers the next retry
```

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

`Daemon` calls each service's `health_check()` every 5 seconds. `NativeEngineService` surfaces dead
PIDs (`(pid, endpoint_id)`); the Daemon decides the recovery path. Dead engines are reported to the
Controller via the software-fault channel; when `enable_engine_relaunch` is set (NodeManager
`fault_tolerance_config`, mirroring the Controller-side switch of the same name) the suicide
arbitration is frozen for `engine_restart_wait_timeout_sec` so the in-place relaunch can complete.
Without the switch the freeze is skipped and the pod self-terminates (k8s restarts the container)
once the abnormal-report threshold is reached.

`Daemon` counts consecutive `ABNORMAL` observations from `HeartbeatManager`. After five observations
it sets the suicide flag. The main application tick sees the flag, stops modules, and exits for
platform rescheduling.

Controller HTTP 503 triggers the existing re-registration path. Heartbeat HTTP requests use a
shared long-lived client with bounded retry behavior in `ControllerApiClient`.

### Phase 5: Shutdown

``` text
NodeManager.shutdown() — stop modules in reverse registration order:
  HeartbeatManager.stop()  → join threads
  RegisterManager.stop()     → stop registration thread
  NodeManagerAPI.stop()    → shutdown FastAPI
  Daemon.stop()            → stop 5s monitor thread, then stop each service
                             in reverse registration order (SIGKILL engine PIDs)
  (there is no NodeManagerConfig.stop() — the config object has no lifecycle)
```

### Container Snapshot

When `snapshot_config.enable_snapshot=true`, Native Runtime supports snapshot only for vLLM. The
resolved metadata path is forwarded in `vllm serve --snapshot-config` together with
`enable_auto_checkpoint=true`; routable endpoints use `/snapshot/health` instead of `/health` for
NodeManager readiness probes.

The vLLM snapshot-aware contract is:

| Response | Runtime state | Meaning |
|----------|---------------|---------|
| `200` | `READY` | engine health and the current suspend/resume phase are complete |
| `202` | `RUNNING` | engine is healthy but suspend/resume is still in progress |
| request failure / non-2xx | `STARTING` or `UNHEALTHY` | still within `startup_timeout` → keep `STARTING`; otherwise `UNHEALTHY` |

Suspend / resume lifecycle stays inside vLLM. NodeManager still owns the Motor-side glue:

1. Forward snapshot metadata into the native `vllm serve` CLI and select `/snapshot/health` probes.
2. Prepare / refresh snapshot metadata and framework state around restore (`job_name`, `pod_ip`,
   Controller DNS, model save/load paths, `data_parallel_master_ip`).
3. After host-side restore `start_cmd`, rebind local engine runtime endpoint ids before heartbeat
   probes use the new id block.
4. Gate cold-start heartbeat until checkpoint is done, and gate restore readiness until
   `start_cmd` has completed (`started_after_restore`).
5. Start suicide arbitration only after `HeartbeatManager` is bound.

The vLLM sentinel continues to use the engine-only `/health` route internally. During cold start,
`/snapshot/health` READY means suspend has completed; the `checkpoint` field in snapshot metadata
barriers heartbeat until the container checkpoint is done. During container snapshot restore,
`/snapshot/health` READY means resume has completed and the engine is ready.

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
| `motor/node_manager/core/services/native_engine/virtual_inference/` | vLLM-only DP0 virtual inference: enablement gate, immutable spec and worker (completions + AI Cube sampling + failure counting); SGLang uses generative GET /health instead |
| `motor/common/utils/ai_cube.py` | `npu-smi info watch -s u` AI Cube usage sampling (shared, no engine_server dependency) |
| `motor/node_manager/core/services/registry.py` | Service registration and backend discovery |
| `motor/node_manager/core/services/memcache/` | Optional KV-store service implementation |
| `motor/node_manager/core/heartbeat_manager.py` | Native state polling, status mapping, heartbeat and suicide threshold |
| `motor/node_manager/core/register_manager.py` | Controller registration, StartCmdMsg validation, ranktable and snapshot metadata/restore helpers |
| `motor/node_manager/api_client/controller_api_client.py` | Controller register, reregister and heartbeat HTTP client |
| `motor/node_manager/core/fault_reporter.py` | Optional native engine software-fault polling |
| `motor/config/node_manager.py` | NodeManager schema, endpoint derivation, ports and vLLM-only snapshot validation |

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
- Keep the snapshot lifecycle (suspend/resume) inside vLLM. NodeManager forwards metadata, consumes
  the `/snapshot/health` readiness contract, prepares restore metadata, rebinds endpoint ids after
  restore, and gates heartbeat/readiness around checkpoint and restore `start_cmd`.

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
  mapping; SGLang always pins `SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION=true` and keeps probe `GET /health`.
- `tests/node_manager/core/test_heartbeat_manager.py`: generation-safe status mapping and heartbeat
  behavior.
- `tests/node_manager/core/services/native_engine/test_service.py`: recovery latch, process-death handling, vLLM virtual worker registration gates (SGLang never creates a Motor monitor), runtime-state merge, CAS install, pull/stop serialization and rollback identity rules.
- `tests/node_manager/core/services/native_engine/virtual_inference/test_worker.py`: warmup, loop intervals, failure threshold, abnormal flag, idempotent start and thread/client cleanup (vLLM requester only).
- `tests/node_manager/core/services/native_engine/virtual_inference/test_capabilities.py`: `should_enable_vllm_virtual_inference` gates (config / DP0 / headless / threshold).
- `tests/common/utils/test_ai_cube.py`: npu-smi watch support detection and usage parsing.
