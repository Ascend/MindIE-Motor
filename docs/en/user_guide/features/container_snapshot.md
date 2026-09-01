# Container Snapshot

## Feature Introduction

The container snapshot feature is used to save the running state of instance node containers and quickly restore inference services in scenarios such as instance rescheduling. The Motor service framework handles device-side suspend, resume, and control-plane registration after recovery. MindCluster or the user is responsible for performing Host-side checkpoint on the instance node containers.

A container snapshot consists of the following two parts:

- Runtime model weights persisted to the host mount path.

- Container Host snapshot image, which contains the Device snapshot state.

This feature supports two types of application scenarios:

- **Default snapshot application scenario**: MindCluster instance rescheduling. MindCluster mounts the snapshot metadata, queries the steady-state point, performs container checkpoint, and saves the Host snapshot image.

- **User-defined application scenario**: The user creates and mounts the snapshot metadata file, queries the steady-state point, performs container checkpoint, and manages the Host snapshot image and runtime weights.

## Environment Constraints

- **Operating system**: Only EulerOS R15C10/HCE 3.0 is supported, and CRIU 3.19 and grus must be preinstalled.

- **Container runtime**: Only containerd is supported.

- **Inference engine**: It must support the saving and restoration of Device snapshots and provide the corresponding suspend and resume interfaces.

## Container Snapshot Creation

The container snapshot creation process is as follows:

1. After the instance cold-starts and enters the healthy state, the Engine Server automatically executes suspend, locks the Device state, saves the Device snapshot, and writes the runtime model weights to `model_save_path`.

2. After all Engine Servers on this node complete suspend, the instance node container reaches the steady state.

   - MindCluster instance resizing scenario: determined by the Node Manager's `/readiness` returning `200`.

   - User-defined application scenario: determined by `/node-manager/status` returning `200 {"status": true}`.

3. After reaching the steady state, MindCluster or the user uses grus to execute checkpoint on the instance node container and save the container Host snapshot image.

4. After the Host-side checkpoint is completed, the metadata field `checkpoint` is updated to `"done"`. After detecting this state, the Engine Server unlocks the Device, and the cold-started instance resumes providing inference services.

An instance in the checkpoint process cannot provide inference services. After reaching the steady state but before `checkpoint` is completed, the Node Manager pauses reporting normal heartbeats to the Controller.

## Snapshot Restoration

The container snapshot restoration process is as follows:

1. MindCluster or the user restores the instance node container from the Host snapshot image and mounts the corresponding runtime weights and snapshot metadata files.

2. Node Manager reads `job_name` and `namespace` from the metadata, refreshes the Pod IP and Controller DNS, and then re-registers with the Controller.

3. The Controller delivers the startup command. Node Manager updates the `model_load_path` and `data_parallel_master_ip` fields in the snapshot metadata file; the snapshot restoration scenario does not recreate the Engine Server process.

4. Engine Server uses `model_load_path` and `data_parallel_master_ip` from the metadata to perform resume. After all endpoints are restored to `NORMAL`, the instance re-enters the ready state.

## Enabling Container Snapshot Creation Configuration

### `user_config.json`

Add the container snapshot-related configuration group `motor_container_snapshot_config` to `user_config.json`:

```json
"motor_container_snapshot_config": {
    "enable_snapshot": true,
    "snapshot_metadata_path": "/path/to/snapshot_metadata.json"
}
```

**`enable_snapshot`**: master switch for container snapshots, defaulting to `false`. When this field is `false`, all other fields do not take effect; when it is `true`, it indicates that the snapshot creation and restoration capability of the instance node container is enabled.

**`snapshot_metadata_path`**: path to the snapshot metadata file inside the container, defaulting to an empty string.

- When the configuration is empty, the default snapshot application scenario is entered, that is, MindCluster instance rescheduling. The snapshot metadata is mounted by MindCluster through a ConfigMap, and Node Manager copies it to the default writable path `/snapshot/snapshot_metadata.json` before use.

- When the configuration is non-empty, the user-defined application scenario is entered. The user must create the snapshot metadata file in advance and mount it to the container path specified by the configuration.

The snapshot metadata file must be a JSON object, and the values of the following fields are all strings:

| Field | Usage Phase | Preparation Requirements | Description |
|------|----------|----------|------|
| `model_save_path` | Snapshot creation | Must be prepared before creating a container snapshot | Disk path for persisting runtime model weights, which must be a host mount path |
| `model_load_path` | Snapshot restoration | Must be prepared before restoring from a container snapshot | Loading path for runtime model weights, which must be a host mount path |
| `job_name` | Snapshot restoration | Must be prepared before restoring from a container snapshot | Unique identifier of the inference instance, used to update the Node Manager task name during registration after restoration |
| `namespace` | Snapshot restoration | Must be prepared when the Controller uses the in-cluster `.svc.cluster.local` DNS | Namespace to which the inference service belongs, used to update the Controller DNS; may be omitted in non-cluster DNS scenarios |
| `data_parallel_master_ip` | Snapshot restoration | May not be preconfigured | IP of the Pod where the instance Master DP resides; the value in the file is used first, and when not configured, Node Manager writes the value delivered by the Controller |
| `checkpoint` | Snapshot creation | Written after the Host-side checkpoint is completed | After being updated to `"done"`, the Engine Server unlocks the Device, and the cold-start instance resumes the inference service |

In the user-defined application scenario, before creating a container snapshot, the `model_save_path` field must be prepared; before restoring from a container snapshot, `model_load_path` and `job_name` must be prepared; when the in-cluster Controller DNS is used, `namespace` must also be prepared.

## Container Snapshot Application Scenarios

By directly loading the node container snapshot of an instance, the time required for the instance to be restored to the ready state can be shortened. The default application scenario is MindCluster instance rescheduling.

### Instance Rescheduling

The container snapshot feature works with MindCluster instance rescheduling by default. In this scenario, the snapshot metadata file is mounted by MindCluster through a ConfigMap, and `snapshot_metadata_path` can be omitted or left empty.

The Motor service framework must configure a Kubernetes Readiness Probe for the instance node Pod so that MindCluster can query whether the instance node has reached steady state. After the instance node reaches steady state, MindCluster performs a checkpoint and saves the container Host snapshot image.

