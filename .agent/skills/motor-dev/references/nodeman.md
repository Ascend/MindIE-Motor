# NodeManager Module — Architecture & Implementation

## Architecture: Sidecar Daemon

One NodeManager process per NPU pod/container. Its job is to manage the lifecycle of `engine_server` subprocesses on the local machine and report health to the Controller.

``` text
NodeManager Process (sidecar, one per pod) — NodeManager(Application) main loop
│
├── NodeManagerAPI (FastAPI thread)
│     POST /node-manager/start   — spawn engines with StartCmdMsg
│     POST /node-manager/stop    — kill engines
│     POST /node-manager/pause   — pause endpoints (snapshot/upgrade flow)
│     POST /node-manager/resume  — resume endpoints
│     GET  /node-manager/status  — current engine states
│     GET  /readiness            — k8s readiness probe
│     (TLS via mgmt_tls_config)
│
├── EngineManager (ThreadSafeSingleton)
│     Registration protocol: POST /controller/register (with retry)
│     Ranktable file writing: saves ranktable JSON for engine RPC
│
├── Daemon (ThreadSafeSingleton)
│     Service registry orchestration (core/services/registry.py):
│       "engine" services  → EngineService (subprocess.Popen, device pinning)
│       "kv-store" backends → memcache/lifecycle services
│     Device pinning via ASCEND_RT_VISIBLE_DEVICES env var
│     SIGKILL on stop (no graceful shutdown — engines are stateless)
│     5s process monitor thread → svc.health_check() for every service
│
├── HeartbeatManager (ThreadSafeSingleton)
│     Two daemon threads:
│       _engine_server_status_thread — poll each engine GET /status every interval
│       _heartbeat_report_thread     — POST /controller/heartbeat every interval
│     Fault detection: 120s grace period + 5 consecutive abnormal reports → suicide
│
└── FaultReporter (ThreadSafeSingleton)
      ZMQ SUB sockets on engine PUB ports (topic: vllm_fault)
      → report_software_fault to Controller
```

All core components use `ThreadSafeSingleton` — `__new__` + `threading.Lock` ensures one instance per process with thread-safe lazy initialization.

## Complete Lifecycle

### Phase 1: Startup & Registration

``` text

1. main.py is a thin wrapper (41 lines): load config → port setup → NodeManager(config).run()

   The real orchestration lives in NodeManager(Application) (motor/node_manager/node_manager.py)
   and the Application base class (motor/common/app/application.py): signal handlers,
   daemon tick loop, module init/start, graceful shutdown. There is no separate
   suicide_procedure() function — suicide is driven by the tick loop (see Phase 4).

2. NodeManager.init_modules() — registration order:
   Daemon → NodeManagerAPI → [EngineManager → HeartbeatManager]

   EngineManager/HeartbeatManager are registered ONLY when daemon.has_engine
   is True. A KV-only pod (kv_cache_store_config mode="separated") registers
   just Daemon + NodeManagerAPI.

3. EngineManager._register() [background thread]

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

### Phase 2: Receiving Start Command

``` text
Controller → POST /node-manager/start (StartCmdMsg)
  {
    instance_id: int,
    endpoints: [Endpoint],     # host, business_port, mgmt_port, dp_rank, role
    ranktable: {...},          # for distributed RPC
    job_name: str,
    role: str,                 # prefill / decode / union
    master_dp_ip: str,
    d2d_peer_ips: [str],
    node_rank: int,
  }

1. EngineManager.parse_start_cmd(msg):
   - Validates all fields (endpoint IPs, ports in range)
   - Stores instance_id, endpoints, role, master_dp_ip, d2d_peer_ips, node_rank
   - Writes ranktable JSON to file (for engine RPC initialization)

2. Daemon.pull_engine(pd_role_info, endpoints, instance_id, ...):
   - Phase 1: run PreparableService.prepare() on KV-store services
   - Phase 2: EngineService.pull() — for each endpoint:

     - Device pinning (_calc_visible_device_ids, core/services/engine.py):
         start_device = i * local_world_size % device_num
         visible = local_world_size consecutive devices from start_device
                   (wraps around when past the end)
       ASCEND_RT_VISIBLE_DEVICES is set ONLY when enable_multi_endpoints
       is enabled; the logic moved from Daemon into EngineService.

     - subprocess.Popen (shell=False, with env):
       engine_server --dp-rank <id> --instance-id <id> --role <role>
         --host <ip> --port <business_port> --mgmt-port <mgmt_port>
         --master-dp-ip <ip> --node-rank <rank> --config-path <path>
       optional flags appended when configured:
         --snapshot-metadata / --kv-port / --dp-rpc-port /
         --lookup-rpc-port / --d2d-peer-ips
     - Track PID in self.engine_pids list

3. HeartbeatManager.start() — takes NO arguments:
   - Starts _engine_server_status_thread
   - Starts _heartbeat_report_thread
   - instance/role/endpoint fields are set afterwards via
     update_endpoint(StartCmdMsg)

```

### Phase 3: Steady State

``` text
Every heartbeat_interval — _engine_server_status_thread:
  For each endpoint:
    GET http://{ip}:{mgmt_port}/status
    → parse EndpointStatus (initial / normal / abnormal / paused / wait2start)
    → update self._endpoints[i].status

Every heartbeat_interval (default 3s) — _heartbeat_report_thread:
  POST /controller/heartbeat
  body: HeartbeatMsg {
    job_name, ins_id, ip,
    status: {endpoint_id: endpoint_status}   # dict keyed by endpoint_id
  }
