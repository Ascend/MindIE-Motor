# DP Domain Scale-In

## Feature Introduction

DP domain scale-in (card-reduction scale-in) is used in large-EP, multi-DP inference instances. When the NPUs or engine processes corresponding to some DP ranks are unrecoverable, Motor coordinates the surviving ranks to reduce the effective DP size and continue serving **without restarting the entire instance**.

Motor is responsible for fault collection, the scale-in gate (FtGate), delivering FT commands to NodeManager, and updating the instance scheduling view. vLLM / vLLM-Ascend is responsible for performing rank removal, communication group rebuilding, and EPLB reallocation inside the engine. The two parties collaborate through `/v1/fault_tolerance/apply` and `/v1/fault_tolerance/status`.

### How It Works

1. NodeManager polls the native engine FT status and reports hardware/software faults in combination with the MindCluster ConfigMap and node status. The Controller FaultManager aggregates evidence for each DP rank within the collection window.
2. When the DP scale-in conditions are met and FtGate passes, FaultManager selects `DpScaleDownStrategy` and delivers the list of **original DP ranks** to be removed to each surviving NodeManager. The engine then performs the scale-in asynchronously.
3. After the scale-in is committed, Motor polls the surviving engines within `execution_deadline_sec` until the FT status returns to HEALTHY. If the scale-in fails or times out, Motor enters the existing recovery paths such as full-instance reconfiguration/rescheduling.
4. After the scale-in succeeds, the instance enters the `SCALED_DOWN_RUNNING` phase, and the Coordinator continues scheduling according to the updated DP view. If `enable_dp_scale_up` is enabled and all DP ranks in a Pod have been removed, Motor stops the corresponding NodeManager, and the cluster reclaims the resources of the empty Pod.

### Constraints and Limitations

| Constraint Dimension | Description |
| -------- | ---- |
| Hardware | Only **Atlas 800I A3 SuperPoD** is supported; typical scenarios are large-EP, multi-Pod, multi-DP deployments. Scale-in depends on MC2 / MegaMoE Mask fault tolerance and is currently verified only on the A3 form factor. |
| Inference engine | Only **vLLM** has the capability to execute DP domain scale-in when the engine-side `enable_fault_tolerance` is enabled; SGLang is not supported. **The Prefill role does not support** this feature. For **detailed constraints** on the engine-side `user_config` / `env.json` and version combinations, see "Engine Usage Constraints" in the following table. |
| Motor switches | `motor_controller_config.fault_tolerance_config.enable_fault_tolerance` and `enable_dp_scale_down` must both be true. When the global FT switch is disabled, DP scale-in is not executed. |
| Deployment mode | K8s + Infer Operator / InferServiceSet is recommended. When DP scale-in is enabled and engine FT is enabled for Decode/Union, Deployer generates `fault-scheduling: external-force-pod-failed` and `pod-rescheduling: on` for inference Pods. For details, see [Instance Rescheduling](./instance_rescheduling.md). |
| Feature exclusivity | Cannot be stacked with [Container Snapshot Container Snapshot Feature](../container_snapshot.md). The engine's `fault_tolerance_config.auto_recovery` must be false (mutually exclusive with DP scale-in; verified by FtGate). |
| Service continuity | During scale-in execution, the instance is briefly removed from traffic. After the scale-in succeeds, the instance continues to serve with a smaller DP size. Evaluate the acceptable minimum number of DPs and expert redundancy. |

#### Engine Usage Constraints

The following table lists the conditions that Decode / Union engines and the accompanying `env.json` must satisfy.

