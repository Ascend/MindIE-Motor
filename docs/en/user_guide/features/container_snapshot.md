# Container Snapshot Feature

## Feature Introduction

Container snapshot is used to save the running state of instance node containers and directly restore inference services when an instance is subsequently created or rescheduled, reducing the waiting time caused by model reloading and initialization.

A container snapshot consists of the following two parts:

- Runtime model weights: saved by the inference engine to a host mount directory.
- Container Host snapshot image: saves the container process and the Device snapshot state.

The inference engine is responsible for Device-side suspend and resume; Motor is responsible for preparing snapshot metadata, detecting the engine state, and refreshing control-plane information such as the Controller domain name, job name, and Pod IP after restoration. Host-side checkpoint and restore are performed by the default K8s + MindCluster deployment environment or by the user.

### Constraints and Limitations

| Dimension | Constraint |
|------|----------|
| Hardware | <ul><li>Atlas 800I A2</li><li>Atlas 800I A3</li></ul>Container snapshots of the Atlas 800I A2 can be shared only between machines of the same chip generation. |
| Automatic management mode | Only the default K8s + MindCluster CRD deployment mode with the containerd container runtime is supported. |
| Inference engine | It must support Device snapshot saving and restoration and provide suspend and resume interfaces. Currently, only vLLM is supported. |
| Feature mutual exclusion | Hot update of the Node Manager configuration is not supported after container snapshot is enabled. |
| Operating system | openEuler 24.03 and later versions, and HCE 3.0 are supported. The openEuler-customized CRIU 3.19 must be preinstalled. |
| Storage | The container Host snapshot image save directory must not be mounted into the container. The runtime model weight directory must be mounted into the container and remain accessible during snapshot creation and restoration. |

## Feature Usage

### Environment Preparation

Before enabling container snapshot, complete the following preparations:

1. Confirm that the node operating system meets the requirements, and install the openEuler-customized CRIU 3.19 from source on the node.

   1. Install the compilation dependencies:

      ```bash
      dnf --setopt=sslverify=false install -y \
          libcap-devel \
          libnet-devel \
          libnl3-devel \
          libselinux-devel \
          protobuf-c-devel \
          protobuf-devel \
          python3-devel \
          python3-protobuf \
          python3-wheel \
          xmlto
      ```

   2. Download the openEuler-customized CRIU 3.19 source code, and compile and install it:

      ```bash
      git clone https://atomgit.com/src-openeuler/criu.git
      cd criu
      git checkout openEuler-24.03-LTS-SP4
      mkdir -p /root/rpmbuild/SOURCES/
      cd ..
      cp -r criu/* /root/rpmbuild/SOURCES/
      cd /root/rpmbuild/SOURCES/
      rpmbuild -bp criu.spec
      cd /root/rpmbuild/BUILD/criu-3.19
      vi criu/include/pipes.h # When the number of DPs in a single container is large, pipe OOM occurs during snapshot creation. You need to increase the NR_PIPES_WITH_DATA macro in pipes.h to 2048.
      make GRUS=1 -j
      make npu_plugin -j
      cp /root/rpmbuild/BUILD/criu-3.19/criu/criu /usr/sbin/
      mkdir -p /usr/lib/criu/
      cp /root/rpmbuild/BUILD/criu-3.19/plugins/npu/npu_plugin.so /usr/lib/criu/
      ```

   3. Check the CRIU version to confirm that the installation is successful:

      ```bash
      criu --version
      ```

      If the version number in the command output is `3.19`, the installation is successful.

