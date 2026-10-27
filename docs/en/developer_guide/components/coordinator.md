# Coordinator

## Function Description

The Coordinator process entry point is `motor/coordinator/main.py`: in the asynchronous `main()`, it constructs `CoordinatorConfig.from_json()`, calls `reconfigure_logging` as needed, and then creates and runs **`CoordinatorDaemon`** (`motor/coordinator/daemon/coordinator_daemon.py`).

`CoordinatorDaemon` is responsible for uniformly managing three types of subprocesses (key names are defined in `motor/coordinator/process/constants.py`):

| Process Dictionary Key (Constant Name / Value) | Management Class | Description |
|--------|--------|------|
| `PROCESS_KEY_MGMT` (`"MgmtProcess"`) | `MgmtProcessManager` | Manages HTTP and the control plane (instance membership table, circuit breaker, global precision latch, schema-4 workload SHM, ZMQ ROUTER/PUB) |
| `PROCESS_KEY_OBS` (`"ObsProcess"`) | `ObsProcessManager` | Observability API process |
| `PROCESS_KEY_INFERENCE` (`"InferenceWorkers"`) | `InferenceProcessManager` | Inference Workers: OpenAI/Anthropic HTTP; the scheduling hot path scores locally and then performs CAS accounting on the Rust POSIX SHM |

There is no independent Scheduler subprocess. The IPC path names are still `scheduler_frontend` / `scheduler_instance_pub` (bound by Mgmt; the historical names are unchanged).

When master-standby mode is not enabled, Mgmt → Obs → Inference are started according to `START_ORDER`. When `standby_config.enable_master_standby` is enabled, the
`StandbyManager`'s `on_become_master` / `on_become_standby` start and stop the Infer-related subprocesses only on the master node;
Mgmt and Obs run on both the master and standby sides. It can also work with the shared memory `RoleShmHolder` to write the role byte (for details, see the comments in the same file).

The startup and shutdown constants come from `motor/coordinator/process/constants.py`:

- `START_ORDER = [MgmtProcess, ObsProcess, InferenceWorkers]`
- `STOP_ORDER = [InferenceWorkers, ObsProcess, MgmtProcess]`

That is: **the shutdown order is the reverse of the startup order**. Inference traffic is stopped first, then Obs is stopped, and finally Mgmt is stopped (the control plane and the SHM lifecycle end with Mgmt).

Subprocesses are monitored by `SubprocessSupervisor`; the Daemon main loop handles signals and exits (see the latter part of `CoordinatorDaemon.run`).

For the OpenAI-compatible inference path, see [Service Interfaces](../../user_guide/api/service_interfaces.md).

### Pre-Routing Token Processing

After `render_config.enable=true` is set, the Coordinator preferentially uses the vLLM Render Sidecar in the local Pod to process Chat Completions and Completions before routing, and reuses the token IDs for the context budget, KV cache affinity scheduling, and P/D routing. When `kv_cache_affinity` scheduling is enabled, it is recommended to enable Render at the same time. Even if the context budget is enabled at the same time, the local tokenizer is loaded when Render falls back; if Render is unavailable or the response is invalid, the Coordinator automatically falls back, without affecting the Coordinator startup.

For plain text requests, the Render token IDs are reused in both streaming and non-streaming scenarios: Prefill in PD uses the token-only interface, and Decode uses the native vLLM OpenAI response; Union injects the token IDs into the native OpenAI request. For Completion multi-prompt, Prefill and native Decode are executed prompt by prompt, and then the choices and usage are merged. `enable_streaming=true` switches only the plain streaming Decode to token-only + Derender; data obfuscation still uses the token-only link. Streaming Derender requires vLLM >= 0.27.0.

When `context_budget_mode=on`, the final output budget is synchronized to the Render parameters. For configuration and boundary descriptions, see
[Configuration Reference](../../user_guide/configuration/config_reference.md),
[max_tokens Adaptation](../../user_guide/features/max_tokens_adaptation.md), and
[KV Cache Affinity Scheduling](../../user_guide/features/kvcache_affinity.md).

## Environment Preparation

- Configuration source: `CoordinatorConfig.from_json()`, which usually comes from the merged result such as **`motor_coordinator_config`** in the mounted `user_config.json`. For field definitions, see `motor/config/coordinator.py`. In standalone deployment, the entry point reads only the `USER_CONFIG_PATH` environment variable.

- For deployment and port conventions, see [Configuration Reference: motor_coordinator_config](../../user_guide/configuration/config_reference.md) and [Interface Description](../../user_guide/api/README.md).

- The workload SHM requires `libmindie_workload_shm.so`. By default, `bash build.sh` skips cargo if `lib/*.so` already exists, and compiles it only when it is missing and then **packs it into the wheel** (packaging is prohibited when the library is missing). After modifying a `.rs` file, set `SKIP_WORKLOAD_SHM_BUILD=0`. When cargo and libzmq are available and `bin/kv-conductor` is missing, kv-conductor is packed at the same time; when libzmq is missing, it is skipped automatically (you can also run `SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh`). When the `.so` is missing, the startup fails loudly and does not fall back to the Python ledger.

- For the startup procedure that does not depend on the Controller or Node Manager, see [Coordinator Standalone Deployment](../../user_guide/deployment/standalone.md).

## Configuration Description

Use the **`motor_coordinator_config`** section in [Configuration Reference](../../user_guide/configuration/config_reference.md) as the authoritative field description. The fields strongly related to the Daemon in the code include:

- `standby_config.enable_master_standby`: whether to use the master-standby and Infer start/stop branches.
- `scheduler_config`: `scheduler_type`, and so on. The inference Router is dynamically selected according to the current instance role and `dispatch_capabilities` and no longer reads `deploy_mode` .
- `inference_workers_config.worker_metaserver_base_port`: an independent metaserver port for each Worker in vLLM layerwise/trigger mode; the default value is `12000`, and `0` disables it. The listening address preferentially uses `POD_IP`, and otherwise uses `coordinator_api_host`. In case of a port conflict, the inference port continues to work, and Trigger returns 503.
- `api_config`: inference ports, management ports, and so on (subject to the parts consistent with `interface_description.md`).

## Usage Examples

```bash
python -m motor.coordinator.main
```

The entry point has no additional argparse; the configuration path is determined by the internal parsing logic of `CoordinatorConfig` (if `config_path` exists, it is printed in the log).

## Errors and Logs

- When startup fails, `main.py` records `Server startup failed` and the traceback, and exits with code `1`.
- Behaviors such as subprocess crashes and restarts are logged by `SubprocessSupervisor` and the various `ProcessManager` classes, and must be investigated together with the logs inside the Pod.
- When the Worker logs show that the native workload SHM cannot be attached, check whether Mgmt has created the SHM and whether the image contains `libmindie_workload_shm.so`.