| No. | Constraint | Related Configuration / Environment | Additional Notes |
| ---- | -------- | --------------- | -------- |
| 1 | **Motor and engine FT switches** | Controller: `enable_fault_tolerance` and `enable_dp_scale_down` are true; **Decode or Union** `engine_config.enable_fault_tolerance` is true | Prefill does not support DP domain scale-in. |
| 2 | **Weight loading and quantization** | Only the vLLM default `DefaultModelLoader`; **W8A8 only**; remove MTP / speculative configurations | The DSV4-Flash-W8A8 can be used as the handover test example; custom loaders (such as runai_streamer) are not supported for now. |
| 3 | **PCP not supported** | `prefill_context_parallel_size` = 1, `enable_dsa_cp` = false | — |
| 4 | **Deployment role and CUDA Graph** | Only **Decode / Union**; `max_num_batched_tokens` ≤ the maximum value of `cudagraph_capture_sizes` | Example: `max_num_batched_tokens`: 32, `cudagraph_capture_sizes`: [4,8,12,16,20,24,28,32] |
| 5 | **External load balancing** | `data_parallel_external_lb` = true, `data_parallel_size_local` = 1 | Only one Engine per API Server. |
| 6 | **Redundant experts** | `enable_eplb` = true, `eplb_config.num_redundant_experts` is sufficiently large | Estimate it by "number of expert slots per single DP rank × expected number of removals". |
| 7 | **Local communication resource version** | Configure `"ASCEND_LOCAL_COMM_RES": "{\"version\":\"1.2\"}"` in **`motor_common_env`** of `env.json` | Injected by Deployer into the common environment of each component Pod; a missing or mismatched version may cause communication resource exceptions during the scale-in phase. |
| 8 | **Mutually exclusive with TASK_QUEUE** | In `motor_engine_decode_env` (for PD co-deployment, `motor_engine_union_env`) of `env.json`, `TASK_QUEUE_ENABLE` = 0 | An abnormal exit of TASK_QUEUE causes subsequent device operations to hang. |
| 9 | **Mutually exclusive with HCCL asynchronous error handling** | Same as above, `HCCL_ASYNC_ERROR_HANDLING` = 0 | The HCCL watchdog may directly kill the process when collective communication is abnormal. |
| 10 | **DP synchronization uses the CPU communication group** | Automatically set by the framework after fault tolerance is enabled | DP synchronization uses only Gloo (CPU) and cannot rely on HCCL. |
| 11 | **Hardware timeout response** | About 23s by default for A3 SuperPoD | Motor scale-in may occasionally time out and then use [Instance Rescheduling](./instance_rescheduling.md). |
| 12 | **Fault detection and timeout balancing** | `cpu_distributed_timeout_seconds`, the FT communication timeout in `additional_config`; `HCCL_EXEC_TIMEOUT` in `motor_engine_*_env` (example: 29) | Detection is slow when an idle DP has no traffic. **Normal scale-in recovery takes less than 30s**; if detection takes too long, rescheduling is triggered after up to about **1 minute**; `execution_deadline_sec` must be evaluated at the same time. |

#### RAS Capability Boundaries (Feature Stacking)

| Granularity | Capability | Stacking with DP Domain Scale-In |
| ---- | ---- | ---------------- |
| Service level | RAS-Monitor | Supported |
| Instance level | [Virtual Inference Health Probe](../sim_inference.md) | Supported |
| Instance level | Engine Relaunch | Supported |
| Instance level | [Instance Rescheduling](./instance_rescheduling.md) | Supported |
| Instance level | PD disaggregation degraded to co-deployment | Supported |
| Instance level | [ScaleP2D Fault Recovery](./scale_p2d.md) | Supported |
| Instance level | [Active/Standby Switchover](./standby.md) | Supported |
| Instance level | [D2D Weight Loading Feature](../startup_acceleration.md) | Supported |
| Instance level | [Container Snapshot Feature](../container_snapshot.md) | **Not supported** |
| Request level | [Request Rescheduling](./rescheduler.md) | Supported |
| Request level | Faulty instance circuit breaking | Supported |
| Advanced RAS | [Precision Detection](../precision_detection.md) | Supported |

## Using the Feature

### Environment Preparation

