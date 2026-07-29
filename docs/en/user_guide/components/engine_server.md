# Motor Engine Server (Inference Engine Side Process)

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:06:37.189Z pushedAt=2026-06-30T08:22:39.241Z -->

## Feature Description

In this repository, **Engine Server** refers to the executable entry point **`engine_server`** (entry point in `setup.py`: `engine_server = motor.engine_server.cli.main:main`), with the corresponding implementation in `motor/engine_server/cli/main.py`.

Primary functions:

1. **Parse endpoint configuration**: `EndpointConfig.init_endpoint_config()`, which obtains the specific engine configuration through `ConfigFactory` (in `motor/engine_server/factory/config_factory.py`, the configuration class is selected based on the type, such as `vllm` or `sglang`).

2. **Management plane HTTP (MgmtEndpoint)**: In `motor/engine_server/core/mgmt_endpoint.py`, uvicorn is started on `mgmt_port`, mounting Prometheus-related routes and **`GET /status`** (the path constant `STATUS_INTERFACE`, with the value `/status`). The status field key is the constant corresponding to `STATUS_KEY` (used in conjunction with `NORMAL_STATUS` / `ABNORMAL_STATUS` / `INIT_STATUS` in the same file). **TLS** (`mgmt_tls_config.enable_tls`) is optional.

3. **Inference plane (InferEndpoint)**: Constructed by `EndpointFactory.get_infer_endpoint(config)` based on the engine type (such as `VLLMEndpoint` and `SGLangEndpoint`, see `motor/engine_server/factory/endpoint_factory.py`). Runs in parallel with `MgmtEndpoint` via `run()`. The main thread blocks on `infer_endpoint.wait()` until exit, then shuts down both endpoints.

On the Node Manager side, this process is launched via the subprocess command **`engine_server`**, with arguments hardcoded in `pull_engine` of `motor/node_manager/core/daemon.py`, including `--dp-rank`, `--instance-id`, `--role`, `--host`, `--port`, `--mgmt-port`, `--master-dp-ip`, and `--config-path` (value is `Env.user_config_path`); in single-container mode, `--kv-port`, `--dp-rpc-port`, etc. are also appended (see the same method).

## Relationship with Node Manager/Controller

- **Start**: After the Controller assembles instances, Node Manager's `POST /node-manager/start` triggers `Daemon.pull_engine`, which then executes `engine_server ...` via `subprocess.Popen`.

- **Health check**: `motor/node_manager/api_client/engine_server_api_client.py` uses `SafeHTTPSClient` to initiate **`GET /status`** to `{ip}:{mgmt_port}`, with TLS options sourced from `NodeManagerConfig.from_json().mgmt_tls_config`.

- **Stop**: `POST /node-manager/stop` calls `Daemon.stop`, sending `SIGKILL` to the recorded PID.

## Configuration Description

Engine and endpoint-related fields are distributed across the engine configuration in `user_config` and **`motor_nodemanger_config`**, among others. For a comparison with vLLM/SGLang engine sub-fields, see [Configuration reference](../service_deployment/config_reference.md) and its **`motor_engine_prefill_config` / `motor_engine_decode_config`** sections. For items that overlap with `EndpointConfig`, such as TLS, refer to `motor/config/endpoint.py` and `config_reference`.

## Usage Example (Local Debugging)

Consistent with the commands issued by Node Manager, the environment and parameters required by `EndpointConfig` must be provided, for example:

```bash
engine_server --dp-rank 0 --instance-id 1 --role prefill \
  --host 127.0.0.1 --port 8000 --mgmt-port 8001 \
  --master-dp-ip 127.0.0.1 --config-path /path/to/user_config.json
```

The actual port and role are subject to the scheduling result. For single-machine debugging, refer to the test and example configurations.

## Errors and Logs

- For default log file path constants, see `motor/engine_server/constants/constants.py` (e.g., `LOG_DEFAULT_FILE` relative to `./engine_server_log/`).

- The management plane `/status` endpoint returns `ABNORMAL_STATUS` when the health check is abnormal (see the `mgmt_endpoint.get_status` implementation). The Node Manager side updates the endpoint status accordingly and may participate in the suicide decision (see `HeartbeatManager`).
