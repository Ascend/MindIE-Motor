# Coordinator

## Function Description

The Coordinator process entry point is `motor/coordinator/main.py`: in the asynchronous `main()`, it constructs `CoordinatorConfig.from_json()`, calls `reconfigure_logging` as needed, and then creates and runs **`CoordinatorDaemon`** (`motor/coordinator/daemon/coordinator_daemon.py`).

`CoordinatorDaemon` is responsible for uniformly managing three types of subprocesses (key names are defined in `motor/coordinator/process/constants.py`):

| Process Dictionary Key (Constant Name / Value) | Management Class | Description (from the `CoordinatorDaemon` Module Docstring and the `run` Implementation) |
|--------|--------|---------------------------------------------------------------|
| `PROCESS_KEY_SCHEDULER` (`"SchedulerProcess"`) | `SchedulerProcessManager` | Scheduler process |
| `PROCESS_KEY_MGMT` (`"MgmtProcess"`) | `MgmtProcessManager` | Management-plane API process |
| `PROCESS_KEY_INFERENCE` (`"InferenceWorkers"`) | `InferenceProcessManager` | Inference worker processes (including the inference HTTP port, optional metaserver port, etc.) |

Regarding the startup order, the documentation states: **Scheduler first, then Mgmt**, so that Mgmt can successfully `connect`. When master-standby mode is not enabled, Inference is started after Scheduler/Mgmt. When `standby_config.enable_master_standby` is enabled, the `StandbyManager`'s `on_become_master` and  `on_become_standby` start and stop the Infer-related subprocesses only on the master node; it can also work with the shared memory `RoleShmHolder` to write the role byte (for details, see the comments in the same file).

The startup and shutdown constants come from `motor/coordinator/process/constants.py`:

- `START_ORDER = [SchedulerProcess, MgmtProcess, InferenceWorkers]`

- `STOP_ORDER = [InferenceWorkers, MgmtProcess, SchedulerProcess]`

That is: **the shutdown order is the reverse of the startup order**. Inference traffic is stopped first, then Mgmt is stopped, and finally Scheduler is stopped, to avoid dangling connections during the shutdown process.

Subprocesses are monitored by `SubprocessSupervisor`; the Daemon main loop handles signals and exits (see the latter part of `CoordinatorDaemon.run`).

For the OpenAI-compatible inference path and metaserver behavior, see [Service Interfaces](../../user_guide/api/service_interfaces.md).

## Environment Preparation

- Configuration source: `CoordinatorConfig.from_json()`, which usually comes from the merged result such as **`motor_coordinator_config`** in the mounted `user_config.json`. For field definitions, see `motor/config/coordinator.py`.

- For deployment and port conventions, see [Configuration Reference: motor_coordinator_config](../../user_guide/configuration/config_reference.md) and [Interface Description](../../user_guide/api/README.md).

## Configuration Description

Use the **`motor_coordinator_config`** section in [Configuration Reference](../../user_guide/configuration/config_reference.md) as the authoritative field description. The fields strongly related to the Daemon in the code include:

- `standby_config.enable_master_standby`: whether to use the master-standby and Infer start/stop branches.

- `scheduler_config`: `deploy_mode`, `scheduler_type`, and so on, which affect inference routing.

- `api_config`: inference ports, management ports, and so on (subject to the parts consistent with `interface_description.md`).

## Usage Examples

```bash
python -m motor.coordinator.main
```

The entry point has no additional argparse; the configuration path is determined by the internal parsing logic of `CoordinatorConfig` (if `config_path` exists, it is printed in the log).

## Errors and Logs

- When startup fails, `main.py` records `Server startup failed` and the traceback, and exits with code `1`.

- Behaviors such as subprocess crashes and restarts are logged by `SubprocessSupervisor` and the various `ProcessManager` classes, and must be investigated together with the logs inside the Pod.
