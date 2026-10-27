# Coordinator Standalone Deployment on Bare Metal

Deploy the Motor Coordinator separately on a physical machine or virtual machine (without K8s) to connect to existing native vLLM Prefill / Decode engines, supporting multiple P and multiple D. The Coordinator is a pure CPU service and does not depend on the NPU, Controller, or Node Manager.

Instance registration (available once motor is installed): `python3 -m motor.coordinator.register`. For external listening, you can optionally write a configuration file, as described in Section 2.

For routine addition, deletion, and query, see [Appendix C](#appendix-c-instance-add-delete-and-query). KV cache affinity is an optional enhancement, see [Appendix B](#appendix-b-kv-cache-affinity-optional).

## Overall Deployment Process

1. Obtain the code and install the dependencies and motor
2. (Optional) Write `coordinator.json`
3. Start the Coordinator
4. Register P/D instances
5. Verify end to end

## Prerequisites

| Item | Requirement |
|----|------|
| OS | Linux (aarch64 / x86_64), with the same glibc as the runtime environment |
| Python | `>= 3.11` |
| Editable source code (Method A) | A working Rust toolchain is available (`cargo --version` succeeds). This page does not describe the Rust installation steps |
| Self-packaged (Method B) | `curl` and a C compiler (`build-essential` / `gcc`); if cargo is unavailable, `build.sh` installs it through rustup |
| Network | The HTTP ports of all P/D engines are reachable (commonly `8000` / `10000`) |
| Ports | Ports `1025` (inference) / `1026` (management) / `1027` (observability) on the local machine are free |

## 1. Installation

```bash
git clone https://gitcode.com/Ascend/MindIE-Motor.git
cd MindIE-Motor
git checkout master

python3 -m venv /opt/motor-coord/venv
source /opt/motor-coord/venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Install motor using one of the following three methods.

**A. Editable source code** (prerequisite: a working cargo is available; pip does not compile Rust)

```bash
pip install --no-deps -e .
cargo build --release --manifest-path motor/coordinator/workload_shm_rs/Cargo.toml
```

If [KV cache affinity](#appendix-b-kv-cache-affinity-optional) is required, build kv-conductor separately (libzmq must already be available on the local machine):

```bash
cargo build --release --manifest-path motor/kv_conductor/Cargo.toml
```

**B. Self-packaged whl** (workload-shm is mandatory; kv-conductor is also packaged when cargo and libzmq are available, and is skipped automatically when zmq is missing)

```bash
bash build.sh
pip install --no-deps --force-reinstall dist/motor-*.whl
```

To explicitly skip kv-conductor, replace `bash build.sh` with `SKIP_KV_CONDUCTOR_BUILD=1 bash build.sh`.

**C. Prebuilt whl**

```bash
pip install --no-deps --force-reinstall /path/to/motor-*.whl
```

`--no-deps`: the whl does not declare dependencies, so `requirements.txt` must be installed first. For offline scenarios, see [Appendix A](#appendix-a-offline-installation).

Method A: `pip install --no-deps -e .` only turns the source tree into an editable installation and does **not** compile Rust. With a working cargo in place, you must separately build two artifacts: `libmindie_workload_shm.so` (required for the Coordinator to start) and `kv-conductor` (required only for [KV cache affinity](#appendix-b-kv-cache-affinity-optional)). At run time they are loaded from the `target/release/` directory of the corresponding crate, so there is no need to copy them to `lib/` or `bin/` again. After modifying a `.rs` file, run the above `cargo build --release` again for the corresponding crate. This page does not describe the Rust toolchain installation.

Method B: When `lib/*.so` / `bin/kv-conductor` already exist, `bash build.sh` skips cargo by default; after modifying `.rs` files, use `SKIP_WORKLOAD_SHM_BUILD=0` / `SKIP_KV_CONDUCTOR_BUILD=0`. If rustup cannot be installed, use `WORKLOAD_SHM_PREBUILT=/path/to/libmindie_workload_shm.so`. The resulting wheel **must** contain `libmindie_workload_shm.so`; when the library is missing, `build.sh` refuses to emit the package and prints `refusing to emit`. When the conductor binary is missing and both cargo and libzmq are available, `kv-conductor` is included as well (`unzip -l dist/motor-*.whl | grep -E 'kv-conductor|libmindie_workload_shm.so'`).

## 2. Configuration File (Optional)

By default, the main procedure performs startup, registration, and verification on the machine where the Coordinator runs, and no configuration file is required. The Coordinator listens on `127.0.0.1` by default, so the subsequent commands can be copied and executed directly.

If cross-host access is required, or if you deploy with Docker / Kubernetes, configure the listening address as described in [Appendix D](#appendix-d-listening-address). When the management port `1026` is exposed beyond the local machine, production environments should also enable a dedicated API key as described in [Appendix E](#appendix-e-management-plane-api-key).

## 3. Starting the Coordinator

```bash
source /opt/motor-coord/venv/bin/activate
export MOTOR_LOG_PATH=/opt/motor-coord/logs
mkdir -p "$MOTOR_LOG_PATH"
python3 -m motor.coordinator.main
```

```bash
curl http://127.0.0.1:1026/liveness
curl http://127.0.0.1:1026/readiness   # instance_count=0 at this point is normal
```

If you wrote `coordinator.json` in Section 2, run `export USER_CONFIG_PATH=...` before starting.

## 4. Registering P/D Instances

Each engine has been started with `vllm serve` and is reachable. One `--prefill` / `--decode` entry corresponds to one instance; multiple DP endpoints of the same instance are joined with commas (for a single DP, write only one `IP:PORT`). For the first deployment, use the default `set` event to replace the entire table; for later addition, deletion, and query, see [Appendix C](#appendix-c-instance-add-delete-and-query).

```bash
python3 -m motor.coordinator.register \
    --prefill "10.10.0.11:8000,10.10.0.11:8001" \
    --prefill "10.10.0.13:8000,10.10.0.13:8001" \
    --decode  "10.10.0.12:8000,10.10.0.12:8001" \
    --decode  "10.10.0.15:8000,10.10.0.15:8001"
```

```bash
curl http://127.0.0.1:1026/instances   # List the registered instances
curl http://127.0.0.1:1026/readiness   # Whether scheduling is ready (a usable P and D are both required)
```

The External Deployer can also send compact events directly to `POST /instances/refresh`.
Coordinator standalone currently supports only a single model. When `model_name` is omitted, the Coordinator
accesses `/v1/models` of the first reachable vLLM endpoint in the order of
`instances[]`; when the interface returns only one model
ID, that ID is adopted automatically. In multi-model scenarios, `model_name` must be explicitly carried at the top level of the request.
`dispatch_capabilities` and `engine_type` can also be omitted to use their default values, so each element in `instances[]`
only needs to provide `id`, `role`, and `endpoints[]`.

### Request Templates

**`set` — replace the managed topology in full** (must be replayed on the first deployment or after the Coordinator restarts):

```json
{
  "event": "set",
  "instances": [
    {
      "id": 1,
      "role": "prefill",
      "endpoints": [
        {"id": 0, "address": "10.0.0.11:8000"}
      ]
    },
    {
      "id": 2,
      "role": "prefill",
      "endpoints": [
        {"id": 0, "address": "10.0.0.12:8000"}
      ]
    },
    {
      "id": 3,
      "role": "decode",
      "endpoints": [
        {"id": 0, "address": "10.0.0.21:8000"}
      ]
    }
  ]
}
```

**`add` — add to the managed topology incrementally**:

```json
{
  "event": "add",
  "instances": [
    {
      "id": 4,
      "role": "prefill",
      "endpoints": [
        {"id": 0, "address": "10.0.0.13:8000"}
      ]
    }
  ]
}
```

**`del` — remove from the managed topology incrementally** (`id` must be consistent with a registered instance):

```json
{
  "event": "del",
  "instances": [
    {
      "id": 4,
      "role": "prefill",
      "endpoints": [
        {"id": 0, "address": "10.0.0.13:8000"}
      ]
    }
  ]
}
```

```bash
# Save the set template above as topology_set.json and then run the following command
curl -X POST http://127.0.0.1:1026/instances/refresh \
     -H 'Content-Type: application/json' \
     -d @topology_set.json
```

### Field Description

| Field | Level | Mandatory | Description |
|------|------|------|------|
| `event` | Top level | Yes | Event type: `set` for full replacement; `add` for incremental addition; `del` for incremental deletion |
| `model_name` | Top level | No (mandatory for multiple models) | For a single model, it is obtained automatically from `/v1/models` of the first reachable endpoint by default; it must be explicitly specified when the interface returns multiple models. If `motor_coordinator_config.aigw.id` is configured in the Coordinator, the value must be consistent with it (case-insensitive) |
| `dispatch_capabilities` | Top level | No | Only `prefill_handoff_decode` (default, handoff) and `concurrent_engine_sync` (trigger/sync) are allowed; other values such as `decode_colocation` are rejected |
| `engine_type` | Top level | No | Default value: `vllm`; the External Deployer currently supports only `vllm` |
| `instances` | Top level | Yes | List of instances involved in this batch; the complete topology for `set`, or an incremental subset for `add`/`del` |
| `instances[].id` | Instance | Yes | Globally unique instance ID, an integer ≥ 1; must not be repeated within the same request |
| `instances[].role` | Instance | Yes | Instance role: `prefill` or `decode` |
| `instances[].endpoints` | Instance | Yes | Reachable engine HTTP endpoints of the instance, at least 1; for multiple DPs, list multiple endpoints in the same array |
| `endpoints[].id` | Endpoint | Yes | DP sequence number within the instance, an integer ≥ 0; must not be repeated within the same instance |
| `endpoints[].address` | Endpoint | Yes | Engine `host:port`; for IPv6 use `[addr]:port` (for example, `"[2001:db8::1]:8200"`) |

`set` replaces the managed topology in full, `add` adds instances to the managed topology, and `del` removes instances from the managed topology. These events only change
the in-memory instance pool of the Coordinator; they do not start, stop, or destroy P/D processes. After the Coordinator restarts,
the External Deployer must replay the complete `set`.

## 5. End-to-End Verification

```bash
curl http://127.0.0.1:1025/v1/completions \
     -H 'Content-Type: application/json' \
     -d '{"model":"Qwen3-8B","prompt":"Hello, how are you?","max_tokens":16}'
```

HTTP 200 is expected. Set `model` to the model name actually exposed by the engine.

## 6. FAQs

| Symptom | Handling |
|------|------|
| `ImportError` (uvicorn / zmq and so on) | Install `requirements.txt` first, and then install motor with `--no-deps` |
| curl to 1025 fails from outside or through a Docker mapping | Configure `coordinator_api_host` as `0.0.0.0` |
| The registration script reports not live | Start the Coordinator before registering |
| Inference returns 503 (Service is not available) after registration | Check the engine `IP:PORT`; use `--dry-run` to inspect the payload |
| The log directory is incorrect | Set `MOTOR_LOG_PATH` |

## Appendix A: Offline Installation

```bash
# On the source machine:
#   pip download -r requirements.txt -d /tmp/offline_pkgs \
#       --python-version 3.11 --platform manylinux2014_aarch64 --only-binary=:all:
# On the target machine:
#   pip install /tmp/offline_pkgs/*.whl
#   pip install --no-deps --force-reinstall /path/to/motor-*.whl
```

Packages that contain binary extensions must match the architecture and Python version of the target machine.

## Appendix B: KV Cache Affinity (Optional)

Required only when `scheduler_type=kv_cache_affinity`. For the complete field list, see [KV Cache affinity](../features/kvcache_affinity.md).

```json
{
  "motor_coordinator_config": {
    "api_config": { "coordinator_api_host": "0.0.0.0" },
    "scheduler_config": { "scheduler_type": "kv_cache_affinity" }
  },
  "kv_conductor_config": {
    "conductor_service": "<KV_CONDUCTOR_IP>",
    "http_server_port": 13333,
    "npu_endpoint": "tcp://*:5557",
    "replay_endpoint": "tcp://*:6667",
    "engine_type": "vLLM",
    "model_path": "/mnt/weight/Qwen3-8B",
    "block_size": 128,
    "re_register_interval_sec": 30
  }
}
```

`npu_endpoint` must be consistent with `--kv-events-config` of the P engine, and `block_size` must be consistent with `--block-size` of the engine (default 128); otherwise the hit ratio is always 0.

```bash
source /opt/motor-coord/venv/bin/activate
python3 -c "from motor.kv_conductor import get_binary_path; p=get_binary_path(); print(p); assert p and p.is_file()"
export RUST_LOG=info,kv_conductor=debug
export MOTOR_LOG_PATH=/opt/motor-coord/logs
nohup python3 -m motor.kv_conductor --host 0.0.0.0 --port 13333 &
```

Restart the Coordinator after changing `conductor_service`. Send requests with the same long prefix twice; the `matched` value in the log of the second (warm) request should be greater than 0.

## Appendix C: Instance Add, Delete, and Query

`set` (the default) replaces the entire instance table; `add` / `del` incrementally add or remove instances by the same `IP:PORT`; `list` queries the current registrations. The instance `id` is derived from `role` plus the sorted complete endpoint group, so the input order of endpoints does not affect the registration result, and you do not need to remember numeric IDs for addition or deletion.

If the management plane API key is enabled, all the following `register` commands must additionally append
`--mgmt-api-key-file /opt/motor-coord/secrets/mgmt-api-key`; see [Appendix E](#appendix-e-management-plane-api-key).

### Query

```bash
python3 -m motor.coordinator.register list
# Equivalent: curl http://127.0.0.1:1026/instances
```

Returns `count` and `instances[]` (`id` / `role` / `job_name` / `model_name` / `status` / `pool` / `healthy` / `circuit_breaker` / `endpoints`). For the complete field list, see [Instance Query Interface](../api/management_interfaces.md#instance-query-interface).

### Add

```bash
python3 -m motor.coordinator.register add --prefill "10.10.0.17:8000,10.10.0.17:8001"
python3 -m motor.coordinator.register add --decode  "10.10.0.18:8000,10.10.0.18:8001"
```

For a single DP, simply omit the comma: `--prefill 10.10.0.17:8000`.

### Delete

```bash
# The same endpoint group as used with add (the order may differ; the actual registered ID is queried before deletion)
python3 -m motor.coordinator.register del --prefill "10.10.0.17:8000,10.10.0.17:8001"

# Or run list first and then delete by id
python3 -m motor.coordinator.register del --id 1234
```

### Other Common Parameters

```bash
# Only print the JSON to be submitted without actually registering
python3 -m motor.coordinator.register add --prefill "10.10.0.17:8000,10.10.0.17:8001" --dry-run

# Specify explicitly when the engine /v1/models is unreachable or returns multiple models
python3 -m motor.coordinator.register add --prefill "10.10.0.17:8000,10.10.0.17:8001" \
    --model-name Qwen3-8B --no-health-check

# Remote Coordinator (default http://127.0.0.1:1026)
python3 -m motor.coordinator.register list --coordinator http://10.10.0.1
python3 -m motor.coordinator.register --help
```

## Appendix D: Listening Address

`coordinator_api_host` applies to the inference port `1025`, the management port `1026`, and the observability port `1027` at the same time.

### Cross-Host Access from Bare Metal / Virtual Machines

`coordinator_api_host` is the local listening address on the machine where the Coordinator runs, not the address of a P/D engine or client. It is recommended to bind it to the service NIC IP of the Coordinator:

```json
{
  "motor_coordinator_config": {
    "api_config": {
      "coordinator_api_host": "10.10.0.20"
    }
  }
}
```

Replace `10.10.0.20` with the actual service NIC IP, and specify the configuration file before starting the Coordinator:

```bash
export USER_CONFIG_PATH=/opt/motor-coord/coordinator.json
```

For remote registration, specify this address through `--coordinator`. The management port uses `1026` by default and does not need to be written into `--coordinator`:

```bash
python3 -m motor.coordinator.register list --coordinator http://10.10.0.20
```

### Docker

Set it to `0.0.0.0` so that the service listens on all NICs of the container; do not query or pin the container IP, because the container IP may change. Clients access the service through the host port mapping address or the service name in the container network.

```json
{
  "motor_coordinator_config": {
    "api_config": {
      "coordinator_api_host": "0.0.0.0"
    }
  }
}
```

### Kubernetes

The Coordinator code does not actively query the Pod IP; it only reads the `POD_IP` environment variable. The Pod YAML should inject it through the Downward API:

```yaml
env:
  - name: POD_IP
    valueFrom:
      fieldRef:
        fieldPath: status.podIP
```

The Coordinator deployment template in the repository already includes this configuration. After injection, the Coordinator binds to the Pod IP by default, and clients usually access it through the Service DNS. If a custom YAML neither injects `POD_IP` nor configures `coordinator_api_host`, the Coordinator falls back to `127.0.0.1`, and the Service cannot access it.

## Appendix E: Management Plane API Key

The management plane API key is disabled by default, which does not affect local startup, registration, or inference. When the management port `1026` can be accessed by entities other than the local machine, production environments should enable a dedicated API key.

First generate the key file:

```bash
# Create a key directory accessible only to the current user
install -d -m 700 /opt/motor-coord/secrets

# Generate a high-strength random API key and write it to the file
python3 -c "import secrets; print(secrets.token_urlsafe(32))" \
    > /opt/motor-coord/secrets/mgmt-api-key

# Allow only the owner to read and write the key file
chmod 600 /opt/motor-coord/secrets/mgmt-api-key
```

The user who runs the commands should be the same as the user who runs the Coordinator; otherwise, adjust the file owner to ensure that the service can read it. Then add `mgmt_api_key_config` to `coordinator.json`:

```json
{
  "motor_coordinator_config": {
    "api_config": {
      "coordinator_api_host": "10.10.0.20"
    },
    "mgmt_api_key_config": {
      "enable_api_key": true,
      "api_key_file": "/opt/motor-coord/secrets/mgmt-api-key"
    }
  }
}
```

Specify the configuration file before starting the Coordinator:

```bash
export USER_CONFIG_PATH=/opt/motor-coord/coordinator.json
```

After it is enabled, all `register` commands must carry the same key file:

```bash
python3 -m motor.coordinator.register list \
    --coordinator http://10.10.0.20 \
    --mgmt-api-key-file /opt/motor-coord/secrets/mgmt-api-key
```

When calling protected management interfaces directly, the request header is `X-Motor-Management-Key`:

```bash
curl http://10.10.0.20:1026/instances \
    -H "X-Motor-Management-Key: $(< /opt/motor-coord/secrets/mgmt-api-key)"
```

The management API key protects the instance query, instance refresh, and precision alarm state clearing interfaces; the startup, liveness, and readiness probes are not authenticated. The API key only performs identity verification and does not encrypt traffic. When the management port is accessed across hosts, you should also configure `mgmt_tls_config` or terminate TLS at a trusted proxy, and restrict the access scope through firewalls, container networks, or NetworkPolicy; do not expose `1026` directly to the public network.
