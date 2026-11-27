# Controller Module — Architecture & Implementation

## Architecture: Observer Pattern

The Controller manages all inference instances (engines) in the cluster. It uses the **Observer pattern** to decouple instance lifecycle events from the modules that react to them.

The process is orchestrated by `Controller(Application)` (`motor/controller/controller.py`), following the same `Application` base-class pattern as NodeManager (`motor/common/app/application.py`): `main.py` is a thin wrapper (49 lines: config load → port setup → `Controller(config).run()`); module init/start, daemon loop, signal handling, and graceful shutdown all live in the `Controller` class. It supports **standalone** (all modules start immediately) and **master/standby** (only ControllerAPI starts; other modules follow `StandbyManager` transitions).

```text
Controller process
├── Controller(Application) — module orchestration (motor/controller/controller.py)
│     init_modules(): InstanceAssembler → EventPusher → [FaultManager if enabled]
│                     → InstanceManager → [Observability if enabled] → ControllerAPI
│     then attaches observers (EventPusher, FaultManager) to InstanceManager
│     master/standby: _start_standby_mode() → only ControllerAPI + StandbyManager;
│     _on_become_master / _on_become_standby start/stop modules (except ControllerAPI)
│     config hot-reload: on_config_updated() toggles fault_tolerance on/off dynamically
│     daemon loop interval from config.daemon_loop_interval (default 5s), hot-reloadable
│
├── InstanceManager (Subject, singleton via ThreadSafeSingleton)
│     owns: instances dict, state machine, heartbeat management thread
│     notifies: all attached observers on state change
│     persists: full instance data to ETCD with versioning + checksum
│
├── EventPusher (Observer) — subscribes READY/SEPARATED/PAUSED/RESUMED/REMOVED
│     pushes ADD/DEL/PAUSE/RESUME events to Coordinator over HTTP
│     failed incremental pushes enqueue one full SET reconciliation; a failed
│       SET is not recursively queued and does not advance the sent fingerprint
│     runs Coordinator heartbeat detection (1 loss → full SET push)
│     and periodic full-instance SET sync (coordinator_set_sync_interval)
│     REMOVED clears the SET roster under the same lock as the newer-instance id check
├── FaultManager (Observer) — subscribes INSTANCE_INITIAL/INSTANCE_SEPARATED/INSTANCE_REMOVED
│     owns: nodes dict (fault history per physical node), instances dict (per-instance fault level)
│     uses: mixins for persistence (_PersistenceMixin) and resource management (_ResourceManagerMixin)
│
└── api_server/controller_api.py — main Controller HTTP API + observability API (both TLS-capable)
      observability/ is a non-HTTP facade (Observability + AlarmStore + InventoryCollector)
      served through the observability API routes in controller_api.py
```

### Probe-based Coordinator discovery

A non-empty `motor_deploy_config.service_discovery_config.probe_ips.coordinator` list enables
Coordinator probing. `Controller._start_modules()` starts a process-owned `EndpointMonitor`;
without this list, the existing addressing path remains.
Candidates share ports from the caller's local `user_config.json`:
`motor_coordinator_config.api_config.coordinator_api_mgmt_port` is required, and `coordinator_obs_port`
is also required when Coordinator metrics proxying is enabled.
`motor_coordinator_config.port_allocator_config.enable` must be `false`.
Calls retain the existing management authentication and TLS.

`CoordinatorApiClient` reads cached targets for notifications and metrics. `query_status()` reads
cached readiness: HTTP 200 determines target eligibility, while the response's `ready` field feeds
the existing topology recovery condition. Business requests do not initiate probes.

Topology broadcasts use all configured Coordinator members, including standby members, and retain
the existing at-least-one-success result contract.

### State Machine (5 states × 6 events = 19 transitions)

**States:** `INITIAL` → `ACTIVE` → `INACTIVE` / `PAUSED` → `DELETED`

**Events** (`InsConditionEvent`, `motor/common/resources/instance.py`): `INSTANCE_INIT`, `INSTANCE_NORMAL`, `INSTANCE_ABNORMAL`, `INSTANCE_HEARTBEAT_TIMEOUT`, `INSTANCE_PAUSED`, `INSTANCE_RESUMED`

