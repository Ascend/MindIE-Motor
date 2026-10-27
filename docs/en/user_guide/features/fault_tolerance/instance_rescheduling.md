# Instance Rescheduling

## Feature Introduction

Instance rescheduling is used to automatically reclaim a faulty inference instance and create a new instance on a healthy node when an unrecoverable fault occurs on the Pod or on the node hosting the inference instance, thereby reducing manual intervention and service capacity loss. This capability is implemented by MindIE Motor in coordination with Kubernetes and MindCluster: Motor detects the instance anomaly, isolates the faulty instance from the scheduling pool, and makes the abnormal Pod exit; MindCluster reclaims the original instance, reallocates NPU resources, and creates the Pod; Motor then registers the new instance, assembles it, and starts the engine.

Instance rescheduling and [request rescheduling](./rescheduler.md) act at different levels. Instance rescheduling restores the capacity of inference instances, while request rescheduling forwards the affected inference requests to other healthy instances. The two capabilities can be used together to shorten request interruptions during a fault.

> [!NOTE]NOTE
> The current Motor deployment template configures `fault-scheduling: grace` and `fault-retry-times: "10000"` for inference Pods by default. When workloads are managed by Infer Operator, `fault-scheduling` also supports the `external-*` modes in which Infer Operator takes over Pod deletion. Instance rescheduling does not need to be enabled separately in `user_config.json`.

### How It Works

1. NodeManager continuously monitors the status of the inference engine in its own Pod and reports instance heartbeats to the Controller. The Controller also learns about hardware faults through the node and NPU fault information maintained by MindCluster.

2. When the Controller confirms that an instance is abnormal, it marks the instance as `INACTIVE` and notifies the Coordinator to remove the instance, preventing new requests from being scheduled to the faulty instance. For a multi-Pod instance in which some Pods are unreachable, the Controller also notifies the still-reachable NodeManagers to exit, preventing old and new Pods of the same collective communication group from coexisting.

3. For engine process faults, Motor preferentially tries the enabled lightweight recovery policies, such as restarting the engine in place. If recovery fails or the lightweight recovery conditions are not met, NodeManager exits with an abnormal status, and K8s and MindCluster enter the instance rescheduling process.

4. MindCluster handles the faulty instance according to `fault-scheduling` and `fault-retry-times`, deletes the Pod of the original instance, reselects a healthy node that meets the resource, affinity, and hardware type constraints, and creates a new Pod.

5. The NodeManager in the new Pod registers with the Controller. The Controller reassembles the instance by `job_name` and role, allocates an instance ID to the new instance, and delivers the startup command to all NodeManagers.

6. NodeManager starts the inference engine and resumes heartbeats. After all endpoints of the instance are ready, the status changes from `INITIAL` to `ACTIVE`, and the Controller publishes the new instance to the Coordinator again, restoring request scheduling.

If the NPU on the faulty node is still marked as unavailable, the new instance is not rescheduled to that faulty resource. The actual rescheduling location is determined by Kubernetes and MindCluster based on resource headroom, node labels, affinity, and fault isolation status.

### Constraints and Limitations

| Constraint Dimension   | Requirement                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Deployment mode   | Only K8s deployment is supported, and MindCluster is required to manage inference jobs. Docker only deployment has no instance scheduler and does not support instance rescheduling.                                                                                                                                                                                                                                                                                                                              |
| Inference engine   | vLLM and SGLang are supported. Instance rescheduling is located at the container and instance lifecycle layer and does not depend on the request protocol.                                                                                                                                                                                                                                                                                                                                                    |
| Software dependency   | Kubernetes, Volcano, Ascend Device Plugin, ClusterD, and MindCluster Infer Operator must be installed in versions matching the current Motor version. For matching versions, see [Release Notes](../../../release_note_motor.md).                                                                                                                                                                                                                                                                                                                |
| Resource requirements   | Healthy resources that meet the NPU count, hardware model, node label, affinity, and network topology requirements of the instance must exist in the cluster. When resources are insufficient, the new Pod stays in `Pending` and the instance cannot be recovered.                                                                                                                                                                                                                                                                                      |
| Multi-Pod instance  | Instance-level rescheduling requires the complete instance to be reclaimed and rebuilt. Directly deleting a single Pod from a P/D multi-Pod instance does not guarantee that full instance rescheduling is triggered, and may also cause the remaining Pods to wait for collective communication. When verifying fault recovery, trigger it through a real fault or through the instance-level method supported by the deployment platform.                                                                                                                                                                 |
| RoCE network   | When the RoCE network is not configured or is unhealthy, single-node inference instances in non-SuperPoD scheduling scenarios may still be scheduled successfully, but the KV transmission between inference instances may be abnormal, preventing the inference job from running properly. The same risk exists in SuperPoD scheduling scenarios when the number of logical SuperPoDs is 1. An instance being in `Running` or having completed rescheduling does not mean that the KV transmission link is normal; for details, see the MindCluster 26.0 document "[Deploying MindIE Motor Inference Jobs](https://www.hiascend.com/document/detail/en/mindcluster/2600/clustersched/dlug/docs/en/scheduling/usage/mindie_motor_best_practice/01_deploying_mindie_motor.md)". |
| Service continuity | From the time the faulty instance is removed from the scheduling pool until the new instance becomes `ACTIVE`, that instance does not take new requests. If there is no other healthy instance, the inference interface may be temporarily unavailable; whether requests already executing on the faulty instance continue depends on the request rescheduling configuration.                                                                                                                                                                                                                                                                                         |
| State data   | Instance rescheduling creates a new Pod and restarts the engine, so non-persistent data local to the Pod is not retained. Model weights, configurations, logs, and KV Cache must use shared storage or persistent volumes according to service requirements.                                                                                                                                                                                                                                                                                   |
| Other features   | Container snapshots can shorten the engine recovery time after rescheduling; in PR/DT heterogeneous deployment, Prefill can be rescheduled only to PR nodes and Decode only to DT nodes. Recovery is impossible when no idle node of the corresponding type is available.                                                                                                                                                                                                                                                                             |

