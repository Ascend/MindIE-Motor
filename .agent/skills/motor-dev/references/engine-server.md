# EngineServer Module — Architecture & Implementation

## Two-Endpoint Pattern

Each `engine_server` is a separate OS process spawned by NodeManager's Daemon. It wraps the underlying engine framework (vLLM or SGLang) behind two FastAPI HTTP servers.

``` text
EngineServer Process
│
├── MgmtEndpoint (FastAPI on :mgmt_port)
│     GET  /status   → engine health (polls InferEndpoint /health + SimInference)
│     GET  /metrics  → Prometheus multiprocess metrics
│
└── InferEndpoint (FastAPI on :port)
      POST /v1/chat/completions  → OpenAI chat completions (via dispatch adapter)
      POST /v1/completions       → OpenAI completions (via dispatch adapter)
      POST /v1/metaserver        → KV-transfer-aware dispatch control
      POST /v1/dispatch/stop     → dispatch stop propagation (HTTP 499 to peer)
      GET  /v1/models            → model listing
      GET  /health               → app.state.health_checker()
      POST /suspend              → snapshot: suspend engine to disk (model_save_path)
      POST /device_unlock        → snapshot: unlock devices after suspend
      POST /resume               → snapshot: resume engine (data_parallel_master_ip, model_path)
      POST /start_profile        → start profiling (only when profiler is configured)
      POST /stop_profile         → stop profiling (only when profiler is configured)
```

**Why two ports:** Management operations (health checks, metrics scraping) are separated from inference traffic. NodeManager polls mgmt_port for health without interfering with inference requests; Prometheus scrapes mgmt_port for metrics.

## Engine Abstraction Layer

ABC + factory pattern supporting multiple engine backends (vLLM primary, SGLang secondary):

``` text
IConfig (ABC)     → VLLMConfig / SGLangConfig       — CLI arg generation from deploy config
Engine (ABC)      → VLLMEngine / SGLangEngine         — engine client creation + lifecycle
Endpoint (ABC)    → MgmtEndpoint / InferEndpoint      — HTTP server lifecycle
InferEndpoint.get_lifespan() → VLLMEndpoint / SGLangEndpoint  — engine-specific startup
```

The transitional EngineServer reuses the CLI configuration adapters owned by
`motor/node_manager/core/services/native_engine/backends/`. Process launch and supervision for the
new direct-native path remain in `NativeEngineService`; the classes in this document describe only
the legacy in-process EngineServer path.

### Config → CLI Args Pipeline

1. `ConfigFactory` looks up the engine's `IConfig` in an explicit `_ENGINE_CONFIG_MAP` dict (`vllm` / `sglang` → module paths); `parse()` runs the full `initialize()` → `convert()` → `validate()` pipeline
2. `initialize()` — sets up engine-specific config (DP addresses, KV transfer, D2D config)
3. `convert()` — replaces `sys.argv` with the generated args, builds `FlexibleArgumentParser`, calls `make_arg_parser()` + `parse_args()`
4. `validate()` — for vLLM: calls `validate_parsed_serve_args()` on the parsed args

**Field mapping** (`_get_default_mapping()` in `vllm_config.py` — 9 entries):

```python
{
    'model_path': 'model',
    'model_name': 'served_model_name',
    'npu_mem_utils': 'gpu_memory_utilization',
    'dp_size': 'data_parallel_size',
    'tp_size': 'tensor_parallel_size',
    'pp_size': 'pipeline_parallel_size',
    'enable_ep': 'enable_expert_parallel',
    'dp_rpc_port': 'data_parallel_rpc_port',
    'cp_kv_cache_interleave_size': 'cp_kv_cache_interleave_size',
}
```

Other fields are passed through as `--field value` with type-aware serialization (`bool`: `--flag` presence; `list`: repeated values; `dict`: JSON string).

### PD Disaggregation (vLLM)

In PD mode, `VLLMConfig.initialize()` sets up KV transfer based on the configured connector — there is no longer any "TransferEngine" concept:

- **MooncakeConnector** (single, default): role sets `kv_role` (producer on prefill, consumer on decode), `engine_id` = instance_id, and injects prefill/decode parallel config into `kv_connector_extra_config`
- **MultiConnector**: `connectors[0]` is processed as the transport (mooncake-style keys), `connectors[1]` as the store — may be a `UCMConnector` (kept `kv_both`, no injected rpc port) or `MoonCakeStoreV1` / `AscendStoreConnector` (injected `mooncake_rpc_port` / `lookup_rpc_port` = instance_id)
- **UCMConnector standalone**: only supported in the `union` role (centralized-PD topology); prefill/decode raise a loud error instead of silently injecting mooncake-style keys
- KV transfer config is serialized to JSON and passed as `--kv-transfer-config` CLI arg
- D2D (decode-to-decode) config is set up separately via `_process_d2d_config()`

### Lifespan-Based Initialization

Heavy engine initialization (model loading, weight allocation) happens inside FastAPI's `lifespan` context manager — NOT at import time:

```python
@asynccontextmanager
async def _vllm_lifespan(app: FastAPI):
    # Startup: build engine config, create AsyncLLM engine (loads model weights)
    engine_config = vllm.AsyncEngineArgs.from_cli_args(args)
    vllm_endpoint_config = engine_config.create_engine_config(
        usage_context=UsageContext.OPENAI_API_SERVER,
        headless=headless,
    )
    engine_client = AsyncLLM.from_vllm_config(
        vllm_config=vllm_endpoint_config,
        usage_context=UsageContext.OPENAI_API_SERVER,
    )
    app.state.engine_client = engine_client
    app.state.openai_serving_chat = OpenAIServingChat(...)
    yield  # Server is ready
    # Shutdown: cleanup engine
    engine_client.shutdown()
```