**Key transitions:**

- `INITIAL + INSTANCE_NORMAL → ACTIVE`: First successful heartbeat after registration
- `INITIAL + INSTANCE_ABNORMAL → INACTIVE`: Instance reported ABNORMAL during initialization
- `INITIAL + INSTANCE_HEARTBEAT_TIMEOUT → DELETED`: Instance timed out before ever becoming healthy
- `ACTIVE + INSTANCE_HEARTBEAT_TIMEOUT → INACTIVE`: Instance stopped responding (guarded by node-manager pre-check, see Heartbeat Management)
- `ACTIVE + INSTANCE_ABNORMAL → INACTIVE`: Instance reported ABNORMAL health
- `INACTIVE + INSTANCE_NORMAL → ACTIVE`: Instance recovered (heartbeat resumed with healthy status)
- `INACTIVE + INSTANCE_INIT → INITIAL`: Instance re-registered while inactive
- `INACTIVE + INSTANCE_HEARTBEAT_TIMEOUT → DELETED`: Instance timed out while already unhealthy
- `PAUSED + INSTANCE_RESUMED → ACTIVE`: Paused instance resumed
- `PAUSED + INSTANCE_ABNORMAL → INACTIVE`: Paused instance reported ABNORMAL health
- `PAUSED + INSTANCE_HEARTBEAT_TIMEOUT → DELETED`: Paused instance timed out

**Implementation** (`instance_manager.py`): `self.states` maps `InsStatus → handler Callable`; `self.transitions` maps `(from_status, event) → to_status` (19 entries). The state handler dispatches to observer notifications based on the new state.

### Observer Events

```python
class ObserverEvent(Enum):
    INSTANCE_INITIAL = 0   # New instance registered
    INSTANCE_READY = 1     # Instance reached ACTIVE state (ready for inference)
    INSTANCE_SEPARATED = 2 # Instance isolated due to fault
    INSTANCE_REMOVED = 3   # Instance deleted (heartbeat timeout or explicit removal)
    INSTANCE_PAUSED = 4    # Instance paused (e.g., maintenance)
    INSTANCE_RESUMED = 5   # Instance resumed from pause
```

Each observer implements `update(instance: ReadOnlyInstance, event: ObserverEvent)`. InstanceManager passes a read-only snapshot (`ReadOnlyInstance`) so observers cannot mutate instance state directly.

### Heartbeat Management

The `_instances_management_loop` thread runs every `instance_manager_check_interval` seconds:

1. Iterates all instances under `ins_lock`
2. Checks each instance's last heartbeat timestamp against the timeout
3. If heartbeat is stale: emits `INSTANCE_HEARTBEAT_TIMEOUT` event → state machine transition
4. If heartbeat is fresh but status changed: emits the corresponding `InsConditionEvent`

The heartbeat timeout is **not a config item** — it is a hardcoded constant (`motor/common/resources/instance.py`):

- `DEFAULT_ACTIVE_HEARTBEAT_TIMEOUT = 10` (seconds) — used while the instance is in ACTIVE state
- `CLEAR_INSTANCE_TIMEOUT = 300` (seconds) — used for non-ACTIVE instances

`ACTIVE + INSTANCE_HEARTBEAT_TIMEOUT → INACTIVE` is additionally guarded by a pre-check in `_handle_inactive` (`instance_manager.py`): before isolating the instance, the controller actively probes the node managers via `_check_node_managers_status`. If all node managers are healthy (e.g. the timeout was caused by controller master/standby switching, not by the node), the instance is **not** set to INACTIVE — instead its heartbeat timestamp is refreshed so it is not immediately re-timed-out.

