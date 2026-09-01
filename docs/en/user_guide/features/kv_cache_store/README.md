# KV Pooling Capability Deployment

## Feature Introduction

Prefill/Decode (P/D) instances are allowed to share the KV Cache through a KV cache pool. The P instance pushes the computed KV Cache into the cache pool, and the D instance pulls and reuses it from the cache pool, thereby improving memory utilization and inference throughput in the PD disaggregation scenario.

The MindIE Motor KV pooling capability is based on the pooling capability of vllm-ascend itself. For capability introduction and environment dependencies, see the [vllm-ascend pooling documentation](https://docs.vllm.ai/projects/ascend/en/main/user_guide/feature_guide/kv_pool.html).

After modifying the `user_config.json` configuration file, you can complete the service deployment through the `deploy.py` script.

## Prerequisites

- A PD disaggregation inference service must already be deployed using MindIE Motor. KV pooling is enabled on top of this service and does not affect the Controller or Coordinator.

- For the constraints of the KV pooling capability, see [vllm-ascend kv_pool](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/kv_pool.html).

- Before enabling the pooling capability, refer to [MindIE Motor Quick Start](../../quick_start.md) to ensure that the environment can complete basic PD disaggregation service deployment normally.

- **A patch is required only when the `vllm-ascend` version is earlier than `v0.17.0rc2` (excluding `v0.17.0rc2`)** (see the Applying Patches section below); for `v0.17.0rc2` and later versions, skip the patch step directly.

- All subsequent operations are performed only on the management node (master node) of the k8s cluster.

## Applying Patches

> **[Important]**
> **This patch is required only when the `vllm-ascend` version is earlier than `v0.17.0rc2` (excluding `v0.17.0rc2`).**
> If your `vllm-ascend` version is `v0.17.0rc2` or later, the patch has already been merged into the mainline. **Skip this section directly; no patching is required.**

Because the layerwise KV-cache transfer combined with KV pooling in the vllm code has an inference bug, you need to apply the `vllm_multi_connector.patch`. For detailed steps, see [MindIE Motor Applying Patches](https://gitcode.com/Ascend/MindIE-Motor/blob/v3.1.0/patch/README.md).

## Configuring `user_config.json`

To enable the KV pooling capability in MindIE Motor, you only need to modify the `user_config.json` configuration file. All other configuration items remain the same as when pooling is disabled. Pay attention to the following two configurations.

> Note: Before enabling the pooling capability, refer to [MindIE Motor Quick Start](../../quick_start.md) to ensure that the environment can properly complete basic PD disaggregation service deployment.

### `kv_transfer_config` (in the `engine_config` of P/D Instances)

Pooling is implemented by combining a transfer connector (`connectors[0]`) and a pooling backend connector (`connectors[1]`) through `MultiConnector`. The following uses `MooncakeConnectorV1` (P/D collaboration) + `AscendStoreConnector` (KV pool backend) as an example.

**P instance (`motor_engine_prefill_config`):**

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

**D instance (`motor_engine_decode_config`):**

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

> `lookup_rpc_port` does not need to be filled in manually. The value of each DP instance is automatically adapted by Motor.

The `backend` field of `AscendStoreConnector` determines the pooling backend to use. The remaining structure is completely identical across backends, and **only the `backend` value differs**:

| Pooling Backend | `backend` Value | Description |
|----------|-------------|------|
| [Mooncake](backend/mooncake.md) | `mooncake` | Natively supported, no additional installation required |
| [MemCache](backend/memcache.md) | `memcache` | Default backend, natively supported, no additional installation required |
| Yuanrong | `yuanrong` | TODO: to be supported in a later version |

> For more details about the Connector principles, as well as the identification whitelist and the `dispatch_profile` escape hatch.

### `kv_cache_store_config` (Global Configuration)

`kv_cache_store_config` is the global configuration for KV pooling, shared by P/D instances (using the default backend MemCache as an example):

```json
"kv_cache_store_config": {
  "backend": "memcache",
  "local_service_mode": "standalone",
}
```

`backend` determines the pooling backend and must be consistent with the `backend` in `AscendStoreConnector`. The parameters of each backend are described as follows:

**Common Parameters**

| Parameter | Type | Default Value | Description |
|------|------|--------|------|
| `backend` | string | `memcache` | Pooling backend: `mooncake`, `memcache`; defaults to `memcache` when not configured |

**Mooncake-specific Parameters**

| Parameter | Type | Default Value | Description |
|------|------|--------|------|
| `metadata_server` | string | `P2PHANDSHAKE` | Metadata server mode, defaults to the peer-to-peer handshake mode |
| `protocol` | string | `ascend` | Underlying transport protocol |
| `device_name` | string | `""` | The bound NIC name; automatically selected when empty |
| `global_segment_size` | string | `1GB` | Global shared memory segment size |
| `port` | int (optional) | `50088` | KV Pool service port; `deploy.py` fills in the default value when not configured |
| `default_kv_lease_ttl` | int (optional) | `11000` | Default lease TTL of KV objects (in milliseconds); the configured value must be greater than `ASCEND_CONNECT_TIMEOUT` and `ASCEND_TRANSFER_TIMEOUT` of the vllm instance in `env.json` |
| `eviction_high_watermark_ratio` | float | 0.9 | High watermark eviction line of the pooling space, passed to the `mooncake_master` process |
| `eviction_ratio` | float | 0.1 | Eviction ratio per eviction, passed to the `mooncake_master` process |

**MemCache-specific Parameters**

| Parameter | Type | Default Value | Description |
|------|------|--------|------|
| `local_service_mode` | string (optional) | <ul><li>Atlas 800I A2 inference server/Atlas 850 SuperPoD Server: `inprocess`</li><li>Atlas 800I A3 SuperPoD Server: `standalone`</li></ul> | LocalService deployment mode: `inprocess` (same process as vLLM) or `standalone` (independent process) |

> **All internal memcache configuration items** (DRAM pool size, communication protocol, MetaService port, SSD cache, UBSIO parameters, etc.) are managed directly by the user in `mmc-local-inprocess.conf` and do not need to be configured in `user_config.json`. For details, see [MemCache Backend Documentation](backend/memcache.md).

## Deploying the Service

Deploy the service using the `deploy.py` script in the `examples/deployer` directory.

```bash
cd examples/deployer

# (Recommended) Method 1: Specify the configuration directory
python deploy.py --config_dir ../infer_engines/vllm

# Method 2: Specify the configuration files separately
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```

After completion:

- A ConfigMap `motor-config` is created or updated in the cluster (its content comes from the currently input `user_config.json`), serving as the baseline for subsequent scaling and refresh operations.

- YAML files for each service are generated under `output/deployment/`.

- The P and D instances automatically start the master process of the corresponding backend based on `kv_cache_store_config` (the Mooncake backend uses `mooncake_master`, and the MemCache backend uses MetaService) to manage the shared video memory pool.

The MemCache backend is used by default, so the service can be deployed directly without additional operations. If you need to use another backend, complete the installation by referring to the corresponding document below, and then replace `backend` in `AscendStoreConnector` and `kv_cache_store_config` with the corresponding value:

| Backend | Document |
|------|------|
| Mooncake | [backend/mooncake.md](backend/mooncake.md) |
| MemCache | [backend/memcache.md](backend/memcache.md) |
| Yuanrong | TODO: to be supported in a later version |

## Principle Description

### Overall KV Pooling Process

The MindIE Motor KV pooling capability is implemented based on the KV transfer layer of vllm-ascend. The overall process is as follows:

1. **PreFill phase**: After the P instance completes the PreFill computation, it pushes the KV Cache into the shared KV cache pool at layer granularity through `MooncakeLayerwiseConnector`.

2. **KV cache pool management**: `kv_cache_store_config` controls the metadata service mode, transfer protocol, global shared segment size, and eviction policy of the cache pool. The cache pool shares memory resources among multiple instances to improve overall utilization.

3. **Decode phase**: The D instance pulls the KV Cache of the corresponding sequence from the cache pool and directly uses it for Decode computation, eliminating the need for repeated computation.

4. **P/D collaboration**: The P and D instances establish a connection by configuring the same `kv_port` and `kv_connector`, and distinguish the producer/consumer roles through `kv_role`.

The pooling backend is switched through the `backend` field of `AscendStoreConnector`. The MemCache backend automatically starts the MetaService process through the deployer to manage the cache pool metadata, while the Mooncake backend uses the `mooncake_master` process. For detailed descriptions of each backend, see the corresponding backend documentation.

## FAQs

1. **KV Cache cannot be transferred between P/D instances after the service starts**

   Check whether `kv_role` is correct (P is `kv_producer`, D is `kv_consumer`).

2. **Inference performance of the P instance degrades**

   After KV pooling is enabled, the P instance needs to additionally push the KV Cache into the cache pool, which may introduce a small amount of performance overhead. You can appropriately increase `kv_parallel_size` to improve transfer efficiency.

3. **The D instance times out when pulling the KV Cache**

   Check whether `ASCEND_CONNECT_TIMEOUT` and `ASCEND_TRANSFER_TIMEOUT` in `env.json` are large enough, and whether `default_kv_lease_ttl` is greater than these two timeout values.

4. **MemCache MetaService fails to start**

   Check whether `config_store_port` and `metrics_port` in `kv_cache_store_config` are occupied, and whether the `POD_IP` environment variable is correctly injected (provided by `fieldRef: status.podIP` in `kv_store_template.yaml`).

5. **The configuration does not take effect after switching the backend**

   The `backend` in `AscendStoreConnector` and `kv_cache_store_config` must be consistent. If only one of them is modified, the backends will not match. Ensure that the `backend` values in both places are the same.
