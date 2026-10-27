# Virtual Inference Health Probe

## Feature Introduction

Virtual inference is used to periodically and proactively send lightweight requests to the inference plane, and to determine whether the inference engine is available based on the NPU AI Cube usage (scenarios where the engine process is alive and `/health` is normal but inference cannot actually be performed, such as silent faults including NPU hang and driver exceptions).

On the native launch path, virtual inference is executed directly by Node Manager (`motor/node_manager/core/services/native_engine/virtual_inference/`) and no longer depends on the mgmt surface of Engine Server:

- Each Node Manager instance maintains a single virtual inference monitor (vLLM only). The monitor is initially bound to DP0 and automatically migrates to the new DP Master after a DP scale-in is committed. It periodically probes `GET /health` of the native engine and idempotently starts the virtual inference loop after the current target is observed as READY for the first time.

- Virtual inference requests are sent directly to the inference plane of the vLLM engine through `POST /v1/completions`.

- When virtual inference reaches the failure threshold, only the endpoint presents `UNHEALTHY`/`ABNORMAL`. The process is not terminated and the engine is not restarted.

- `HeartbeatManager` maps the runtime `UNHEALTHY` status to the heartbeat `ABNORMAL` status. After the daemon observes abnormal for 5 consecutive times, Pod self-termination rescheduling is triggered (k8s restarts the container). Virtual inference itself does not kill the process directly; if engine relaunch is enabled, the Controller may also receive abnormal reports and trigger an in-place restart of the engine.

Benefits of using the virtual inference feature:

- Detect silent faults: covers scenarios where the engine process is alive and `/health` is normal but the NPU hangs or the driver is abnormal, so that the unavailable state of the inference engine is detected in a timely manner.

- Reduce O&M overhead: engine exceptions are automatically detected without manual intervention, and node self-healing is implemented through heartbeat reporting.

- Reduce troubleshooting time: shortens the fault discovery time and prevents invalid traffic from being scheduled to a faulty engine.

### Working Principle

The virtual inference loop starts after the native `/health` is READY for the first time (`NativeEngineService.runtime_state()` merges the two). At startup, `npu-smi info watch -h` is also used to check whether the HDK supports AI Cube Usage; if not, virtual inference is automatically disabled. Virtual inference requests use two timeouts:

- warmup (first) request: fixed at 180 seconds (keeps the previous behavior and is independent of the configuration described below);

- periodic requests: use `health_check_config.virtual_inference_timeout` (default 5 seconds), which takes effect only for vLLM and is user-configurable.

**vLLM engine**: sends `POST /v1/completions` to the inference plane, with the request body being `prompt: "1"` and `max_tokens: 1`, and `X-Request-Id` carrying the `{microsecond timestamp}_virtual` identifier (for example, `1693123456789123_virtual`). vLLM layerwise decode (`dispatch_profile=trigger`) additionally carries `kv_transfer_params.do_virtual: true` and PD disaggregation-related fields; handoff decode and the Prefill/Union roles send ordinary completion requests.

**SGLang engine**: when Node Manager starts SGLang, it explicitly writes the `SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION` environment variable into the startup command env and keeps it permanently `true` (it is not affected by an environment variable with the same name outside the container, nor by `enable_virtual_inference`), so that the `/health` of SGLang uses its own generative health check, which automatically performs virtual request verification. Motor does not initiate any virtual inference requests to SGLang. The SGLang heartbeat timeout uses `health_collector_timeout`/`health_collector_timeout_retry_attempts` and does not use `virtual_inference_timeout`.

**NPU load sampling**: use `npu-smi info watch -s u` to collect the AI Cube usage (the peak value within a 5-second sampling window is used).

Dynamic probing interval:

| Peak AI Cube Usage (5-Second Sampling Window) | Next Interval       |
| -------------------------------- | ---------------- |
| ≥ 80%                           | 20 seconds             |
| <`npu_usage_threshold`         | 5 seconds (default)      |
| `[npu_usage_threshold, 80%)`   | Keep the current interval unchanged |

**Abnormality determination (vLLM)**:

* When the peak AI Cube usage is lower than `npu_usage_threshold` and the virtual inference request fails, the `/metrics` of the same engine is further read. The consecutive failure count is accumulated only when `vllm:num_requests_waiting_by_reason{reason="capacity"}` is available and the sum of all matching samples is 0; after reaching `max_failure_count`, the runtime state of the endpoint is downgraded from `READY` to `UNHEALTHY`, the heartbeat reports `ABNORMAL`, and the virtual inference loop stops.

* A `capacity` waiting count greater than 0 indicates that requests are queued because the scheduling capacity (including the KV Cache capacity) is insufficient. In this case, a virtual inference timeout does not indicate that the engine is down, and the failure count is not accumulated. The failure count is also not accumulated when the metrics request fails, the metric is missing, or the value is invalid; in these cases, the existing failure count is retained and reset only after a subsequent probe succeeds or the AI Cube usage recovers above the threshold.