`PartialLossPolicy` reads one DP runtime snapshot for heartbeat validation and legacy-shutdown protection. Active
scale-down transaction phases suppress the legacy partial-loss shutdown; `SCALED_DOWN_RUNNING` ignores only
`dead_committed`, so a new survivor loss still enters normal fault handling. `RECONFIGURING` is unprotected.
The policy applies when FT and DP scale-down are enabled, or while an enabled fast-recovery owner pauses routing
(`recovery_paused`; legacy WAITING snapshots also accept `preserve_recovery_streams`). Outside those windows, heartbeats must contain the
original exact endpoint set; with it on, only committed ranks may be absent. An empty heartbeat is valid only for a
fully committed local Pod.
Heartbeat handler return codes:

- `200`: Heartbeat processed, instance status unchanged
- `500`: Error processing heartbeat
- `503`: Controller restarted — NodeManager should re-register

### ETCD Persistence

When persistence is enabled (`etcd_config.enable_etcd_persistence`):

- On startup: `restore_data()` reads the latest `PersistentState` from ETCD with checksum verification
- On every state change: `persist_data()` writes a versioned snapshot with `calculate_checksum()` — SHA256 of `str(list(data.items())) + version + timestamp`, **not** a hash of JSON-serialized data (`motor/common/etcd/persistent_state.py`)
- `_data_version` is a monotonic counter (incremented on every write) to prevent stale writes from overwriting newer data
- `forced_separated_instances` is **memory-only and not persisted to or restored from ETCD** — after a controller restart the set starts empty, so force-isolation protection does not survive a restart

DP runtime persistence stores only `phase`, `original_dp_ranks`, `dead_committed`, `fallback_strategy`, and
`reconfiguration_dispatched`, plus the committed DP master; observation-only updates do not write ETCD. Interrupted
transactions resume their original fallback as `RECONFIGURING` without repeating engine apply. After whole-instance reconfiguration is
successfully dispatched, the old instance remains `RECONFIGURING`; Controller restart restores the dispatch marker
and does not submit the same reconfiguration again.

### FaultManager Internals

**Mixin architecture** (for maintainability):

- `_PersistenceMixin`: ETCD save/restore of nodes and instances
- `_ResourceManagerMixin`: node sync and resource monitoring (multi-instance per node)
- `FaultManager` itself: lifecycle, config, fault evaluation, strategy processing

**Data structures:**

- `self.nodes: dict[node_name, NodeMetadata]` — fault history per physical node. Nodes are preserved across instance removals so fault history survives node transfers between instances (e.g., scale_p2d).
- `self.instances: dict[instance_id, InstanceMetadata]` — current fault level and running strategy per instance.

**Fault evaluation flow:**

1. `ResourceMonitor` detects hardware faults on a node via k8s `CoreV1Api` Node watch + ConfigMap watch (`fault_tolerance/k8s/resource_monitor.py`) — not via a device plugin API
2. `FaultManager` maps fault → `FaultCategory` + `FaultLevel` via `FaultInfo`
3. `OriginFaultLevel` is first mapped to `FaultLevel` via `map_fault_level` (`fault_types.py`); the strategy is then selected from `generate_strategy_map()`, keyed by `FaultLevel` (HEALTHY + L1–L6) (`strategy/strategy.py`)
4. Strategy is executed: isolate instance → notify observers → track recovery

**Common recovery orchestration:**

The framework lives in `fault_tolerance/engine_recovery/` with five implementation modules:
`contracts`, `planner`, `runner`, `runtime`, and `serving`. The package initializer has no eager exports.
Contracts do not import strategies at runtime; Planner loads concrete strategies only when choosing an action.
FaultManager retains fault facts, task submission and settlement; concrete protocols remain in `strategy/`.
`EngineRecoveryStrategy` keeps its class name and persisted behavior, but is imported from `runner`, not `strategy`.
Framework tests mirror this package in `tests/controller/fault_tolerance/engine_recovery/`.

Engine-level actions live under `strategy/engine_ft/`: `fast_recovery.py`, `engine_relaunch.py`, and
`dp_scaling/`. The latter still owns both automatic scale-down and the existing scale-up/user-recovery flows;
this directory grouping does not split their task ownership or change feature switches. Its `context.py`
retains the existing `ScaleDownContext` alias. NM suicide, whole-instance reconfiguration and P-to-D resource
adjustment remain directly under `strategy/`. The `engine_ft` initializer has no eager exports; the existing
`strategy` facade continues exporting strategy classes. Tests mirror `strategy/engine_ft/`, with DP scaling
coverage in `dp_scaling/test_dp_scaling.py`.

