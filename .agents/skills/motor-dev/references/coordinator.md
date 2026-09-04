# Coordinator Module — Architecture & Implementation

## Multi-Process Architecture

The Coordinator is the **inference scheduling gateway** — it exposes an OpenAI- and Anthropic-compatible API, schedules requests across engine instances, and runs as a **multi-process system** with ZMQ-based IPC and POSIX shared memory for workload data.

``` text
CoordinatorDaemon (parent process, async main loop)
│
├── MgmtServer (1 process)               — Management HTTP + control plane
│     owns: InstanceManager master (TYPE_MGMT, KV register), CircuitBreakerManager,
│           precision tables, schema-4 WorkloadSharedMemoryOwner, ZMQ ROUTER + PUB
│     start order: 1st | stop order: last
│
├── ObsServer (1 process)                — Observability API
│     owns: MetricsCollector, instance provider via SchedulerConnectionManager (DEALER to Mgmt)
│     start order: 2nd
│
└── InferenceWorkers (N processes)        — OpenAI- & Anthropic-compatible API
      owns: RequestManager, SchedulerClient (ZMQ DEALER for control-plane RPC only),
            WorkloadSharedMemoryReader (Rust CAS allocate/release)
      start order: last | shared socket via SO_REUSEPORT
```

**Why spawn (not fork):** `multiprocessing.get_context("spawn")` starts each process from a clean Python interpreter. This avoids inherited file descriptors, lock states, and CUDA/NPU context corruption that plague `fork`.

**Process lifecycle:**

- Start order: Mgmt (bind ROUTER/PUB + create SHM) → Obs → Inference
- Stop order: Inference → Obs → Mgmt (reverse, via `STOP_ORDER` constant)
- Termination: `terminate()` → `join(timeout=10s)` → `kill()` (three-stage, graceful first)
- Health supervision: `SubprocessSupervisor` monitors child PIDs, auto-restarts dead processes

### vLLM Render Tokenization

With `render_config.enabled=true`, each inference worker prefers the local vLLM Render sidecar before context-budget
adaptation and routing. Normalized token IDs are stored in `RequestInfo`, so scheduling and P/D routing remain
independent of the tokenizer source. Render health does not gate Coordinator startup.

Invalid responses and Render failures fall back to `TokenizerManager`. Transient failures open a five-second circuit.
With Render enabled, context budget reuses Render token IDs and keeps the local tokenizer lazy until fallback; KV
affinity/Conductor still load it eagerly. Failed local loads have a 30-second retry cooldown.

Render-facing models remain topology-neutral: `TokenizedRequest` carries token IDs and Render metadata, while Router
strategies translate their scheduling result into an `EngineLegSpec` containing only phase, generation constraints,
leg context, and optional KV transfer parameters. `VllmProtocolAdapter.build_tokenized_request()` is the single
token-only request builder; Trigger keeps compatibility wrappers that only translate metaserver callback parameters.

Non-streaming vLLM Chat and Completion requests tokenized by Render use `/inference/v1/generate` across HANDOFF,
TRIGGER, and Union/PDHybrid, then Derender into the client response. Completion batches preserve prompt order under
one scheduling allocation. Streaming Chat and single-prompt Completion may use token-only Prefill; batched streaming
Completion, SGLang, and locally tokenized requests keep the existing path.

The deployer adds the sidecar to Coordinator Pods in all supported deployment modes. `render_config.image_name`
selects a CPU image; otherwise the service image and read-only Ascend driver libraries are reused without requesting
NPU resources.

### HA: Master/Standby

- `StandbyManager` controls which node is master via external coordination (e.g., etcd lease)
- `RoleShmHolder` creates `coordinator_standby_role` shared memory (9 bytes: 1B role + 8B heartbeat ns)
- Daemon writes heartbeat every `ROLE_HEARTBEAT_INTERVAL_SEC` (2s); Mgmt process checks staleness (>5s = unhealthy)
- **Only InferenceWorkers are started on master** — Mgmt and Obs run on both master and standby. The daemon unconditionally starts MGMT/OBS (`_start_processes([MGMT, OBS])`); only Inference is gated by role (standby keeps it stopped for instance sync readiness).
- On role change: `on_become_master` starts Inference workers; `on_become_standby` stops them

