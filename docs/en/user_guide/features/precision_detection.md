# Precision Detection

## Feature Introduction

The precision detection feature is used to discover token-level output quality anomalies that occur during inference on Decode instances, such as repeated large segments, garbled text, and rare-character anomalies. After it is enabled, the Coordinator injects `logprobs`, `top_logprobs`, and `return_token_ids` into Decode requests, collects token IDs and logprobs from inference responses, and then periodically submits them for checking by instance group dimension.

When the same instance group is detected as anomalous for multiple consecutive times, the Coordinator initiates a fixed Q&A probe against the target instance group through the internal Router. After the probe is completed, the Coordinator reports a precision anomaly alarm to the Controller. If automatic recovery is enabled on the Controller side, the Controller terminates the Decode instances in the alarm and, when the alarm carries a Prefill instance ID, synchronously terminates the Prefill instance.

## Applicable Scenarios

| Dimension | Description |
|------|------|
| Deployment form | PD disaggregation, CDP/Hybrid, and other deployment forms in which Decode requests are forwarded through the Coordinator Router |
| Detection target | The token sequence output by Decode and the corresponding logprob |
| Detection granularity | PD instance group; PD disaggregation uses `(p_instance_id, d_instance_id)`, and the Hybrid scenario uses `(None, union_id)` |
| Anomaly type | `logprobs_count=1` supports large segment repetition; `>=3` additionally supports garbled text; `>=5` additionally supports rare characters |
| Handling method | The Coordinator reports an alarm; the Controller can optionally automatically terminate the D/P instance |

**Not applicable to:**

- Direct inference requests that do not pass through the Coordinator Router.

- Inference engines that do not support `return_token_ids`, `logprobs`, or `top_logprobs`.

- Production environments where the `msprobe` runtime dependency is not installed.

## Configuration Description

Precision detection consists of the detection switch on the Coordinator side and the automatic recovery switch on the Controller side. The two are independent of each other: when only the Coordinator switch is enabled, anomalies are detected and alarms are reported; only when automatic recovery is also enabled on the Controller will the Controller terminate instances based on precision alarms.

### Coordinator Configuration

Add `precision_detection_config` to `motor_coordinator_config` in `user_config.json`:

```json
{
  "motor_coordinator_config": {
    "precision_detection_config": {
      "precision_check_enabled": true,
      "interval_seconds": 30.0,
      "logprobs_count": 5,
      "precision_issue_threshold": 10,
      "precision_clear_threshold": 10,
      "probe_max_attempts": 3,
      "probe_timeout_seconds": 600.0
    }
  }
}
```

| Configuration Item | Default Value | Description |
|--------|--------|------|
| `precision_check_enabled` | `false` | Master switch for precision detection. When disabled, no logprobs are injected, no sampling is performed, and no detection is run, resulting in zero additional performance overhead. |
| `interval_seconds` | `30.0` | Minimum interval, in seconds, at which each instance group is allowed to submit one complete request sample for detection. |
| `logprobs_count` | `1` | Top-k width injected into Decode requests. A larger value provides stronger detection capability but also incurs higher engine-side overhead. |
| `precision_issue_threshold` | `10` | Number of consecutive detected anomalies in the same instance group required to trigger a probe and an alarm. |
| `precision_clear_threshold` | `10` | Number of consecutive valid normal samples required under an active alarm to report a clear alarm. |
| `probe_max_attempts` | `3` | Number of precision probe requests. |
| `probe_timeout_seconds` | `600.0` | Timeout for a single probe, in seconds. |

### Controller Configuration

To automatically terminate an instance after a precision alarm is received, enable the following in the Controller configuration:

```json
{
  "motor_controller_config": {
    "precision_auto_recovery_enabled": true
  }
}
```

| Configuration Item | Default Value | Description |
|--------|--------|------|
| `precision_auto_recovery_enabled` | `false` | Whether the Controller automatically terminates the D/P instance in the alarm after receiving a precision alarm with `alarm_id=0xFC001009` |

## Deployment Process

1. Confirm that the inference engine supports the `return_token_ids` and `logprobs` return fields.

2. Confirm that `msprobe` is installed in the runtime environment and that `msprobe.response_anomaly.detector.ILLDetector` can be imported.

3. Enable `precision_detection_config.precision_check_enabled` in the Coordinator configuration.

4. Enable `precision_auto_recovery_enabled` in the Controller configuration as needed.

5. Redeploy the service using the existing deploy script.

```bash
cd examples/deployer
python deploy.py --config_dir ../infer_engines/vllm
```

## Operating Mechanism

After the feature is enabled, the request processing pipeline of the Coordinator changes as follows:

1. The Router injects `logprobs`, `top_logprobs`, and `return_token_ids` into each Decode request.

2. The Router caches `prompt_token_ids`, `output_token_ids`, `logprobs`, and `topk_logprobs` from streaming or non-streaming responses.

3. After the request is completed, `SampleController` performs instance-group-level egress gating through Scheduler ZMQ. For the same instance group, at most one sample is submitted for detection every `interval_seconds`.

4. `PrecisionReporter` invokes `MsprobeChecker` to detect the sample, and records the number of consecutive anomalies across Workers through the Scheduler.

5. After the number of consecutive anomalies reaches `precision_issue_threshold`, `InternalRouterProbe` performs a fixed Q&A probe on the target instance group.

6. `PrecisionAlarm` constructs a precision anomaly alarm and reports it to the Controller.

7. The Controller decides whether to invoke the recovery service to terminate the instance based on `precision_auto_recovery_enabled`. After the termination succeeds, the Controller reports CLEAR and notifies the Coordinator to clear the Scheduler active state.

8. Under an active alarm, after the Coordinator performs **valid normal** detection for `precision_clear_threshold` consecutive times, it automatically reports CLEAR to clear the alarm (independent of auto-recovery, applicable to scenarios where automatic recovery is disabled or the alarm remains active).

9. If CCAE Reporter is deployed, the Reporter on the Controller side calls the existing Controller interface `/controller/terminate_instance` to terminate the D instance. The request body can carry `p_instance_id` and `precision_alarm_clear=true`, so that the Controller additionally clears the precision alarm after terminating the P/D instance group, and continues to successfully report `controlStatus=Completed` to CCAE 10 times before stopping the reporting of this precision task.

## Verification Method

After the service starts, the following keywords appearing in the Coordinator log indicate that the precision detection link is enabled:

```text
Precision check (token sampling): interval=...
exit_gate=scheduler_zmq streak=scheduler_zmq probe=internal_router
```

After sending a normal inference request, you can observe the sampling link in the Coordinator log:

| Log Keyword | Meaning |
|------------|------|
| `PrecisionSample: inject_logprobs` | The Router has injected sampling parameters into the Decode request. |
| `SampleController: confirmed (scheduler)` | The Scheduler has released one sample of this instance group. |
| `PrecisionSample: submit` | The sample has been constructed and submitted to the detection link. |
| `MsprobeChecker: result` | msprobe has returned the detection result. |
| `PrecisionReporter: threshold reached` | Consecutive anomalies have reached the threshold, and probe and alarm begin. |
| `PrecisionAlarm: reporting alarm_id=0xFC001009` | The precision alarm has been reported to the Controller. |
| `Precision auto-recover: terminating D instance_id=` | The Controller has triggered auto-recovery. |

## Limitations and Constraints

1. After `precision_check_enabled=true`, logprobs are injected into every Decode request; the frequency at which requests actually enter the detection pipeline is controlled by `interval_seconds`.

2. When the Scheduler client is unavailable, the Coordinator sets `sampling_manager` to `None` during startup, and the complete sampling pipeline is not enabled for this process.

3. Fail-open is adopted when an anomaly is detected. Scenarios such as msprobe execution failure, misalignment between top-k and token counts, and Scheduler ZMQ failure do not interrupt user requests, nor do they falsely trigger recovery.

4. The precision probe uses the fixed question "Who invented the theory of relativity?" A single probe is considered passed only when the response contains "Einstein".

5. Automatic recovery is controlled solely by `precision_auto_recovery_enabled` on the Controller side and does not depend on the observability alarm display switch.

## Logs and Troubleshooting

| Symptom | Possible Cause | Suggestion |
|------|----------|------|
| No `Precision check` log after startup | `precision_check_enabled=false` or the configuration is not loaded | Check `motor_coordinator_config.precision_detection_config` |
| The log indicates Scheduler client unavailable | The Coordinator is not connected to the Scheduler | Check the Scheduler process and the ZMQ connection |
| `sample incomplete` | The engine returns token_ids but does not return logprobs | Check whether the engine supports the logprobs parameter |
| `MsprobeChecker: msprobe not installed` | msprobe is missing from the environment | Install msprobe or explicitly inject a mock checker in the test |
| Rare characters are never detected | `logprobs_count < 5` or the token2category mapping is missing | Increase `logprobs_count` and check the msprobe mapping file |
| An alarm is raised but the instance is not terminated | Auto-recovery is not enabled on the Controller | Check `precision_auto_recovery_enabled` |
| Only D is terminated but P is not | `p_instance_id` is empty in the alarm | Check whether the deployment mode and the instance group key can resolve the P instance |