DP scale-down `execute()` retains preparation and the outer abort/executor cleanup. Its apply, result polling,
NodeManager commit and post-commit publication/user-recovery phases are separate methods in the same strategy.
The execution deadline spans apply through commit; `mutation_started` is set immediately before dispatch, and
`committed` is accepted before post-commit runtime updates so publication failures do not abort a committed round.

- FM owns fault facts, one task slot per instance and settlement. Initial selection and preparation updates share
  `_build_engine_recovery_plan`; initial selection passes its locked snapshot to preserve lock ordering.
  Settlement and failure cleanup atomically check runtime ownership and skip removed or superseded owners;
  the strategy center isolates exceptions per instance so one failure cannot stop other instances' recovery.
- `engine_recovery/contracts.py` defines immutable evidence, plans, admission results and behavioral outcomes. The former shrink context import remains a compatibility alias;
  strategy-specific planning and execution stay in their modules.
- `EngineRecoveryStrategy` owns preparation, one selected child and cleanup in one Future with one `recovery_id`.
  A handoff submits the next task only after the previous Future returns; cancellation is forwarded to the child.
- Strategies declare `runs_under_recovery_owner` / `requires_recovery_preparation` for wrapping, binding and submission
  rollback. Common scheduling does not identify these behaviors through class-name strings.
- `engine_recovery/planner.py` maps recovery causes to fast-recovery strategies, classified evidence scenes to
  initial strategies, and `FailurePolicy` to failure handlers; scene classification preserves evidence priority.
- `RecoveryResult` carries failure policy, next-action advice and scheduling/protection requirements. FM persists the
  concrete child's result; Planner reads behavior fields. Names are for logs/history; old records are decoded on read.
- `engine_recovery/runtime.py` stores shared runtime; `engine_recovery/serving.py` owns serving projections. Common consumers depend on
  these interfaces rather than strategy-private state. Existing strategy protocols are outside this refactor.
  Serving pauses if either the instance or runtime requests it, independently of the previous recovery stage.
- FT status is controlled by the Controller FT master switch, independently of recovery-policy switches.
  Status defaults to a summary without NodeManager queries; Alive ranks describe topology, not live health; queries do not change recovery state.
- Optional-feature switches retain native recovery boundaries. Tests cover initial/update planning, task submission,
  cancellation, result persistence and fallback; detailed strategy behavior remains in its existing tests.
- ScaleP2D waits for D reinitialization by `job_name`. Only `ACTIVE` proves that rescheduling recovered D; an `INITIAL`
  replacement may contain only a partial endpoint set and therefore keeps waiting. If it remains non-ACTIVE at the
  configured deadline, ScaleP2D treats recovery as incomplete and proceeds to evaluate Prefill capacity. Superseded
  instance ids still block strategies that could stop replacement NodeManagers, but do not block ScaleP2D: it resolves
  the latest D by job name and either observes its ACTIVE recovery or releases Prefill capacity for it.
- Successful ScaleP2D Prefill preemption is acknowledged separately from DP scale-down: each physical node stores
  handled Decode `job_name`s for its continuous L3+ hardware fault episode. Submission and execution both check
  this acknowledgement; same-job workers share a non-blocking guard across replacement instance ids. Only pending
  nodes count toward further Prefill capacity release. Additional codes on an already handled node do not rearm it.
  ConfigMap and Node-status updates rearm only after all L3+ hardware evidence (including Node reboot) clears;
  an episode token rejects late acknowledgements after recovery/refault. These fields follow existing node ETCD
  persistence, including resets on orphaned nodes. Missing hardware snapshots retain baseline behavior, and failed
  preemption is not acknowledged. Real hardware faults, health/isolation and other strategy fallbacks stay unchanged;
  ScaleP2D never writes DP `handled_hardware_faults` as a substitute for actual hardware recovery.