* If the active probe fails but AI Cube ≥ threshold, the engine is considered busy and the failure count is not accumulated; the failure count is also not accumulated when AI Cube sampling is unavailable. Virtual inference changes only the status reporting and does not trigger a process restart or Pod-level recovery.

* If an unexpected exception occurs in the monitoring loop itself, only a log is recorded and a backoff retry is performed. The engine is not determined to be abnormal on this basis (the failure count is not accumulated and the state is not downgraded to `UNHEALTHY`).

### Core Functions

The virtual inference health probe provides the following core functions:

- **Active health probing**: sends lightweight `POST /v1/completions` requests to the inference plane during low business load, and determines whether the inference engine is available based on the NPU AI Cube usage.

- **Silent fault detection**: covers fault scenarios that cannot be detected by conventional health checks, such as the engine process being alive and `/health` being normal while the NPU hangs or the driver is abnormal.

- **Dynamic probing interval**: automatically adjusts the probing frequency based on the peak AI Cube usage, reducing the probing frequency under high load and increasing it under low load.

- **Abnormal status reporting**: after the failure threshold is reached, downgrades the runtime state of the endpoint to `UNHEALTHY` and reports `ABNORMAL` through the heartbeat, working with `HeartbeatManager` to implement node self-healing.

- **SGLang generative health check**: for the SGLang engine, enables its own generative health check through the `SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION` environment variable; Motor does not initiate virtual inference requests.

### Constraints and Limitations

| Dimension     | Description                                                                       |
| -------- | ------------------------------------------------------------------------------ |
| Hardware     | <ul><li>Atlas 800I A2</li><li>Atlas 800I A3</li><li>Ascend950PR&950DT products</li></ul>               |
| Deployment scenario | <ul><li>PD disaggregation service deployment</li><li>PD aggregation service deployment</li></ul>                                               |
| Engine     | Only the vLLM engine is supported.                                                         |
| Feature mutual exclusion | None                                                                             |
| Software dependency | HDK 26.0.RC1 and later versions. The AI Cube Usage metric provided by `npu-smi info watch -s u` is required. |
| Other constraints | The `ASCEND_GLOBAL_LOG_LEVEL` log level must be set to ERROR.                           |

## Using the Feature

### Usage Scenarios

The virtual inference health probe applies to the following scenarios:

- PD disaggregation deployment scenarios and PD aggregation deployment scenarios: enable virtual inference in the Prefill, Decode, and Union engine configurations to detect silent NPU faults.

- Low business load periods: proactively send lightweight requests during low business load to avoid affecting normal inference requests.

- High business load periods: increase the request interval during high business load to reduce the impact on service performance.

- High availability deployment scenarios: require rapid detection of engine exceptions and node self-healing through heartbeat reporting.

### Usage Example