## IPC: ZMQ Protocol + Shared Memory

### ZMQ (Mgmt control plane ↔ Workers/Obs)

**Transport:** `zmq.asyncio` ROUTER/DEALER over Unix IPC sockets (`ipc://<tmpdir>/scheduler_frontend`). Mgmt binds; Infer/Obs connect. Path names are historical (P3 did not rename IPC).

**Serialization:** `msgspec.msgpack` (not pickle) with zero-copy optimization — payloads >1024 bytes go in separate ZMQ frames to avoid msgpack decoding overhead on the receiver side.

**Request types** (defined in `zmq_protocol.py: SchedulerRequestType`). Data-plane allocate/release is **not** an RPC — Workers CAS on schema-4 SHM.

| Request | Direction | Purpose |
|---------|-----------|---------|
| `GET_AVAILABLE_INSTANCES` | Worker/Obs → Mgmt | Cold start / PUB loss / stale heartbeat: instance list and workload SHM name |
| `CONFIRM_SAMPLE` | Worker → Mgmt | Cross-worker precision-sampling exit gate |
| `RECORD_PRECISION_RESULT` | Worker → Mgmt | Records global consecutive failures + probing state |
| `FINISH_PRECISION_ACTION` | Worker → Mgmt | Clears probing after a probe/alarm cycle |
| `DISMISS_PRECISION_ALARM_STATE` | Worker → Mgmt | External recovery cleared the alarm |
| `CIRCUIT_BREAKER_REPORT` | Worker → Mgmt | Worker reports instance failure/success to the circuit breaker |

There is no `ALLOCATE_ONLY`, `UPDATE_WORKLOAD`, or `REFRESH_INSTANCES` RPC.

**Instance change broadcast:**

- Mgmt publishes multipart `[INSTANCE_CHANGE_TOPIC, version_bytes]` via ZMQ PUB (`ipc://<tmpdir>/scheduler_instance_pub`)
- For ADD/DEL events an extra msgpack frame carries an incremental **delta** of the instance list change; workers patch their cache with it. SET (full-replace) events skip the delta.
- Each worker subscribes to the PUB socket; on notification, invalidates or patches its cached instance list
- Workers also detect `instance_version` bumps in the workload SHM header as a backup signal
- Circuit-breaker state changes are published on `CIRCUIT_BREAKER_TOPIC` (multipart `[topic, msgpack_payload]`)
- Trip writes SHM `flags.BLOCKED` first; `CIRCUIT_BREAKER_TOPIC` PUB follows only after that write succeeds (heartbeat retries both)

### Workload Shared Memory

**Purpose:** Workers need per-endpoint `active_tokens` on every scheduling decision. Allocate/release is per-slot CAS in the Rust `.so`; there is no ZMQ round-trip per request.

**Design:** Mgmt is the only membership writer (seqlock snapshot + heartbeat + `BLOCKED` flags). Infer Workers attach via Rust `shm_open` (not CPython `SharedMemory`) and CAS tokens.

**Layout** (`workload_shm/layout.py`, **SCHEMA_VERSION=4**):

``` text
Offset  Size   Field
0       4B     magic              = 0x574B4C44 ("WKLD")
4       2B     schema_version     — SCHEMA_VERSION=4
6       2B     (padding)
8       8B     sequence           — membership seqlock only (token CAS does not bump)
16      4B     entry_count        — number of valid entries
20      4B     max_entries        — slot capacity (default 10240)
24      8B     instance_version   — bumped on membership snapshot (ADD/DEL/SET)
32      8B     heartbeat_sequence — Mgmt bumps ~1/s (AtomicU64 Relaxed)
40      8B     prefill_sequence   — P membership change counter
48      8B     decode_sequence    — D membership change counter
56      8B     hybrid_sequence    — U membership change counter
64      N×24B  entries            — per-endpoint slots (max 10240)
```

Header is 64B, each entry 24B:

``` text
0   4B  instance_id
4   4B  endpoint_id
8   1B  role
9   1B  flags (bit0=BLOCKED, bit1=VALID)
10  2B  generation (ABA on slot reuse)
12  4B  reserved
16  8B  active_tokens (f64 bits as AtomicU64; 8-aligned for aarch64)
```

`active_tokens` is at **offset 16**, not 12: a 24B stride from a 64B header would leave offset 12 only 4-byte aligned, which faults an 8-byte atomic on aarch64. Scoring must atomic-load tokens every pass (seqlock no longer covers token updates). Schema 3 readers are hard-rejected.

**SHM name:** `mindie_workload_<mgmt_pid>` — includes PID for uniqueness and orphan detection. Created via Rust `create_v4`. `shm_open(O_CREAT|O_EXCL)` failure unlinks and retries **only on `EEXIST`** (orphan); other errno values return SYSCALL without touching a live segment.

**Membership snapshot:** Mgmt keeps **stable slots** for still-live `(iid, eid)` pairs (new pairs take the lowest free slot; removed pairs become INVALID holes). `write_entry_v4` never `store`s caller tokens over a live pair: same slot leaves Worker CAS bits in place; a moved pair atomic-loads the old slot. `_generation` is not pruned when a pair leaves (ABA). `_add_instances` resets `endpoint.workload` to empty, so a new pair's IM seed is 0; non-zero tokens come only from Worker `cas_add`.

**Recovery:** Workers detect stale SHM (heartbeat >5s old) → trigger full `GET_AVAILABLE_INSTANCES` refresh. Attach failure is loud (`NativeWorkloadShmUnavailable`); there is no Python writer fallback. Native **ABI_VERSION=2** (`mindie_wl_abi_version`; Python `MIN_ABI_VERSION=2` refuses older `.so`). Scoring refresh uses one FFI `load_entries` (atomic-load flags/tokens in Rust); `cas_add` / `cas_sub_floor0` take a slot hint from that snapshot (`SLOT_HINT_NONE` scans; a stale hint is `SLOT_INVALID`, no rescan). Both reject non-finite or negative `delta` with `BAD_ARG`. `update_workload` is release-only (`RELEASE_TOKENS`).

### Role Shared Memory (HA)

``` yaml
Name: coordinator_standby_role
Size: 9 bytes
  [0]:     role byte (ROLE_SHM_MASTER=1 / ROLE_SHM_STANDBY=0)
  [1..8]:  heartbeat timestamp (nanoseconds, 8B unsigned)
```

Role byte is 0 (standby/unknown) by default; the daemon writes 1 only after acquiring the master lock (e.g., etcd lease). Initial role is always standby when master/standby is enabled, so Mgmt does not report master before the lock is acquired.

Mgmt process checks this SHM for liveness probe: `getppid() != daemon_pid OR role_shm_heartbeat stale >5s → unhealthy`.

## Scheduling & Routing

### Scheduling Policies (pluggable)

Located in `scheduler/policy/`, each policy implements `BaseSchedulingPolicy`:

| Policy | Algorithm | When to Use |
|--------|-----------|-------------|
| `RoundRobinPolicy` | Simple atomic counter, mod endpoint count | Uniform workload, no KV cache locality |
| `LoadBalancePolicy` | Reads workload SHM, picks endpoint with minimum active tokens | Heterogeneous workloads, varying request lengths |
| `KvCacheAffinityPolicy` | Queries KV Conductor (via `ConductorApiClient`) for prefix match; prefers endpoints with cached blocks | High prefix reuse, PD disaggregation |

**Conductor `/query` wire encoding** (`ConductorApiClient.query_conductor`):
`kv_conductor_config.query_encoding` (default `"msgpack"`) selects the wire
format. MessagePack requests are sent via `SafeHTTPSClient.post_bytes()`
(msgspec-encoded, `Content-Type: application/msgpack`) and responses are
decoded by Content-Type (msgpack via `msgspec`, otherwise JSON — legacy
JSON-only conductors keep working). Set `query_encoding: "json"` for older
kv-conductor binaries.<br>

**Factory registration** (`factory.py`): `SchedulingPolicyFactory` maps policy name → class. New policies register here.