```

### Phase 4: Fault Detection & Suicide

``` text
Grace period: 120s hardcoded from engine start
  (engines need time to load models — don't kill them during warmup)

After grace period:
  If any endpoint.status == ABNORMAL:
    _consecutive_abnormal_count += 1
  Else:
    _consecutive_abnormal_count = 0

  If _consecutive_abnormal_count >= 5 (5 consecutive reports):
    _should_suicide = True
    → NodeManager._on_daemon_tick() detects the flag → stop_event.set()
    → main loop exits → shutdown() gracefully stops all modules
      (Daemon.stop SIGKILLs engine subprocesses — no os._exit(-1))
    → run() returns exit code -1
    → Kubernetes restarts the pod → fresh registration

HTTP 503 from Controller:
  → Controller restarted → HeartbeatManager._reregister()
  → POST /controller/reregister (ReregisterMsg) — single attempt, no backoff;
    a later heartbeat exception triggers the next retry
```

### Phase 5: Shutdown

``` text
NodeManager.shutdown() — stop modules in reverse registration order:
  HeartbeatManager.stop()  → join threads
  EngineManager.stop()     → stop registration thread
  NodeManagerAPI.stop()    → shutdown FastAPI
  Daemon.stop()            → stop 5s monitor thread, then stop each service
                             in reverse registration order (SIGKILL engine PIDs)
  (there is no NodeManagerConfig.stop() — the config object has no lifecycle)
```

### Snapshot Restore

When `is_restored_from_host_side_snapshot()` returns True:

- `_checkpoint_done_inspect_retry_count` tracks checkpoint readiness polling
- `_register_after_restore()` — single POST /controller/register, no backoff;
  on failure the next heartbeat-report exception triggers it again
- `_start_after_restore()` does NOT exist — after restore the Controller
  re-issues a fresh StartCmdMsg via /node-manager/start, and the engine
  resumes through the snapshot routes (see engine-server.md)

## Key Files

| File | Role |
|------|------|
| `motor/node_manager/main.py` | Thin wrapper: config → port setup → `NodeManager(config).run()` |
| `motor/node_manager/node_manager.py` | `NodeManager(Application)`: init_modules, daemon tick, suicide detection, shutdown |
| `motor/node_manager/api_server/node_manager_api.py` | FastAPI: `/node-manager/start`, `/stop`, `/pause`, `/resume`, `/status`, `/readiness` (TLS via `mgmt_tls_config`) |
| `motor/node_manager/core/engine_manager.py` | Registration thread with exponential backoff + ranktable file writing + StartCmdMsg handling |
| `motor/node_manager/core/daemon.py` | Service registry orchestration (engine + KV-store services), 5s process monitor thread |
| `motor/node_manager/core/services/engine.py` | `EngineService`: subprocess.Popen, device pinning (`_calc_visible_device_ids`), CLI args, `_check_params` (business_port range), PID-death → SIGTERM self |
| `motor/node_manager/core/services/registry.py` | `@register_service` decorator + `_MODULE_MAP`; discovers active services by pod profile |
| `motor/node_manager/core/services/memcache/` | KV-store (memcache) service implementation |
| `motor/node_manager/core/heartbeat_manager.py` | Two daemon threads: status polling + heartbeat reporting, fault detection state machine, reregister |
| `motor/node_manager/core/fault_reporter.py` | ZMQ SUB on engine PUB ports (topic `vllm_fault`) → `report_software_fault` to Controller |
| `motor/node_manager/api_client/controller_api_client.py` | Sync HTTP client to Controller: `/register`, `/reregister`, `/heartbeat` |
| `motor/node_manager/api_client/engine_server_api_client.py` | Sync HTTP client: `GET /status` on engine's mgmt port |
| `motor/config/node_manager.py` | `NodeManagerConfig`: BasicConfig, APIConfig, EndpointConfig, SnapshotConfig, SingleContainerConfig, PortAllocatorConfig |

## Port Allocation

- `service_ports[i] = base_port + i * 2` (even ports — inference API)
- `mgmt_ports[i] = base_port + i * 2 + 1` (odd ports — management API)
- Ports validated in range [1024, 65535]
- Endpoint count: `min(dp_size, device_num // local_world_size)`
- When `port_allocator_config.enable`, runtime probing (motor/common/utils/port_allocator.py,
  `apply_node_manager_ports`) probes the host (`allocate_auto` with scan_range +
  probe_timeout) and re-allocates any busy port: node_manager_port, each
  service/mgmt port, and in single-container mode kv_port, lookup_rpc_port,
  dp_rpc_port.

## Development Rules

- **New core components** → use `ThreadSafeSingleton` pattern (`__new__` + double-checked locking with `threading.Lock`).
- **Module lifecycle**: init-then-start pattern — construct in `init_modules()`, start via API command, stop in `shutdown()`.
- **Fault thresholds**: keep configurable constants: grace period (120s, hardcoded in heartbeat_manager), consecutive abnormal count (5), heartbeat interval (default 3s) — do not hardcode deeper in the code.
- **Engine process management** → always through Daemon → EngineService; never `subprocess.Popen` elsewhere. Daemon tracks all PIDs for cleanup.
- **Device pinning**: `ASCEND_RT_VISIBLE_DEVICES` env var set per engine subprocess by `EngineService._calc_visible_device_ids` (only when `enable_multi_endpoints`).
- **New engine/KV-store backends** → add a service module with `@register_service` and an entry in the registry's `_MODULE_MAP`; Daemon stays unchanged.
- **Snapshot support**: check `is_restored_from_host_side_snapshot()` before registration; use longer timeouts during restore.

## Testing

```bash
bash tests/run_tests.sh tests/node_manager/
```
