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
│     runs Coordinator heartbeat detection (2 consecutive losses → full SET push)
│     and periodic full-instance SET sync (coordinator_set_sync_interval)
├── FaultManager (Observer) — subscribes INSTANCE_INITIAL/INSTANCE_SEPARATED/INSTANCE_REMOVED
│     owns: nodes dict (fault history per physical node), instances dict (per-instance fault level)
│     uses: mixins for persistence (_PersistenceMixin) and resource management (_ResourceManagerMixin)
│
└── api_server/controller_api.py — main Controller HTTP API + observability API (both TLS-capable)
      observability/ is a non-HTTP facade (Observability + AlarmStore + InventoryCollector)
      served through the observability API routes in controller_api.py
```

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

**A2 PD-disagg isolation** (`A2_PD_ISOLATION_FAULT_CODES` in `fault_types.py`; currently `0x81078603` / `CardNetworkUnhealthy` → `PreSeparateNPU`):

- On Atlas `800I_A2`, these codes stay L6 (not downgraded to L2 while business is active). Add a code only when it needs all three: keep L6, NPU attribution, Decode NmSuicide.
- Node ConfigMap faults are still stored per **node**, but instance isolation uses `pre_separate_fault_affects_instance()` (`fault_types.py`): the named NPU (`Ascend910-N` / `npu-N`) must intersect the instance endpoint `device_id` list. Prefill and Decode sharing a node therefore do not both take L6 when only one role's cards are down. Unknown `npu_name` fails open (still isolate). An instance with an empty `device_id` list fails closed (not treated as owner) so a colocated INITIAL instance is not isolated.
- L6 Prefill / Decode isolation-set codes / multi-pod union on Atlas `800I_A2` → `NmSuicideStrategy` (`strategy/nm_suicide.py`): POST `/node-manager/stop` on every NodeManager of **that** instance. Timeout / connection refused is treated as already exiting. HTTP 5xx and other dispatch errors `mark_failed` so the strategy center can retry or escalate to EngineRelaunch. A superseded instance id (newer id for the same `job_name`) is skipped so stop is not sent to a replacement Pod IP. Other Prefill L6 (non-isolation codes / non-A2) stays a no-op at the strategy layer.
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
| `motor/controller/fault_tolerance/fault_types.py` | Fault enums (`FaultCategory`, `OriginFaultLevel`, `FaultLevel`, `FaultInfo`) + `map_fault_level` + A2 linkdown NPU attribution (`parse_npu_chip_ids`, `pre_separate_fault_affects_instance`) |
| `motor/controller/fault_tolerance/strategy/strategy.py` | Strategy map generation from fault levels (L6: A2 isolation Prefill/Decode/multi-pod union→NmSuicide; other Decode L6→ScaleP2D) |
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