The policy is selected by `SchedulerType` (`config/coordinator.py`): `LOAD_BALANCE` / `ROUND_ROBIN` / `KV_CACHE_AFFINITY` (default). For `scheduler_type=kv_cache_affinity`, a sub-mode is chosen by `kv_affinity.mode`:

- `unified` (default) — single score fusing affinity and live load; pick the minimum
- `load_gated` — keep the N least-loaded endpoints, then pick the longest cached prefix

Tunables live under `CoordinatorConfig.scheduler_config.kv_affinity`: `mode`, `load_weight`, `overlap_credit`, `prefill_load_scale`, `load_gate_topn`, `w_npu`, `w_cpu`, `w_disk`.

### Router Strategies (dynamic, by live topology)

There is **no DeployMode → router class map**. `select_router_class()` (`router/dispatch.py`) decides the router per request from the live instance topology (roles currently online + dispatch compatibility):

``` text
P and D roles both online AND a compatible dispatch pair exists
  (both roles have non-blocked instances)          → UnifiedPDRouter
otherwise, degrade (fallback to hybrid enabled or
  hybrid deployment) with an unblocked U/P instance, or a supported
  vLLM D instance with decode_colocation capability     → PDHybridRouter
no routable topology at all                         → HTTP 503
```

- `UnifiedPDRouter` (strategies/unified_pd.py): routes to P/D pairs sharing a dispatch capability (e.g., common kv_connector or explicit `dispatch_profile`).
- `PDHybridRouter` (strategies/pd_hybrid.py): single instance runs prefill+decode together; also the degradation target when PD separation is unavailable (e.g., P/D instances circuit-broken or advertising no shared dispatch). Candidate priority is U → P → D. Decode is fail-closed to unblocked vLLM instances advertising the connector-derived `decode_colocation` capability; Scheduler filters by engine type and capability before applying the configured load-balancing policy, so mixed D pools stay safe without pinning all traffic to one instance. Its request remains bare (no `kv_transfer_params`/metaserver injection).
- Both subclass `BaseRouter` (strategies/base.py); `_is_pd_hybrid_deploy` / `_is_pd_separation_fallback_to_hybrid_enabled` gates fallback (config `scheduler_config.enable_pd_separation_fallback_to_hybrid`, default true).
- The inference-plane availability gate and management-plane readiness probe apply the same fallback policy before routing. `InstanceReadiness.ONLY_DECODE` is runnable only when `enable_pd_separation_fallback_to_hybrid` is enabled and an unblocked vLLM Decode instance advertises `decode_colocation`; the default `InstanceReadiness.is_run()` call remains fail-closed for decode-only topology.

**vLLM P/D coordination modes** (`UnifiedPDRouter`):

| Instance `dispatch_capabilities` | Mode | Order |
|---|---|---|
| homogeneous `prefill_handoff_decode` | HANDOFF | allocate P → prefill → allocate D → decode |
| homogeneous `concurrent_engine_sync` (vLLM layerwise / `dispatch_profile=trigger`) | TRIGGER | allocate D first → decode with `do_remote_prefill` + `metaserver` → D POSTs Worker `/v1/metaserver` → same Worker allocates P and forwards prefill |
| mixed handoff + trigger in one cluster | — | HTTP 503 |

Mode selection uses allocated-instance `dispatch_capabilities` **and** cluster detection from the Worker-local instance cache (`get_local_instances`). That cache already holds `dispatch_capabilities`; SHM only has workload numbers. `GET_AVAILABLE_INSTANCES` remains a force-refresh RPC and must not run on every request. Instance payloads (GET / PUB) use `_instance_to_dict` (`Instance.model_dump`), which includes `dispatch_capabilities`. There is no ALLOCATE-era `_serialize_instance_minimal` path. If caps were dropped, Worker rebuilds empty caps and falls back to adapter HANDOFF while still allocating Decode first. If the selected mode is TRIGGER but the attempt has no decode resource (handoff-style P-first / D-deferred), fail closed with HTTP 503 — do not return TRIGGER and then `RuntimeError` into retry→500.

SGLang stays on native bootstrap (`CoordinationMode.BOOTSTRAP`); that path is unchanged.

