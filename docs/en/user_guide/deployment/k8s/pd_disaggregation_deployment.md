# PD-Disaggregated Deployment (Based on the vLLM Inference Engine)

This document guides developers through deploying PD disaggregation in a Kubernetes environment based on the vLLM inference engine of MindIE Motor, and provides best-practice guidance for configuration optimization in production environments. If the underlying inference engine is SGLang, see [PD Disaggregation Service Deployment (Based on the SGLang Inference Engine)](./pd_disaggregation_sglang.md).

## Environment Pre-Check

When you deploy an inference service on Ascend 950PR&950DT products, you need to prepare the **hixlep configuration path**. (Skip this step for other hardware products.)

On each server, check whether the `/lib/route.conf` and `/etc/hccl_rootinfo.json` configuration files and the `/etc/hixlep` directory (UB link topology) exist and are correct. If they do not exist or are incorrect, refer to the [hixlep Configuration File Generation Guide](https://gitcode.com/cann/hixl/wiki/A5%20LocalCommRes%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md) to generate the corresponding content, and use the "D2D scenario" when generating `/etc/hixlep`.

## Obtaining the Startup Script

1. Prepare the environment as follows.
   - Prepare inference servers with sufficient storage for the model weights.
   - Complete [Environment Setup](../../environment_preparation.md) on all servers.
   - Download the model weights and save them to a path accessible from the servers.

2. Prepare the image.

   Obtain the image in either of the following ways, and load the image to all nodes of the K8s cluster:

   - Method 1: Downloading the official complete MindIE Motor image

      Go to the [Ascend official image repository](https://www.hiascend.com/en-sg/developer/ascendhub), enter `motor` in the search box, click the "mindie-motor" card, and on the "Image Version" tab, download the corresponding MindIE Motor image based on the device model.
   - Method 2: Installing MindIE Motor in an existing image

      The base image already has components such as CANN, vLLM, and vLLM Ascend installed. Refer to [Installing MindIE Motor Based on the vllm-ascend/sglang Image](../../maintenance/build_motor_image_from_vllm_ascend.md#installing-mindie-motor-based-on-the-vllm-ascendsglang-image) to additionally install MindIE Motor.

    After obtaining the image, use the following command to load the image to the servers:

    ```bash
    docker load -i xxxx.tar
    ```

    After the image is imported, use the following command to check whether the Docker image exists:

      ```bash
      docker images
      ```

3. Prepare the service startup script.

    Upload the `examples` directory to the master node of the K8s cluster. The `examples` directory can be obtained in either of the following two ways:

   - Method 1: Downloading the official complete MindIE Motor image

      The path in the image is `/tmp/motor/examples`. Run the following commands to obtain it:

        ```bash
        # Define a variable IMAGE that stores the image name or image ID
        IMAGE="<image name or image ID>"
        # Create a temporary container that is not started, and record the container ID
        cid=$(docker create "$IMAGE")
        # Copy the /tmp/motor/examples directory in the container to the current directory on the host
        docker cp "$cid:/tmp/motor/examples" ./examples
        # Delete the temporary container $cid created above to release resources and avoid leaving useless containers
        docker rm "$cid"
        ```

   - Method 2: Installing MindIE Motor in an existing image

     After cloning the code repository with git clone, the startup script is located in the `MindIE-Motor/examples` directory.

    The structure of the `examples` directory is as follows:

    ```text
    examples/
    ├── deployer/                  # Deployment tool directory
    │   ├── deploy.py              # Deployment entry script
    │   ├── delete.sh              # Uninstallation script
    │   ├── show_log.sh            # Log viewing script
    │   ├── README.md              # Deployment tool usage instructions
    │   ├── yaml_template/         # K8s YAML template
    │   ├── startup/               # Startup script
    │   ├── probe/                 # Probe script
    │   ├── log_collect/           # Log collection
    │   └── output_yamls/          # Generated YAML output directory
    └── model_configs/             # Configuration examples for each engine
        └── vllm/                  # vLLM engine configuration
            ├── user_config.json   # User configuration
            ├── env.json           # Environment variable configuration
            └── models/            # Model-specific configuration
    ```

    >[!NOTE] Note
    >
    >- The configuration files are located in the `examples/model_configs` directory. Select the corresponding configuration based on the engine type and model.
    >- For details about how to use the deployment tool, see `examples/deployer/README.md`.

## Generating the Configuration Files

Refer to the [MindIE Motor Configuration Auto-Generation Guide](../../../../../model_configs/vllm/models/README.md) to automatically generate the `user_config.json` and `env.json` configuration files.

## Deploying and Verifying the Service

Perform the following operations on the master node of the K8s cluster.

1. Run the following commands to start the service.

    ```bash
    # Create a namespace: <namespace> must be the same as job_id in user_config.json; the default is mindie-motor
    kubectl create namespace <namespace>

    # Enter the deployment tool directory
    cd examples/deployer
    # Start the PD disaggregation service: --config_dir specifies the directory containing user_config.json and env.json
    python3 deploy.py --config_dir ../../model_configs/vllm
    ```

    >[!NOTE] Note
    >- Image and weights: Ensure that image_name can be pulled normally within the cluster and that `weight_mount_path` exists on the host.
    >- Deployment failure: You can first uninstall the current service using the termination command in step 5, then troubleshoot and modify the configuration, and then redeploy.
    >- Weight loading timeout: In some vLLM versions, weight loading that takes longer than about 10 minutes may report a `timeout`, which usually does not affect program running. The description for the image/engine version in use takes precedence.
    >- Instance rescheduling constraint: The instance rescheduling capability depends on MindCluster. When a P/D instance contains multiple Pods, deleting one Pod directly does not trigger instance rescheduling.
    >- Impact of Prefix Cache on performance testing: Prefix Cache is enabled by default. If you need baseline performance data, add the following to the engine_config parameter block:
    >   - vLLM inference engine: "no-enable-prefix-caching": true
    >   - SGLang inference engine: "disable_radix_cache": true

2. Run the following command to view the service status.

   ```bash
   # View the Pod status: <namespace> must be the same as job_id above
   kubectl get pods -n <namespace> -o wide
   ```

   The names of the Pods/Deployments in the command output may vary with the template and engine_type. Identify them as follows:

   | Runtime Type | Description |
   | --- | --- |
   | mindie-motor-controller-xxxxx | Service management Pod, which monitors the health status of each instance. |
   | mindie-motor-coordinator-xxxx | Request scheduling Pod, which serves as the entry point for service requests. |
   | vllm-dx-xxxx | D instance Pod, which handles Decode inference. |
   | vllm-px-xxxx | P instance Pod, which handles Prefill inference. |

   A Pod status of `Running` only indicates that the Pod has been successfully scheduled and started; whether the service is ready still needs to be further confirmed based on the logs.

3. Run the following commands to view logs.

   ```bash
   # Edit log_collect/log_config.ini: change name_space to a namespace with the same name as job_id
   vim log_collect/log_config.ini
   # Collect and continuously follow logs: output directory log_collect/log/
   bash show_log.sh
   ```

4. Run the following command to verify the service.

   ```bash
   # Replace the placeholders: <master_node_ip> is the master node IP, and <served_model_name> is the value of the served_model_name field in user_config.json
   curl -X POST http://<master_node_ip>:31015/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "<served_model_name>",
       "messages": [{"role": "user", "content": "who are you?"}],
       "max_tokens": 36,
       "stream": true
     }'
   ```

   - If `{"detail":"Service is not available"}` is returned, the service is not ready yet, which is usually because the P/D or the Coordinator is not fully ready. Wait and retry, and check the logs of each Pod.
   - If streaming JSON is returned, inference is working properly. For more interfaces, see [Service Interfaces](../../api/service_interfaces.md).

5. Run the following command to terminate the service.

   ```bash
   # Uninstall the service: <namespace> must be the same as job_id above
   bash delete.sh <namespace>
   ```

## Feature Configuration

The user_config.json and env.json configuration files generated in [Generating the Configuration Files](#generating-the-configuration-files) have the following capabilities enabled by default: master/standby switchover, abnormal instance restart, service rate limiting, KV Cache affinity scheduling, KV pooling, and other capabilities. If you only need to adjust one of these capabilities, make the minimal configuration change described in this section.

### Master/Standby Switchover

Multiple Controllers and Coordinators are created and run on different servers. When a server becomes abnormal (down, restart, or maintenance and upgrade), the Controller and Coordinator components deployed on other servers take over the cluster, preventing an abnormally functioning single server from making service requests unschedulable.

**How It Works**

Multiple management plane and service plane Pods are created and distributed across multiple servers. When the master Pod becomes abnormal, a backup Pod set is selected to undertake the current service. Externally, the service cluster shows no abnormality at all.

**Enabling the Feature**

>[!NOTE] Note
>ETCD must be deployed in advance (the server side requires at least 3 replicas to ensure the reliability of the ETCD cluster). For details about this feature, see [Master/Standby Switchover](../../features/fault_tolerance/standby.md).

  ```json
  "motor_controller_config": {
    "standby_config": { "enable_master_standby": true }
  },
  "motor_coordinator_config": {
    "standby_config": { "enable_master_standby": true }
  }
  ```

**Disabling the Feature**

Delete the `standby_config` parameter block, or set the `enable_master_standby` parameter to `false`.

### Abnormal Instance Restart

When a P/D instance becomes abnormal, the inference instance is restarted to prevent the instance from staying in an abnormal state for a long time and affecting the inference capability of the cluster.

**How It Works**

When the main inference process exits unexpectedly or in similar situations, Motor automatically restarts the corresponding instance. Throughput decreases during the restart and returns to normal after the restart.
**Enabling the Feature**

Enabled by default. No additional operation is required.

**Disabling the Feature**

This feature cannot be disabled.

### Service Rate Limiting

Limit the maximum number of requests that the Coordinator, the inference entry point, processes per unit of time to prevent service overload.

**How It Works**

The Coordinator records the number of requests received within a period of time and stops accepting external requests once the threshold is reached.

**Enabling the Feature**

  ```json
  "motor_coordinator_config": {
    "rate_limit_config": {
      "enable_rate_limit": true,
      "max_requests": 10000,
      "window_size": 60
    }
  }
  ```

The preceding configuration indicates that a maximum of 10000 requests are processed within 60 seconds.

**Disabling the Feature**

Delete the `rate_limit_config` parameter block, or set the `enable_rate_limit` parameter to `false`.

>[!NOTE] Note
>For details about this feature, see [Service Rate Limiting](../../features/rate_limiting.md). For field descriptions, see the `rate_limit_config` field in [motor_coordinator_config](../../configuration/config_reference.md#motor_coordinator_config).

### Virtual Inference Health Probe

The NodeManager is responsible for the readiness probe, process supervision, and virtual inference health probe of the native engine:

- **Readiness probe**: Polls `/health` on the service port of the native engine, stays in STARTING within the startup_timeout window, and allows scheduling only after the probe succeeds.
- **Process liveness**: Supervises the process group of the native engine and triggers instance recovery when the main process or a worker process exits unexpectedly.
- **vLLM virtual inference**: Sends lightweight inference requests to the DP0 instance and identifies silent faults in combination with the AI Cube utilization. After the failure threshold is reached, the status is degraded, but the process is not terminated directly.
- **SGLang health probe**: Uses the SGLang native generative `GET /health` and does not create a Motor virtual inference monitor.
- **Software fault**: When the engine provides `/fault_tolerance/status`, EngineFtManager can supplement software fault reporting.

**Enabling the Feature**

  ```json
  "motor_engine_prefill_config": {
    "health_check_config": {
      "enable_virtual_inference": true,
      "npu_usage_threshold": 10,
      "health_collector_timeout": 5,
      "health_collector_timeout_retry_attempts": 3,
      "startup_timeout": 1800
    }
  }
  ```

**Disabling the Feature**

Set the `enable_virtual_inference` parameter to `false`. This does not disable the SGLang native generative `GET /health`.

**Log Level Restriction**

vLLM virtual inference can be enabled only when the final engine environment has `ASCEND_GLOBAL_LOG_LEVEL=3` (ERROR by default when not configured). For details about this feature, see [Virtual Inference Health Probe](../../features/sim_inference.md).

### KV Cache Affinity Scheduling

Schedule requests with the same prefix to the same instance to reuse the existing KV Cache and reduce Prefill time.

**How It Works**

The KV Cache affinity scheduling capability of MindIE Motor relies on the Mooncake Conductor component of the Mooncake community. It allows the scheduler to preferentially schedule requests to the instances that have cached the corresponding KV based on the KV Cache location, thereby reducing the overhead of KV Cache transfer across instances and improving inference throughput and response speed.

**Enabling the Feature**

- `motor_coordinator_config`: enables KV Cache affinity scheduling.
- `motor_engine_prefill_config`: enables KV event publishing and the Prefix Cache feature.
- `kv_conductor_config`: KV Conductor configuration, at the same level as `motor_engine_prefill_config`.

```json
"motor_coordinator_config": {
  "scheduler_config": { "scheduler_type": "kv_cache_affinity" }
},
"motor_engine_prefill_config": {
  "engine_config": {
    "kv-events-config": {
      "publisher": "zmq",
      "enable_kv_cache_events": true,
      "endpoint": "tcp://*:5557",
      "topic": "kv-events",
      "replay_endpoint": "tcp://*:6667"
    },
    "enable-prefix-caching": true
  }
},
"kv_conductor_config": {
  "kvevent_instance": { "mooncake_master": { "type": "Mooncake" } },
  "http_server_port": 13333
}
```

**Disabling the Feature**

Delete the preceding configuration items.

>[!NOTE] Note
>Ensure that the KV Conductor component is installed in the image. For details about this feature, see [KV Cache Affinity Scheduling](../../features/kvcache_affinity.md).

### PD Heterogeneous (PR/DT) Scheduling

Ascend 950PR&950DT products are divided into two types: PR and DT. Their memory differs. DT has larger memory bandwidth and better performance, making it more suitable for deploying Decode; Prefill can be deployed on PR. Therefore, in **PR + DT networking**, P instances must be scheduled to PR nodes and D instances to DT nodes.

**How It Works**

Append `huawei.com/npu.chip.name` to the `nodeSelector` of the Prefill/Decode Pods in PD disaggregation so that it matches the existing chip label on the node.

**Feature Constraints**

After this feature is enabled, Prefill can be scheduled only to PR nodes and Decode only to DT nodes, and recovery or scaling of the same role can use only idle nodes of the corresponding type.

| Feature | PR+DT Heterogeneous Networking | Description |
|------|------------|------|
| ScaleP2D | Not supported | Stopping P releases PR nodes, but D can still be scheduled only to DT nodes and cannot use these nodes to complete recovery. |
| MindCluster instance rescheduling, container snapshot default rescheduling | Restricted | P can be rescheduled only to idle PR nodes and D only to idle DT nodes. Recovery is impossible when no idle node of the corresponding type is available. |
| Automatic elastic scaling | Restricted | Instances can be scaled in or out only by role on PR/DT nodes; PR resources cannot be transferred to D scale-out by scaling in P. Scaling out P requires idle PR nodes, and scaling out D requires idle DT nodes. |
| Manual scaling | Restricted | Instances can be scaled in or out only by role on PR/DT nodes; PR resources cannot be transferred to D scale-out by scaling in P. Scaling out P requires idle PR nodes, and scaling out D requires idle DT nodes. |
| In-place restart of abnormal instances, engine relaunch | Supported | The instance remains in the original Pod on the original node, and the card type does not change. |
| Controller/Coordinator master/standby switchover | Supported | The management plane does not use the PR/DT `nodeSelector`. |
| Request rescheduling in Coordinator failure scenarios | Supported | Requests are forwarded to other healthy instances, and the node type binding of P/D is not changed. |
| Fault isolation, virtual inference health probe, token-level re-inference | Supported | Does not depend on occupying nodes across PR/DT. |
| KV Cache affinity scheduling, KV pooling, service rate limiting, Tracing | Supported | Unrelated to card type scheduling. |

In PR+DT networking, it is recommended to keep `enable_scale_p2d` set to `false`. Instance rescheduling and elastic scaling work as originally designed only when nodes of the corresponding type have idle capacity.

**Enabling the Feature**

Fill in the corresponding fields in the Prefill/Decode configurations. If a field is not filled in, this `nodeSelector` entry is not added.

  ```json
  "motor_engine_prefill_config": {
    "npu_chip_name": "Ascend950PR"
  },
  "motor_engine_decode_config": {
    "npu_chip_name": "Ascend950DT"
  }
  ```

**Disabling the Feature**

Delete the preceding fields or leave them empty.

>[!NOTE] Note
>This feature takes effect only in Kubernetes PD disaggregation. When `hardware_type` is `Ascend950`, the base `nodeSelector` is `accelerator: huawei-npu`; the chip name label is an additional entry and does not need to be filled in for all-PR or all-DT networking. Docker-based deployment has no scheduler, so deploy according to the output of [Deploying Multi-Container PD Disaggregation Services with Docker](../docker/host/pd_disaggregation_deployment.md#starting-service) (that is, issue the Prefill command on PR and the Decode command on DT).

### KV Pooling Capability

Offload the KV cache to a shared pool through MultiConnector, enabling reuse across instances and reducing memory pressure.

**How It Works**

Allow P/D instances to share KV cache through the KV cache pool: P instances push the computed KV Cache into the cache pool, and D instances pull and reuse it from the cache pool, thereby improving memory utilization and inference throughput in PD disaggregation scenarios.

**Enabling the Feature**

For details about this feature, see [KV Pooling Capability](../../features/kv_cache_store/README.md).

**Disabling the Feature**

Switch to a single connector other than MultiConnector, and delete the kv_cache_pool_config parameter block.
