# ScaleP2D Fault Recovery

## Feature Introduction

ScaleP2D (Scale Prefill to Decode) is a fault self-healing strategy of MindIE Motor in the PD disaggregation scenario. When some nodes of a D instance become unavailable due to L4–L6 hardware faults, the system actively stops several P instances to release computing power and node resources, freeing up capacity for the recovery or replacement of the faulty D instance.

### How It Works

The ScaleP2D fault recovery mechanism is roughly divided into the following four steps:

1. Load D instances: count the number of L3+ faulty nodes on the D instance (missing metadata is treated as a fault), and calculate the number of nodes to be vacated, `num_required_node`.
2. Wait for D self-recovery: poll the D instance status within `scale_p2d_d_instance_reinit_wait_timeout`; if it recovers to `initial` / `active`, cancel ScaleP2D; after the timeout, if it is still in a preemptible state such as `inactive`, continue.
3. Select P instances: select the P instances to be stopped within the available P capacity (available nodes = `nodes_per_P × (P_count - 1)`).
4. Stop P instances: issue `stop` to all NodeManagers of the selected P instances, and the CRD forcibly reclaims the Pods and releases the nodes.

### Constraints and Limitations

| Constraint Dimension | Description |
|----------|------|
| Hardware |<ul><li>Atlas 800I A2</li><li>Atlas 800I A3</li></ul>|
| Deployment scenario | <ul><li>Only PD disaggregation service deployment is supported, and the faulty object must be a Decode instance.</li><li>Not applicable to Prefill instance faults or scenarios where the fault level does not reach L4.</li><li>Not applicable to Ascend950 PR + DT heterogeneous networking: the nodes released after P is stopped are PR nodes, and D cannot be scheduled to these nodes for recovery. In this networking, it is recommended to set `enable_scale_p2d` to `false`. For details, see [PD heterogeneous (PR / DT) scheduling](../../deployment/k8s/pd_disaggregation_deployment.md#pd-heterogeneous-prdt-scheduling) in PD disaggregation service deployment.</li></ul> |
| Software dependency | Depends on the priority scheduling and forced instance deletion capabilities of MindCluster. The MindCluster version must be 26.1.0 or later. |
| Other restrictions | <ul><li>At least one P instance must be retained; recovery fails when available nodes are insufficient.</li><li>By default, all P instances are assumed to have the same number of nodes.</li><li>Currently, P instances are selected and sorted by instance status (`initial` first) and instance ID; a load/priority model may be integrated later.</li><li>On the Atlas 800I A2, isolation-type fault codes (such as `CardNetworkUnhealthy`) stop the entire instance of Prefill / Decode through NmSuicide instead of ScaleP2D.</li></ul> |

## Using the Feature

### Environment Preparation

- The environment has been deployed as described in [PD disaggregation service deployment](../../deployment/k8s/pd_disaggregation_deployment.md).
- The MindCluster version must be 26.1.0 or later, Infer Operator must be installed, and the priority scheduling and forced instance deletion capabilities must be available.
- The InferServiceSet CRD deployment mode is used.
- The K8s cluster administrator `kubectl` permission is available.
- It is recommended that the number of Prefill instances be no less than 2: ScaleP2D must retain at least one P instance; in a 1P1D scenario, the available nodes are 0, and preemption cannot be completed.

### Usage Scenarios

| Dimension | Description |
|------|------|
| Deployment form | PD disaggregation service deployment |
| Fault object | Decode instance (role == decode) |
| Fault level | Instance-level faults reaching L4, L5, or L6 |
| Node fault | Nodes on the D instance with L3 or higher device-level hardware faults, or nodes with missing metadata |
| Pre-isolation | The D instance has left business-active states such as `initial` / `active` (entering states such as `inactive` after isolation is triggered by FaultManager) |

When all of the following conditions are met, FaultManager asynchronously triggers the ScaleP2D recovery process:

- `enable_fault_tolerance == true` and `enable_scale_p2d == true`
- The `role == "decode"` of the faulty instance
- The fault level of the instance is L4 / L5 / L6

The instance fault level is mapped by FaultManager according to the highest hardware/software fault of the node where the instance resides, as follows:

| Level | Original Fault Type | Meaning | ScaleP2D Behavior |
|------|-------------|------|--------------|
| L4 | FreeRestartNPU | NPU isolation required | Trigger scale-in of P to preserve D |
| L5 | RestartNPU | NPU restart required | Delegated to the L4 policy, with the same behavior as L4 |
| L6 | SeparateNPU / PreSeparateNPU / ManuallySeparateNPU | NPU separation or node restart required | Delegated to the L4 policy, with the same behavior as L4 |

L3 and lower levels do not trigger ScaleP2D: L3 requires manual intervention, L2 uses other policies such as token reinference, and L1 only sends notifications.

### Usage Example

To enable ScaleP2D, both the Controller-side JSON configuration (the `motor_controller_config` field in `user_config.json`) and the InferServiceSet YAML configuration (in the CRD deployment scenario) must be completed.

**Procedure**

1. Configure the `motor_controller_config` field in `user_config.json` as shown in the following example. For more parameter descriptions, see [full parameter description of the user_config.json configuration file](../../configuration/config_reference.md#motor_controller_config).

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

   **Table 1** Parameter description

   | Configuration Item | Type | Value Range | Mandatory | Default Value | Description |
   |--------|------|----------|------|--------|------|
   | enable_fault_tolerance | bool | true / false | No | true | Whether to enable Motor Controller fault self-healing. FaultManager is started only when the value is `true`; after it is disabled, ScaleP2D does not actually take effect. |
   | enable_scale_p2d | bool | true / false | No | true | Whether to enable ScaleP2D. It actually takes effect only when `enable_fault_tolerance=true` is also set. It is recommended to explicitly set it to `true` in the user configuration. |
   | scale_p2d_d_instance_reinit_wait_timeout | int | 1–600 | No | 60 | Maximum time (seconds) to wait for the D instance to self-recover (reinitialize) before ScaleP2D performs preemption. During the waiting period, if the D instance recovers to `initial` / `active`, ScaleP2D is no longer executed; after the timeout, if the D instance is still in a preemptible state such as `inactive`, the subsequent P instance selection process continues. |
   | strategy_center_check_interval | int | > 0 | No | 10 | Strategy center polling interval (seconds). The strategy center is also woken up immediately by fault reports, and this interval serves only as a fallback periodic re-evaluation. Setting it to `1` in the example accelerates inspection. |

2. Enable the priority scheduling capability on the InferServiceSet CRD side.

   File to modify: `examples/deployer/yaml_template/infer_service_template.yaml` (in CRD mode, the deploy script generates `output_yamls/infer_service.yaml` based on this file).

   Add `schedulingStrategy` under `InferServiceSet.spec.template` and set the type to `Priority`, as shown in the following example:

   ```yaml
   spec:
     template:
       schedulingStrategy:
         type: Priority
       roles:
         # ...
   ```

3. Configure the `priority` field for the prefill / decode roles.

   Add the `priority` field at the same level as `spec` in both the `prefill` and `decode` roles (it takes effect only when priority scheduling is enabled).

   In the PD disaggregation scenario, it is recommended that the `priority` value of prefill be greater than that of decode (that is, prefill has the lowest priority and is more likely to be preempted), which is consistent with the ScaleP2D policy of preferentially releasing P computing power. Example:

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

   | Configuration Item | Type | Value Range | Mandatory | Default Value | Description |
   |--------|------|----------|------|--------|------|
   | priority | int | 1–32 | No | None | Takes effect only when `schedulingStrategy.type` is `Priority`. The smaller the value, the higher the scheduling priority. When it is not configured, the role does not participate in priority preemption. |

4. Enable the forced instance deletion capability on the InferServiceSet CRD side.

   Change `fault-scheduling` in the Pod templates (`spec.template.metadata.labels`) of the `prefill` and `decode` roles from the default `grace` to `external-force`:

   ```yaml
           template:
             metadata:
               labels:
                 fault-scheduling: external-force   # Originally grace
                 fault-retry-times: "10000"
                 app: mindie-server
                 # ...
   ```

   | Configuration Item | Type | Value Range | Mandatory | Default Value | Description |
   |--------|------|----------|------|--------|------|
   | fault-scheduling | string | grace / external-force | Yes | grace | When the value is `external-force`, instance-level rescheduling is enabled: the original instance is forcibly deleted and Pods are cascade-deleted, allowing ScaleP2D to forcibly release P instances. |

5. Create a namespace with the same name as `job_id`, and then deploy the service using the deploy script. `{namespace}` must be the same as `job_id` in `user_config.json`.

   ```bash
   kubectl create namespace {namespace}
   cd examples/deployer
   python3 deploy.py --config_dir ../../model_configs/vllm --nostep
   ```

   When the deployment is successful, `all deploy end.` and `Deploy complete.` appear in the log.

### Verification

The following uses PD disaggregation 1P1D as an example. Replace {namespace} in the commands with the actual namespace.

1. Check whether all Pods are Running.

   ```bash
   kubectl get pod -n {namespace}
   ```

   Example output:

   ```text
   NAME                         READY   STATUS    RESTARTS   AGE
   vllm-0-controller-0-xxx      1/1     Running   0          1m
   vllm-0-coordinator-0-xxx     1/1     Running   0          1m
   vllm-0-decode-0-0            1/1     Running   0          1m
   vllm-0-kv-store-0-xxx        1/1     Running   0          1m
   vllm-0-prefill-0-0           1/1     Running   0          1m
   ```

   The `fault-scheduling` label of Prefill / Decode Pods should be `external-force`:

   ```bash
   kubectl get pod -n {namespace} -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.fault-scheduling}{"\n"}{end}'
   ```

   ```text
   vllm-0-decode-0-0     external-force
   vllm-0-prefill-0-0    external-force
   ```

2. Check whether priority scheduling is enabled for InferServiceSet and whether the `priority` of prefill / decode has taken effect.

   ```bash
   kubectl get inferserviceset vllm -n {namespace} -o jsonpath='{.spec.template.schedulingStrategy}{"\n"}{range .spec.template.roles[*]}{.name} priority={.priority} replicas={.replicas}{"\n"}{end}'
   ```

   Example output:

   ```text
   {"type":"Priority"}
   controller priority= replicas=1
   coordinator priority= replicas=1
   prefill priority=2 replicas=1
   decode priority=1 replicas=1
   ```

3. Check whether the ScaleP2D configuration has been written into the ConfigMap.

   ```bash
   kubectl get configmap motor-config -n {namespace} -o jsonpath='{.data.user_config\.json}' | python3 -m json.tool | grep -A 6 fault_tolerance_config
   ```

   Example output:

   ```json
   "fault_tolerance_config": {
     "enable_fault_tolerance": true,
     "enable_scale_p2d": true,
     "scale_p2d_d_instance_reinit_wait_timeout": 60,
     "strategy_center_check_interval": 1
   }
   ```

4. Check the Controller log to confirm that FaultManager and ScaleP2D have been enabled.

   ```bash
   kubectl logs -n {namespace} -l app=mindie-motor-controller | grep -E "Scale P2D|FaultManager|strategy center"
   ```

   When the feature takes effect, the following keywords appear:

   ```text
   FaultManager initialized.
   Fault tolerance strategy center started
   FaultManager started.
     ├─ Advanced RAS:         Enabled
     │   ├─ Scale P2D:        Enabled
   ```

   After P / D instances are registered, FaultManager starts a ResourceMonitor for the corresponding node, for example:

   ```text
   FaultManager update instance {job_id}-vllm-0-d0 with event: ObserverEvent.INSTANCE_INITIAL.
   Added instance 1 ({job_id}-vllm-0-d0) with 1 nodes
   Resource monitor started for node {decode-node}
   FaultManager update instance {job_id}-vllm-0-p0 with event: ObserverEvent.INSTANCE_INITIAL.
   Added instance 2 ({job_id}-vllm-0-p0) with 1 nodes
   Resource monitor started for node {prefill-node}
   ```

5. After a real L4–L6 Decode fault occurs, search the Controller log for ScaleP2D recovery keywords.

   Log prefix: `[motor/controller/fault_tolerance/scale_p2d]` or `[fault_tolerance][scale_p2d.py]`

   | Log Keyword | Meaning |
   |------------|------|
   | ScaleP2D strategy started | ScaleP2D recovery has been triggered |
   | D instance ready for ScaleP2D | The D instance has been isolated, and P instance selection starts |
   | P instances selected for kill | The P instances to be stopped have been selected |
   | ScaleP2D recovery succeeded | Recovery succeeded |
   | ScaleP2D recovery failed | Recovery failed; troubleshoot with `last_error` |

   >[!NOTE]NOTE
   >The formula for available P nodes is `nodes_per_P × (P_count - 1)`, and at least one P instance must be retained. In 1P1D deployment, the available nodes are 0, so preemption is not completed even if a D instance is faulty, and `Insufficient Prefill nodes` appears in the log. To verify the complete recovery process, the number of P instances must be at least 2.

## FAQs

### instance_not_in_instance_manager

**Problem Description**

`instance_not_in_instance_manager` or `instance_not_found` appears in the Controller log.

**Cause Analysis**

When ScaleP2D is triggered, the corresponding D instance can no longer be found in InstanceManager. This usually occurs when ETCD synchronization is not complete or the instance has been deleted.

**Solution**

1. Check the connectivity between the Controller and ETCD, and confirm that instance registration has been completed.
2. Run `kubectl get pod -n {namespace}` to confirm that the Decode instance Pod still exists.
3. Check the InstanceManager registration/deletion records in the Controller log.

### ScaleP2D not needed + initial/active

**Problem Description**

`D instance recovered, ScaleP2D not needed` appears in the log, and the status is `initial` or `active`.

**Cause Analysis**

The D instance has self-recovered within the waiting window, or FaultManager has not yet isolated the D instance to `inactive`, so ScaleP2D actively cancels the preemption.

**Solution**

1. Confirm whether the fault level of the D instance has reached L4 or higher.
2. Check whether the `separate_instance` isolation process sets the D instance to `inactive`.
3. If D has recovered its services, ScaleP2D no longer needs to be executed.

### did not become INACTIVE

**Problem Description**

`did not become INACTIVE` or `D instance status check timed out` appears in the log.

**Cause Analysis**

The D instance never enters a preemptible state within `scale_p2d_d_instance_reinit_wait_timeout`.

**Solution**

1. Check whether isolation and status reporting are delayed.
2. Increase `scale_p2d_d_instance_reinit_wait_timeout` appropriately (value range: 1–600).
3. Check whether the `status` of the instance in InstanceManager is synchronized with the node fault.

### Node metadata missing

**Problem Description**

`FaultManager returned no node data` appears in the log, or all D nodes are counted as faulty nodes.

**Cause Analysis**

FaultManager has not yet synchronized the node metadata of the instance, and the ResourceMonitor / pod_ip mapping has not been established.

**Solution**

1. Confirm that FaultManager has been started and that the `mindx-dl-deviceinfo-*` ConfigMap of the target node exists.
2. Check whether ResourceMonitor is listening to the corresponding node.
3. Check whether the `pod_ip` of NodeManager is consistent with the FaultManager node table.

### no_p_instances

**Problem Description**

`no_operational_p_instances` or `No operational Prefill instances` appears in the log.

**Cause Analysis**

There is no P instance in the `initial` / `active` state in the cluster, so nodes cannot be released.

**Solution**

1. Check whether the Prefill deployment and registration are successful.
2. Confirm that P instances have not been stopped by other fault policies.

### Insufficient Prefill nodes

**Problem Description**

`Insufficient Prefill nodes for ScaleP2D` appears in the log.

**Cause Analysis**

The number of available P nodes is less than `num_required_node`. Available nodes = `nodes_per_P × (P_count - 1)`.

**Solution**

1. Scale out P instances to ensure that at least one P instance is retained after the fault.
2. Alternatively, reduce the number of L3+ faulty nodes on a single D instance and then observe again.

### Failed to stop P instance node

**Problem Description**

`Failed to stop P instance node` appears in the log.

**Cause Analysis**

Issuing `stop` to the NodeManager of the P instance failed, and the process, network, or Pod lifecycle is abnormal.

**Solution**

1. Check whether the Pod of the target P instance is Ready and whether the NodeManager port is reachable.
2. Confirm that `fault-scheduling` has been set to `external-force` so that the CRD can forcibly reclaim the Pod.
3. Check the network connectivity between the P instance and the Controller.

### algorithm_not_implemented

**Problem Description**

`Using placeholder P instance selection algorithm` and `reason=algorithm_not_implemented` appear in the log.

**Cause Analysis**

Currently, P instance selection is a placeholder implementation that sorts by status and instance ID. This WARNING indicates that the load/priority cost model has not yet been integrated, and does not mean that this ScaleP2D failed.

**Solution**

No action is required. If P instances need to be selected by load, contact the developers to confirm the subsequent selection policy.
