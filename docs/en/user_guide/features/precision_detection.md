# Precision Detection

## Feature Introduction

The precision detection feature is used to detect token-level output quality anomalies that occur during inference on Decode instances, such as large blocks of repetition, garbled text, and rare-character anomalies.

### Working Principle

After the precision detection function is enabled, the Coordinator preempts the sampling window of a Decode instance after selecting it, and injects logprobs, top_logprobs, and return_token_ids only into requests that successfully preempt the window. After the response is complete, the collected token IDs and logprobs enter background detection; the detection results are accumulated by the final P/D instance group.

When the same instance group is detected as anomalous for multiple consecutive times, the Coordinator initiates a fixed Q&A probe against the target instance group through the internal Router. After the probe is completed, the Coordinator reports a precision anomaly alarm to the Controller. If automatic recovery is enabled on the Controller side, the Controller terminates the Decode instances in the alarm and, when the alarm carries a Prefill instance ID, synchronously terminates the Prefill instance.

The request processing chain of the Coordinator changes as follows:

1. After the Router selects a Decode instance, the SampleController atomically preempts the sampling window for that Decode instance through Scheduler ZMQ.

2. Only requests that successfully preempt the window are injected with logprobs, top_logprobs, and return_token_ids; each Decode instance is injected at most once every interval_seconds.

3. The Router caches the complete prompt_token_ids, output_token_ids, logprobs, and topk_logprobs from streaming or non-streaming responses, and restores the original logprobs count before returning the response to the user: the logprobs are deleted if the user did not request them, trimmed if the user requested fewer, and not reduced if the user requested more.

4. After the user response ends, the complete sample enters a tracked background task; PrecisionReporter and MsprobeChecker do not block the user response or TPOT.

5. PrecisionReporter records the number of consecutive anomalies across Workers by (p_instance_id, d_instance_id) through the Scheduler; the Hybrid scenario uses (None, union_id).

6. After the number of consecutive anomalies reaches precision_issue_threshold, InternalRouterProbe performs a fixed Q&A probe on the target instance group.

7. PrecisionAlarm constructs a precision anomaly alarm and reports it to the Controller.

8. The Controller decides whether to call the recovery service to terminate the instance according to precision_auto_recovery_enabled; after the termination succeeds, the Controller reports CLEAR and notifies the Coordinator to clear the Scheduler activity state.

9. Under an active alarm, the Coordinator automatically reports CLEAR to clear the alarm after precision_clear_threshold consecutive valid normal detections (independent of auto-recovery, applicable when automatic recovery is disabled or the alarm is still active).

10. If the CCAE Reporter is deployed, the Reporter on the Controller side calls the existing Controller interface /controller/terminate_instance to terminate the D instance; the request body can carry p_instance_id and precision_alarm_clear=true, so that the Controller appends the precision alarm clearing after terminating the P/D instance group and continues to report controlStatus=Completed successfully to CCAE 10 times before stopping the reporting of this precision task.

### Constraints and Limitations

The following runtime constraints are summarized based on the current implementation. The verification scope of hardware, engine versions, and feature combinations must be confirmed by test results before release.

