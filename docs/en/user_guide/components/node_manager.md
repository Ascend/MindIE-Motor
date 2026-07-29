# Motor Node Manager

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:06:43.266Z pushedAt=2026-06-30T08:33:34.361Z -->

## Feature Description

The Node Manager process entry point is `motor/node_manager/main.py`. `init_all_modules` creates the following in sequence:

| Object | Source Code | Funtion |
|------|------|----------|
| `NodeManagerConfig` | `motor/config/node_manager.py` | Node-side configuration. |
| `NodeManagerAPI` | `motor/node_manager/api_server/node_manager_api.py` | Runs uvicorn in a background thread, mounting FastAPI routes. |
| `Daemon` | `motor/node_manager/core/daemon.py` | Launches the **`engine_server`** child process based on the start command issued by the Controller, or uniformly stops and cleans up the PID. |
| `EngineManager` | `motor/node_manager/core/engine_manager.py` | Registers/Re-registers with the Controller in a background thread; parses `StartCmdMsg`, writes the ranktable file, etc. |
| `HeartbeatManager` | `motor/node_manager/core/heartbeat_manager.py` | Reports heartbeats to the Controller; requests each endpoint's engine management plane `/status` at specified intervals; accumulated exceptions can trigger the suicide flag. |

In the main loop, if `HeartbeatManager().should_suicide()` is true, `suicide_procedure()` is executed: the config watcher is stopped, all modules are stopped, and the process exits with return code `-1` (see the "-1: rescheduling" comment in `main.py`).

## Environment Setup

- Configuration path: `Env.user_config_path or Env.config_path` is passed to `NodeManagerConfig.from_json` (see `main.py`).

- For information consistent with K8s deployment, mounting `user_config`, probes, etc., see [Environment Setup](../environment_preparation.md).

## Configuration Description

The corresponding block in the user configuration is **`motor_nodemanger_config`** (the key name is consistent with [Configuration Reference](../service_deployment/config_reference.md)). The code involves:

- `api_config.pod_ip`, `node_manager_port`: The listening address and port for `NodeManagerAPI`.

    - When `pod_ip` is not configured, the host falls back to `0.0.0.0` (see `NodeManagerAPI.__init__`).

    - `node_manager_port` defaults to `1026` in `motor/config/node_manager.py`; only when `NodeManagerAPI` is not passed a `config` instance (as a fallback branch only), the code falls back to `8080`, which is not reached in normal deployments.

- `mgmt_tls_config`: When true, enables TLS for the Node Manager HTTP service (`CertUtil.create_ssl_context`).

- `basic_config`: `job_name`, `heartbeat_interval_seconds`, `parallel_config`, `device_num`, `enable_multi_endpoints`, etc., used by `Daemon` / `EngineManager` / `HeartbeatManager`.

## Node Manager HTTP API

FastAPI app registration in `node_manager_api.py`:

| Method | Path | Behavior |
|------|------|------|
| `POST` | `/node-manager/start` | Parse `StartCmdMsg` → `EngineManager.parse_start_cmd` → `Daemon.pull_engine` → `HeartbeatManager.update_endpoint` and `start()` |
| `POST` | `/node-manager/stop` | `Daemon().stop`, stops all launched engine processes |
| `GET` | `/node-manager/status` | `HeartbeatManager().check_all_endpoints_normal`, returns `{"status": bool}` |

## Usage Example

```bash
python -m motor.node_manager.main
```

You can also call the equivalent module path through the entry script in the deployment image; refer to the actual image and `deployer` template.

## Errors and Logs

- `/node-manager/start` returns `400`/`422` when validation fails, and returns `500` when body parsing is abnormal or `pull_engine` fails (see the `detail` of `HTTPException` in the code for the message).

- If `Daemon.pull_engine` fails, it throws a `RuntimeError`, and the log contains information such as the child process exiting immediately.