- Motor services have been deployed according to [PD-Disaggregated Deployment (Based on the vLLM Inference Engine)](../../deployment/k8s/pd_disaggregation_deployment.md) or **Decode/Union** PD co-deployment; cluster nodes are **A3 SuperPoDs**, and the Controller, Coordinator, and all inference Pods are registered properly.
- The target instance is started with a 3.2.0 Motor image; the model is **W8A8** with the default weight loader, and **MTP is not enabled**. The engine has configured `user_config` and `env.json` according to [Engine Usage Constraints](#engine-usage-constraints) (see [Configuration Example](#configuration-example)).
- In `env.json`, configure `ASCEND_LOCAL_COMM_RES` in **`motor_common_env`** (see Engine Usage Constraint 7); write `TASK_QUEUE_ENABLE`, `HCCL_ASYNC_ERROR_HANDLING`, `HCCL_EXEC_TIMEOUT`, and other variables into **`motor_engine_decode_env`** or **`motor_engine_union_env`** (see items 8–12).
- If the engine in the image does not yet contain the DP scale-in implementation, apply the fault tolerance patch provided by the v3.2.0 Motor deployer before the container starts and before `engine.sh` is executed. After the patch is merged, the path is:

    ```bash
    # Insert in deployer/startup/boot.sh before sourcing engine.sh
    bash /path/to/deployer/patch/0.30.0/apply_patches.sh \
        /path/to/deployer/patch/0.30.0/fault_tolerance/vllm_v0.30.0_ft.patch \
        /path/to/deployer/patch/0.30.0/fault_tolerance/vllm_ascend_v0.30.0_ft.patch
    ```

    Replace `/path/to/deployer` with the actual mount path of `examples/deployer` in the container. You can use the `PYTHON` environment variable to specify the interpreter used by `pip show` to locate the installation directory; the default is `python3`. If the container snapshot patch is also used, **apply the fault tolerance patch first, then the snapshot patch**.

- The cluster has MindCluster fault management and (optional) Infer Operator external scheduling capabilities. Reclamation of empty Pods after scale-in depends on `enable_dp_scale_up` and the behavior of the cluster scheduler.

### Configuration Example

The Controller switches for DP domain scale-in are centralized in `motor_controller_config.fault_tolerance_config`. **There is no need** to repeat Motor feature switches at the top level of `motor_engine_prefill_config` / `motor_engine_decode_config`. On the engine side, enable vLLM native FT and scale-in dependencies in the `engine_config` of **Decode or Union**. **Prefill does not support** this feature.

**Controller side**

```json
"motor_controller_config": {
  "fault_tolerance_config": {
    "enable_fault_tolerance": true,
    "fault_collection_timeout_seconds": 60,
    "enable_dp_scale_down": true,
    "enable_dp_scale_up": false,
    "dp_scale_down_config": {
      "request_timeout_sec": 10.0,
      "poll_interval_sec": 1.0,
      "execution_deadline_sec": 30
    }
  }
}
```

**Engine side (Decode example; field names can use kebab-case)**

```json
"motor_engine_decode_config": {
  "engine_type": "vllm",
  "engine_config": {
    "enable_fault_tolerance": true,
    "data_parallel_external_lb": true,
    "data_parallel_size_local": 1,
    "prefill_context_parallel_size": 1,
    "enable_dsa_cp": false,
    "max_num_batched_tokens": 32,
    "cudagraph_capture_sizes": [4, 8, 12, 16, 20, 24, 28, 32],
    "enable_eplb": true,
    "eplb_config": {
      "num_redundant_experts": 128
    },
    "cpu_distributed_timeout_seconds": 30,
    "additional_config": {
      "ft_communication_abort_timeout": 30
    },
    "fault_tolerance_config": {
      "auto_recovery": false
    }
  }
}
```

**env.json (excerpt)**

This file is provided to Deployer together with `user_config.json`. `ASCEND_LOCAL_COMM_RES` must be written in **`motor_common_env`**; role-related variables are written in **`motor_engine_decode_env`** (for PD co-deployment, **`motor_engine_union_env`**).

```json
{
  "motor_common_env": {
    "ASCEND_LOCAL_COMM_RES": "{\"version\":\"1.2\"}"
  },
  "motor_engine_decode_env": {
    "TASK_QUEUE_ENABLE": 0,
    "HCCL_ASYNC_ERROR_HANDLING": 0,
    "HCCL_EXEC_TIMEOUT": 29
  }
}
```

Do not configure MTP (for example, `method: mtp` in `speculative_config` / `speculative-config`). `num_redundant_experts` must be planned according to the model and the expected number of consecutive scale-ins. `data_parallel_size` must be greater than 1 for scale-in to be meaningful. For `env.json` field descriptions, see each [Deployment Guide](../../deployment/README.md) and the `model_configs` typical configurations. For more parameters, see [user_config.json Configuration Reference](../../configuration/config_reference.md#motor_controller_config) and [motor_engine_prefill_config / motor_engine_decode_config](../../configuration/config_reference.md#motor_engine_prefill_configmotor_engine_decode_config).

| Parameter | Description |
| ---- | ---- |
| `enable_dp_scale_down` | Controller uniformly controls DP scale-in; default false. |
| `enable_dp_scale_up` | Whether to stop NodeManager to reclaim empty Pods when all DP ranks in a Pod are removed after a successful scale-in; default false. When disabled, empty Pods are retained, and the card-level scale-in result is not affected. |
| `dp_scale_down_config.request_timeout_sec` | Timeout for Controller to call the NodeManager FT interface. This value must leave margin for NodeManager's default 5-second request to the engine. |
| `dp_scale_down_config.poll_interval_sec` | Polling interval for waiting for the engine to complete scale-in. |
| `dp_scale_down_config.execution_deadline_sec` | Maximum time for Motor to wait for the engine scale-in recovery. When the timeout is reached, the original fault recovery process is entered. |

After deployment, you can use the following command to confirm the inference Pod labels (generated only when Controller DP scale-in and the engine FT of the corresponding role are both enabled):

```bash
kubectl get pod -n {namespace} \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.fault-scheduling}{"\t"}{.metadata.labels.pod-rescheduling}{"\n"}{end}'
```

### Verifying the Feature

> [!WARNING] WARNING
> Fault injection and DP scale-in verification interrupt requests on the target instance. Perform them in a test environment and ensure that sufficient surviving DP capacity remains.

1. Record the target instance ID and the DP layout of each Pod (you can confirm it through the Controller instance query or deployment configuration).
2. Inject a `pkill -9 VLLM::Worker_DP` process into the Decode instance.
3. Query the Controller DP scale-in runtime status (management plane port `controller_api_port`, default `1026`):

    ```bash
    curl "http://{controller-ip}:{port}/controller/fault_tolerance/status"
    curl "http://{controller-ip}:{port}/controller/fault_tolerance/status?instance_id={id}"
    ```

    The `phase` in the response goes through states such as `DECIDING`, `SCALING_DOWN`, `POST_SCALE_CLEANUP`, and `SCALED_DOWN_RUNNING`; when `enable_dp_scale_down` is false, `DISABLED` is returned.

4. Observe the `DpScaleDownStrategy` and `scale-down` related records in the Controller / NodeManager logs, and confirm that `removed_dp_ranks` and the surviving ranks meet expectations.
5. After the scale-in is complete, send inference requests to the Coordinator to confirm that the instance can still serve with the smaller DP size. If [Virtual Inference Health Probe](../sim_inference.md) is enabled, NodeManager migrates the virtual inference monitor after the DP master changes.

## FAQs

### FtGate Rejects Scale-In

When `scale-down gate rejected` appears in logs or in the runtime `last_error`, common `reason_codes` are as follows:

| reason_code | Meaning | Handling Suggestion |
| ----------- | ---- | -------- |
| `FEATURE_DISABLED` | Controller has not enabled `enable_dp_scale_down`, or the global FT switch is disabled. | Check `motor_controller_config.fault_tolerance_config`. |
| `ENGINE_FT_REQUIRED` | The instance does not declare engine FT capability. | Enable `enable_fault_tolerance` in the corresponding role's `engine_config` and redeploy. |
| `EXTERNAL_LB_REQUIRED` | external-lb is not enabled. | Set `data_parallel_external_lb` / `data-parallel-external-lb` to true. |
| `AUTO_RECOVERY_CONFLICT` | Engine FT auto recovery conflicts with DP scale-in. | Set the engine `fault_tolerance_config.auto_recovery` to false. |
| `ENGINE_SCALE_DOWN_UNSUPPORTED` | The engine is not vLLM, or FT is not enabled. | Confirm that `engine_type` is vLLM and FT is enabled. |
| `EPLB_REQUIRED` | EPLB is not enabled. | Set `enable_eplb` and configure `eplb_config`. |
| `EXPERT_REDUNDANCY_REQUIRED` | The number of redundant experts is 0. | Set `eplb_config.num_redundant_experts` to a value greater than 0. |

### Scale-In Timeout or Fallback to Full-Instance Recovery

If the engine FT status does not return to HEALTHY within `execution_deadline_sec`, Motor marks the failure and enters fallback paths such as `RECONFIGURING`. The agreed conclusion requires that **normal scale-in recovery take less than 30 seconds**. If any of the following occurs, occasional timeouts may happen and instance rescheduling is triggered: the A3 hardware timeout response (about 23s by default), delayed detection caused by idle DPs without traffic, or improper operator timeout balancing. You can appropriately increase `execution_deadline_sec` (upper limit: 600) and evaluate it together with `cpu_distributed_timeout_seconds`, `ft-communication-abort-timeout`, and `HCCL_EXEC_TIMEOUT`. Confirm that the 0.30.0 fault tolerance patch has been applied.

### Difference from Manual/Automatic Instance Scaling

- **Manual / HPA scaling**: Modify `p_instances_num` / `d_instances_num` or HPA replicas. What is increased or decreased is the **number of Prefill/Decode instance Pods**.
- **DP domain scale-in**: Reduce the **DP ranks** within the same instance. InferServiceSet replicas remain unchanged unless the cluster reclaims empty Pods after `enable_dp_scale_up` is enabled.

### Enabling Together with Container Snapshot

Container snapshot is mutually exclusive with DP domain scale-in/scale-out. If the service requires snapshot to accelerate rescheduling, do not enable `enable_dp_scale_down` on the same service. For details, see the constraint table in [Container Snapshot](../container_snapshot.md).