**A2 PD-disagg isolation** (`A2_PD_ISOLATION_FAULT_CODES` in `fault_types.py`; currently `0x81078603` / `CardNetworkUnhealthy` → `PreSeparateNPU`):

- On Atlas `800I_A2`, these codes stay L6 (not downgraded to L2 while business is active). Add a code only when it needs all three: keep L6, NPU attribution, Decode NmSuicide.
- Node ConfigMap faults are still stored per **node**, but instance isolation uses `pre_separate_fault_affects_instance()` (`fault_types.py`): the named NPU (`Ascend910-N` / `npu-N`) must intersect the instance endpoint `device_id` list. Prefill and Decode sharing a node therefore do not both take L6 when only one role's cards are down. Unknown `npu_name` fails open (still isolate). An instance with an empty `device_id` list fails closed (not treated as owner) so a colocated INITIAL instance is not isolated.
- Endpoint `device_id` provenance in `InstanceAssembler._build_device_infos`: `RegisterMsg.physical_device_ids` (launch order) → synthesized `0..device_num-1` when the field is omitted. Ranktable has been removed; hardware-fault matching uses the physical ids reported by NodeManager and never depends on HCCL data.
- L6 Prefill / Decode isolation-set codes / multi-pod union on Atlas `800I_A2` → `NmSuicideStrategy` (`strategy/nm_suicide.py`): POST `/node-manager/stop` on every NodeManager of **that** instance. Timeout / connection refused is treated as already exiting. HTTP 5xx and other dispatch errors `mark_failed` so the strategy center can escalate to EngineRelaunch when enabled. A superseded instance id (newer id for the same `job_name`) is skipped so stop is not sent to a replacement Pod IP. Other Prefill L6 (non-isolation codes / non-A2) stays a no-op at the strategy layer.
- Single-pod union keeps the PreSeparateNPU L6→L2 downgrade. Other Decode L6 (not in the isolation set, or not A2) still uses ScaleP2D.

**Fault levels** (`fault_types.py`):

- `OriginFaultLevel` (9 members, `fault_types.py`): `NotHandleFault`, `SubHealthFault`, `RestartRequest`, `RestartBusiness`, `FreeRestartNPU`, `RestartNPU`, `SeparateNPU`, `PreSeparateNPU`, `ManuallySeparateNPU` — parsed from the Device Plugin ConfigMap, normalized by `_normalize_fault_level_string` (`fault_tolerance/k8s/configmap_parser.py`)
- `FaultLevel` (severity 0–6): `HEALTHY`, `L1` (sub-health, no action), `L2` (self-healing / pre-separation), `L3` (cannot be handled automatically), `L4` (severe isolation), `L5` (NPU restart → separation), `L6` (NPU separation → separation)
- `OriginFaultLevel` → `FaultLevel` conversion happens in `map_fault_level` (`fault_types.py`)

### Thread Safety

- `ins_lock` (`threading.Lock`) — protects `self.instances` dict
- `config_lock` (`threading.RLock`) — protects config fields + ETCD client
- `_version_lock` — protects `_data_version` monotonic counter
- All public methods acquire locks before accessing shared state

## Key Files