**Trigger metaserver (per Worker, not on the infer port):**

- `RequestInfo` is process-local. Infer workers share `coordinator_api_infer_port` via `SO_REUSEPORT`, so Decode's metaserver callback cannot land on the infer socket.
- `inference_workers_config.worker_metaserver_base_port` default **12000**. Worker `i` listens on `base+i`; set to `0` to disable.
- The deployer passes `inference_workers_config.num_workers` to the Render sidecar as
  `--renderer-num-workers` (default **4**) so frontend preprocessing capacity tracks Coordinator workers.
- Dedicated uvicorn app (`InferenceServer.create_metaserver_app()`) exposes only `POST /v1/metaserver` — no API key, no infer TLS (`lifespan=off`). Default API-key / rate-limit skip sets include `/v1/metaserver`. Decode engine callbacks have no API key; do not require one on this socket. Infer is the primary uvicorn; metaserver is a sidecar. Bind/init/`serve()` failure logs ERROR, clears this process's `worker_metaserver_port`, and leaves the infer port running. Trigger requests then 503 via `_ensure_trigger_metaserver`. Infer exit sets `should_exit` and cancels the sidecar.
- The metaserver listen host prefers `POD_IP` when set, otherwise `api_config.coordinator_api_host` (same fallback as the advertised callback URL). Do not bind loopback: Decode may run on another node. Infer uvicorn still listens on `coordinator_api_host`.
- The callback URL advertises `POD_IP` when available, otherwise `api_config.coordinator_api_host`; IPv6 literals are RFC 3986 bracketed. `0.0.0.0`/`::` remain valid listen hosts at startup (including default `worker_metaserver_base_port=12000`). Trigger rejects them as advertised callback addresses when `POD_IP` is absent (HTTP 503 + error log), because wildcard listen addresses are not routable Decode callback destinations.
- Callback `request_id` is trimmed (`chatcmpl-` / `cmpl-…-0`) then looked up in that Worker's `RequestManager`. Query `?attempt=` must match the bound attempt (404 unknown request, 409 stale attempt).
- Each trigger attempt serializes callbacks with `AttemptContext.trigger_lock`. The active callback is registered as the attempt's Prefill task so disconnect/Decode failure during TTFT cancels it; a retry after Prefill completion returns idempotent success without allocating P again.
- If SHM CAS allocation succeeds but Worker-local attempt workload registration fails (including `CancelledError`), the allocation is rolled back with `cas_sub_floor0` using the same demand delta. `add_req_workload` / `add_req_attempt_workload` store `(instance_id, endpoint_id)` so teardown can reclaim.
- Request teardown (`BaseRouter._manage_request_context`) drains in-flight releases, then `RequestManager.pop_residual_workloads` and `cas_sub_floor0` any leftover ledger commits. `del_req_info` must not be the only owner of residual records (that path only logs a leak).
- Runtime field `CoordinatorConfig.worker_metaserver_port` is per-process (`base+worker_index`) and is in the hot-reload skip-set.

**Request lifecycle:**

1. `prepare_resource(plan)` — scheduling policy scores locally → Worker `cas_add` on schema-4 SHM (stale expected → reload + same Python scorer, not blind retry)
2. `forward_request(plan)` — HTTP POST to engine's infer endpoint (streaming or non-streaming)
3. `release_all(plan)` — Worker `cas_sub_floor0` on the same SHM slot (no UPDATE ZMQ)
4. Teardown — drain pending releases, reclaim residual SHM tokens, then `del_req_info`

**T_sched→P logs** (full INFO, never `_should_log_scheduling_sample`):

- Start: `Scheduling metric stage=request_arrive req_id=… unix_ts=…` in `__create_request_info` immediately after `RequestInfo` construction. `unix_ts` is `ReqState.ARRIVE` (`time.time()`).
- End: `Scheduling metric stage=dispatch_to_p … elapsed_ms=…` in `BaseRouter.forward_request` / `forward_stream_request`, immediately before the first httpx POST to a **P or U** instance. Decode/Encode and later retries of the same request are skipped. `elapsed_ms` is \(T_{\mathrm{sched}\rightarrow P}\). Use `time.time()` (not `perf_counter`) so the line pairs with `kubectl logs --timestamps`.

