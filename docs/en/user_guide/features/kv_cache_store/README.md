# KV Pooling Capability

## Feature Introduction

KV pooling allows P/D (Prefill/Decode) instances to share and reuse KV Cache through external storage, thereby reducing repeated computation and improving inference throughput. UCM is one of the functions of the KV pooling capability, and it reduces Prefill computation by reusing the same prefix across requests.

### How It Works

KV pooling combines a transfer Connector and a Store Connector through `MultiConnector`. Different Store Connectors are all connected to the KV Cache query, load, and save process of vLLM, but their internal storage implementations are different.

**AscendStoreConnector**

- Both P/D instances load `AscendStoreConnector`. The P side writes KV Cache into the KV Pool as `kv_producer`, and the D side queries and loads KV Cache as `kv_consumer`.

- `AscendStoreConnector` is responsible for the unified matching, loading, and saving process, and its `backend` determines whether MemCache or Mooncake Store is used at the bottom layer.

- `kv_cache_store_config` configures the service address, port, and runtime parameters of the selected backend. MemCache uses MetaService, and Mooncake uses `mooncake_master`.

- The Mooncake transfer Connector in `connectors[0]` is responsible for real-time P/D KV transfer, which is different from the responsibility of the shared KV Pool in `connectors[1]`.

**UCMConnector**

- For the first request, Prefill computes the KV Cache, and `UCMConnector` writes the reusable prefix into storage layers such as Cache and POSIX according to `store_pipeline`.

- When subsequent requests have the same prefix, Prefill queries and loads the saved KV Cache through UCM, reducing repeated Prefill computation.

- The real-time P/D KV transfer of the current request is still completed by the Mooncake transfer Connector. In the current distributed P/D solution, Decode does not load UCM.

### Core Functions

MindIE Motor uses `MultiConnector` to combine the P/D transfer Connector with the Store Connector. The following pooling functions are currently supported:

- **Shared KV Pool based on Mooncake**: Both P/D load `AscendStoreConnector` and configure `backend: "mooncake"` to write and read the shared KV Cache respectively. For details, see [Mooncake Backend](./backend/mooncake.md).

- **Shared KV Pool based on MemCache**: Both P/D load `AscendStoreConnector` and configure `backend: "memcache"` to write and read the shared KV Cache respectively. For details, see [MemCache Backend](./backend/memcache.md).

- **UCM**: Combines UCM Stores such as Cache and POSIX through the `store_pipeline` of `UCMConnector`. In the current distributed P/D solution, Prefill saves and loads cross-request prefixes, and Decode does not load UCM. For details, see [Deploying UCM in MindIE Motor](./backend/ucm.md).

>[!NOTE]NOTE
>
>- MemCache and Mooncake are backends of `AscendStoreConnector`; UCM is another Store Connector but still belongs to the KV pooling capability. Do not configure `"backend": "ucm"`.
>- In addition, `MooncakeConnectorV1`, `MooncakeHybridConnector`, and others are responsible for real-time P/D transfer, and are not the same layer of configuration as the Mooncake Backend of `AscendStoreConnector`.

### Constraints and Limitations

| Constraint | Requirement |
|----------|------|
| Hardware | The default value of `local_service_mode` of the MemCache backend depends on the hardware type.<ul><li>Atlas 800I A2 and Ascend 950PR products: the default is `inprocess`</li><li>Atlas 800I A3: the default is `standalone`</li></ul> |
| Deployment scenario | Only the PD disaggregation deployment scenario is supported. |
| Engine | Only the vLLM inference engine is supported. |
| Feature mutual exclusion | <ul><li>`"backend": "ucm"` is not allowed, because UCM does not use the backend mechanism of `AscendStoreConnector`.</li><li>The `backend` in `AscendStoreConnector` and `kv_cache_store_config` must be consistent.</li><li>`MooncakeConnectorV1` and others are responsible for real-time P/D transfer, and are not the same layer of configuration as the Mooncake Backend of `AscendStoreConnector`, so they must not be confused.</li></ul> |
| Software dependency | <ul><li>Depends on the vllm-ascend KV transfer layer.</li><li>MemCache requires no additional installation and is preinstalled in the Motor image.</li><li>Mooncake is integrated by vllm-ascend and requires no additional installation.</li><li>UCM requires additional installation of the UCM wheel.</li></ul> |
| Other restrictions | <ul><li>`lookup_rpc_port` does not need to be filled in manually, and is automatically adapted by Motor.</li><li>`default_kv_lease_ttl` must be greater than `ASCEND_CONNECT_TIMEOUT` and `ASCEND_TRANSFER_TIMEOUT` in `env.json`.</li><li>`eviction_high_watermark_ratio` and `eviction_ratio` of the Mooncake backend are mandatory parameters, and `deploy.py` enforces validation.</li><li>MemCache internal configuration items are managed in `mmc-local-inprocess.conf` or `mmc-local-standalone.conf` of the corresponding mode, and do not need to be configured in `user_config.json`.</li></ul> |