| Constraint Dimension | Requirement |
|----------|------|
| Hardware | <ul><li>Atlas 800I A2</li><li>Atlas 800I A3</li><li>Ascend950 PR&950 DT products</li></ul> |
| Deployment scenario | <ul><li>**Only** deployment forms in which Decode requests are forwarded through the Coordinator Router are **supported**, such as PD disaggregation and CDP/Hybrid;</li><li>Direct inference requests that do not pass through the Coordinator Router are **not supported**.</li></ul> |
| Engine | Currently only the vLLM engine is supported. `motor_engine_*_config.engine_type` in `user_config.json` must be `vllm`; the engine must accept the `logprobs`, `top_logprobs`, and `return_token_ids` request parameters, and return token IDs and logprobs that can be aligned in the response. |
| Feature mutual exclusion | When the user actively requests logprobs, sampling still forces return_tokens_as_token_ids=true, which may change the representation of tokens; clients that depend on original text tokens must verify compatibility. |
| Software dependency | msprobe must be installed in the runtime environment, and msprobe.response_anomaly.detector.ILLDetector must be importable. |
| Other restrictions | <ul><li>After precision_check_enabled=true is set, logprobs are injected only into Decode requests that successfully preempt the sampling window; a failed request also consumes the window, and the window is not rolled back or transferred.</li><li>When the Scheduler client is unavailable, the Coordinator sets sampling_manager to None during startup, and the complete sampling chain is not enabled in the current process.</li><li>Fail-open is used when a detection is abnormal: scenarios such as msprobe execution failures, misalignment between the top-k and the number of tokens, and Scheduler ZMQ failures do not interrupt user requests or mistakenly trigger recovery.</li><li>The precision probe uses the fixed question "Who invented the theory of relativity", and a single probe is considered passed only when the response contains "Einstein".</li><li>Automatic recovery is controlled only by precision_auto_recovery_enabled on the Controller side and does not depend on the observability alarm display switch.</li><li>This version only ensures that the number of logprobs requested by the user is not changed by the additional sampling of Motor. Sampling requests still force return_tokens_as_token_ids=true, so when the user actively requests logprobs, the token representation may differ from that of unsampled requests.</li><li>The precision detection object is assembled at process startup; after modifying the nested precision_detection_config, the Coordinator Worker must be restarted to ensure that all fields take effect.</li></ul> |

## Feature Usage

### Usage Scenarios

| Dimension | Description |
|------|------|
| Deployment form | Deployment forms in which Decode requests are forwarded through the Coordinator Router, such as PD disaggregation. |
| Detection target | The token sequence output by Decode and the corresponding logprob. |
| Detection granularity | PD instance group; PD disaggregation uses (p_instance_id, d_instance_id), and the Hybrid scenario uses (None, union_id) |
| Anomaly type | logprobs_count=1 supports large segment repetition; 3 or greater additionally supports garbled text; 5 or greater additionally supports rare characters. |
| Handling method | The Coordinator reports an alarm, and the Controller can optionally automatically terminate the D/P instance. |

### Environment Preparation

msprobe is **not in the dependency list of the Motor package** (it is not declared in `requirements.txt`), so whether it needs to be installed separately depends on the runtime environment:

<ul><li>**Using the Motor image provided by the repository** (the Dockerfile under `docker/mindie-motor-vllm/`): the image has msprobe preinstalled with the version pinned to `mindstudio-probe==26.1.0.post1`, and no separate installation is required.</li><li>**Installing only the Motor package (wheel) in your own environment**: the environment needs a matching msprobe installed by yourself, and the version should be consistent with that used by the released image.</li></ul>

Confirm that it can be imported in the container or Python environment where the Coordinator Worker resides:

```bash
python -c "from msprobe.response_anomaly.detector import ILLDetector; print('ILLDetector import OK')"
```

If you need to install it yourself:

```bash
python -m pip install "mindstudio-probe==26.1.0.post1"
```