1. Configure the `user_config.json` file.
   Enable virtual inference in the `health_check_config` sub-module of the corresponding engine configuration: use `motor_engine_prefill_config`/`motor_engine_decode_config` for PD disaggregation scenarios, and `motor_engine_union_config` for PD aggregation scenarios. Set `enable_virtual_inference` to `true` and adjust the other parameters as required by your service. An example is as follows.

   ```json
   "health_check_config": {
     "enable_virtual_inference": true,
     "npu_usage_threshold": 3,
     "max_failure_count": 6,
     "virtual_inference_timeout": 5.0,
     "health_collector_timeout": 5,
     "health_collector_timeout_retry_attempts": 3
   }
   ```

   **Configuration Parameter Description**

   | Configuration Item                                  | Type  | Value Range     | Mandatory/Optional | Default Value | Description                                                                                                                                                                                                                                                                                    |
   | --------------------------------------- | ----- | ------------ | --------- | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
   | enable_virtual_inference                | bool  | true / false | Optional      | false  | Switch for Motor active virtual inference. Only enables or disables Motor virtual inference for vLLM; `false` does not disable the native generative `/health` of SGLang (the env is always true at startup). Takes effect only for DP rank 0 and non-headless vLLM endpoints. In addition, the final engine environment `ASCEND_GLOBAL_LOG_LEVEL` must be ERROR (the default is ERROR if not set); if it is explicitly non-ERROR, NodeManager does not create the monitor and prints a warning |
   | npu_usage_threshold                     | int   | 1–100       | Optional      | 3      | AI Cube usage threshold (%); used only by vLLM Motor virtual inference                                                                                                                                                                                                                                        |
   | max_failure_count                       | int   | ≥ 1         | Optional      | 6      | Upper limit of consecutive virtual inference failures; used only by vLLM Motor virtual inference                                                                                                                                                                                                                                      |
   | virtual_inference_timeout               | float | > 0          | Optional      | 5.0    | Client timeout (seconds) of periodic active virtual inference requests. It must be a positive number and takes effect only for vLLM Motor virtual inference; the first warmup request is fixed at 180 seconds and is not affected by this configuration. The configuration is retained for compatibility, and SGLang ignores this field                                                                                                                               |
   | health_collector_timeout                | int   | > 0          | Optional      | 5      | Timeout (seconds) for probing the inference plane `GET /health`; used by both vLLM and SGLang heartbeats                                                                                                                                                                                                                             |
   | health_collector_timeout_retry_attempts | int   | ≥ 1          | Optional      | 3      | Number of retries for the inference plane `GET /health` timeout (including the first attempt; triggered only on timeout); used by both vLLM and SGLang heartbeats                                                                                                                                                                                                         |

   For complete field descriptions, see [Configuration Reference health_check_config](../configuration/config_reference.md#health_check_config).

2. On the native launch path, virtual inference is executed and started by Node Manager.
   After the configuration is enabled, NodeManager idempotently starts the virtual inference loop after detecting that the `/health` of the vLLM engine is READY for the first time.
   In the Node Manager container/Pod logs, the appearance of the following keywords indicates that virtual inference has started properly.

   ```text
   AI Cube usage check thread started for endpoint 0
   Virtual inference started for endpoint 0, first virtual request warmup timeout is 180s, ...
   Virtual inference warmup in progress for endpoint 0, request timeout is 180 seconds
   Virtual inference warmup completed successfully for endpoint 0
   ```

   When the engine is determined to be abnormal, the following logs can be observed.

   ```text
   WARNING: Reach maximum failure count for endpoint 0, set abnormal status
   WARNING: Virtual inference marked endpoint 0 abnormal
   INFO: Native engine rank 0, status change from normal to abnormal
   ERROR: Reached 5 consecutive abnormal endpoint observations, setting suicide flag for main to handle (k8s pod restart)
   ```

### Verification

#### Verification Method 1: Check the AI Cube Usage

Use the `npu-smi` command to view the AI Cube usage and confirm that the HDK version supports virtual inference:

```bash
npu-smi info watch -s u
```

The expected output is in table form, and the last column is the AI Cube usage percentage (`u - AI Cube Usage` in `npu-smi info watch -h`). After virtual inference is started, the usage may fluctuate periodically.

#### Verification Method 2: Check the Virtual Inference Request Logs

Check the Node Manager container/Pod logs to confirm that the virtual inference loop is running properly:

```bash
kubectl logs <node-manager-pod> | grep -E "Virtual inference|AI Cube usage|marked endpoint.*abnormal|status change from"
```

Example output:

```text
INFO: AI Cube usage rate: 2%, virtual request: successful (endpoint 0)
INFO: AI Cube usage is below 3%, virtual inference sleeps default 5s for endpoint 0
WARNING: AI Cube usage (2%) < threshold (3%), virtual request failed, and no request is waiting for capacity on endpoint 0
WARNING: Current failure count: 1/6 for endpoint 0
```

The details of virtual inference HTTP requests (including `X-Request-Id`) are output only at the DEBUG level. If needed, temporarily raise the Node Manager log level and then search for `_virtual`.

## FAQs

### Virtual Inference Does Not Take Effect After Being Enabled

**Symptom**

After `enable_virtual_inference: true` is configured, virtual inference is not started properly.

**Cause Analysis**

1. The engine type is not vLLM (virtual inference supports only vLLM, and SGLang uses its own generative health check).

2. The endpoint is not DP rank 0 or is a headless endpoint.

3. `ASCEND_GLOBAL_LOG_LEVEL` is explicitly set to a non-ERROR value.

4. `npu-smi` does not support the AI Cube Usage metric (the HDK version is earlier than 26.0.RC1).

5. The `/health` of the engine has not yet returned READY.

**Solution**

1. Confirm that the engine type is vLLM.

2. Confirm that the endpoint is DP rank 0 and is not headless.

3. Check the `ASCEND_GLOBAL_LOG_LEVEL` environment variable and ensure that it is ERROR or is not set.

4. Use `npu-smi info watch -h` to check whether the HDK supports AI Cube Usage.

5. Check the engine logs and confirm that `/health` has returned READY.

### Virtual Inference Causes Excessive Engine Logs

**Symptom**

After virtual inference is enabled, a large number of virtual inference request records appear in the engine logs.

**Cause Analysis**

Virtual inference requests carry the `X-Request-Id: {microsecond timestamp}_virtual` identifier, and the vLLM engine records these request logs.

**Solution**

Reduce the log output by adjusting the engine log level, or ignore requests with the `_virtual` identifier through log filtering rules.
