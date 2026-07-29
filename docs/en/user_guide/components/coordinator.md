# Motor Coordinator

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:06:29.806Z pushedAt=2026-06-30T08:09:49.264Z -->

## Feature Description

The Coordinator process entry point is `motor/coordinator/main.py`: the asynchronous `main()` constructs `CoordinatorConfig.from_json()`, calls `reconfigure_logging` as needed, and then creates and runs **`CoordinatorDaemon`** (`motor/coordinator/daemon/coordinator_daemon.py`).

`CoordinatorDaemon` is responsible for uniformly managing three types of subprocesses (key names defined in `motor/coordinator/process/constants.py`):

| Process Dictionary Key (Constant Name / Value) | Management Class | Description (from `CoordinatorDaemon` Module Docstring and `run` Implementation) |
|--------|--------|---------------------------------------------------------------|
| `PROCESS_KEY_SCHEDULER` (`"SchedulerProcess"`) | `SchedulerProcessManager` | Scheduler process |
| `PROCESS_KEY_MGMT` (`"MgmtProcess"`) | `MgmtProcessManager` | Management plane API process |
| `PROCESS_KEY_INFERENCE` (`"InferenceWorkers"`) | `InferenceProcessManager` | Inference worker processes (including inference HTTP, optional metaserver port, etc.) |

Regarding the startup order, the documentation states: **Scheduler first, then Mgmt**, so that Mgmt can successfully `connect`. When master-standby mode is not enabled, Inference is started after Scheduler/Mgmt. When `standby_config.enable_master_standby` is enabled, the `StandbyManager`'s `on_become_master` / `on_become_standby` methods start and stop Inference-related subprocesses only on the master node; this can also work with shared memory `RoleShmHolder` to write the role byte (for details, see the comments in the same file).

The start and stop constants come from `motor/coordinator/process/constants.py`:

- `START_ORDER = [SchedulerProcess, MgmtProcess, InferenceWorkers]`

- `STOP_ORDER = [InferenceWorkers, MgmtProcess, SchedulerProcess]`

That is: **the stop order is the reverse of the start order**—first stop Inference traffic, then stop Mgmt, and finally stop Scheduler—to avoid dangling connections during the stop process.

Child processes are monitored by `SubprocessSupervisor`; signal handling and exit are processed in the Daemon main loop (see the latter part of `CoordinatorDaemon.run`).

For the inference-side OpenAI-compatible path and metaserver behavior, see [Service-Oriented Interface Description](../service_oriented_interface/description.md).

## Environment Setup

- Configuration source: `CoordinatorConfig.from_json()`, typically from the merged result of **`motor_coordinator_config`** in the mounted `user_config.json`. For field definitions, see `motor/config/coordinator.py`.

- For deployment and port conventions, see [Configuration Reference: motor_coordinator_config](../service_deployment/config_reference.md) and [Interface Description](../../api_reference/interface_description.md).

## Configuration Description

Refer to the **`motor_coordinator_config`** section in [Configuration Reference](../service_deployment/config_reference.md) for authoritative field descriptions. The fields strongly related to the Daemon in the code include:

- `standby_config.enable_master_standby`: Whether to use the master-standby and inference start/stop branch.

- `scheduler_config`: `deploy_mode`, `scheduler_type`, etc., which affect inference routing (see [PD Disaggregation](../../features/PD_disaggregation.md)).

- `api_config`: Inference port, management port, etc. (subject to consistency with `interface_description.md`).

## Usage Example

```bash
python -m motor.coordinator.main
```

The entry point has no additional argparse; the configuration path is determined by the internal parsing logic of `CoordinatorConfig` (if `config_path` exists, it will be printed in the log).

## Errors and Logs

- `main.py` records `Server startup failed` along with the traceback upon startup failure and exits with code `1`.

- Behaviors such as subprocess crashes and restarts are logged by `SubprocessSupervisor` and various `ProcessManager` classes, and need to be investigated in conjunction with the logs within the Pod.