For the environment requirements, component deployment, and usage process on the MindCluster side, see [Container Snapshot Deployment and Usage](https://gitcode.com/Ascend/mind-cluster/blob/branch_v26.1.0/docs/zh/scheduling/04_usage/09_infer_operator_best_practice/06_container_snapshot_usage.md).

**Constraints of the container snapshot feature in the instance rescheduling application scenario**:

- MindCluster supports saving the Host snapshot image of the instance node container only through the CRD deployment mode.

- MindCluster saves only one container Host snapshot image for instances of the same type. For example, in a 2P1D scenario, the container Host snapshot image is saved only for the first P instance.

- To facilitate management of the container Host snapshot image of an instance, MindCluster currently supports saving the Host snapshot image only in the cluster shared storage path.

**Configuration required on the Motor service framework side**:

Add the following to `user_config.json`:

```json
"motor_container_snapshot_config": {
    "enable_snapshot": true
}
```

Modify the configuration in `infer_service_template.yaml`. The following uses a Union instance as an example and shows only the changes; for the YAML baseline configuration, see `examples/deployer/yaml_template`:

```yaml
...
    - name: union
      replicas: 4
      workload:
        apiVersion: apps/v1
        kind: StatefulSet
      # --------TODO 1: Add the snapshot label in metadata--------
      metadata:
        labels:
          infer.huawei.com/container-snapshot: 'true'
      # ----------------------------------------------------
      spec:
        # --------TODO 2: Add the pod parallel startup policy--------
        podManagementPolicy: Parallel
        # ------------------------------------------
        replicas: 2
        selector:
          matchLabels:
            app: mindie-server
        template:
          metadata:
            labels:
              fault-scheduling: grace
              fault-retry-times: "10000"
              app: mindie-server
              ring-controller.atlas: ascend-910b
          spec:
            schedulerName: volcano
            nodeSelector:
              accelerator: huawei-Ascend910
              accelerator-type: module-910b-8
            terminationGracePeriodSeconds: 30
            automountServiceAccountToken: false
            securityContext:
              fsGroup: 1001
            containers:
            - image: mindie:1.0.0-aarch64-800I-A2
              imagePullPolicy: IfNotPresent
              name: mindie-server
              securityContext:
                allowPrivilegeEscalation: false
                # Because the syscalls on which thread creation depends differ across architectures, they are filtered and blocked under the RuntimeDefault seccomp policy
                # Therefore, set seccompProfile.type to Unconfined to disable seccomp syscall filtering for optimal compatibility
                # Note that Unconfined increases the container attack surface and is recommended only when necessary
                # If your cluster runs normally with seccompProfile.type: RuntimeDefault, you can use RuntimeDefault directly to obtain the runtime default security filtering
                # For details, see MindIE Motor/examples/features/pod_permission_guide/README.md
                seccompProfile:
                  type: Unconfined
              # --------TODO 3: Enable the readiness probe for MindCluster to detect the steady state--------
              readinessProbe:
                exec:
                  command:
                  - bash
                  - -c
                  - "$CONFIGMAP_PATH/probe.sh readiness"
                periodSeconds: 5
                timeoutSeconds: 4
                failureThreshold: 12
              # -----------------------------------------------------------------
              env:
              - name: POD_IP
                valueFrom:
                  fieldRef:
                    fieldPath: status.podIP
              - name: HOST_IP
                valueFrom:
                  fieldRef:
                    fieldPath: status.hostIP
              - name: CRIU_LOG_LEVEL
                value: "3"
              - name: CONFIGMAP_PATH
                value: /mnt/configmap
              - name: CONFIG_PATH
                value: /usr/local/Ascend/pyMotor/conf
              # --------TODO 4: Add the container host snapshot image save path (this path must be a shared storage path and must not be mounted inside the container)--------
              - name: host_snapshot_dir_path
                value: "path/to/container_host_image"
              # ---------------------------------------------------------------------------------------------
              lifecycle:
                preStop:
                  exec:
                    command: ["bash", "-c", "$CONFIGMAP_PATH/prestop.sh"]
              command: ["/bin/bash", "-c", "source /mnt/configmap/boot.sh;"]
              resources:
                requests:
                  memory: "64Gi"
                  cpu: "16"
                  huawei.com/Ascend910: 1
                limits:
                  memory: "256Gi"
                  cpu: "64"
                  huawei.com/Ascend910: 1
              volumeMounts:
              # --------TODO 5: Remove the host disk mount--------
              # - name: data
              #   mountPath: /data
              #   readOnly: true
              # ------------------------------------------
              - name: motor-config
                mountPath: /mnt/configmap
              - name: queue-schedule
                mountPath: /var/queue_schedule
              # --------TODO 5: Remove the host disk mount--------
              # - name: dshm
              #   mountPath: /dev/shm
              # - name: coredump
              #   mountPath: /var/coredump
              # ------------------------------------------
              - name: mnt
                mountPath: /mnt
              - name: hccn-tool
                mountPath: /usr/local/Ascend/driver/tools/hccn_tool
              - name: hccn-conf
                mountPath: /etc/hccn.conf
              - name: weight-mount
                mountPath: /mnt/weight
              # --------TODO 5: Remove the host disk mount--------
              # - name: plog-path
              #   mountPath: /root/ascend/log
              # ------------------------------------------

              # --------TODO 6: Add the following mount paths--------
              - name: snapshot-weight
                mountPath: /snapshot/weight
              - name: dcmi
                mountPath: /usr/local/dcmi
              - name: ascend-driver
                mountPath: /usr/local/Ascend/driver
                mountPropagation: "HostToContainer"
              - name: npu-smi
                mountPath: /usr/local/bin/npu-smi
              # ---------------------------------------

            volumes:
            # --------TODO 5: Remove the host disk mount--------
            # - name: data
            #   hostPath:
            #     path: /data
            # ------------------------------------------
            - name: motor-config
              configMap:
                name: motor-config
                defaultMode: 360
            - name: queue-schedule
              hostPath:
                path: /var/queue_schedule
            # --------TODO 5: Remove the host disk mount--------
            # - name: dshm
            #   emptyDir:
            #     medium: Memory
            #     sizeLimit: 4Gi
            # - name: coredump
            #   hostPath:
            #     path: /var/coredump
            #     type: DirectoryOrCreate
            # ------------------------------------------
            - name: mnt
              hostPath:
                path: /mnt
            - name: hccn-tool
              hostPath:
                path: /usr/local/Ascend/driver/tools/hccn_tool
            - name: hccn-conf
              hostPath:
                path: /etc/hccn.conf
            - name: weight-mount
              hostPath:
                path: /mnt/weight
            # --------TODO 5: Remove the host disk mount--------
            # - name: plog-path
            #   hostPath:
            #     path: /root/ascend/log
            #     type: DirectoryOrCreate
            # ------------------------------------------

            # --------TODO 6: Add the following mount paths--------
            - name: snapshot-weight
              hostPath:
                path: /mnt/snapshot/weight
                type: DirectoryOrCreate
            - name: dcmi
              hostPath:
                path: /usr/local/dcmi
            - name: ascend-driver
              hostPath:
                path: /usr/local/Ascend/driver
            - name: npu-smi
              hostPath:
                path: /usr/local/bin/npu-smi
            # ---------------------------------------
...
```
