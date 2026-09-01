# ScaleP2D Fault Recovery

## Feature Introduction

 Scale Prefill to Decode (**ScaleP2D**) is a fault self-healing strategy of MindIE Motor in the **PD disaggregation** (Prefill/Decode decoupling) scenario. When some nodes of a **Decode (D) instance** become unavailable due to **L4–L6 hardware faults**, the system **actively stops several Prefill (P) instances** to release computing power and node resources, thereby freeing up capacity for the recovery or replacement of the faulty D instance.

## Version Description

This feature depends on the priority scheduling and forced instance deletion capabilities of MindCluster, and **requires MindCluster version 26.1.0 or later**.

## Applicable Scenarios

| Dimension | Description |
|------|------|
| Deployment form | PD disaggregation: P instances handle Prefill, and D instances handle Decode |
| Fault object | **Decode instances** (`role == decode`) |
| Fault level | Instance-level faults reaching **L4, L5, or L6** |
| Node fault | Nodes on D instances with **L3 or higher** device-level hardware faults, or nodes with missing metadata |
| Pre-isolation | D instances have left the `initial`/`active` business-active states (entering `inactive` and other states after isolation triggered by FaultManager) |

**Not applicable to:**

- Prefill instance faults

- Fault level ≤ L3 without escalation to L4+

- `enable_scale_p2d == false`

## Trigger Conditions

When all of the following conditions are met, FaultManager asynchronously triggers the ScaleP2D recovery process:

1. `enable_scale_p2d == true`

2. The `role` of the faulty instance is `"decode"`.

3. The fault level of the instance is **L4/L5/L6**.

## Recovery Process Description

ScaleP2D recovery generally consists of four steps:

| Step | Description |
|------|------|
| 1. Load D instances | Count the number of L3+ faulty nodes on D instances (missing metadata is treated as a fault), and calculate the number of nodes to be vacated `num_required_node`. |
| 2. Wait for D self-recovery | Poll the D instance status within `scale_p2d_d_instance_reinit_wait_timeout`; if it recovers to `initial`/`active`, cancel ScaleP2D; if it remains in a preemptible status such as `inactive` after the timeout, continue ScaleP2D. |
| 3. Select P instances | Select the P instances to be stopped within the available P capacity (available nodes = `nodes_per_P × (P_count - 1)`). |
| 4. Stop P instances | Issue `stop` to all NodeManagers of the selected P instances, and the CRD forcibly reclaims the Pods and releases the nodes. |

## Configuration Description

To enable ScaleP2D, both the **Controller-side JSON configuration** and the **InferServiceSet YAML configuration** (in the CRD deployment scenario) must be completed.

### Controller Configuration

| Configuration Item | Type | Description |
|--------|------|------|
| `enable_fault_tolerance` | bool | Must be `true` to start FaultManager. |
| `enable_scale_p2d` | bool | Whether to enable ScaleP2D (defaults to `false` on the user side). |
| `scale_p2d_d_instance_reinit_wait_timeout` | int | The maximum time (in seconds) to wait for the D instance to self-recover (reinitialize) before ScaleP2D performs preemption. During the waiting period, if the D instance recovers to `initial`/`active`, ScaleP2D is no longer executed. After the timeout, if the D instance is still in a preemptible state such as `inactive`, the subsequent P instance selection process continues. Default: `60`. |
| `strategy_center_check_interval` | int | The polling interval (in seconds) of the strategy center. |

```json
{
  "fault_tolerance_config": {
    "enable_fault_tolerance": true,
    "enable_scale_p2d": true,
    "scale_p2d_d_instance_reinit_wait_timeout": 60,
    "strategy_center_check_interval": 1
  }
}
```

For details, see [Configuration Reference](../../configuration/config_reference.md#motor_controller_config).

### InferServiceSet YAML Configuration (CRD Deployment)

In addition to the Controller configuration described above, ScaleP2D also depends on the **priority scheduling** and **forced instance deletion** capabilities on the InferServiceSet CRD side: after the policy stops the P instance through NodeManager, the CRD Controller must forcibly reclaim the corresponding Pod and release the node for use by the faulty D instance recovery.

File to modify: `examples/deployer/yaml_template/infer_service_template.yaml` (in CRD mode, the deploy script generates `output_yamls/infer_service.yaml` based on this file).

#### 1. Enabling Priority Scheduling

Add `schedulingStrategy` under `InferServiceSet.spec.template` and set its type to `Priority`:

```yaml
spec:
  template:
    schedulingStrategy:
      type: Priority
    roles:
      # ...
```

#### 2. Configure priority for the prefill/decode roles

Add the `priority` field at the same level as `spec` in both the `prefill` and `decode` roles (**takes effect only when priority scheduling is enabled**):

| Field | Type | Value Range | Description |
|------|------|----------|------|
| `priority` | int | 1–32 | The smaller the value, the higher the scheduling priority |

In the PD disaggregation scenario, it is recommended that the `priority` value of **prefill be greater than that of decode** (that is, prefill has the lowest priority and is more likely to be preempted), which is consistent with the ScaleP2D policy of "releasing P compute power first". Example:

```yaml
    - name: prefill
      replicas: 4
      priority: 2          # Lowest priority
      # ...

    - name: decode
      replicas: 4
      priority: 1
      # ...
```

#### 3. Changing the Pod label `fault-scheduling` to `external-force`

Change `fault-scheduling` in the Pod templates (`spec.template.metadata.labels`) of the `prefill` and `decode` roles from the default `grace` to `external-force`:

| Label | Before Change | After Change | Description |
|------|--------|--------|------|
| `fault-scheduling` | `grace` | `external-force` | Enables instance-level rescheduling; forcibly deletes the original instance and cascades the deletion of Pods, allowing ScaleP2D to forcibly release P instances. |

```yaml
        template:
          metadata:
            labels:
              fault-scheduling: external-force   # Originally grace
              fault-retry-times: "10000"
              app: mindie-server
              # ...
```

## Logs and Troubleshooting

Log prefix: `[motor/controller/fault_tolerance/scale_p2d]`

| Keyword | Possible Cause | Suggestion |
|--------|----------|------|
| `instance_not_in_instance_manager` | The D instance does not exist. | Check ETCD / InstanceManager synchronization. |
| `ScaleP2D not needed` + `initial/active` | D is not isolated. | Check the `separate_instance` process. |
| `did not become INACTIVE` | Status check timed out. | Check isolation and status reporting latency. |
| `Node metadata missing` | The node is not synchronized. | Check the ResourceMonitor/pod_ip mapping. |
| `no_p_instances` | No P instance exists. | Check deployment and registration. |
| `Insufficient Prefill nodes` | Insufficient P capacity. | Scale out P or reduce the number of faulty nodes. |
| `Failed to stop P instance node` | NodeManager is unreachable. | Check the process, network, and Pod lifecycle. |
| `algorithm_not_implemented` | The selection algorithm is not implemented. | Contact development personnel to confirm the P instance selection policy. |

## Limitations and Constraints

1. **Policy scope**: Only Decode instances with L4–L6 fault levels are supported; L3 is handled by other policies or isolation logic.

2. **P capacity**: At least one P instance must be retained; recovery fails when available nodes are insufficient.

3. **Resource assumption**: By default, all P instances are assumed to have the same number of nodes.

4. **P selection policy**: This is currently a placeholder implementation (sorted by instance ID), and a load/priority model may be integrated later.