| File | Role |
|------|------|
| `motor/controller/core/instance_manager.py` | Instance lifecycle, state machine, heartbeat management, ETCD persistence |
| `motor/controller/core/instance_assembler.py` | Instance / deployment spec assembly |
| `motor/controller/core/observer.py` | `Observer` ABC + `ObserverEvent` enum |
| `motor/controller/core/event_pusher.py` | Pushes instance events to Coordinator via HTTP; Coordinator heartbeat detection + periodic SET sync. `INSTANCE_REMOVED` of a superseded (smaller) instance id does not send DEL when a newer READY instance already occupies the same `job_name`. |
| `motor/controller/core/recovery_service.py` | Instance recovery orchestration |
| `motor/controller/fault_tolerance/fault_manager.py` | Hardware fault detection, recovery strategy generation |
| `motor/controller/fault_tolerance/fault_types.py` | Fault enums and mapping plus A2 linkdown NPU attribution (`parse_npu_chip_ids`, `pre_separate_fault_affects_instance`) |
| `motor/common/http/engine_ft_client.py` | Versioned vLLM FT status/apply protocol plus group query/dispatch helpers |
| `motor/controller/fault_tolerance/engine_recovery/runtime.py` | Shared FT phases/runtime, singleton store, atomic owner checks, persistence and lifecycle projections |
| `motor/controller/fault_tolerance/engine_recovery/contracts.py` | Evidence, plans, admission results, behavioral outcomes and legacy-record decoding |
| `motor/controller/fault_tolerance/engine_recovery/planner.py` | Static admission, initial strategy selection and failure handoff decisions |
| `motor/controller/fault_tolerance/engine_recovery/runner.py` | Common task owner for collection, endpoint probing, child execution, cancellation and handoff |
| `motor/controller/fault_tolerance/strategy/engine_ft/dp_scaling/topology.py` | Fault-to-rank normalization and Pod-to-DP topology resolution |
| `motor/controller/fault_tolerance/strategy/engine_ft/dp_scaling/strategy.py` | DP scale-down execution, pre-apply readiness, current-fault checks and guard renewal |
| `motor/controller/fault_tolerance/pod_lifecycle.py` | One-shot Error-Pod stop dispatch; MindCluster owns subsequent recycling |
| `motor/controller/fault_tolerance/engine_recovery/serving.py` | Immutable surviving-endpoint projections used for Coordinator DEL/ADD/SET publication |
| `motor/controller/fault_tolerance/strategy/strategy.py` | Strategy map generation (including A2 isolation Prefill/Decode/multi-pod union→NmSuicide; other Decode L6→ScaleP2D) |
| `motor/controller/fault_tolerance/strategy/engine_ft/fast_recovery.py` | Existing recovery policies invoked by the common owner |
| `motor/controller/fault_tolerance/strategy/engine_ft/engine_relaunch.py` | Engine relaunch with existing container-restart fallback |
| `motor/controller/fault_tolerance/strategy/reconfiguration.py` | Whole-instance recovery, including escalation from fast recovery or engine relaunch. When both `enable_kv_migration` and `enable_kv_migration_on_reconfiguration` are enabled, registered source Pods drain concurrently after isolation and share one deadline before stop dispatch. The config loader accepts the former `enable_hw_evacuate_migration` key when the new key is absent. |
| `motor/controller/fault_tolerance/kv_migration_service.py` | Whole-instance recovery checks each target's effective fault level, isolation and recovery state before inventory and again before submission. Only members with the same instance ID, Pod IP and registered UID in both checks remain eligible. A UID-guarded stop counts as complete if the responding NodeManager explicitly identifies a replacement Pod at the same address. |
| `motor/controller/fault_tolerance/scale_in_migration.py` | Kubernetes target liveness checks for recovery and voluntary KV exit. Shares terminating ingress Pods' drain acknowledgements by instance generation and Pod UID, and restricts Kubernetes migration targets to retained workloads in the same service. |
| `motor/controller/fault_tolerance/strategy/nm_suicide.py` | Stop all NodeManagers of A2 isolation Prefill/Decode/multi-pod union L6 (`/node-manager/stop`) |
| `motor/controller/fault_tolerance/k8s/resource_monitor.py` | Per-node hardware monitoring via k8s Node/ConfigMap watch (NPU faults, network, etc.) |
| `motor/controller/fault_tolerance/k8s/configmap_parser.py` | Parses Ascend Device Plugin ConfigMap |
| `motor/controller/api_client/coordinator_api_client.py` | HTTP client for Coordinator APIs |
| `motor/controller/api_client/node_manager_api_client.py` | HTTP client for NodeManager APIs. `/node-manager/stop` returns `NodeManagerStopStatus`: timeout/connection refused is UNREACHABLE (already exiting); other errors are FAILED. |
| `motor/controller/api_server/controller_api.py` | Main Controller HTTP API + observability API (both TLS-capable via `mgmt_tls_config` / `observability_tls_config`) |
| `motor/controller/controller.py` | `Controller(Application)`: module orchestration, standalone / master-standby modes, fault-tolerance dynamic toggle, daemon loop |
| `motor/controller/main.py` | Thin wrapper: config load → port setup → `Controller(config).run()` |
| `motor/controller/observability/` | Non-HTTP alarm/inventory facade (`Observability` 86, `AlarmStore` 131, `InventoryCollector` 291) |