2. In the default K8s + MindCluster deployment mode, confirm that the cluster uses containerd and that the MindCluster components and related CRDs that support container snapshot have been installed. For the environment requirements, component deployment, and usage process on the MindCluster side, see [Container Snapshot Deployment and Usage](https://gitcode.com/Ascend/mind-cluster/blob/master/docs/en/scheduling/04_usage/10_infer_operator_best_practice/06_container_snapshot_usage.md).
3. Prepare the host storage directories required for saving the container Host snapshot image and runtime model weights. If cross-node restoration is required, ensure that the target node can access these directories.
4. When other deployment modes are used, the deployment platform or the user must prepare Host-side CRIU tools (for example, GRUS), the snapshot metadata file, and the corresponding container mounts.

### Application Scenarios

**Scenario 1: Default K8s + MindCluster deployment mode**

- Applicable scenario: a standard K8s deployment that uses containerd and MindCluster CRDs and needs to shorten the time consumed by instance creation or rescheduling.
- Characteristics: after container snapshot is enabled through `deploy.py`, the deployment environment automatically completes snapshot configuration, baseline snapshot creation, cache detection, and snapshot restoration.
- Benefits: after the first cold start generates a baseline snapshot, subsequent instances can be restored directly from the existing snapshot cache, reducing the time consumed by model weight loading and runtime environment initialization.

**Scenario 2: Other deployment modes**

- Applicable scenario: the default K8s + MindCluster deployment process is not used, but the deployment platform provides Host-side and Device-side snapshot capabilities, for example, deploying Motor through the ModelArts platform.
- Characteristics: Motor does not provide automatic container snapshot management for non-default deployment modes. The deployment platform must integrate snapshot metadata management, container mounts, snapshot file maintenance, and the checkpoint and restore process by itself.

### Usage Examples

#### Scenario 1: Default K8s + MindCluster Deployment Mode

The user needs to configure the following in `user_config.json`:

```json
"motor_container_snapshot_config": {
    "enable_snapshot": true,
    "host_snapshot_image_path": "/shared/container-snapshots",
    "mnt_mount_path": "/mnt/runtime-data",
    "device_snapshot_weight_path": "/shared/snapshot-weights"
}
```

| Configuration Item | Type | Mandatory | Default Value | Description |
|--------|------|------|--------|------|
| enable_snapshot | bool | Yes | `false` | Master switch for container snapshots. When this field is set to `false`, all other fields do not take effect. |
| host_snapshot_image_path | string | Yes | None | Host save directory for the container Host snapshot image, which must be an absolute path. Snapshots in this directory are named after the namespace of the current service. This directory must not be mounted into the container, and must not be the same as or a parent or child directory of `mnt_mount_path`. |
| mnt_mount_path | string | Yes | None | Host path and container mount path of the `mnt` volume, which must be an absolute path and must not overlap with `host_snapshot_image_path`. |
| device_snapshot_weight_path | string | Yes | None | Host save directory for Device runtime model weights, which must be an absolute path. Snapshot weights in this directory are named by combining the model weight name, the number of DPs, and the number of TPs; inside the container, they are always mounted to `/snapshot/weight`. |

When the service is started through `deploy.py`, Motor completes the following configurations:

- Renders the configurations required for container snapshot based on the general deployment template, and generates the final `infer_service.yaml`.
- Configures the snapshot label, the Readiness Probe, and the required mount items.
- Verifies that no path intersection exists between `host_snapshot_image_path` and `mnt_mount_path`.

Snapshot metadata is managed by the deployment environment, and `snapshot_metadata_path` does not need to be configured. After container snapshot is enabled, the deployment environment saves and uses the snapshot cache according to the following rules:

- The container Host snapshot image is saved in `host_snapshot_image_path` and named after the namespace of the current service.
- Device snapshot weights are saved in `device_snapshot_weight_path` and named by combining the model weight name, the number of DPs, and the number of TPs.

The instance creation process depends on whether the current service already has a snapshot cache:

- No snapshot cache: the instance first performs a cold start; after the snapshot steady state is reached, the deployment environment creates and saves a baseline snapshot.
- Existing snapshot cache: the instance skips the complete cold start process and directly loads the container Host snapshot image and the corresponding Device snapshot weights to restore startup.

Each time the service is created or restarted, the deployment environment checks whether a snapshot named after the current namespace exists in `host_snapshot_image_path`, and selects cold start or snapshot restoration startup accordingly.

> [!NOTE]NOTE
> When the baseline snapshot is created for the first time, the instance still needs to complete a cold start and Device suspend. Only after the baseline snapshot is available can subsequent instances be started through snapshot restoration.

<!-- -->
> [!WARNING]WARNING
> The snapshot cache is bound to the model and parallel configuration used when the snapshot is created. After the model is changed or parallel configurations such as DP and TP are modified, if a new snapshot needs to be created, you must first delete the original snapshot cache named after the current namespace in `host_snapshot_image_path`, and then restart the service to create a new snapshot. Otherwise, the service hits the old cache and starts with the snapshot corresponding to the old model or old parallel configuration. Old snapshot weights that are no longer used in `device_snapshot_weight_path` can be cleaned up as well.

#### Scenario 2: Other Deployment Modes

Other deployment modes do not automatically generate deployment YAML templates through `deploy.py`, and the script does not perform checkpoint, restore, or snapshot file management. The deployment platform must integrate the following process. If the deployment platform does not provide automatic management capabilities, the user must complete the process manually. The following uses the GRUS tool as an example.

1. Create a snapshot metadata file and mount it to the instance container. The file must be a valid JSON object, and its initial content can be empty.

   ```json
   {}
   ```

   The snapshot metadata file must be readable and writable inside the container. The fields used during snapshot creation and restoration are as follows:

   | Field | Usage Phase | Mandatory | Description |
   |------|----------|------|------|
   | model_save_path | Snapshot creation | Yes | Save path for runtime model weights, which must point to a host mount directory inside the container. |
   | model_load_path | Snapshot restoration | Yes | Load path for runtime model weights, which must point to a host mount directory inside the container. |
   | job_name | Snapshot restoration | Yes | Unique identifier of the restored inference instance. Node Manager uses this field to update the job name. |
   | namespace | Snapshot restoration | As required | Kubernetes namespace, which is configured when the in-cluster Controller DNS is used. It can be omitted in other network modes. |
   | controller_ip | Snapshot restoration | As required | IPv4 or IPv6 address of the Controller after restoration, which is configured when the Controller is communicated with by IP. It can be omitted when the in-cluster Controller DNS is used. |
   | data_parallel_master_ip | Snapshot restoration | No | IP address of the Data Parallel Master. If it is not configured, Node Manager writes the address delivered by the Controller. |
   | checkpoint | Snapshot creation | Yes | Set to `"done"` after the Host-side checkpoint is completed, which is used to notify the inference engine to release the Device lock. |

2. Enable container snapshot in `user_config.json`, and set `snapshot_metadata_path` to the path of the metadata file inside the container.

   ```json
   "motor_container_snapshot_config": {
       "enable_snapshot": true,
       "snapshot_metadata_path": "/snapshot/snapshot_metadata.json"
   }
   ```

3. Before cold-starting the instance, configure the save path for runtime model weights in the snapshot metadata file.

   ```json
   {
       "model_save_path": "/snapshot/weight"
   }
   ```

4. After the instance cold-starts, query the Node Manager status to confirm whether the instance has reached the steady state at which checkpoint can be executed.

   ```bash
   curl http://{instance-ip}:{node-manager-management-port}/node-manager/status
   ```

   When the interface returns HTTP 200 and the response is `{"status": true}`, use GRUS to perform the Host-side checkpoint and save the container Host snapshot image:

   ```bash
   curl -sS -N --unix-socket /var/run/grus/grus.sock \
     -X POST "http://localhost/checkpoint" \
     -d "container_id={container-id}&location={host-snapshot-image-path}"
   ```

5. After the checkpoint is completed, set `checkpoint` to `"done"` in the snapshot metadata file. After detecting this state, the inference engine releases the Device lock, and the instance resumes providing inference services.

   ```json
   {
       "model_save_path": "/snapshot/weight",
       "checkpoint": "done"
   }
   ```

6. When the container needs to be restored from the snapshot, configure the GRUS RuntimeClass, the container Host snapshot image path, and the restoration flag in the YAML of the instance to be restored. The following shows only the related configurations.

   ```yaml
   spec:
     template:
       spec:
         runtimeClassName: grus
         containers:
         - name: <container-name>
           env:
           - name: GRUS_SNAPSHOT_IMAGE_PATH
             value: "/path/to/host-snapshot-image"
           - name: GRUS_SNAPSHOT_RESTORED_FLAG
             value: "true"
   ```

7. Before performing restore, supplement the information required for restoration in the snapshot metadata file.

   ```json
   {
       "model_save_path": "/snapshot/weight",
       "checkpoint": "done",
       "model_load_path": "/snapshot/weight",
       "data_parallel_master_ip": "x.x.x.x",
       "job_name": "<job-name>",
       "namespace": "<namespace>"
   }
   ```

   Configure `namespace` or `controller_ip` according to the Controller communication mode: configure `namespace` when the in-cluster Controller DNS is used; when IP communication is used, replace `namespace` in the preceding example with `"controller_ip": "<controller-ip>"`. `data_parallel_master_ip` can be omitted, and Node Manager writes it after receiving the address delivered by the Controller.

8. After the container is restored, query the Node Manager status.

   ```bash
   curl http://{instance-ip}:{node-manager-management-port}/node-manager/status
   ```

   When the interface returns HTTP 200 and the response is `{"status": true}`, the instance has been restored and is ready.

   > [!NOTE]NOTE
   > An instance cannot provide inference services during the checkpoint. Before `checkpoint` is updated to `"done"`, Node Manager does not report normal heartbeats.

### Verification

#### Verify Whether the Container Snapshot Is Created

In the configured `host_snapshot_image_path`, go to the directory named after the namespace of the current service, and check whether the `snapshot_status.json` file is generated in the subdirectory of each instance. The directory structure is as follows:

```text
<host_snapshot_image_path>/<namespace>/<instance-name>/snapshot_status.json
```

For example, when `host_snapshot_image_path` is configured as `/path/to/host-snapshot`, the namespace is `mindie-motor`, and the instance name is `vllm-0-decode`, the corresponding file is:

```text
/path/to/host-snapshot/mindie-motor/vllm-0-decode/snapshot_status.json
```

If `snapshot_status.json` is generated in the directory of each instance of the current service, the container snapshot is created. If some instances lack this file, check the logs of the corresponding instance and of the MindCluster infer-operator.

#### Verify Whether the Instance Is Restored from the Snapshot Successfully

After an instance is created from a snapshot, check the instance logs and verify the Host-side container restoration and the Device-side restoration results in sequence.

1. Check whether the logs contain the following content:

   ```text
   [snapshot] Node manager is restored from host side snapshot, registering...
   ```

   You can run the following command to query it:

   ```bash
   kubectl logs -n <namespace> <instance-pod> | grep -i "restored from host side snapshot"
   ```

   The presence of this content in the logs indicates that Node Manager has identified that the current container is restored from a Host snapshot.

2. Check the number of occurrences of the following content in the instance logs:

   ```text
   It took <time> seconds to resume.
   ```

   You can run the following command to count them:

   ```bash
   kubectl logs -n <namespace> <instance-pod> | grep -c "It took .* seconds to resume"
   ```

   The number of log entries should be equal to the number of DPs of the instance. If the Host-side container restoration log exists and the Device side prints resume success logs whose number equals the number of DPs, the instance is restored from the snapshot successfully.

When the feature is enabled for the first time and no snapshot cache exists, the instance performs a cold start and creates a baseline snapshot, so the restoration logs are not printed. If the current namespace is confirmed to have a snapshot cache but the content still does not appear in the logs, check whether the snapshot name in `host_snapshot_image_path` is consistent with the current namespace, whether the file content is complete, whether the MindCluster infer-operator component is running properly, and whether the target node can access the snapshot.

#### Verify Whether the Instance Is Ready Again

Check the Coordinator logs to see whether they contain the log indicating that the current instance has been added to the available instance pool:

```text
Added instance ID <instance-id> (role: <role>, job_name: <job-name>) with <endpoint-num> endpoints to available pool successfully
```

You can run the following command to query it:

```bash
kubectl logs -n <namespace> <coordinator-pod> | grep "Added instance ID"
```

If the logs contain a record corresponding to the `job_name` of the current instance, the instance has been added to the available instance pool of the Coordinator again and is ready.

## FAQs

### Path Intersection Is Reported When the Snapshot Path Is Configured

**Symptom**

When container snapshot is enabled and the service is started, the system reports that a path intersection exists between `host_snapshot_image_path` and `mnt_mount_path`.

**Cause Analysis**

`host_snapshot_image_path` and `mnt_mount_path` are configured as the same directory, or one of them is the parent directory of the other. The container Host snapshot image save directory must not be mounted into the container, so the two paths must not intersect.

**Solution**

1. Check the configurations of `host_snapshot_image_path` and `mnt_mount_path`.
2. Change `host_snapshot_image_path` to a standalone host directory that is not mounted into the container, and ensure that the two paths are neither the same nor parent and child directories of each other.
3. Run `deploy.py` again to start the service.

### Old Snapshot Is Still Loaded After the Model or Parallel Configuration Is Changed

**Symptom**

After the model is changed or parallel configurations such as DP and TP are modified and the service is restarted, the instance still loads the snapshot from before the modification.

**Cause Analysis**

When the service starts, it preferentially looks for the snapshot named after the current namespace in `host_snapshot_image_path`. If the old snapshot cache of the current namespace is not deleted, the service directly starts with the snapshot corresponding to the old model or old parallel configuration.

**Solution**

1. Delete the old snapshot cache named after the current namespace in `host_snapshot_image_path`.
2. Clean up the old snapshot weights that are no longer used in `device_snapshot_weight_path` as required.
3. Restart the service, and confirm that the instance completes a cold start with the new model and new parallel configuration and creates a new baseline snapshot.
