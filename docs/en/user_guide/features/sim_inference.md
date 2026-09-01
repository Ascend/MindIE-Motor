# Virtual Inference Health Probe

## Feature Introduction

Virtual inference (implemented in `motor/engine_server/core/sim_inference.py`) is used to proactively send lightweight requests to the inference surface during low business load, and determine whether the Engine Server inference engine is available based on the NPU **AI Cube usage**. The configuration is located in the **`health_check_config`** sub-block of `motor_engine_prefill_config`/`motor_engine_decode_config` in `user_config`, and is **disabled by default**.

Node Manager periodically requests **`GET /status`** on the Engine Server mgmt surface; the returned value combines the inference surface `/health` and the virtual inference result. When the status is continuously abnormal, `HeartbeatManager` can trigger node self-termination and rescheduling.

**Version requirement**: Virtual inference supports only **HDK 26.0.RC1** and later versions.

## Working Mechanism

**Enabling conditions** (all of the following must be met):

1. `engine_type` is **`vllm`** (for the SGLang engine, virtual inference is automatically disabled at runtime even if `enable_virtual_inference: true` is configured)

2. `health_check_config.enable_virtual_inference` is `true`.

3. The process environment variable `ASCEND_GLOBAL_LOG_LEVEL` is **ERROR** (`3`; `0`=DEBUG, `1`=INFO, `2`=WARNING, and `3`=ERROR). Virtual inference **can be enabled only at the ERROR log level**; when it is not configured, the default value is ERROR. If it is explicitly configured to a value other than `3`, Engine Server disables virtual inference and prints a warning before starting virtual inference.

4. `0 < health_check_config.npu_usage_threshold <= 100`

5. The inference surface `GET /health` returns normally (probed by `HealthCollector`, with `health_collector_timeout` controlling the timeout and `health_collector_timeout_retry_attempts` controlling the number of timeout retries).

6. Virtual inference is executed only on **DP rank 0** (automatically disabled when running on a non-DP0 node).

After the conditions are met, `mgmt_endpoint.py` calls `run_virtual_inference()` on the first `/status` request to start the virtual inference loop.

**Virtual inference request**: sends `POST /v1/completions` to the inference surface, with the request body being `prompt: "1"` and `max_tokens: 1`. vLLM **layerwise decode** (`dispatch_profile=trigger`) additionally carries `kv_transfer_params.do_virtual: true` and PD disaggregation-related fields; **handoff decode** and the Prefill/Union roles send ordinary completion requests.

**NPU load sampling**: Use `npu-smi info watch -s u` to collect **AI Cube usage**. Before starting virtual inference, `npu-smi info watch -h` is used to check whether the help contains `u - AI Cube Usage`; if the current HDK does not support this metric, Engine Server automatically disables virtual inference.

**Dynamic probing interval**:

| Peak AI Cube Usage (5-second Sampling Window) | Next Interval |
|-----------------------------------|------------|
| ≥ 80% | 20 seconds |
| < `npu_usage_threshold` | 5 seconds (default) |
| `[npu_usage_threshold, 80%)` | Keep the current interval unchanged |

**Abnormality determination**: When the peak AI Cube usage is lower than `npu_usage_threshold` and the virtual inference request fails, the consecutive failure count is accumulated; after reaching `max_failure_count`, `GET /status` returns `abnormal` and the virtual inference loop stops. After Node Manager's `HeartbeatManager` receives `abnormal` for 5 consecutive times, it triggers self-termination rescheduling.

**vLLM metric filtering (v0.18+)**: When virtual inference is enabled, Engine Server patches vLLM `OutputProcessor._update_stats_from_finished` to skip virtual inference requests whose `external_req_id` contains the `_virtual` suffix (corresponding to the virtual inference `X-Request-Id: {timestamp}_virtual`) before writing per-request metrics. Only per-request metrics such as `request_success_total` are filtered; iteration-level counters such as `prompt_tokens`/`generation_tokens` are still accumulated.

## Configuration Description

**Configuration example** (items not configured use the following default values):

```json
"health_check_config": {
  "enable_virtual_inference": false,
  "npu_usage_threshold": 3,
  "max_failure_count": 6,
  "health_collector_timeout": 5,
  "health_collector_timeout_retry_attempts": 3
}
```

| Configuration Item | Type | Default Value | Description |
|--------|------|--------|------|
| enable_virtual_inference | bool | `false` | Master switch for virtual inference. Supported only for vLLM; when configured as `true` for SGLang, it is automatically disabled at runtime. Allowed only under the ERROR log level (`ASCEND_GLOBAL_LOG_LEVEL=3`, which is the default if not set). If explicitly configured to a non-ERROR level, it will be disabled before the Engine Server starts virtual inference. |
| npu_usage_threshold | int | `3` | AI Cube usage threshold (%). |
| max_failure_count | int | `6` | Maximum number of consecutive virtual inference failures. |
| health_collector_timeout | int | `5` | Timeout for probing the inference surface `/health` (seconds). |
| health_collector_timeout_retry_attempts | int | `3` | Number of retries for the inference surface `/health` timeout (including the first attempt; triggered only on timeout). |

For complete field descriptions, see [Configuration Reference health_check_config](../configuration/config_reference.md#health_check_config).

## Enabling Method

In the `user_config.json` of the PD disaggregation deployment, set `health_check_config.enable_virtual_inference` of the Prefill and Decode engine configurations to `true`, and adjust `npu_usage_threshold` and `max_failure_count` according to your service. Virtual inference requires the ERROR log level: when `ASCEND_GLOBAL_LOG_LEVEL` is not configured, the default is ERROR; if the engine process environment explicitly configures it to a value other than `3`, the current virtual inference ends before Engine Server starts virtual inference. For configuration examples and field descriptions, see [PD Disaggregation Deployment](../deployment/k8s/pd_disaggregation_deployment.md#virtual-inference-health-check).