P99 acceptance greps `stage=dispatch_to_p` and takes `elapsed_ms` on successful-to-P requests; do not use `stage=select_and_allocate`.

### Native Responses Create Route

`POST /v1/responses` forwards the native Responses request body and path to the
engine. Its array input is a Responses input-item union, not a Chat Completions
message array. The HTTP boundary validates message-shaped items, including the
`developer` role, but defers schema validation for other explicitly typed items
to the native engine. For scheduling tokenization, `responses_input.py` maps
`developer` to `system` without changing the request body sent to the engine.
Non-message and other unsupported scheduling shapes fall back instead of
claiming an incorrect KV prefix match.

### Hot-Reload

Hot-reload is driven by a `ConfigWatcher` in the **Mgmt process** (not the daemon's loop): when the config file changes, it calls `CoordinatorConfig.reload()` (re-parse from JSON) and pushes the updated config into the running `ManagementServer`. The reload skip-set is exactly `frozenset({"worker_index"})` — the runtime-only field that must not change mid-flight; everything else re-applies. If no valid config path exists, hot-reload is disabled.

## Key Files

| File | Lines | Role |
|------|-------|------|
| `motor/coordinator/main.py` | | Entry point: loads config, creates CoordinatorDaemon |
| `motor/coordinator/daemon/coordinator_daemon.py` | | Process orchestration, start/stop order, HA role management |
| `motor/coordinator/daemon/subprocess_supervisor.py` | | Health-check loop: monitors child PIDs, auto-restarts dead processes |
| `motor/coordinator/daemon/role_shm_holder.py` | | Creates/owns role shared memory + heartbeat thread for HA |
| `motor/coordinator/process/base.py` | | `BaseProcessManager` ABC: start/stop/health check/termination |
| `motor/coordinator/process/mgmt_manager.py` | | `MgmtProcessManager` + `run_mgmt_server_proc` |
| `motor/coordinator/process/obs_manager.py` | | `ObsProcessManager` + `run_obs_server_proc` |
| `motor/coordinator/process/inference_manager.py` | | `InferenceProcessManager` + shared socket + `run_inference_worker_proc` |
| `motor/coordinator/process/constants.py` | | Process keys, start/stop order |
| `motor/coordinator/scheduler/scheduler.py` | | `Scheduler`: Mgmt precision sampling/alarm state (not SchedulingFacade) |
| `motor/coordinator/scheduler/policy/factory.py` | | `SchedulingPolicyFactory` registry |
| `motor/coordinator/scheduler/runtime/scheduler_server.py` | | `AsyncSchedulerServer`: Mgmt control plane (ROUTER+PUB, CB, precision, SHM owner) |
| `motor/coordinator/scheduler/runtime/scheduler_client.py` | | `AsyncSchedulerClient`: control-plane DEALER + instance cache + SHM CAS |
| `motor/coordinator/scheduler/runtime/zmq_protocol.py` | | Request/response types, msgpack framing, topic constants |
| `motor/coordinator/scheduler/allocate_arbitration.py` | | Shared LB/KVA/RR reselect (R4; no ZMQ) |
| `motor/coordinator/scheduler/runtime/workload_shm/` | | schema-4 layout + Reader/Owner + `native.py` ctypes |
| `motor/coordinator/workload_shm_rs/` | | Rust cdylib: POSIX SHM create/attach, seqlock snapshot, per-slot CAS |
| `motor/coordinator/domain/instance_manager.py` | | Central instance pool (available/unavailable/paused); `snapshot_instances()` for mgmt list |
| `motor/coordinator/domain/request_manager.py` | | Request ID generation, per-request workload records + residual reclaim owners |
| `motor/coordinator/router/dispatch.py` | | `select_router_class` (dynamic router selection from live topology) + `handle_request` + `handle_metaserver_request` |
| `motor/coordinator/router/strategies/` | | `BaseRouter` + `PDHybridRouter` + `UnifiedPDRouter` implementations |
| `motor/coordinator/router/dispatch_session.py` | | Dispatch attempt session/state tracking |
| `motor/coordinator/router/rescheduler/` | | `Rescheduler` (retry plans for failed requests) |
| `motor/coordinator/api_client/` | | `ConductorApiClient` / `ControllerApiClient` / `NativeEngineApiClient` (HTTP clients to kv-conductor, controller, engine) |
| `motor/coordinator/api_server/management_server.py` | | Mgmt: `/liveness`, `/readiness`, `GET /instances`, `/instances/refresh`, `/precision/alarm_cleared` |
| `motor/coordinator/api_server/observability_server.py` | | Obs: `/metrics`, `/health` (`/instance/metrics` deprecated → `GET /metrics?type=instance`) |
| `motor/coordinator/api_server/inference_server.py` | | Infer: `/v1/completions`, `/v1/chat/completions`, `/v1/responses`, `/v1/models`, `/v1/messages` + `/v1/messages/count_tokens` (Anthropic); dedicated metaserver app `POST /v1/metaserver` |
| `motor/coordinator/domain/responses_input.py` | | Text-only scheduling view for native Responses input; maps `developer` to `system` without rewriting the engine request |
| `motor/coordinator/scheduler/runtime/scheduler_connection_manager.py` | | Shared ZMQ DEALER to Mgmt control plane (used by Obs/Infer) |
| `motor/coordinator/domain/circuit_breaker.py` | | Per-instance circuit breaker state (closed/open) |
| `motor/coordinator/domain/scheduling_pin.py` | | Pinned-instance resolution, endpoint selection for an instance |
| `motor/coordinator/domain/workload_calculator.py` | | Workload demand calculation per role |
| `motor/coordinator/domain/scheduling_constraint.py` | | Scheduling constraints (incl. precision-probe targeting) |
| `motor/coordinator/fault_tolerance/` | | Precision sampling / alarm / probe (see Fault Tolerance section) |
| `motor/coordinator/middleware/` | | `SimpleRateLimitMiddleware` (token bucket) etc. |
| `motor/coordinator/tracer/` | | `TracerManager` (OpenTelemetry-style tracing of requests) |
| `motor/config/coordinator.py` | | `CoordinatorConfig` dataclass with all coordinator ports |

## Event Flow: Controller → Mgmt → Workers

``` text
Controller detects instance change
  → POST /instances/refresh (InsEventMsg: ADD/DEL/SET + instance list)
    → Mgmt refresh lock: duplicate IDs / identity conflicts fail closed (400 / 409)
      → apply_refresh: InstanceManager + schema-4 SHM membership snapshot
        (stable slots; do not store over in-flight tokens)
      → PUB socket: INSTANCE_CHANGE_TOPIC (+ delta frame for ADD/DEL)
        → Workers: patch/invalidate caches; CAS uses the new generation/slots

Controller clears a handled precision alarm
  → POST /precision/alarm_cleared
    → Mgmt in-process dismiss_precision_alarm_state (no extra ZMQ hop)
```

`InstanceManager` treats instance IDs as globally unique across all roles and the available, unavailable, and paused
pools. ADD is idempotent only when ID, role, job name, and endpoint structure match; otherwise it is a conflict. SET
rejects duplicate request IDs before building its ID-keyed diff while preserving same-ID structural updates. An
endpoint-based DEL validates the role plus the order-independent physical endpoint multiset (`ip`, business/bootstrap
ports, and `headless`) while ignoring order-derived endpoint IDs and endpoint map keys. An explicit ID-only DEL remains
available for administrative removal.

Standalone `motor.coordinator.register` IDs occupy signed-int32 range `0x40000000..0x7fffffff` and hash the role plus
the sorted complete endpoint group. The same normalization also determines job names and endpoint IDs, so CLI input
order does not change registration identity. This namespace separates them from ordinary low sequential Controller
IDs but does not make CRC32 collision-free. The CLI checks `GET /instances`, and the management server remains the
authoritative collision boundary. Endpoint-based deletion resolves the registered ID from `GET /instances` by role
plus an order-independent network endpoint signature instead of deleting a recomputed ID. ID-only deletion also
looks up the registered instance first and submits its actual role, job name, model name, and engine type.
Duplicate IDs in one request return 400; identity conflicts return 409. Membership write is in-process
(`apply_refresh`); there is no separate Scheduler ACK.

`motor.coordinator.domain` keeps its package-level compatibility exports (for example `InstanceReadiness` and
`RequestManager`) behind module `__getattr__` lazy loading. Domain submodules are imported directly by Coordinator models,
so `domain/__init__.py` must not eagerly import modules that depend on `models.request`; doing so creates a
`models.request → domain package → request_manager/scheduling → models.request` cycle in a fresh process.

### Management API Authentication

`mgmt_api_key_config.enable_api_key` enables a dedicated shared-secret boundary for privileged management APIs.
The secret is read from `api_key_file` and supplied in `X-Motor-Management-Key`. It protects `GET /instances`,
`POST /instances/refresh`, and `POST /precision/alarm_cleared`; `/startup`, `/liveness`, and `/readiness` remain
unauthenticated so Kubernetes probes continue to work. Controller and standalone `motor.coordinator.register` clients
load the same secret from a mounted/local file. This authentication is independent from inference `api_key_config`
and from `mgmt_tls_config`; use TLS as well when management traffic crosses an untrusted network.

## Fault Tolerance: Circuit Breaker & Precision Detection

**Circuit breaker** (`domain/circuit_breaker.py`): per-instance state machine tracking consecutive failures. Each instance is `"closed"` (normal, schedulable) or `"open"` (tripped, blocked from scheduling). Workers report instance outcomes via `CIRCUIT_BREAKER_REPORT`; Mgmt's `CircuitBreakerManager` (inside `AsyncSchedulerServer`) is constructed from `circuit_config` (default: trip after 3 consecutive failures, 30s first timeout, 300s cap; `enable=false` disarms counting). Config is snapshotted at Coordinator startup. Success or auto-recovery resets the failure count. State changes are mirrored onto SHM `flags.BLOCKED` first; `CIRCUIT_BREAKER_TOPIC` PUB is sent only after that write succeeds (heartbeat retries both). Allocate CAS is the final gate for workers that miss the PUB. `select_router_class()` consults the Worker-local breaker cache: a P/D pair is only "compatible" if both roles have non-blocked instances, and 503 is returned when all instances are circuit-broken.

**Precision detection** (`fault_tolerance/precision/` + `fault_tolerance/probe/`): cross-worker sampling (`sample_controller.py`, `streak_result.py`) coordinated with Mgmt via the four precision request types — `CONFIRM_SAMPLE` (cross-worker exit gate), `RECORD_PRECISION_RESULT` (global consecutive failures + probing state), `FINISH_PRECISION_ACTION` (clear probing after probe/alarm), `DISMISS_PRECISION_ALARM_STATE` (external recovery cleared the alarm). Alarm publishing lives in `fault_tolerance/alarm/` (`precision_alarm.py`); probes (`chat_probe.py`, `router_probe.py`) route identically to user traffic through `select_router_class()`.

## Development Rules

- **New scheduling policies**: subclass `BaseSchedulingPolicy`, implement `select_instance()`, register in `factory.py`.
- **New router strategies**: subclass `BaseRouter`, implement `prepare_resource`/`forward_request`/`release_all`, place in `router/strategies/`, and wire the class into `select_router_class()` in `dispatch.py` (there is no static router map).
- **New ZMQ request types**: add to `SchedulerRequestType` enum in `zmq_protocol.py`; add handler method in `scheduler_server.py`; add client method in `scheduler_client.py`.
- **New process types**: subclass `BaseProcessManager`, implement `start()`/`stop()`/`health_check()`; add key to `PROCESS_KEY_*` constants; add to start/stop order in `process/constants.py`.
- **Observability endpoints**: `/metrics` is served by ObsServer (port `coordinator_obs_port`, default 1027), NOT MgmtServer. Controller and ccae_reporter connect to the obs port.
- **HA**: StandbyManager is shm-agnostic — role byte is written by Daemon's `on_role_changed` callback, not by StandbyManager itself.

## Testing

```bash
bash tests/run_tests.sh tests/coordinator/
```

For metrics-specific development, read `references/metrics.md`.