## Development Rules

- **Add observers** rather than calling InstanceManager methods directly — this minimizes coupling. Each observer handles a subset of events.
- **New events**: add to `ObserverEvent` enum in `observer.py`; update `transitions` dict in instance_manager; update all relevant observers' `update()` method.
- **State transitions**: must go through `self.transitions` dict — never modify `instance.status` directly.
- **Thread safety**: always acquire `ins_lock` before accessing `self.instances`. Use `config_lock` for config changes.
- **Fault strategy**: add new fault categories to `FaultCategory` enum; add corresponding strategy generation in `strategy/strategy.py`.
- **ETCD persistence**: every new field in `Instance` must be JSON-serializable (used by `model_dump(mode='json')`).
- **Endpoint ids are DP ranks**: in multi-endpoint single-node mode (`enable_multi_endpoints && nnodes <= 1`) the assembler
  allocates endpoint ids from the free slots in `[0, dp_size)` (`_allocate_endpoint_ids`); a registration with no free slot
  returns -1 and the NodeManager retries after `_filter_abnormal_endpoints` drops dead pods. Never derive ids from
  `get_endpoints_num()`: a recreated Pod has a new IP, its dead predecessor's endpoint stays in the roster until the probe
  filters it, and a count-based offset shifts DP ranks (e.g. 0/1 → 1/2). Cross-node PCP (`nnodes > 1`) and
  single-endpoint mode keep count-based ids.
- **Never derive role/master from registration order**: `_send_start_command` sets `master_dp_ip` to the pod owning the
  lowest non-headless endpoint id (DP rank 0), and for `nnodes == 1` sets `node_rank` to the pod's lowest endpoint id.
  Registration order can diverge from rank order after a dead pod is filtered and a later replacement takes the freed
  rank.

## Ascend Device Plugin ConfigMap Reference

When modifying ConfigMap parsing logic in `motor/controller/fault_tolerance/k8s/configmap_parser.py`, consult:

**Reference doc:** <https://gitcode.com/Ascend/mind-cluster/blob/master/docs/zh/scheduling/06_api/02_ascend_device_plugin.md>

Key points (MindCluster 26.0.0):

- **DeviceList key naming** — Two conventions: old (`huawei.com/Ascend910-Fault`) and new Atlas 350/850/950 (`huawei.com/npu-Fault`). Code must support both via `_resolve_device_list_key`.
- **Fault entry fields**: the parser only reads `fault_type`, `npu_name`, `fault_level`, `fault_code` (comma-separated hex, e.g. `"8F180E00,110001024"`) — there is no `fault_handling` / `large_model_fault_level` handling in the parser (`configmap_parser.py`).
- **SwitchInfoCfg FaultLevel**: MindCluster 26.0.0+ shortened forms `"NotHandle"` → `NotHandleFault` and `"Separate"` → `SeparateNPU`; other values map 1:1 to `OriginFaultLevel` member names via `_normalize_fault_level_string` (`configmap_parser.py`).
- **SwitchInfoCfg FaultTimeAndLevelMap key**: Old `[0x2001,info]_chip_port` → New (26.0.0+) `0x2001_chip_port`.
- **NPU naming**: Old `Ascend910-N` → New (Atlas 950) `npu-N`. `process_manually_separate_npu` must handle both.

## Testing

```bash
# Individual components
bash tests/run_tests.sh tests/controller/core/test_event_pusher.py
bash tests/run_tests.sh tests/controller/fault_tolerance/test_fault_manager.py

# Integration (observer pattern, state machine, heartbeat)
bash tests/run_tests.sh tests/controller/core/test_instance_manager.py

# Module-level
bash tests/run_tests.sh tests/controller/
```