**Headless PCP follower mode:** when `nnodes > 1` and `node_rank_within_dp > 0`, `_run_vllm()` skips the engine core entirely and starts only a `MultiprocExecutor` (workers only, following vLLM's `run_headless()` pattern); the headless flag also disables virtual inference.

**Why lifespan:** Uvicorn starts the HTTP listener immediately on `uvicorn.run()`. If model loading happened at import time, the port wouldn't accept connections for 30-120 seconds (model load time), causing NodeManager's health probes to fail. With lifespan, Uvicorn binds the socket first, then loads the model — health probes get a connection-refused or 503 until loading completes, which NodeManager handles via the grace period.

## Dispatch Adapter (Request Path Core)

Every inference request on InferEndpoint flows through the dispatch adapter (`core/dispatch_adapter/`) before reaching the engine:

``` text
InferEndpoint handler
  → create_dispatch_adapter(config)         — factory picks engine-specific subclass
  → adapt_request_body(body)                — attach MotorDispatch, rewrite request body
  → maybe_prepare_response / should_finish_prepared_response
  → engine request (chat/completions/metaserver)
  → normalize_response / normalize_stream_chunk
  → error mapping: map_serving_exception / map_engine_error / map_stream_error
  → dispatch control: is_dispatch_stopped / stop_peer / finish_dispatch
```

Responsibilities:

- **Dispatch attach**: wraps the request with `MotorDispatch` (prefill→decode handoff, request-body rewrite for the target role)
- **Stop propagation**: `POST /v1/dispatch/stop` (`handle_stop`) stops a dispatch; peers are stopped with HTTP 499, in-flight requests are aborted and stream chunks normalized
- **Response normalization**: `normalization.py` adapts engine responses/stream chunks to the unified OpenAI schema (e.g. completions-style bodies/chunks lifted to chat format, token_id stripping, request-id synthesis)
- **Error mapping**: engine exceptions are mapped to serving HTTP errors (and vice versa) via registered error handlers
- **KV-aware metaserver**: `POST /v1/metaserver` requests go through `prepare_metaserver_request` (engine_body + dispatch + KV params validation)

Files: `base.py` (534, `DispatchAdapter` + `DispatchAttemptRegistry` + stop client), `vllm_adapter.py` (340, `VLLMDispatchAdapter`), `sglang_adapter.py` (49, `SGLangDispatchAdapter`), `normalization.py` (220), `factory.py` (23, `create_dispatch_adapter`).

## Health Monitoring Stack (4 Layers)

``` text
Layer 1: NodeManager.HeartbeatManager
  → GET http://{ip}:{mgmt_port}/status  (every heartbeat_interval, default 3s)

Layer 2: MgmtEndpoint.HealthCollector
  → GET http://127.0.0.1:{port}/health  (async HTTP, same process)

Layer 3: InferEndpoint /health handler
  → app.state.health_checker()          (infer_endpoint.py)
    → engine_client.check_health()      (vLLM: AsyncLLM.check_health; SGLang: always True)

Layer 4: SimInference (proactive health)
  → virtual inference request: POST /v1/completions {"prompt": "1", "max_tokens": 1}
  → npu-smi subprocess: poll NPU AICore usage percentage
  → Logic: if AICore usage is low AND virtual requests fail for max_failure_count
    consecutive times → ABNORMAL
  → Details:
      max_failure_count default 6 (HealthCheckConfig.max_failure_count)
      enable_virtual_inference default False — forced off for SGLang, headless
        mode, and DP rank != 0 (only DP0 performs virtual inference)
      180s warmup stage (first virtual request timeout), then 5s interval —
        stretched to 20s when AI Cube peak >= 80%
```

**SimInference rationale:** An engine can pass basic health checks (process alive, port listening) but be unable to perform inference (e.g., NPU hang, driver issue, memory corruption). SimInference catches "silent failures" by running actual inference and monitoring hardware utilization.

## Snapshot Support

- `core/snapshot_sentinel.py` (200) — `SnapshotSentinel` thread: waits for InferEndpoint to be healthy, reaches the checkpoint, then drives `POST /suspend` / `POST /resume` against the engine
- `core/snapshot_monitor.py` (51) — `SnapshotMonitor` (ThreadSafeSingleton) tracks suspend/unlock/resume completion states
- Routes: `POST /suspend` (`model_save_path` query param), `POST /device_unlock`, `POST /resume` (`data_parallel_master_ip` + `model_path`); each 501/400s when the engine lacks `suspend`/`resume`/`device_unlock` support

## Cross-Node PCP (nnodes > 1)

- NodeManager injects `--node-rank` and `--master-dp-ip` into the engine command line (see nodeman.md)
- In the engine, `--node-rank`/`--master-dp-ip` are parsed into `EndpointConfig.node_rank` / `master_dp_ip` and wired into the vLLM parallel config
- Headless follower nodes (`node_rank_within_dp > 0`) run `MultiprocExecutor` workers only — no EngineCore, no AsyncLLM

## TLS Support

- `infer_tls_config` (InferEndpoint) and `mgmt_tls_config` (MgmtEndpoint) — when `enable_tls`, uvicorn gets an SSL context from `CertUtil.create_ssl_context(...)` and serves `https://`
- NodeManagerAPI serves TLS from the same `mgmt_tls_config`

## Key Files

| File | Role |
|------|------|
| `motor/engine_server/cli/main.py` | Entry point: CLI arg parsing, factory wiring, start Mgmt+Infer endpoints |
| `motor/node_manager/core/services/native_engine/backends/base.py` | Shared `IConfig` ABC used by the transitional EngineServer and native backends |
| `motor/engine_server/core/engine.py` | `Engine` ABC: `launch()`, `shutdown()` |
| `motor/engine_server/core/endpoint.py` | `Endpoint` ABC: `run()`, uvicorn lifecycle |
| `motor/engine_server/core/infer_endpoint.py` | `InferEndpoint`: FastAPI app, uvicorn, route registration, lifespan, dispatch adapter wiring, TLS |
| `motor/engine_server/core/mgmt_endpoint.py` | `MgmtEndpoint`: `/status` + `/metrics`, HealthCollector + SimInference, TLS |
| `motor/engine_server/core/health_collector.py` | Async HTTP health polling (calls InferEndpoint `/health`) |
| `motor/engine_server/core/sim_inference.py` | Virtual inference requests + `npu-smi` AICore monitoring |
| `motor/engine_server/core/dispatch_adapter/` | Dispatch adapter subsystem: `base.py` (534), `vllm_adapter.py` (340), `normalization.py` (220), `sglang_adapter.py` (49), `factory.py` (23) |
| `motor/engine_server/core/snapshot_sentinel.py` | `SnapshotSentinel` thread: checkpoint wait + suspend/resume driving |
| `motor/engine_server/core/snapshot_monitor.py` | `SnapshotMonitor`: suspend/unlock/resume completion states |
| `motor/node_manager/core/services/native_engine/backends/vllm/config.py` | `VLLMConfig`: field mapping, DP address, KV transfer, D2D config |
| `motor/engine_server/core/vllm/vllm_engine.py` | `VLLMEngine`: `AsyncEngineArgs.from_cli_args` + `AsyncLLM.from_vllm_config`, headless PCP follower |
| `motor/engine_server/core/vllm/vllm_endpoint.py` | `VLLMEndpoint`: `_vllm_lifespan` context manager, route init |
| `motor/engine_server/core/vllm/vllm_openai_compat.py` | OpenAI-compat shims (model lists, request mapping) for the vLLM backend |
| `motor/node_manager/core/services/native_engine/backends/sglang/config.py` | `SGLangConfig`: native CLI conversion and validation |
| `motor/engine_server/core/sglang/` | Transitional `SGLangEngine` and `SGLangEndpoint` |
| `motor/node_manager/core/services/native_engine/config_factory.py` | `ConfigFactory`: explicit `_ENGINE_CONFIG_MAP` + `parse()` (initialize→convert→validate) |
| `motor/engine_server/factory/endpoint_factory.py` | InferEndpoint loading by engine name |
| `motor/config/endpoint.py` | `EndpointConfig`: engine_type, host, port, mgmt_port, role, dp_rank, master_dp_ip, node_rank, d2d_peer_ips, snapshot_metadata, deploy_config; `HealthCheckConfig`: timeout, retry attempts, npu_usage_threshold, enable_virtual_inference, max_failure_count |

## Development Rules

### Adding a New Engine Backend

To add support for a new engine (e.g., "trtllm"):

1. Create `motor/node_manager/core/services/native_engine/backends/{engine}/config.py`
2. Implement the engine config adapter as an `IConfig` subclass and register it in `ConfigFactory._ENGINE_CONFIG_MAP`
3. For transitional EngineServer support, create `motor/engine_server/core/{engine}/`
4. Implement `{engine}_engine.py`: subclass `Engine`, wrap engine client creation in `launch()`
5. Implement `{engine}_endpoint.py`: subclass `InferEndpoint`, implement `get_lifespan()` + `init_request_handlers()`
6. Register the transitional endpoint in `EndpointFactory`

### Other Rules

- **New management endpoints** → add routes to MgmtEndpoint; **new inference endpoints** → add routes to InferEndpoint (inference routes should go through the dispatch adapter)
- **Config flattening**: `_flatten_config()` merges engine > model > parallel (engine takes precedence for conflicting keys). Add new fields to the appropriate source config class.
- **PD disaggregation**: KV transfer config is set up during `IConfig.initialize()` based on the endpoint's role (prefill=producer, decode=consumer) and the selected connector (Mooncake/Multi/UCM/AscendStore).
- **Device pinning**: handled by NodeManager via `ASCEND_RT_VISIBLE_DEVICES` env var. EngineServer must NOT manage device assignment — it inherits visibility from the parent process.
- **Prometheus multiprocess**: `PROMETHEUS_MULTIPROC_DIR` must be set in `main()` before any metric import. Each engine_server writes its own metrics file to this directory.
- **Port ranges**: `EndpointConfig.validate()` validates business and management ports before the engine process is spawned.

## Testing

```bash
bash tests/run_tests.sh tests/engine_server/
```
