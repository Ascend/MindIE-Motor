# PD Disaggregation Deployment Guide

This document uses a **complete and detailed** deployment case to guide developers through deploying the Motor-based PD disaggregation service, and provides guidance on configuration optimization practices for production environments.

## Pre-check for 950 Series Servers (Can Be Omitted for Other Server Series)

- **Preparing the hixlep configuration path**

    When deploying inference services on 950 series servers, check on each server whether the `/lib/route.conf` and `/etc/hccl_rootinfo.json` configuration files and the `/etc/hixlep` directory (UB link topology structure) exist and are correct. If they do not exist or are incorrect, refer to the [hixlep configuration file generation document](https://gitcode.com/cann/hixl/wiki/A5%20LocalCommRes%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md) to generate the corresponding content, and use the "D2D scenario" when generating `/etc/hixlep`.

## Obtaining the Startup Script (Setup and Image Preparation)

1. **Environment requirements**

   - Prepare inference servers with sufficient storage for model weights.

   - All servers have completed [environment preparation](../../environment_preparation.md).

   - Model weights have been downloaded and saved to a path accessible from the servers.

2. **Preparing the image**

   Obtain the image through the following methods, and **load the image to all nodes in the K8s cluster**:

   - **Method 1**: Downloading the official complete MindIE Motor image

     Go to the [Ascend official image repository](https://www.hiascend.com/developer/ascendhub), search for `motor`, and select the corresponding MindIE Motor image based on the device model.

   - **Method 2**: Installing MindIE Motor in an existing image

     The base image already has CANN, vLLM, vllm-ascend, and other components installed. You can additionally install MindIE Motor by referring to [Building a MindIE Motor Image from vllm-ascend](../../maintenance/build_motor_image_from_vllm_ascend.md#installing-mindie-motor-based-on-the-vllm-ascendsglang-image).

   After obtaining the image, run the following command to load the image to the server:

     ```bash
     docker load -i xxxx.tar
     ```

   After the image is imported, run the following command to check whether the Docker image exists:

     ```bash
     docker images
     ```

3. **Preparing the service startup script**

   Upload the `examples` directory to the master node of the K8s cluster:

   - Using the **official full MindIE Motor image**: The path in the image is `/tmp/motor/examples`. You can run:

     ```bash
     IMAGE="<image name or image ID>"
     cid=$(docker create "$IMAGE")
     docker cp "$cid:/tmp/motor/examples" ./examples
     docker rm "$cid"
     ```

   - Using the **image with MindIE Motor manually installed**: After running `git clone` on the code repository, the startup script is located in `MindIE-Motor/examples`.

   For more details about the contents of the `examples` directory, see the appendix at the end of this chapter.

## Preparing Model-related Configuration Files

MindIE Motor provides [**PD disaggregation configuration examples**](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/models/README.md) for common models (deepseek_v4_flash, deepseek_v4_pro, glm 5.2, etc.), and **users can use them directly after modifying a small amount of configuration**.

For models without typical configurations provided, refer to the [MindIE Motor Configuration Auto-generation Guide](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/examples/infer_engines/vllm/models/README.md) to automatically generate the configuration files `user_config.json` and `env.json`.

## Service Deployment and Verification

The following operations are all performed on the master node of the K8s cluster.

1. **Starting the service**

   ```bash
   # Create the namespace: <namespace> must be consistent with job_id in user_config.json. The default value is mindie-motor
   kubectl create namespace <namespace>

   # Enter the deployment tool directory
   cd examples/deployer
   # Start the PD disaggregation service: --config_dir specifies the directory containing user_config.json and env.json
   python3 deploy.py --config_dir ../infer_engines/vllm
   ```

2. **Checking the status**

   ```bash
   # Check the Pod status: <namespace> must be consistent with job_id mentioned above
   kubectl get pods -n <namespace> -o wide
   ```

   The names of each `Pod` / `Deployment` in the output may vary with the template and `engine_type`. You can identify them as follows:

   | Runtime Type | Description |
   | --- | --- |
   | `mindie-motor-controller-xxxxx` | Service management Pod that monitors the health status of each instance |
   | `mindie-motor-coordinator-xxxx` | Request scheduling Pod, the entry point for service requests |
   | `vllm-dx-xxxx` | D instance Pod, responsible for Decode inference |
   | `vllm-px-xxxx` | P instance Pod, responsible for Prefill inference |

   A Pod status of `Running` only indicates that it has been successfully scheduled and started. Whether the service is ready still needs to be further confirmed based on the logs.

3. **Viewing logs**

   ```bash
   # Enter the deployment tool directory
   cd examples/deployer
   # Edit log_collect/log_config.ini: change name_space to the same namespace as job_id
   vim log_collect/log_config.ini
   # Collect and continuously track logs: output directory log_collect/log/
   bash show_log.sh
   ```

4. **Verifying the service**

   ```bash
   # Replace the placeholders: <master_node_ip> with the master node IP, and <served_model_name> with the value of the served_model_name field in user_config.json
   curl -X POST http://<master_node_ip>:31015/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{
       "model": "<served_model_name>",
       "messages": [{"role": "user", "content": "who are you?"}],
       "max_tokens": 36,
       "stream": true
     }'
   ```

   - Returns `{"detail":"Service is not available"}`: the service is not ready yet. Retry later.

   - Returns streaming JSON: inference is normal. For more interfaces, see [Service Interfaces](../../api/service_interfaces.md).

5. **Terminating the service**

   ```bash
   # Enter the deployment tool directory
   cd examples/deployer
   # Uninstall the service: <namespace> is the same as the job_id above
   bash delete.sh <namespace>
   ```

## Feature Configuration Guide

The full examples of `user_config.json` and `env.json` above have enabled capabilities such as active/standby switchover, abnormal instance restart, service rate limiting, virtual inference, KV affinity scheduling, and KV pooling by default. If you only need to adjust a specific capability, you can make minimal configuration changes by referring to this section.

### Active/Standby Switchover

Create multiple Controllers and Coordinators and run them on different servers. When a server becomes abnormal (crash, restart, or maintenance upgrade), the Controller and Coordinator components deployed on other servers take over the cluster, preventing a single server failure from making service requests unschedulable.

- **Principle**: Create multiple Pods on the management plane and service plane, distributed across multiple servers. When the active Pod becomes abnormal, a backup Pod is selected to take over the current service. Externally, the entire service cluster shows no abnormality.

- **Enabling**:

  ```json
  "motor_controller_config": {
    "standby_config": { "enable_master_standby": true }
  },
  "motor_coordinator_config": {
    "standby_config": { "enable_master_standby": true }
  }
  ```

- **Disabling**: Delete the `standby_config` configuration block, or set `enable_master_standby` to `false`.

- **Note**: etcd must be deployed in advance (three replicas are recommended). For details, see [Active/Standby Feature Description](../../features/fault_tolerance/standby.md).

### Abnormal Instance Restart

When a P/D instance becomes abnormal, the inference instance is restarted to prevent the instance from remaining in an abnormal state for a long time and affecting the cluster inference capability.

- **Principle**: When the inference main process exits abnormally, Motor automatically restarts the corresponding instance. During the restart, the throughput decreases, and it returns to normal after the restart.

- **Enabling**: Enabled by default, no additional operation is required.

- **Disabling**: No need to disable it.

### Service Rate Limiting

Limit the maximum number of requests per unit time at the inference entry Coordinator to prevent service overload.

- **Principle**: The Coordinator records the number of requests received within a period of time and stops accepting external requests after the threshold is reached.

- **Enabling**:

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

- **Disabling**: Delete the `rate_limit_config` configuration block, or set `enable_rate_limit` to `false`.

- **Note**: For field descriptions, see the **`rate_limit_config` field** in [motor_coordinator_config](../../configuration/config_reference.md#motor_coordinator_config).

### Virtual Inference Health Check

Probe the service health status to avoid business losses caused by silent faults. A silent fault manifests as follows: some processes hang, and the service appears normal but cannot perform inference properly.

- **Principle**: When the business traffic is low, lightweight inference requests are sent; when the business traffic is high, the NPU compute core utilization is checked. Unhealthy P/D instances are restarted to eliminate silent faults.

- **Enabling**:

The virtual inference feature must be enabled separately for P and D instances. The method for enabling the virtual inference health check for a P instance is as follows, and the method for a D instance is the same.

  ```json
  "motor_engine_prefill_config": {
    "health_check_config": {
      "enable_virtual_inference": true,
      "npu_usage_threshold": 10
    }
  }
  ```

- **Disabling**: Delete the `health_check_config` configuration block, or set `enable_virtual_inference` to `false`.

- **Note**: Virtual inference **can be enabled only at the ERROR log level** (`ASCEND_GLOBAL_LOG_LEVEL=3`; if not configured, the default is ERROR). If the engine process environment explicitly configures a non-ERROR level, Engine Server disables virtual inference and prints a warning before starting it. For details about this feature, see [Virtual Inference Health Check](../../features/sim_inference.md).

### KV Cache Affinity Scheduling

Schedule requests with the same prefix to the same instance to reuse the existing KV Cache and reduce Prefill latency.

- **Principle**: The MindIE Motor KV Cache affinity scheduling capability relies on the Mooncake Conductor component from the Mooncake community. It allows the scheduler to preferentially schedule requests to the instance that has cached the corresponding KV based on the KV Cache location, thereby reducing the overhead of cross-instance KV Cache transfer and improving inference throughput and response speed.

- **Enabling**:

  - `motor_coordinator_config`: enables KV affinity scheduling.

  - `motor_engine_prefill_config`: enables KV event publishing and the prefix cache feature.

  - `kv_conductor_config`: KV conductor configuration, at the same level as `motor_engine_prefill_config`.

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

- **Disabling**: Delete the preceding configuration items.

- **Note**: Ensure that the KV Conductor component is installed in the image. For details about this feature, see [KV Cache Affinity Scheduling](../../features/KV_cache_affinity.md).

### KV Pooling

Offload the KV cache to a shared pool through `MultiConnector`, supporting cross-instance reuse and reducing memory pressure.

- **Principle**: Allow P/D instances to share the KV cache through the KV cache pool. The P instance pushes the computed KV cache into the cache pool, and the D instance pulls and reuses it from the cache pool, thereby improving memory utilization and inference throughput in the PD disaggregation scenario.

- **Enabling**: This involves many parameters and is beyond the scope of this document. For details, see [KV Pooling Deployment Guide](../../features/kv_cache_store/README.md).

- **Disabling**: Switch to a single connector other than `MultiConnector`, and delete the root node `kv_cache_pool_config`.

- **Note**: For details, see [KV Pooling Deployment Guide](../../features/kv_cache_store/README.md).

## Appendix

### O&M Tips

- **Service not ready**: When the inference API returns `{"detail":"Service is not available"}`, it usually means that the P/D instances or the Coordinator are not fully ready yet. Wait and retry, and check the logs of each Pod.

- **Image and weights**: Ensure that `image_name` can be pulled normally within the cluster and that `weight_mount_path` exists on the host.

- **Deployment failure**: You can first uninstall by following the commands in the `Terminating the Service` section, troubleshoot and modify the configuration, and then redeploy.

- **Weight loading timeout**: For some vLLM versions, weight loading that exceeds about 10 minutes may report `timeout`, which usually does not affect program execution. Refer to the description of the image/engine version in use.

- **Instance rescheduling constraints**: The instance rescheduling capability depends on MindCluster. When a P/D instance contains multiple Pods, directly deleting one of the Pods does not trigger instance rescheduling.

- **Impact of Prefix Cache on performance testing**: Prefix Cache is enabled by default. If baseline performance is required, add `"no-enable-prefix-caching": true` (vLLM) or `"disable_radix_cache": true` (SGLang) to `engine_config`.

### examples Directory Structure

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
└── infer_engines/             # Configuration examples for each engine
    └── vllm/                  # vLLM engine configuration
        ├── user_config.json   # User configuration
        ├── env.json           # Environment variable configuration
        └── models/            # Model-specific configuration
```

- The configuration files are located in `examples/infer_engines/`. Select the corresponding configuration based on the engine type and model.

- For details about how to use the deployment tool, see `examples/deployer/README.md`.