## Using the Feature

### Environment Preparation

- Kubernetes, Volcano, and MindCluster components have been installed according to [Environment Preparation](../../environment_preparation.md), and Infer Operator is running properly.

- The inference service has been created using the K8s deployment mode provided by Motor, and the Controller, Coordinator, and inference instances are all ready.

- The cluster has idle healthy nodes that meet the resource and scheduling constraints of the faulty instance; for a multi-Pod instance, the resource requirements of the complete instance must be met at one time.

- If requests must continue to be processed during recovery, keep at least one healthy instance of the same role and enable [request rescheduling](./rescheduler.md) as required by the service.

### Usage Example

The inference Pod template of Motor contains the following labels by default:

```yaml
spec:
  template:
    metadata:
      labels:
        fault-scheduling: grace
        fault-retry-times: "10000"
```

The parameters are described as follows. For the complete versioned definition, see the latest MindCluster "[Job YAML Configuration Description](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/en/scheduling/06_api/15_yaml_configuration.md)". Deployment and Infer Operator workloads support multiple `fault-scheduling` values:

| Parameter                  | Value                          | Description                                                                                                                                                                                                                                                     |
| --------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fault-scheduling`  | `grace`                     | Volcano gracefully deletes the original Pod first.                                                                                                                                                                                                                               |
| `fault-scheduling`  | `force`                     | Volcano forcibly deletes the original Pod.                                                                                                                                                                                                                               |
| `fault-scheduling`  | `external-force`            | Volcano only writes the fault cause into the Pod annotation, and external components such as Infer Operator detect the fault and forcibly delete the original Pod. This applies to scenarios where Infer Operator is responsible for instance-level rescheduling.                                                                                                                 |
| `fault-scheduling`  | `external-grace`            | Volcano only writes the fault cause into the Pod annotation, and external components such as Infer Operator detect the fault and gracefully delete the original Pod.                                                                                                                                                               |
| `fault-scheduling`  | `external-force-pod-failed` | Volcano only marks the fault, and external components such as Infer Operator handle it; the original Pod is forcibly deleted only for service-plane faults, that is, when a process in the Pod exits with a non-zero status, while the Pod on the faulty node is not deleted for hardware faults. When the DP fault recovery switches on both the Controller and engine sides are enabled, Motor generates this value for the corresponding inference Pod and configures `pod-rescheduling: on` at the same time. |
| `fault-scheduling`  | `off`, not configured, or any other value       | MindCluster breakpoint continuation training fault handling is not enabled, but the retry mechanism of the K8s workload itself may still take effect.                                                                                                                                                                                 |
| `fault-retry-times` | Integer greater than `0`               | Number of unconditional retries allowed for service-plane faults. Using this capability requires the process to exit with a non-zero status when a service-plane fault occurs; when it is not configured or is configured as `0`, unconditional retry for service-plane faults is not enabled. The default value in the Motor template is `10000`.                                                                                        |

To confirm the deployment result, run the following command:

```bash
kubectl get pod -n {namespace} \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.fault-scheduling}{"\t"}{.metadata.labels.fault-retry-times}{"\n"}{end}'
```

When the default Motor template is used, the label values of inference instance Pods should be `grace` and `10000`; when the DP fault recovery capability that automatically generates the Infer Operator external handling label is enabled, `fault-scheduling` should be `external-force-pod-failed`, and `pod-rescheduling: on` should also be present. Non-inference instance Pods such as the Controller and Coordinator may not have these labels.

`fault_tolerance_config.container_restart_managed` of NodeManager controls whether NodeManager is allowed to exit and hand recovery over to the external scheduler when an abnormal engine keeps failing to recover. The default value is `null`, which is automatically resolved to enabled in a K8s Pod, so explicit configuration is usually unnecessary. If it is configured as `false`, Motor keeps the NodeManager process, and the instance rescheduling fallback is not triggered through this abnormal path.

### Verifying the Feature

> [!WARNING]WARNING
> The following operations interrupt requests on the target instance. Perform them only in a test environment and make sure that the cluster has sufficient spare resources. Do not replace instance-level fault verification by deleting a single Pod from a multi-Pod instance.

1. Record the instance Pod, Pod IP, and host node before verification.

   ```bash
   kubectl get pod -n {namespace} -o wide
   ```

2. Trigger rescheduling through the instance-level fault injection or instance reclamation method supported by the current MindCluster version. The faulty node should have been marked as unschedulable by the fault management component, so that the new instance does not use the same faulty resource again.

3. Observe the process in which the old Pod exits and the new Pod is created.

   ```bash
   kubectl get pod -n {namespace} -w
   ```

4. Check the Controller logs. The logs should show, in order, the isolation or removal of the old instance, the registration of the new NodeManager, the instance assembly, and the new instance entering `ACTIVE`.

   ```bash
   kubectl logs -n {namespace} {controller-pod-name} -f
   ```

5. Compare the Pod UID or creation time to confirm that the faulty instance has been recreated. After all new Pods are `Running` and ready, send inference requests to verify that the service is restored.

## FAQs

### No New Pod Is Created After a Fault

**Symptom**

The faulty instance has exited, but no new Pod is created for a long time.

**Cause Analysis**

The following causes are possible:

- The inference Pod is not configured with `fault-scheduling` or `fault-retry-times`, or the label values are not supported by the current MindCluster version.

- Infer Operator, Volcano, ClusterD, or Ascend Device Plugin is abnormal.

- The rescheduling count of the workload has been exhausted.

- Docker only deployment is used, and there is no cluster scheduler capable of instance rescheduling.

**Solution**

1. Check the `fault-scheduling` and `fault-retry-times` labels on the inference Pod.

2. Check the Pod status and logs of Infer Operator, Volcano, ClusterD, and Ascend Device Plugin.

3. Check the inference workload and Pod events to confirm whether retry exhaustion or scheduling failure information exists.

4. Confirm that the K8s deployment mode is used.

### The New Pod Stays in Pending State

**Symptom**

MindCluster has created the new Pod, but the Pod stays in `Pending` for a long time.

**Cause Analysis**

There is no healthy resource in the cluster that simultaneously meets the NPU count, hardware model, node label, affinity, network topology, and fault isolation requirements. In the PR/DT heterogeneous scenario, the corresponding role is also required to use a matching node type.

**Solution**

1. Run the following command to view the Pod scheduling events.

   ```bash
   kubectl describe pod -n {namespace} {pod-name}
   ```

2. Check the NPU headroom, taints and tolerations, `nodeSelector`, Pod anti-affinity, and fault isolation status of the target node.

3. Release or expand node resources that meet the constraints for the corresponding role. When Decode resources are insufficient and the usage conditions are met, see [ScaleP2D Fault Recovery](./scale_p2d.md).

### The Instance Is Not Fully Rescheduled After One Pod Is Deleted

**Symptom**

After a Pod is directly deleted from a multi-Pod Prefill or Decode instance, only a single Pod is rebuilt, or the instance never becomes ready again.

**Cause Analysis**

Directly deleting a single Pod is a Pod lifecycle operation and is not equivalent to MindCluster instance-level fault recovery. A multi-Pod instance must keep its endpoints, Rank Table, and collective communication group consistent, and single-Pod rebuilding cannot guarantee that whole-instance reclamation is triggered.

**Solution**

1. Stop verifying instance rescheduling by deleting a single Pod.

2. Verify again according to the instance-level fault injection or instance reclamation procedure supported by the current MindCluster version.

3. Check the Controller logs to confirm that the old instance has been isolated from the Coordinator and that the new instance has completed registration and assembly of all NodeManagers.

### The New Instance Is Running but Inference Service Is Still Unavailable

**Symptom**

All new Pods are `Running`, but the inference interface still returns that the service is unavailable.

**Cause Analysis**

`Running` only indicates that the containers have started, not that the inference engine and the complete instance are ready. The new instance may still be loading the model, waiting for other Pods to register, or stuck in the `INITIAL` state due to endpoint count, Rank Table, or network connectivity problems.

**Solution**

1. Check the logs of all NodeManagers and inference engines of the new instance to confirm that the model is loaded successfully.

2. Check the Controller logs to confirm that the instance has changed from `INITIAL` to `ACTIVE`.

3. For a multi-Pod instance, confirm that all Pods have registered and that the number of endpoints is consistent with the parallel configuration.

4. Check the network connectivity from the Controller to the Coordinator, from the Controller to the NodeManagers, and within the instance.
