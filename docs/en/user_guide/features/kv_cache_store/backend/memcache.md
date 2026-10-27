# MemCache Backend Feature

## Feature Introduction

MemCache is the default pooled backend of MindIE Motor. It provides efficient KV pooling capabilities based on [memcache_hybrid](https://gitcode.com/Ascend/memcache) and is preinstalled in the MindIE Motor image, requiring no additional installation. The pooling mechanism enables KV cache reuse across requests, improving the cache hit rate, reducing inference TTFT, eliminating repeated computation, and increasing inference throughput.

### Working Principle

On each P/D engine node, MemCache manages DRAM pooled memory through the LocalService process and supports two deployment modes: in-process (inprocess) and standalone process (standalone). LocalService manages KV Cache data in DRAM in a unified manner, and multiple inference engine nodes can share the same pooled backend. MetaService broadcasts KV block metadata events (STORED / REMOVED / CLEARED). Based on `backend_id`, the kv-conductor of Motor calculates the KV affinity and implements cache-aware prefill scheduling, routing requests preferentially to nodes that already cache the prefix.

In addition, MemCache supports connecting to local NVMe SSDs through the UBSIO engine as the third-level cache (HBM → DRAM → SSD), automatically demoting cold KV Cache to SSDs and retaining only hot data in memory.

### Core Features

- LocalService deployment modes: supports inprocess (in-process, integrated in vLLM) and standalone (standalone process, automatically started by NodeManager), which can be flexibly selected based on hardware scenarios and isolation requirements.
- KV events broadcast (cache-aware scheduling): MetaService broadcasts events through ZMQ PUB after KV block metadata is written or deleted. The kv-conductor subscribes to the events and calculates the KV affinity, implementing cache-based request routing and improving prefill efficiency.
- UBSIO/SSD three-level cache: supports connecting to local NVMe SSDs through the UBSIO engine as the third-level cache, implementing the HBM → DRAM → SSD three-level cache architecture and expanding the cache capacity. For Kubernetes block device preparation, see [DRAM + SSD Multi-level Pooling Kubernetes Configuration Example](https://gitcode.com/Ascend/memcache/wiki/DRAM+%20SSD%20%E5%A4%9A%E7%BA%A7%E6%B1%A0%E5%8C%96%20Kubernetes%20%E9%85%8D%E7%BD%AE%E7%A4%BA%E4%BE%8B.md). Production environments still require exclusive block devices, so the default template cannot be used directly for going live at present.
- Multi-service kv_store sharing: supports reusing kv_store across inference services. Configure target_job_id to point to the kv_store of the target service, reducing repeated cache overhead.

### Constraints and Limitations

| Constraint Dimension | Requirement |
|----------|------|
| Hardware | Atlas 800I A2, Ascend 950PR products, and Atlas 800I A3 are supported. |
| Deployment scenario | Only the PD disaggregation deployment scenario is supported. |
| Engine | Only the vLLM inference engine is supported. |
| Deployment mode | Atlas 800I A3 keeps `standalone`. The runtime of Atlas 800I A2 and Ascend 950PR forcibly uses `inprocess`; even if `standalone` is written in `user_config.json`, no standalone process is started. |
| Feature mutual exclusion | <ul><li>Do not set `"backend"` to `"ucm"`. UCM is not connected through the backend mechanism of `AscendStoreConnector`.</li><li>The configurations of `AscendStoreConnector` and `kv_cache_store_config` must be consistent and both set to `"memcache"`.</li><li>`MooncakeConnectorV1` / `MooncakeHybridConnector` and the like belong to the P/D real-time transfer layer and are not the same layer of configuration as the MemCache pooled backend. Do not confuse them.</li></ul> |
| Software dependency | Basic capabilities are ready to use out of the box with no additional installation (memcache_hybrid is preinstalled). When KV events is enabled, the memcache_hybrid version must include the KvEvent feature (**MemCache Hybrid v1.2.0 and later**, corresponding to memcache PR #334). If `MultiConnector` is used, the `vllm_ascend_multi_connector_kv_events.patch` patch must be applied. |
| Other limitations | <ul><li>SSDs must be dedicated block devices. Do not partition or format a whole disk that already has a file system.</li><li>Set `ubsio.disk.path` to the in-container path `/dev/memcache-ubsio0`. Do not set it to the host paths `/dev/nvme*` or `/dev/loop*`.</li><li>The valid range of `ubsio.standalone.device_count` is 0 to 16, which equals the number of LocalService processes whose `dram.size` is greater than 0 on the current node. Set it to `1` for standalone. It does not equal the number of vLLM workers; a value of 32 is rejected by UBSIO at startup.</li><li>The pooling granularity is 128 tokens. Prompts shorter than 128 tokens are not put and leave no corresponding writes on SSDs.</li><li>A new put must first allocate a contiguous blob in DRAM. Even if the SSD has plenty of free space, DRAM fragmentation can still make this put return -1.</li><li>A P2P failure of `MooncakeConnectorV1` cannot be regarded as an SSD failure. The two are not the same layer.</li></ul> |

## Using the Feature

### Environment Preparation

The MindIE Motor environment has been installed and deployed, and MemCache is preinstalled in the MindIE Motor image.

### Usage Example

The following steps show how to configure and use the MemCache backend.

>[!NOTE]NOTE
>The `connectors[0]` of hybrid attention models must use `MooncakeHybridConnector`. For details, see [P/D Transfer Connector Selection](../README.md#table_Connector).

**Procedure**

1. Set backend to memcache.

    Configure `"backend": "memcache"` in `AscendStoreConnector`:

    ```json
    "backend": "memcache"
    ```

2. Configure kv_cache_store_config.
   Set "backend": "memcache" in kv_cache_store_config. The LocalService deployment mode and multi-service kv_store reuse can be configured optionally.

    For A3 standalone, `local_config_path` still points to the inprocess file. When NodeManager starts the standalone LocalService, it switches the configuration of the child process to `mmc-local-standalone.conf` in the same directory.

    ```json
    "kv_cache_store_config": {
      "backend": "memcache",
      "local_service_mode": "standalone",
      "local_config_path": "/usr/local/Ascend/pyMotor/conf/mmc-local-inprocess.conf",
      "target_job_id": "service-a"
    }
    ```

    **Table 1** Configuration parameter description

    | Configuration Item | Type | Value Range | Mandatory/Optional | Default Value | Description |
    |--------|------|----------|-----------|--------|------|
    | backend | String | "memcache" | Mandatory | — | Specifies the backend type as memcache. |
    | local_service_mode | String | "inprocess" / "standalone" | Optional | <ul><li>Atlas 800I A2 / Ascend 950PR products: inprocess;</li><li>Atlas 800I A3: standalone</li></ul> | LocalService deployment mode. The runtime of A2/A5 forcibly uses inprocess. |
    | local_config_path | String | conf path in the container | Optional | `/usr/local/Ascend/pyMotor/conf/mmc-local-inprocess.conf` | Client configuration read by the worker and NodeManager. For standalone, also set this inprocess file. Do not change it to `mmc-local-standalone.conf`. |
    | target_job_id| String | `motor_deploy_config.job_id` of the target service | Optional | — | Reuses the kv_store of another inference service. If it is not configured, is the same as the local `job_id`, or the target kv_store is unavailable, a kv_store is created in the current namespace. For details, see [KV Pooling README — Sharing kv_store Among Multiple Services](../README.md#step3) |

    >[!NOTE]NOTE
    >DRAM, protocol, and SSD parameters are written in two templates, which are located in `examples/deployer/startup/roles/kv_store_backends/memcache/`. During deployment, `common.sh` synchronizes them to `$CONFIG_PATH/`, which corresponds to `/usr/local/Ascend/pyMotor/conf/` in engine containers.
    >- `mmc-local-inprocess.conf`: client configuration of vLLM workers. In A3 standalone mode, keep `dram.size = 0GB`, `storage.enabled = false`, and `ubsio.disk.path` empty.
    >- `mmc-local-standalone.conf`: configuration of the standalone LocalService. Only this file enables DRAM and SSD.
    >The whitelist of `deploy.py --update_config` does not include `kv_cache_store_config`. After `local_service_mode` or `local_config_path` is modified, the engine pods must be redeployed; refreshing only the ConfigMap does not change the running processes.

3. (Optional) Configure the LocalService deployment mode.

    Select the `inprocess` or `standalone` mode based on the hardware and isolation requirements:

    - `inprocess`: DRAM is allocated in the vLLM process. The deployment is simple and resource usage is low.
    - `standalone`: A standalone LocalService process is automatically started and monitored by NodeManager. Memory isolation is better, and a LocalService crash does not affect vLLM.

    Atlas 800I A3 uses standalone. To override the default value for hardware other than A2/A5, explicitly configure `local_service_mode` in `user_config.json`. For the differences between the two modes and deployment examples, see [MemCache Disaggregated Deployment Solution](https://gitcode.com/Ascend/memcache/wiki/MemCache+vLLM+A3%E5%88%86%E7%A6%BB%E9%83%A8%E7%BD%B2%E6%96%B9%E6%A1%88.md).

4. (Optional) Enable KV events broadcast.<a id="kv-events-affinity"></a>

    Enable cache-aware prefill scheduling:
    For Motor-side configuration and acceptance, see [Pooling Affinity Configuration](../../kvcache_affinity.md#pool-affinity).

    1. Enable MetaService broadcast.
      Uncomment the "KV events broadcast" configuration block in examples/deployer/startup/roles/kv_store_backends/memcache/memcache_meta_service.py, and fill in kv_events_model_name / kv_events_block_size as required. (They must be consistent with the model_path / block_size registered in kv_conductor_config; otherwise, events cannot hit the index.)
    2. Restart kv_store.
    3. Configure the subscription address.
      Configure the kv_events broadcast address of MetaService in kv_conductor_config.pool_endpoint, for example, "tcp://mindie-motor-kvs-master:5557" (the port must be consistent with kv_events_endpoint in the script). It can also be written as "tcp://*:5557"; the asterisk is automatically replaced with the KVS_MASTER_SERVICE domain injected by K8s. When registering at startup, the Coordinator notifies kv-conductor of this address and subscribes. The K8s Service mindie-motor-kvs-master exposes the kv-events port 5557 by default.
    4. Automatic backend_id injection.
      ock.mmc.local_service.backend_id of the LocalService on each engine node is automatically replaced by the deployer with the pod IP of the node during deployment, requiring no user configuration. kv-conductor uses it to distinguish the node to which a KV block belongs.

        >[!NOTE]NOTE
        >MultiConnector patch (mandatory): The vLLM upstream does not implement MultiConnector.get_kv_connector_kv_cache_events() (TODO). When MultiConnector is used as kv_transfer_config.kv_connector, the KV events collected by AscendStoreConnector on the worker side are silently discarded, so engine offload events cannot reach kv-conductor and the two-phase matching always lacks the engine side. vllm-ascend has replaced the MultiConnector registration with AscendMultiConnector. During deployment, apply examples/deployer/patch/vllm_ascend_multi_connector_kv_events.patch (it adds sub-connector event proxying for AscendMultiConnector, and one patch is compatible with v0.20.2 to v0.26.0). A suggestion has been synchronized to the vLLM upstream for merging.
        **Prerequisite**: memcache_hybrid must be a version that includes the KvEvent feature (**MemCache Hybrid v1.2.0 and later**, corresponding to memcache PR #334); otherwise, MetaConfig does not recognize the kv_events_* fields.

5. (Optional) Enable the SSD three-level cache.

    For block device preparation, refer to [DRAM + SSD Multi-level Pooling Kubernetes Configuration Example](https://gitcode.com/Ascend/memcache/wiki/DRAM+%20SSD%20%E5%A4%9A%E7%BA%A7%E6%B1%A0%E5%8C%96%20Kubernetes%20%E9%85%8D%E7%BD%AE%E7%A4%BA%E4%BE%8B.md). The following steps enable MemCache SSD with `examples/deployer/deploy.py` according to that example. `kv_cache_store_config.backend` is `memcache`. Atlas 800I A2 and Ascend 950PR use `inprocess`, and Atlas 800I A3 uses `standalone`.

    >[!NOTE]NOTE
    >- For standard attention, `connectors[0]` uses `MooncakeConnectorV1` or `MooncakeConnectorV2`, and `connectors[1]` uses `AscendStoreConnector` with `"backend": "memcache"`. For hybrid attention, `connectors[0]` uses `MooncakeHybridConnector`. For selection, see [P/D Transfer Connector Selection](../README.md#table_Connector).
    >- Set the protocol according to the hardware: `device_rdma` for A2, `device_sdma` for A3, and `device_urma` for A5. The default values in the conf templates cannot be applied directly to other hardware.
    >- `storage.enabled` enables SSD only for the process in which `ubsio.disk.path` is not empty and `dram.size > 0`. Workers in A3 standalone read the inprocess file, in which `dram.size = 0GB`, so workers do not enable SSD. SSD is enabled only by the standalone LocalService according to the standalone file.
    >- Set `ubsio.disk.path` to an in-container path. For a single disk, use `/dev/memcache-ubsio0`, and for multiple disks, join the paths with colons, for example, `/dev/memcache-ubsio0:/dev/memcache-ubsio1`. Do not write host paths `/dev/nvme*` or `/dev/loop*` into the conf.
    >- The storage configuration of `examples/deployer` is a hostPath directory mount and cannot pass block devices. Use PVs/PVCs with `volumeMode: Block` and mount them to engine containers through `volumeDevices`. Set `deploy_mode` to `multi_deployment`. `volumeDevices` is lost when InferServiceSet expands Deployments.
    >- Node memory must be greater than `ubsio.mem.size_in_gb × device_count` plus the vLLM usage. Use only dedicated disks without a file system. Do not partition or format a whole disk that already has a file system. If no physical disk is free, create a loop image on the existing file system and run `losetup` as described in the wiki. `partition_disks.sh` is not included in this repository or the Motor image, so do not use it to generate paths in the conf for Kubernetes scenarios.
    >- Set `ubsio.standalone.force_new_disk` to `true` only when this disk is initialized for the first time. After `Start boostio success`, change it back to `false` before redeploying. After that, restarting LocalService continues to use the cache on the existing disk.

    1. Prepare block devices. If the cluster already has local-static-provisioner, label the node with `memcache.io/ubsio-node` and place symbolic links pointing to the dedicated disk or loop under `/mnt/local-pv/memcache-ubsio`, so that the provisioner generates PVs. If no provisioner is available, manually create a StorageClass (`kubernetes.io/no-provisioner`, `volumeBindingMode: Immediate`) and local PVs with `volumeMode: Block`, and bind them to the node where the engine resides using `nodeAffinity`. Each engine role that needs to write to SSDs uses its own disk. If no bare disk is free, create a loop image on the existing file system, run `losetup`, and then make the loop device a local PV.
    2. Create a PVC with `volumeMode: Block` for each disk, and bind it to the corresponding PV using `volumeName`.
    3. Mount the block devices to the prefill and decode engine pods. Prefill example:

        ```yaml
        volumes:
          - name: ubsio0
            persistentVolumeClaim:
              claimName: ubsio-prefill
        volumeDevices:
          - name: ubsio0
            devicePath: /dev/memcache-ubsio0
        ```

        For Decode, replace `claimName` with its own PVC. The in-container path is still `/dev/memcache-ubsio0`. The two sides can use the same name because they correspond to different host devices on their respective nodes. The current deployer does not automatically generate this `volumeDevices` section. Add it when generating the engine YAML or modify the Deployment before deployment.
    4. Edit the conf according to the mode. For A2, inprocess, and a single disk, edit `mmc-local-inprocess.conf` with the protocol `device_rdma`, `dram.size` greater than 0, and `device_count` equal to `endpoints × local_world_size` and no greater than 16. Replace the values in angle brackets in the example according to the actual site:

        ```text
        ock.mmc.local_service.protocol = device_rdma
        ock.mmc.local_service.dram.size = <DRAM capacity greater than 0>
        ock.mmc.local_service.storage.enabled = true
        ubsio.disk.path = /dev/memcache-ubsio0
        ubsio.mem.size_in_gb = <UBSIO memory in GB>
        ubsio.wcache.evict_water_level = 0
        ubsio.wcache.disk_evict_water_level = 90
        ubsio.standalone.device_count = <endpoints × local_world_size, 0 to 16>
        ```

        For A3 standalone, keep both files. The `local_config_path` in `user_config.json` points to the inprocess file, and workers use the following client configuration:

        ```text
        ock.mmc.local_service.protocol = device_sdma
        ock.mmc.local_service.dram.size = 0GB
        ock.mmc.local_service.storage.enabled = false
        ubsio.disk.path =
        ubsio.standalone.device_count = 1
        ```

        The standalone LocalService uses `mmc-local-standalone.conf` in the same directory. Set `device_count` to 1. In the repository template, `ubsio.wcache.evict_water_level` is 0, and initialization can be completed by using the template values. Keep `force_new_disk` as `false` after the first disk-clearing initialization succeeds:

        ```text
        ock.mmc.local_service.protocol = device_sdma
        ock.mmc.local_service.dram.size = 2GB
        ock.mmc.local_service.storage.enabled = true
        ubsio.disk.path = /dev/memcache-ubsio0
        ubsio.mem.size_in_gb = 8
        ubsio.wcache.evict_water_level = 0
        ubsio.wcache.disk_evict_water_level = 90
        ubsio.standalone.device_count = 1
        ubsio.standalone.force_new_disk = false
        ```

        `dram.size = 2GB` is the template scale. With 96K prefixes and multiple workers putting data at the same time, 2GB of DRAM can easily exceed 90% and run out of contiguous space. To reduce put failures, increase `dram.size` in this standalone file according to the concurrent write volume. Increasing DRAM does not bypass the path in which new data is written to DRAM first.

        `common.sh` synchronizes both files to the ConfigMap. After the engine starts, workers read `/usr/local/Ascend/pyMotor/conf/mmc-local-inprocess.conf`, and standalone child processes then read `mmc-local-standalone.conf` in the same directory. Templates and field comments are in `examples/deployer/startup/roles/kv_store_backends/memcache/`.
    5. In `user_config.json`, set `kv_cache_store_config.backend` to `memcache`. For A3, set `local_service_mode` to `standalone`, and `local_config_path` still points to `mmc-local-inprocess.conf`. Keep the P/D connector consistent with [P/D Transfer Connector Selection](../README.md#table_Connector).
    6. Run `examples/deployer/deploy.py` for full deployment. After the mode, `local_config_path`, or the two conf files above are modified, do not run only `--update_config`. A prompt is put by chunk only when it is no shorter than 128 tokens. Before deployment, confirm that the inference NodePort does not conflict with existing services in the cluster.

### Feature Verification

Verify whether the MemCache backend is configured successfully as follows:

1. **Check the configuration loading log**: Search the Motor startup log for `"backend"`-related output and confirm that backend is `"memcache"` and the configuration items are loaded correctly.
2. **Check the LocalService status**:
   - `inprocess` mode: Check the DRAM pool allocation information in the vLLM startup log.
   - `standalone` mode: Run `ps -eo pid,etime,comm` in the engine pod. `MindIE-Motor::LocalService` is a standalone process whose parent process is NodeManager. The NodeManager log contains `Starting standalone LocalService`, followed by one `Start boostio success` and one `bm register succeed`. Each engine pod should have only one successful boostio initialization.
3. **Verify the KV events feature** (if enabled): Search the kv-conductor log for the `"subscribe kv_events"` or `"kv_events"` keyword and confirm that the broadcast subscription is successful.
4. **Verify the SSD cache** (if enabled):
   1. Search the startup log for `storage.enabled` and `InitUbsIo`. `storage.enabled=true` and `InitUbsIo success` are expected, and `ubsio.disk.path` should have no IO error. Then confirm that `No free standalone disk slot` and `Invalid value for ubsio.standalone.device_count` are absent. A pod in `Ready` state only indicates that the container probes pass; workers may still exit due to slot failures after requests arrive.
   2. Run `ls -l` on `/dev/memcache-ubsio0` in the engine container and record the major and minor device number `MAJ:MIN`. The statistics file is `/sys/dev/block/MAJ:MIN/stat`. The third column is the number of read sectors, the seventh column is the number of write sectors, and each sector is 512 bytes. Prefill and Decode collect statistics for their own block devices.
   3. The policy is `WRITE_BACK`. A new prefix of no fewer than 128 tokens is written to SSD and retained in DRAM at the same time. If it is read back immediately before DRAM eviction, both the read and write sector increments are 0. Continue sending distinct prefixes of no fewer than 128 tokens until `dram.size` is exceeded, and then read back the first one again: if read sectors increase and the write sector increment is 0, the read-back comes from SSD. When the local prefix cache is disabled, `Prefix cache hit rate` is 0 and `External prefix cache hit rate` comes from memcache. `Mooncake transfer failed` only indicates a P2P failure of `connectors[0]`.

**Expected output**:

- The log contains no `"backend"` configuration loading errors or errors related to `"memcache"` initialization failures.
- In `standalone` mode, each engine pod has exactly one LocalService, and each of them has one `Start boostio success`.
- After KV events is enabled, kv-conductor receives and processes events normally, with no subscription failure or connection disconnection logs.
- After SSD is enabled, new prefixes increase the write sectors of the corresponding loop or NVMe device; after DRAM eviction, the read-back increases the read sectors and the write sector increment of this read-back is 0.

## FAQs

### After KV events Is Enabled, No Subscription Success Information Appears in the kv-conductor Log

**Problem Description**

After KV events broadcast is enabled according to the steps, `"subscribe kv_events success"` or a similar keyword does not appear in the kv-conductor log.

**Cause Analysis**

The broadcast address configured in `pool_endpoint` is inconsistent with the `kv_events_endpoint` port in `memcache_meta_service.py`, or `kv_events_model_name` / `kv_events_block_size` does not match the values registered in `kv_conductor_config`.

**Solution**

1. Confirm that the port in `kv_conductor_config.pool_endpoint` is consistent with `kv_events_endpoint` in the script.
2. Confirm that `kv_events_model_name` and `kv_events_block_size` are consistent with the `model_path` / `block_size` registered in `kv_conductor_config`.
3. Restart kv_store for the configuration to take effect.

### KV Events Cannot Be Subscribed When MultiConnector Is Used

**Problem Description**

When `MultiConnector` is used as `kv_transfer_config.kv_connector`, engine offload events cannot reach kv-conductor, and the two-phase matching always lacks the engine side.

**Cause Analysis**

The vLLM upstream does not implement `MultiConnector.get_kv_connector_kv_cache_events()`, so the KV events collected by AscendStoreConnector are silently discarded.

**Solution**

1. Apply the `examples/deployer/patch/vllm_ascend_multi_connector_kv_events.patch` patch.
2. Restart the service for the patch to take effect.

### SSD Does Not Take Effect After the Host Disk Path Is Written into ubsio.disk.path

**Problem Description**

`/dev/nvme0n1` or `/dev/loop0` is written in the conf, but LocalService in the container cannot find the device, or the write sectors of the block device do not increase during requests.

**Cause Analysis**

`ubsio.disk.path` is an in-container path. Host devices must be mapped to `/dev/memcache-ubsio0` through `volumeDevices`. In addition, SSD is not enabled when `dram.size = 0GB` for the current process. In A3 standalone mode, this is the expected state of worker clients; whether SSD is enabled depends on the standalone file loaded by LocalService. If A2 still uses `device_sdma` in the template, the protocol does not match the hardware.

**Solution**

1. Set the PV to `volumeMode: Block`, and set both `volumeDevices.devicePath` and `ubsio.disk.path` in the standalone file to `/dev/memcache-ubsio0`.
2. For A2, set `ock.mmc.local_service.protocol` to `device_rdma`, and let the process with `dram.size > 0` enable SSD. For A3, set `dram.size` to a value greater than 0 and `storage.enabled` to `true` only in `mmc-local-standalone.conf`.
3. Set `deploy_mode` to `multi_deployment` to prevent InferServiceSet from dropping `volumeDevices`.
4. For the block device preparation method, see [DRAM + SSD Multi-level Pooling Kubernetes Configuration Example](https://gitcode.com/Ascend/memcache/wiki/DRAM+%20SSD%20%E5%A4%9A%E7%BA%A7%E6%B1%A0%E5%8C%96%20Kubernetes%20%E9%85%8D%E7%BD%AE%E7%A4%BA%E4%BE%8B.md).

### After MemCache Pooling Is Enabled, Long-Sequence Requests Crash the Decode Instance with the shm_crash Error

**Problem Description**

After MemCache pooling is enabled, short requests can be inferred normally, but long-sequence requests crash the Decode instance, and `shm_crash` appears in the log.

**Cause Analysis**

The `/dev/shm` of the engine pod is only `4Gi` by default. MemCache pooling requires larger shared memory in long-sequence scenarios, and the Decode side crashes due to insufficient shm when the capacity is insufficient.

**Solution**

1. Configure `"dshm_size": "32Gi"` in `motor_deploy_config` of `user_config.json` (or increase it further based on the actual load).
2. Redeploy for the `sizeLimit` of the `dshm` emptyDir (or `--shm-size` for Docker deployment) to take effect.
3. Verify by stress testing: the service still runs normally with 128 requests (80k-1k) at `32Gi`.