In offline environments, you can use a wheel that has been obtained and whose version has been verified. For detailed installation steps, see [msProbe Installation Guide](https://gitcode.com/Ascend/msprobe/blob/26.2.0/docs/en/install_guide/msprobe_install_guide.md).

After installation, you also need to check the `response_anomaly/configs/config.yaml`, `configs/mtype_config.json`, and `token2category` provided with this version, as well as the mapping corresponding to the target model. An `import OK` alone does not prove that the target model can be detected normally; continue with the real request verification described below. For detailed input requirements, see [msProbe Inference Anomaly Detection Description](https://www.hiascend.com/document/detail/en/mindstudio/2610/TITools/msProbe/docs/en/user_guide/response_anomaly_instruct.md).

### Usage Example

1. Confirm that the inference engine supports the `return_token_ids` and `logprobs` response fields.

   Send a request with sampling parameters to the inference entry and check whether the response contains token IDs and logprobs:

   ```bash
   curl -X POST "http://<service IP>:<service port>/v1/chat/completions" \
     -H "Content-Type: application/json" \
     -d '{
       "model": "<model-name>",
       "messages": [{"role": "user", "content": "hello"}],
       "max_tokens": 16,
       "logprobs": true,
       "return_token_ids": true
     }'
   ```

   If `choices[0].logprobs` in the response JSON is not empty and contains token ID related fields, the vLLM engine capability meets the requirements. If the fields are missing, upgrade the engine or adjust the engine configuration first.

2. Add precision_detection_config to motor_coordinator_config in user_config.json to enable the precision detection function.

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

   The following fields are all optional; the default values are used when they are omitted. When detection is enabled, `precision_check_enabled=true` must be set explicitly. Counting parameters are configured as integers, and time parameters support positive integers or positive floating-point numbers.

   | Configuration Item | Type | Value Range | Mandatory | Default Value | Description |
   |--------|------|----------|------|--------|------|
   | precision_check_enabled | boolean | true / false | No | false | Master switch of precision detection. When it is disabled, no additional sampling injection or detection is performed; the user's own logprobs requests are not affected |
   | interval_seconds | number | > 0 | No | 30.0 | Minimum interval, in seconds, at which one request injected with sampling parameters is allowed for each Decode instance; different P/D groups sharing the same D share the window |
   | logprobs_count | integer | ≥ 1, and not exceeding the logprobs upper limit allowed by the engine | No | 1 | Top-k width injected into Decode requests; if the user requests a larger width, the user's width is retained |
   | precision_issue_threshold | integer | ≥ 1 | No | 10 | The probe and alarm are triggered after the number of consecutive detected anomalies of the same instance group reaches this value |
   | precision_clear_threshold | integer | ≥ 1 | No | 10 | The alarm clearing is reported after the number of consecutive valid normal samples reaches this value under an active alarm |
   | probe_max_attempts | integer | ≥ 1 | No | 3 | Number of precision probe attempts |
   | probe_timeout_seconds | number | > 0 | No | 600.0 | Timeout of a single probe, in seconds |

3. (Optional) If instances need to be terminated based on precision alarms, enable the precision_auto_recovery_enabled configuration in the Controller configuration.

   ```json
   {
     "motor_controller_config": {
       "precision_auto_recovery_enabled": true
     }
   }
   ```

   | Configuration Item | Type | Value Range | Mandatory | Default Value | Description |
   |--------|------|----------|------|--------|------|
   | precision_auto_recovery_enabled | boolean | true / false | No | false | Whether the Controller automatically terminates the D/P instances in the alarm after receiving a precision alarm with `alarm_id=0xFC001009` |

   >[!NOTE]NOTE
   >Precision detection consists of the detection switch on the Coordinator side and the automatic recovery switch on the Controller side, which are independent of each other; when only the Coordinator switch is enabled, anomalies are detected and alarms are reported; the Controller terminates instances based on precision alarms only when automatic recovery is enabled on the Controller side at the same time.

4. Redeploy the service with the existing deploy script:

   ```bash
   cd examples/deployer
   python deploy.py --config_dir ../../model_configs/vllm
   ```

   The appearance of the Precision check keyword in the Coordinator logs indicates that the precision detection chain is enabled.

### Verification

After the service is started, the appearance of the following keywords in the Coordinator logs indicates that the precision detection chain is enabled:

```text
Precision check (token sampling): interval=...
entry_admission=scheduler_zmq streak=scheduler_zmq probe=internal_router
```

This log is **a single line** and is shown here wrapped by field; the `logprobs_count`, `threshold`, `probe_attempts`, and `probe_timeout` fields after `interval` are omitted.

The startup log only indicates that the chain has been assembled, not that msprobe has successfully performed detection. Send a normal inference request using the deployed model, wait for the complete response to end, and check `MsprobeChecker: result`; if `fail-open`, a missing dependency, or a mapping error occurs, resolve the corresponding problem first.

`SampleController: claimed` and `PrecisionSample: submit` are output only at the DEBUG level; `PrecisionSample: inject_logprobs` is output at the INFO level when the width requested by the client is changed, and at the DEBUG level when the width is unchanged. To see complete injection records, set `"log_level"` to `"DEBUG"` in `motor_coordinator_config.logging_config`; with the default INFO level, the absence of `claimed` and `submit` cannot be used to determine that the function has failed.

| Log Keyword | Meaning |
|------------|------|
| PrecisionSample: inject_logprobs | The Router has injected sampling parameters into the Decode request |
| SampleController: claimed (scheduler) | The Scheduler has admitted one sampling request for this Decode instance (DEBUG) |
| PrecisionSample: submit | The sample has been constructed and submitted to background detection (DEBUG) |
| MsprobeChecker: result | msprobe has returned the detection result |
| PrecisionReporter: threshold reached | The number of consecutive anomalies reaches the threshold, and the probe and alarm start |
| PrecisionAlarm: reporting alarm_id=0xFC001009 | The precision alarm has been reported to the Controller |
| Precision recovery: terminating D instance_id= | The Controller has triggered automatic recovery and is terminating the Decode instance (terminating P instance_id= is printed when terminating Prefill) |

## Tuning Suggestions

The following are configuration starting points for initial enablement. Actual thresholds should be adjusted based on the load, the model, and the acceptable detection overhead; they are not a guarantee of performance or detection rate.

| Key Parameter | Recommended Value | Description |
|----------|----------|------|
| logprobs_count | Start from 1 if only repetition is detected; use 3 if garbled text detection is required; use 5 if rare character detection is required | The detection capability also depends on the top-k actually returned by the engine and the model mapping; increasing the width increases the overhead of sampling requests |
| interval_seconds | Use the default 30.0 seconds first | Controls the entry sampling frequency per D instance; a smaller value increases the overhead, and a larger value prolongs the time to find anomalies |
| precision_issue_threshold | Use the default 10 first | A smaller value triggers the probe faster but is also more susceptible to short-term anomalies; it is accumulated by P/D group, and the threshold multiplied by the sampling interval cannot be regarded as a definite detection latency |
| precision_clear_threshold | Use the default 10 first | A smaller value clears the alarm faster; invalid detections are not counted as normal samples and do not advance the clearing count |
| probe_timeout_seconds | Use the default 600.0 seconds first, and then evaluate based on the model response time | This is the timeout of a single probe, not the user request timeout; avoid setting it lower than the time required for normal generation of the model |

## FAQs

### No Precision check Log After Startup

**Symptom**

After the service is started, the Precision check keyword does not appear in the Coordinator logs.

**Cause Analysis**

`precision_check_enabled=false` or the configuration is not loaded.

**Solution**

Check whether the motor_coordinator_config.precision_detection_config configuration is loaded correctly.

### Log Message: Precision check enabled but scheduler client unavailable

**Symptom**

The following alarm appears in the Coordinator logs, indicating that precision detection is enabled but the Scheduler client is not ready, so the sampling chain is not enabled in the current process:

```text
Precision check enabled but scheduler client unavailable; disabling precision sampling until scheduler connects
```

**Cause Analysis**

The Worker is not connected to the Mgmt control plane IPC.

**Solution**

Check whether the Mgmt process has bound `scheduler_frontend` and the ZMQ path.

### Log Message: sample incomplete

**Symptom**

The sample incomplete prompt appears in the Coordinator logs.

**Cause Analysis**

The engine returns `token_ids` but does not return `logprobs`.

**Solution**

Check whether the engine supports the `logprobs` parameter.

### Log Message: MsprobeChecker: msprobe not installed

**Symptom**

The MsprobeChecker: msprobe not installed prompt appears in the Coordinator logs.

**Cause Analysis**

msprobe is missing in the environment.

**Solution**

Install the matching msprobe in the Python environment actually used by the Coordinator Worker as described in "Environment Preparation", resend the request, and confirm that `MsprobeChecker: result` appears. The mock checker is only used for development and testing and cannot replace the production detection capability.

### Rare Characters Cannot Be Detected

**Symptom**

After precision detection is enabled, rare-character anomalies are never detected.

**Cause Analysis**

The value of `logprobs_count` is less than 5, or the token2category mapping is missing.

**Solution**

Increase `logprobs_count` and check the msprobe mapping files.

### Alarm Reported but Instance Not Terminated

**Symptom**

The precision alarm has been reported to the Controller, but instance termination is not triggered.

**Cause Analysis**

Automatic recovery is not enabled on the Controller.

**Solution**

Check whether `precision_auto_recovery_enabled` is enabled.

### Only the D Instance Is Terminated, Not the P Instance

**Symptom**

During automatic recovery, only the Decode instance is terminated, and the Prefill instance is not terminated at the same time.

**Cause Analysis**

`p_instance_id` in the alarm is empty.

**Solution**

Check whether the deployment mode and the instance group key can resolve the P instance.