## Using the Feature

### Environment Preparation

- The P/D disaggregation inference service has been deployed according to [PD Disaggregation Service Deployment](../../deployment/k8s/pd_disaggregation_deployment.md) and runs properly. KV pooling is enabled on top of this service and does not change the deployment mode of the Controller and Coordinator.

- For the general constraints of KV pooling, see [vllm-ascend kv_pool](https://docs.vllm.ai/projects/ascend/en/v0.23.0/user_guide/feature_guide/kv_pool.html). When the UCM function is enabled, the requirements in the [UCM deployment document](./backend/ucm.md) must also be met.

- Before enabling it, refer to [MindIE Motor Quick Start](../../quick_start.md) to ensure that the basic P/D disaggregation service can be deployed properly.

- All subsequent operations are performed only on the management node (master node) of the K8s cluster.

### Usage Scenarios

- Scenario 1: Shared KV Pool (using `AscendStoreConnector`)
  Both P/D instances load `AscendStoreConnector` and select MemCache or Mooncake Store as the shared KV Cache backend through `backend`. This scenario is suitable for sharing KV Cache between P/D instances to reduce transfer overhead.
- Scenario 2: UCM (using `UCMConnector`)
  Prefill connects to the UCM Store Pipeline through `UCMConnector` to save and load the KV Cache of the same prefix across requests, reducing repeated Prefill computation. Decode does not load UCM, and the real-time P/D KV transfer is still completed by the Mooncake transfer Connector. This scenario is suitable for requests that share a large amount of the same prefix (such as a shared prompt prefix).

### Usage Example

#### Deployment Example Using AscendStoreConnector

Pooling is implemented by combining a transfer connector (`connectors[0]`) and a pooling backend connector (`connectors[1]`) through `MultiConnector`. The following uses `MooncakeConnectorV1` (P/D collaboration) and `AscendStoreConnector` (KV pool backend) as an example.

When `AscendStoreConnector` is used, you need to configure both the `kv_transfer_config` of the P/D instances and the global `kv_cache_store_config`.

**Procedure**

The P/D transfer Connector is selected as follows:

**Table 1** <a id="table_Connector"></a>P/D transfer Connector selection

| Model attention architecture | `connectors[0]` | Typical model |
|---------------------|-----------------|----------|
| Standard attention | `MooncakeConnectorV1` / `MooncakeConnectorV2` | Qwen3, GLM-5, DeepSeek V3.1 |
| Hybrid attention | `MooncakeHybridConnector` | DeepSeek V4 / V4 Flash / V4 Pro |

>[!WARNING]WARNING
> `MooncakeConnectorV1` and `MooncakeConnectorV2` support only standard attention models. If they are misconfigured for a hybrid attention model, the Decode node may crash and restart during inference.

1. In the `user_config.json` configuration file, configure the **kv_transfer_config** field for `engine_config` under the `motor_engine_prefill_config` field of the P instance and the `motor_engine_decode_config` field of the D instance respectively.

     - **P instance (motor_engine_prefill_config):**

       ```json
       "motor_engine_prefill_config": {
         "engine_type": "vllm",
         "engine_config": {
           "...": "...",
           "kv_transfer_config": {
             "kv_connector": "MultiConnector",
             "kv_role": "kv_producer",
             "kv_connector_extra_config": {
               "connectors": [
                 {
                   "kv_connector": "MooncakeConnectorV1",
                   "kv_role": "kv_producer",
                   "kv_port": "30001"
                 },
                 {
                   "kv_connector": "AscendStoreConnector",
                   "kv_role": "kv_producer",
                   "kv_connector_extra_config": {
                     "backend": "memcache"
                   }
                 }
               ]
             }
           }
         }
       }
       ```

     - **D instance (motor_engine_decode_config):**

       ```json
       "motor_engine_decode_config": {
         "engine_type": "vllm",
         "engine_config": {
           "...": "...",
           "kv_transfer_config": {
             "kv_connector": "MultiConnector",
             "kv_role": "kv_consumer",
             "kv_connector_extra_config": {
               "connectors": [
                 {
                   "kv_connector": "MooncakeConnectorV1",
                   "kv_role": "kv_consumer",
                   "kv_port": "30002"
                 },
                 {
                   "kv_connector": "AscendStoreConnector",
                   "kv_role": "kv_consumer",
                   "kv_connector_extra_config": {
                     "backend": "memcache"
                   }
                 }
               ]
             }
           }
         }
       }
       ```

       **Key parameter description:**
       - kv_connector: connector type. `MultiConnector` is used to combine multiple Connectors.
       - connectors[0]: the real-time P/D transfer Connector, responsible for KV Cache transfer between P/D instances.
       - connectors[1]: the pooling backend Store Connector, responsible for reading and writing the shared KV Pool.
       - backend: the backend type of `AscendStoreConnector`. The options are `memcache` and `mooncake`, and it must be consistent with the global `kv_cache_store_config.backend`.
       - lookup_rpc_port: does not need to be filled in manually. The value of each DP instance is automatically adapted by Motor.

2. Configure kv_cache_store_config (global configuration).

    `kv_cache_store_config` is the global configuration for KV pooling and is shared by P/D instances. The following uses the default backend MemCache as an example:

    ```json
    "kv_cache_store_config": {
      "backend": "memcache",
      "local_service_mode": "standalone"
    }
    ```

    `backend` determines the pooling backend and must be consistent with the `backend` in `AscendStoreConnector`. The parameters of each backend are described as follows:

    **Table 2** Common parameters

    | Configuration Item | Type | Value Range | Mandatory/Optional | Default Value | Description |
    | --- | --- | --- | --- | --- | --- |
    | `backend` | string | `mooncake`, `memcache` | Optional | `memcache` | Pooling backend; defaults to `memcache` when not configured |
    | `target_job_id` | string | motor_deploy_config.job_id of the target service (that is, the target namespace) | Optional | Not configured | Reuse the kv_store of another K8s inference service |

    **Table 3** Mooncake-specific parameters

    | Configuration Item | Type | Value Range | Mandatory/Optional | Default Value | Description |
    | --- | --- | --- | --- | --- | --- |
    | `metadata_server` | string | `P2PHANDSHAKE` | Optional | `P2PHANDSHAKE` | Metadata server mode, defaults to the peer-to-peer handshake mode |
    | `protocol` | string | `ascend` | Optional | `ascend` | Underlying transport protocol |
    | `device_name` | string | NIC name | Optional | `""` | The bound NIC name; automatically selected when empty |
    | `global_segment_size` | string | Memory segment size | Optional | `1GB` | Global shared memory segment size |
    | `port` | int | Port number | Optional | `50088` | KV Pool service port; `deploy.py` fills in the default value when not configured |
    | `default_kv_lease_ttl` | int | Must be greater than `ASCEND_CONNECT_TIMEOUT` and `ASCEND_TRANSFER_TIMEOUT` | Optional | `11000` | Default lease TTL of KV objects (in milliseconds) |
    | `eviction_high_watermark_ratio` | float | 0.9 recommended | Mandatory | None | High watermark eviction line of the pooling space, passed to the `mooncake_master` process; deploy.py enforces validation and reports an error if it is missing |
    | `eviction_ratio` | float | 0.1 recommended | Mandatory | None | Eviction ratio per eviction, passed to the `mooncake_master` process; deploy.py enforces validation and reports an error if it is missing |
    | `store_mode` | string | `embedded`, `standalone` | Optional | `embedded` | Store deployment mode |
    | `local_buffer_size` | string | Size value | Optional | `1GB` | Engine-side transfer staging buffer in standalone mode |
    | `store_http_port` | int | Port number | Optional | `0` | REST port of the store process in standalone mode; the default value `0` indicates that the kernel allocates an ephemeral port |

    **Table 4** MemCache-specific parameters

    | Configuration Item | Type | Value Range | Mandatory/Optional | Default Value | Description |
    | --- | --- | --- | --- | --- | --- |
    | `local_service_mode` | string | `inprocess`, `standalone` | Optional | Atlas 800I A2/Ascend 950PR products: `inprocess`; Atlas 800I A3: `standalone` | LocalService deployment mode: `inprocess` (same process as vLLM) or `standalone` (independent process) |

    >[!NOTE]NOTE
    > All internal MemCache configuration items (DRAM pool size, communication protocol, MetaService port, SSD cache, UBSIO parameters, etc.) are managed by users in `mmc-local-inprocess.conf` or `mmc-local-standalone.conf` of the corresponding mode, and do not need to be configured in `user_config.json`. See [MemCache Backend Documentation](./backend/memcache.md).

3. (Optional) <a id="step3"></a>Configure multiple services to share the kv_store.

    When multiple K8s inference services exist in the cluster (each corresponding to an independent `job_id` / namespace), `target_job_id` can be used to enable subsequent services to reuse the kv_store deployed by the first service, without repeatedly starting MetaService / mooncake_master Pods.

    **Configuration example:**

    - First service (provides the kv_store):

      ```json
      "motor_deploy_config": {
        "job_id": "service-a"
      },
      "kv_cache_store_config": {
        "backend": "memcache"
      }
      ```

    - Second service (reuses the kv_store of the first service):

      ```json
      "motor_deploy_config": {
        "job_id": "service-b"
      },
      "kv_cache_store_config": {
        "backend": "memcache",
        "target_job_id": "service-a"
      }
      ```

    **Behavior description:**

    | Scenario | Behavior |
    | --- | --- |
    | `target_job_id` is not configured | Create a new kv_store Pod in the current namespace |
    | `target_job_id` is the same as the local `job_id` | Create a new kv_store Pod in the current namespace |
    | `target_job_id` points to another service, and a kv_store Service and a kv_store Pod in the Running state exist in the target namespace | Reuse the domain name of the target kv_store, and do not deploy a kv_store Pod for this service |
    | `target_job_id` is incorrect, or no available kv_store exists in the target namespace | Fall back to creating a new kv_store Pod in the current namespace |

    >[!NOTE]NOTE
    > The `deploy_mode` of the two services (`multi_deployment` / `infer_service_set`) should be consistent; otherwise, the Service names may not match, and the reuse fails and falls back to creating a new one.
4. Deploy the service by using the `deploy.py` script in the `examples/deployer` directory with the following commands.

    ```bash
    cd examples/deployer

    # Method 1: Specify the configuration directory (recommended)
    python deploy.py --config_dir ../../model_configs/vllm

    # Method 2: Specify the configuration files separately
    python deploy.py --user_config_path ../../model_configs/vllm/user_config.json --env_config_path ../../model_configs/vllm/env.json
    ```

    **After deployment is complete**

    - A ConfigMap `motor-config` is created or updated in the cluster (its content comes from the currently input `user_config.json`), serving as the baseline for subsequent scaling and refresh operations.
    - YAML files for each service are generated under `output_yamls/`.
    - When `AscendStoreConnector` is used, the deployer starts the corresponding service based on `kv_cache_store_config.backend`: Mooncake uses `mooncake_master`, MemCache uses MetaService, and LocalService is prepared according to the configuration.

    **Deployment notes**

    - **No probe for Mooncake**: `mooncake_master` does not provide `/livez`. The startup / liveness / readiness of the kv-store performs HTTP liveness checks only on the MemCache MetaService; when `backend` is `mooncake` (or another non-memcache backend), the probe skips the HTTP request and succeeds directly. Therefore, the kv-store Pod Ready of Mooncake **does not mean** that `mooncake_master` is ready, and the kubelet probe cannot be used to determine the health status of Mooncake.

#### Deployment Example Using UCMConnector

**Procedure**

1. UCM is a KV pooling function and is connected through `UCMConnector`; it does not use the backend mechanism of `AscendStoreConnector`.

   - `connectors[0]` of Prefill is the Mooncake P/D transfer Connector, and `connectors[1]` is `UCMConnector`.
   - `UCMConnector` keeps `kv_role: "kv_both"`, and the UCM Store Pipeline configuration is inlined in its `kv_connector_extra_config`.
   - Decode is configured only with the Mooncake P/D transfer Connector that matches Prefill, and does not load `UCMConnector`.
   - The `store_pipeline`, `storage_backends`, and capacity parameters of UCM determine how the prefix cache is saved, and `AscendStoreConnector.backend` is not used.

2. The current Motor UCM example still configures `kv_cache_store_config.backend: "mooncake"`, which is used by the current deployer to generate Mooncake kv_store/master resources. This is a deployment adaptation configuration and does not mean that UCM has become the Mooncake Backend. The actual Store of UCM is determined by `store_pipeline` in `UCMConnector`.
   >[!NOTE]NOTE
   > For the complete configuration, storage mounting, deployment, and verification steps, see [Deploying UCM in MindIE Motor](./backend/ucm.md).
3. Deploy the service by using the `deploy.py` script in the `examples/deployer` directory with the following commands.

    ```bash
    cd examples/deployer

    # Method 1: Specify the configuration directory (recommended)
    python deploy.py --config_dir ../../model_configs/vllm

    # Method 2: Specify the configuration files separately
    python deploy.py --user_config_path ../../model_configs/vllm/user_config.json --env_config_path ../../model_configs/vllm/env.json
    ```

    **After deployment is complete**

    - A ConfigMap `motor-config` is created or updated in the cluster (its content comes from the currently input `user_config.json`), serving as the baseline for subsequent scaling and refresh operations.
    - YAML files for each service are generated under `output_yamls/`.
    - When UCM is used, you also need to install UCM for Prefill and mount the directories required by the UCM Store according to the UCM deployment document; the current example also generates Mooncake kv_store/master resources.

### Verification

1. Confirm that the `kv_store` Pod has started successfully.

   ```bash
   kubectl get pods -n <namespace> -l deploy-name=mindie-motor-kv-store
   ```

   The expected output contains a `kv-store` Pod that is in the `Running` state and ready.

   >[!NOTE]NOTE
   > For the Mooncake backend, kv-store Pod Ready cannot prove that `mooncake_master` is ready (there is no `/livez` probe). Confirm it with the master logs or `:50090/metrics`. For details, see "Deployment notes" above.

2. Confirm that the P/D engine Pods start properly and that the logs do not contain KV transfer-related errors.

   ```bash
   kubectl logs <pod-name> -n <namespace> | grep -iE "kv_cache|kv_store"
   ```

   Check the connection and initialization logs according to the selected backend, and confirm that no error-level errors related to KV Cache connection, write, or read occur.

3. Send two consecutive inference requests that contain the same long prefix to verify the KV Cache write and reuse path.

   ```bash
   curl -X POST http://<service IP>:<service port>/v1/chat/completions \
        -H "Content-Type: application/json" \
        -d '{"model":"<model_name>","messages":[{"role":"user","content":"<same long prefix and question>"}]}'
   ```

   Both requests should return HTTP 200 and a normal inference response. Also check the logs of the selected backend and the P/D instances: the first request should complete the KV Cache write, and the second request should show corresponding cache read or hit records. A successful request alone cannot prove that KV pooling has taken effect.

## Tuning Suggestions

- **Increase `kv_parallel_size`**: After KV pooling is enabled, the P instance needs to additionally push the KV Cache into the cache pool, which may introduce a small amount of performance overhead. You can appropriately increase `kv_parallel_size` to improve transfer efficiency.

- **Configure eviction parameters**: `eviction_high_watermark_ratio` (0.9 recommended) and `eviction_ratio` (0.1 recommended) of the Mooncake backend control the eviction behavior of the pooled space and should be adjusted based on the actual memory capacity.

- **Adjust the lease TTL**: `default_kv_lease_ttl` must be greater than `ASCEND_CONNECT_TIMEOUT` and `ASCEND_TRANSFER_TIMEOUT` in `env.json` to prevent the KV Cache from being cleared prematurely during transfer.

## FAQs

### KV Cache Cannot Be Transferred Between P/D Instances After the Service Starts

**Symptom**

After the service starts, KV Cache cannot be transferred between P/D instances.

**Cause Analysis**

`kv_role` is incorrectly configured. The P instance should be `kv_producer` and the D instance should be `kv_consumer`; an incorrect configuration causes the transfer directions to mismatch.

**Solution**

Check the `user_config.json` actually used for deployment through the `motor-config` ConfigMap, and confirm whether `kv_transfer_config.kv_role` meets the requirements.

  ```bash
  kubectl get configmap motor-config -n <namespace> -o yaml | grep -A 15 kv_transfer_config
  ```

  If it does not meet the requirements, modify `user_config.json` and then run `python deploy.py --config_dir ../../model_configs/vllm` again to deploy.

### Inference Performance of the P Instance Degrades

**Symptom**

After KV pooling is enabled, the inference performance of the P instance degrades.

**Cause Analysis**

After KV pooling is enabled, the P instance needs to additionally push the KV Cache into the cache pool, which may introduce a small amount of performance overhead.

**Solution**

Appropriately increase `kv_parallel_size` to improve transfer efficiency.

### The D Instance Times Out When Pulling the KV Cache

**Symptom**

The D instance times out when pulling the KV Cache.

**Cause Analysis**

`ASCEND_CONNECT_TIMEOUT` and `ASCEND_TRANSFER_TIMEOUT` are insufficient, or `default_kv_lease_ttl` is less than these two timeout values.

**Solution**

Check whether `ASCEND_CONNECT_TIMEOUT` and `ASCEND_TRANSFER_TIMEOUT` in `env.json` are large enough, and confirm that `default_kv_lease_ttl` is greater than these two timeout values; redeploy the service after the modification.

### MemCache MetaService Fails to Start

**Symptom**

MemCache MetaService fails to start.

**Cause Analysis**

The `config_store_port` or `metrics_port` is occupied, or the `POD_IP` environment variable is not correctly injected.

**Solution**

Check whether `config_store_port` and `metrics_port` in `kv_cache_store_config` are occupied; confirm whether `fieldRef: status.podIP` in `kv_cache_store_template.yaml` correctly injects the `POD_IP`; locate the specific error by viewing the MetaService Pod logs with `kubectl logs`.

### The Configuration Does Not Take Effect After Switching the Backend

**Symptom**

The configuration does not take effect after the backend is switched.

**Cause Analysis**

The `backend` in `AscendStoreConnector` is inconsistent with that in `kv_cache_store_config`.

**Solution**

Ensure that the `backend` values in both places are the same; run `python deploy.py --config_dir ../../model_configs/vllm` again to deploy after the modification.

### Why the UCM Example Still Contains `backend: "mooncake"`

**Symptom**

The configuration `backend: "mooncake"` still exists in the UCM example.

**Cause Analysis**

This is the configuration used by the current Motor deployer to generate Mooncake kv_store/master resources, not the storage backend of UCM. The actual Store of UCM is determined by `store_pipeline` in `UCMConnector`.

**Solution**

No modification is required. For the UCM Store configuration, check `store_pipeline` and `storage_backends` in `UCMConnector`.

### The Decode Node Repeatedly Crashes and Restarts During Inference

**Symptom**

The Decode node repeatedly crashes and restarts during inference.

**Cause Analysis**

The hybrid attention model configures `connectors[0]` as `MooncakeConnectorV1`.

**Solution**

When a hybrid attention model is used, configure `connectors[0]` as `MooncakeHybridConnector`.

### After MemCache Pooling Is Enabled, Long-Sequence Requests Crash the Decode Instance and Report shm_crash

**Symptom**

After MemCache pooling is enabled, short requests can be inferred normally; long-sequence requests crash the Decode instance, and `shm_crash` appears in the logs.

**Cause Analysis**

The `/dev/shm` of the engine Pod is only `4Gi` by default. MemCache pooling requires larger shared memory in long-sequence scenarios, and when the capacity is insufficient, the Decode side crashes due to insufficient shm.

**Solution**

Configure `"dshm_size": "32Gi"` (or a larger value based on the actual load) in `motor_deploy_config` of `user_config.json` and then redeploy the service. Stress test verification: 128 requests (80k-1k) still run normally with `32Gi`.
