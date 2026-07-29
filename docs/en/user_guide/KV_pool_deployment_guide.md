# KV Pooling Capability Deployment

<!-- md-trans-meta sourceCommit=unknown translatedAt=2026-06-27T02:05:46.155Z pushedAt=2026-07-01T06:11:21.022Z -->

## Feature Introduction

The pyMotor KV pooling capability is based on the native pooling capability of vllm-ascend. For capability introduction and environment dependencies, refer to the [vllm-ascend pooling documentation](https://docs.vllm.ai/projects/ascend/en/main/user_guide/feature_guide/kv_pool.html).

After modifying the `user_config.json` configuration file, you can complete service deployment using the `deploy.py` script.

## Deployment Process

To enable the KV pooling capability of pyMotor, you only need to modify the `user_config.json` configuration file and then deploy the service using the `deploy.py` script. The specific process is as follows.
> NOTE
> Before enabling the pooling capability, refer to [pyMotor Quick Start](../../../README.md) to ensure that the environment is properly set up for basic service deployment.

### Applying the Patch

> **IMPORTANT**
> **This patch is required only when the `vllm-ascend` version is earlier than `v0.17.0rc2` (excluding `v0.17.0rc2`).**
> If your `vllm-ascend` version is `v0.17.0rc2` or later, the patch has already been merged into the mainline. In this case, **skip this section directly; no patching is required.**

Due to an inference bug in vllm code when layerwise KV-cache transfer is combined with KV pooling, the `vllm_multi_connector.patch` needs to be applied. For specific steps, refer to [pyMotor Patch Application](../../../patch/README.md).

### Configuring `user_config.json`

Same as the `kv-transfer-config` configuration in the [vllm-ascend pooling documentation](https://docs.vllm.ai/projects/ascend/en/main/user_guide/feature_guide/kv_pool.html), you only need to adjust the configurations within `kv_transfer_config` and the `kv_cache_pool_config` configuration for P/D instances in the `user_config.json` configuration file. Other configuration content can remain the same as when pooling is not enabled. Using the instance `user_config.json` in [PyMotor Quick Start](../user_guide/quick_start.md) as a reference baseline, an example of the configuration file with KV pooling enabled is as follows (irrelevant configuration items are omitted):

```json
{
  "version": "v2.0",
  "motor_deploy_config": {
    "..."
  },
  "motor_controller_config": {
    "..."
  },
  "motor_coordinator_config": {
    "..."
  },
  "motor_nodemanger_config": {
    "..."
  },
  "motor_engine_prefill_config": {
    "engine_type": "vllm",
    "model_config": {
      "..."
    },
    "engine_config": {
      "...",
      "kv_transfer_config": {
        "kv_connector": "MultiConnector",
        "kv_role": "kv_producer",
        "kv_connector_extra_config": {
          "use_layerwise": true,
          "connectors": [
            {
              "kv_connector": "MooncakeLayerwiseConnector",
              "kv_role": "kv_producer",
              "kv_port": "30001",
              "kv_connector_extra_config": {
                  "send_type": "PUT"
              }
            },
            {
              "kv_connector": "AscendStoreConnector",
              "kv_role": "kv_producer",
              "kv_connector_extra_config": {
                "lookup_rpc_port": "0",
                "backend": "mooncake"
              }
            }
          ]
        }
      }
    }
  },
  "motor_engine_decode_config": {
    "engine_type": "vllm",
    "model_config": {
      "..."
    },
    "engine_config": {
      "...",
      "kv_transfer_config": {
        "kv_connector": "MultiConnector",
        "kv_role": "kv_consumer",
        "kv_connector_extra_config": {
          "use_layerwise": true,
          "connectors": [
            {
              "kv_connector": "MooncakeLayerwiseConnector",
              "kv_role": "kv_consumer",
              "kv_port": "30001",
              "kv_connector_extra_config": {
                  "send_type": "PUT"
              }
            },
            {
              "kv_connector": "AscendStoreConnector",
              "kv_role": "kv_consumer",
              "kv_connector_extra_config": {
                "lookup_rpc_port": "1",
                "backend": "mooncake"
              }
            }
          ]
        }
      }
    }
  },
  "kv_cache_pool_config": {
    "metadata_server": "P2PHANDSHAKE",
    "protocol": "ascend",
    "device_name": "",
    "global_segment_size": "1GB",
    "eviction_high_watermark_ratio": 0.9,
    "eviction_ratio": 0.1
  }
}
```

NOTE
`kv_cache_pool_config` is a global configuration item for KV pooling. The specific parameter descriptions are as follows:

- `metadata_server`: metadata server mode, defaulting to `P2PHANDSHAKE` (point-to-point handshake mode).

- `protocol`: underlying transport protocol, defaulting to `ascend`.

- `device_name`: bound NIC name. If empty, it is automatically selected.

- `global_segment_size`: size of the globally shared video memory segment, defaulting to `1GB`.

- `eviction_high_watermark_ratio` and `eviction_ratio`: Used as startup parameters for the `mooncake_master` process, representing the eviction high watermark and the single eviction ratio of the pooling space, respectively.

- `port`: (Optional) Used to configure the service port for the KV pool. If not configured, `deploy.py` will supplement and adapt it with the default value `50088`.

### Deploying the Service

Deploy the service using the `deploy.py` script in the `examples/deployer` directory. You can specify a configuration directory or specify the configuration file separately:

```bash
cd examples/deployer
# Method 1: Specify the configuration directory (recommended)
python deploy.py --config_dir ../infer_engines/vllm

# Method 2: Specify the configuration file separately
python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
```
