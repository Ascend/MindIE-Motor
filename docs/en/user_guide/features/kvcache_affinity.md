# KV Cache Affinity Scheduling

## Feature Introduction

KV cache affinity scheduling selects an inference endpoint for a request by combining cache prefix hits with the current load.
kv-conductor maintains the cache index, and the Coordinator uses the index results to estimate the remaining prefill workload and select an inference endpoint.
The benefit mainly comes from reusing existing KV and reducing repeated prefill computation; the actual throughput and latency gains depend on the request distribution, the cache medium, and the load.

- **kv-conductor**: Subscribes to events, maintains the HBM prefix tree and the CPU/Disk contiguous-edge index, and returns the number of mutually exclusive hit blocks of each DP.

- **Coordinator**: Obtains the token IDs of the request, weights the hit blocks by medium, and calculates the scheduling score together with the current load.

- **Engine and KV Cache Store**: Responsible for the actual KV reuse and data transfer. The Conductor only maintains the index and does not move KV data.

Affinity scheduling applies to the Prefill (P) role in PD disaggregation and the Union (U) role in PD aggregation.
Enable `kv_cache_affinity` for P/U through `prefill_scheduler_type`; for Decode (D), use
`decode_scheduler_type` to select load balancing or round-robin. Both configuration items default to `load_balance`.

| Sub-strategy | Initial Endpoint Selection Behavior |
|--------|--------------|
| `unified` (default) | Fuses the weighted cache benefit and load of all available candidates, and selects the endpoint with the lowest overall cost. |
| `load_gated` | First retains the N endpoints with the lowest load, then selects the best one by weighted prefix match length; in a tie, the lower load wins. |

Both strategies support `hit_rate_threshold`; for the configuration method, see [Parameter Description](#note001).

## Using the Feature

### Environment Preparation

- Complete the [PD disaggregation deployment](../deployment/k8s/pd_disaggregation_deployment.md) or the
  [PD aggregation deployment](../deployment/k8s/pd_aggregation_deployment.md) first, and confirm that basic inference works normally.
  If the Controller / Node Manager is not deployed, use the [Coordinator standalone deployment](../deployment/standalone.md).

- The configuration examples in this document target vLLM; for the SGLang baseline and cache switches, see
  [SGLang PD disaggregation deployment](../deployment/k8s/pd_disaggregation_sglang.md).

- The engine must publish usable KV events and provide the corresponding cache reuse capability. The P/U event ports must be reachable from the network where the conductor resides.

- The Coordinator must be able to obtain token IDs consistent with those of the engine. `model_path` is derived from the engine configuration by default, and the local tokenizer
  must be able to access the corresponding model directory; the token IDs produced by Render can be reused, and the configuration is described in
  [Coordinator Component Description](../../developer_guide/components/coordinator.md).
  Inconsistent tokenizers, chat templates, or tool calling templates all affect prefix matching.

- The image must be able to start kv-conductor; see the component check method below.

### Usage Example

<a id="build"></a>

1. Confirm whether the kv-conductor component is available in the actually running image.

    ```bash
    python -m motor.kv_conductor --help
    ```

    It is sufficient if the startup parameter description is printed normally. If the component is missing, prepare an image that contains the component according to
    [KV Conductor Build and Installation](../../../../motor/kv_conductor/README.md).

2. Configure the `user_config.json` file.

     - The following configuration parameters are increments merged into an existing configuration and cannot be used directly as a complete deployment configuration.
     - `block_size: 128` assumes that the event granularity of the main attention group is 128; for hybrid KV models, change it according to the [actual event granularity](#deepseek-v4).
     - The PD disaggregation and PD aggregation examples below enable **HBM affinity**; for CPU/Disk, continue to configure [pooling affinity](#pool-affinity) on top of them.

    Merge the increments field by field into the existing `user_config.json`, and keep the model, image, resource, and basic transfer configurations.

    **PD disaggregation configuration**

    Baseline file: [`model_configs/vllm/user_config.json`](../../../../model_configs/vllm/user_config.json).

    ```json
    {
      "motor_coordinator_config": {
        "scheduler_config": {
          "prefill_scheduler_type": "kv_cache_affinity"
        }
      },
      "motor_engine_prefill_config": {
        "engine_config": {
          "kv-events-config": {
            "publisher": "zmq",
            "enable_kv_cache_events": true,
            "endpoint": "tcp://*:5557",
            "topic": "kv-events"
          }
        }
      },
      "kv_conductor_config": {
        "block_size": 128,
        "http_server_port": 13333
      }
    }
    ```

    `kv-transfer-config` belongs to the basic transfer configuration of PD disaggregation; keep the existing deployment settings.

    **PD aggregation configuration**

    Baseline file: [`model_configs/vllm/pd_hybrid/user_config.json`](../../../../model_configs/vllm/pd_hybrid/user_config.json).
    Place the event configuration in `motor_engine_union_config`.

    ```json
    {
      "motor_coordinator_config": {
        "scheduler_config": {
          "prefill_scheduler_type": "kv_cache_affinity"
        }
      },
      "motor_engine_union_config": {
        "engine_config": {
          "kv-events-config": {
            "publisher": "zmq",
            "enable_kv_cache_events": true,
            "endpoint": "tcp://*:5557",
            "topic": "kv-events"
          }
        }
      },
      "kv_conductor_config": {
        "block_size": 128,
        "http_server_port": 13333
      }
    }
    ```

    <a id="note001"></a>
    **Parameter Description**

    The following uses the **K8s Deployer + vLLM** deployment in this document as an example, and the configuration is merged into the existing `user_config.json`.
    `scheduler_config` is located under `motor_coordinator_config`, and `kv_conductor_config` is written at the top level.

    **Table 1** Parameters that must be configured or checked

    | Parameter | Description |
    |--------|----------|
    | scheduler_config.prefill_scheduler_type | Explicitly set to `kv_cache_affinity` to enable affinity scheduling for P/U. |
    | engine_config.kv-events-config of P/U | Set `publisher: zmq`, `enable_kv_cache_events: true`, and the event address as shown in the example; for PD disaggregation, configure it in the Prefill section, and for PD aggregation, in the Union section. |
    | kv_conductor_config.http_server_port | Specify it explicitly at the top level, for example `13333`; the Deployer relies on this item to enable the conductor, so do not rely only on the runtime default value. |
    | kv_conductor_config.block_size | You must check the actual event granularity of the main attention group, and it is recommended to specify it explicitly as in the example. If it is not specified, it is derived from the engine `block-size` and defaults to `128`; for hybrid KV models, override it according to the [actual event granularity](#deepseek-v4). |

    >[!NOTE]NOTE
    >The event ports must be reachable from the network where the conductor resides; with multiple DPs, check each port obtained by adding the DP rank to the base event port.

    **Table 2** Parameters that require no additional configuration

    | Parameter | Description |
    |--------|----------|
    | scheduler_config.kv_affinity | You can omit the entire object and use the default `unified` strategy and weights first.<br>By default, `hit_rate_threshold=0`, so no additional hit rate threshold is set, and `w_disk=0` means the Disk affinity benefit is not counted; after hit verification passes, adjust it according to [Tuning Suggestions](#tuning-suggestions). |
    | scheduler_config.decode_scheduler_type<br>kv_conductor_config.query_encoding | New configurations can keep the default `load_balance` and `msgpack` respectively. |
    | conductor_service<br>model_path<br>endpoint on the conductor side | The deployment process in this document injects the service address and reads the model path and event address from the P/U engine configuration, so there is usually no need to fill them in again; the Coordinator must still be able to access the model directory. |

    **Table 3** Configuration for specific scenarios

    | Scenario | Description |
    |------|----------|
    | Standalone deployment or external conductor | Configure a reachable `conductor_service` and the actual HTTP port; if they cannot be read from the engine configuration, add `model_path` and `endpoint`. |
    | Using SGLang | Set `kv_conductor_config.engine_type` to `sglang`, and configure the engine according to the [SGLang deployment guide](../deployment/k8s/pd_disaggregation_sglang.md). |
    | Using CPU/Disk pooling | Connect the events on both sides as described in [Pooling affinity configuration](#pool-affinity), and configure the subscription address of the corresponding backend. |
    | Re-registration or historical event replay required | For periodic re-registration, set a positive `re_register_interval_sec` (disabled by default); for historical replay, configure `kv-events-config.replay_endpoint` of the engine. Confirm the recovery capability boundary before enabling it. |
    | Interworking with an old conductor that supports only JSON | Set `kv_conductor_config.query_encoding` to `json`; see [Query exception troubleshooting](#coordinator-cannot-query-the-conductor). |

    For the complete parameter definitions, see [Configuration Reference](../configuration/config_reference.md#motor_coordinator_config); for automatic derivation and migration of old configurations, see [Automatic Derivation and Compatibility](../configuration/config_reference.md#kv_conductor_config-automatic-derivation-and-compatibility).
3. Deploy the service.

    ```bash
    cd examples/deployer
    # PD disaggregation
    python deploy.py --config_dir ../../model_configs/vllm

    # PD aggregation uses its own configuration directory
    python deploy.py --config_dir ../../model_configs/vllm/pd_hybrid
    ```

    After deployment, check registration, cache hits, and the actual endpoint selection as described in [Verifying the Feature](#verifying-the-feature).

### Verifying the Feature

Run the following commands in an environment that can access the service, and replace the namespace and address with actual values:

```bash
NAMESPACE=mindie-motor
CONDUCTOR_URL=http://kv-conductor:13333
kubectl -n "$NAMESPACE" get pods,services -o wide
curl --fail "$CONDUCTOR_URL/health"
curl --fail "$CONDUCTOR_URL/workers"
```

Perform the acceptance check in the following order:

1. **Registration**: `/workers` should contain every target P/U DP; in pooling scenarios, the corresponding pool should also be present.
   The actual Coordinator logs include `HBM DP registered`, `Pool registered`, and `YuanRong node pool registered`;
   the conductor log is `worker registered`.
2. **Events**: After sending inference requests, check event reception and parsing, and confirm that there is no persistent `block_size_mismatch`, subscription connection, or parsing error.
   CPU/Disk hits also require the two-stage events of engine offload and backend storage confirmation.
3. **Hits**: Repeatedly send requests that use the same model and the same template and whose prefix is longer than one block; after the cache events become visible,
   check the coverage of each DP in `conductor query response`. Do not use requests shorter than one block for hit acceptance.
4. **Final endpoint selection**: Correlate the `scheduled` log of the request, and check `policy`, `matched`, `hbm/cpu/disk`,
   `instance/endpoint`, and `repicked`. When `policy=load_balance`, interpret the result together with the threshold and the fallback reason.
5. **Performance and recovery**: After the hit check passes, compare throughput and TTFT/tail latency under the same load; if recovery capability is required,
   separately verify the registration of each DP / pool and the hit recovery after a conductor restart in a test environment.

<a id="pool-affinity"></a>

### Advanced Configuration: Pooling Affinity (CPU/Disk)

First complete the storage pool and Store Connector configuration according to the [KV pooling deployment guide](kv_cache_store/README.md) of the selected backend,
and confirm that the KV can be written and read; then apply the configuration in this section. After `kv_cache_store_config` is enabled, you must also connect
**engine offload events** and **storage backend confirmation events** so that the conductor can build the CPU/Disk index.
Keep the HBM event configuration and the affinity scheduling switch described above.

#### MemCache: Enabling CPU Affinity on a vLLM PD Disaggregation Deployment

1. Enable MetaService broadcast according to the [KV events configuration of MemCache](kv_cache_store/backend/memcache.md#kv-events-affinity).
   In the current startup script, configurations such as `kv_events_enable` are commented out by default and must be enabled as instructed there;
   set the event port, the model ID, and the block granularity, and restart kv_store. The MemCache version in use must support these event configuration items.
2. Check that the engine can publish offload events of the Store Connector. When MultiConnector is used, check whether the version in use already supports
   event forwarding of sub-Connectors; if not, apply the corresponding patch as instructed in the MemCache guide above.
3. Merge the following increments into the top-level `kv_conductor_config` of the existing `user_config.json`:

    ```json
    {
      "kv_conductor_config": {
        "store_backend": "Memcache",
        "pool_endpoint": "tcp://*:5557"
      }
    }
    ```

    `5557` is an example broadcast port and must be consistent with `kv_events_endpoint` of the MetaService and the port exposed by the Service.
    The `*` here is replaced by the `KVS_MASTER_SERVICE` injected by K8s; when using an external storage pool, enter the actual domain name or IP that the conductor can access.
    `pool_endpoint` takes the event broadcast address; the read/write port of the storage service cannot be used for this item.
4. Check that `kv_events_model_name` / `kv_events_block_size` of the MetaService are consistent with
   `kv_conductor_config.model_path` / `block_size` used for pool registration, and confirm that the model ID of the pool is consistent with that registered by the target P/U.
   The node IP carried in backend events must be mappable to the target P/U node; the `backend_id` of MemCache is injected automatically according to the backend deployment guide.
5. Apply the Motor configuration as described in [Usage Example](#usage-example), and then check it as described in [Pooling Acceptance](#pool-affinity-validation) below.
   The default CPU weight is `w_cpu=1.0`, and no additional scoring switch is required.

#### Mooncake / YuanRong: Motor Configuration When Event Sources Already Exist

The following describes the Motor access configuration; before applying it, confirm that the backend and the engine already provide events on both sides that are compatible with the conductor.
The installation and startup methods of the event publishing service of each backend are subject to the version actually in use.

| Backend | Configuration merged into the top-level `kv_conductor_config` | To confirm before deployment |
|------|-----------------------------------------|--------------|
| Mooncake | `store_backend: "Mooncake"`; `pool_endpoint` takes the actual ZMQ event broadcast address of the central pool | The [Mooncake deployment document](kv_cache_store/backend/mooncake.md) currently covers mainly the storage service and does not yet provide complete steps for enabling event publishing; confirm the event publishing capability, the address, and the model/block granularity first |
| YuanRong | `store_backend: "YuanRong"`; `cpu_endpoint` / `disk_endpoint` take `tcp://*:<event port of the corresponding medium>` respectively | Node-level event addresses use the node IP and the base port, without the DP rank. Explicitly configure the actual event addresses to avoid falling back to the HBM port; the [backend deployment document](kv_cache_store/backend/yuanrong.md) is still to be completed |

The above configuration subscribes to existing event sources and does not enable broadcast on behalf of the backend.

#### Optional Configuration When Disk Participates in Scoring

Adjust `w_disk` only after Disk storage is actually enabled, Disk events are available, and query hits have been verified.
By default, `w_disk=0`, so Disk hits are not counted in the affinity score; if they need to be counted, merge the following into
`motor_coordinator_config.scheduler_config`, for example:

```json
{
  "motor_coordinator_config": {
    "scheduler_config": {
      "kv_affinity": {
        "w_disk": 1.0
      }
    }
  }
}
```

`1.0` is used only to demonstrate how to enable Disk scoring; adjust the actual value based on the read cost and performance verification.
Changing the weights does not enable Disk storage or supply missing events.

<a id="pool-affinity-validation"></a>

#### Pooling Acceptance

1. Check `/workers` as described in [Verifying the Feature](#verifying-the-feature): in addition to the P/U HBM DPs, MemCache/Mooncake should also have the corresponding central pool,
   and YuanRong should have a node pool. After registration succeeds, further confirm that both engine offload events and backend storage confirmation events have arrived.
2. Construct a request whose **prefix is not covered by HBM but is still retained in CPU or Disk**, and check
   `cpu_blocks` / `disk_blocks` of the target DP in `conductor query response`. The returned coverage is calculated exclusively in the order HBM → CPU → Disk,
   so when the hit comes entirely from HBM, a CPU/Disk value of zero cannot be used to conclude that pooling has failed.
3. Correlate the final `scheduled` log, and check the actual `policy`, `matched`, `hbm/cpu/disk`, and the selected endpoint.
   Interpret the result together with the medium weights, the hit rate threshold, and the load; an index hit still needs to be confirmed as actual KV reuse through engine logs or metrics.

## Tuning Suggestions

| Goal | Adjustment Direction and Boundary |
|------|----------------|
| Initial configuration | Use the default `unified` and the default weights, and first confirm that the index, tokenization, and registration are correct. |
| Strengthen the load influence | Increase `load_weight` while monitoring cache reuse, throughput, and tail latency. |
| Strengthen the affinity preference | Decrease `load_weight`; a value of 0 makes the overall score ignore the load, which may concentrate hot spots and cannot guarantee optimal throughput. |
| Fall back at low hit rates | Set a positive `hit_rate_threshold`; choose the threshold based on the weighted hit rate, and verify the boundary case where the value equals the threshold. |
| Limit the load range of the initial selection | Use `load_gated` with an appropriate N, and take its concurrent re-selection behavior into account. |
| Distinguish cache media | Adjust the medium weights; with the default `w_disk=0`, Disk coverage still appears in the raw query results. |

## FAQs

<a id="deepseek-v4"></a>

### block_size Mismatch Between DeepSeek V4 and Hybrid KV Cache Models

**Symptom**

When `kv_conductor_config.block_size` is inconsistent with the event granularity of the main attention group, the received `BlockStored` events
are dropped for the reason `block_size_mismatch`, which makes the cache index incomplete or prevents requests from obtaining affinity hits.

**Solution**

1. Temporarily enable `RUST_LOG=trace` for the conductor, and check `block_size` and `spec_kind` in the `event_parsed` logs.
2. Use the event granularity of the main attention group (such as `mla_attention`) as the reference and confirm the actual `block_size`; it is not necessarily equal to `--block-size` of the engine.
3. Modify the configuration according to the actual event granularity of the main group. For example, when the actual granularity is 512, merge the following increment into the existing configuration; 512 is not a fixed value for all deployments.

    ```json
    {
      "kv_conductor_config": {
        "block_size": 512
      }
    }
    ```

4. After applying the configuration as described in [Usage Example](#usage-example), check event reception and affinity hits as described in [Verifying the Feature](#verifying-the-feature).
5. After the DCP configuration or the engine version changes, re-check the event granularity; do not rely solely on automatic derivation from the engine page size.

### The Service Is Normal but There Is Never an Affinity Hit

**Symptom**

Basic inference requests can be completed, but after repeatedly sending long-prefix requests, no cache hit or affinity scheduling is observed.

**Solution**

1. Check whether `prefill_scheduler_type` is `kv_cache_affinity`, whether P/U event publishing is enabled,
   and whether `kv_conductor_config.conductor_service` points to a reachable conductor.
2. Check whether the Coordinator can load the tokenizer in the model directory and whether the template is consistent with that of the engine,
   and confirm that the actual number of tokens in the request is not less than one `block_size`.
3. Confirm in `/workers` that the target instances and DPs are registered, and check the event ports, the event granularity of the main attention group,
   and whether the model ID in the query is consistent with the registered value.
4. If the Conductor already returns raw hits but the actual result is `policy=load_balance`, check the medium weights and `hit_rate_threshold`.
   With the default `w_disk=0`, the threshold comparison requires the weighted hit rate to be strictly greater than the threshold.

If basic inference itself or the P/D transfer is abnormal, first troubleshoot according to the corresponding [vLLM PD deployment](../deployment/k8s/pd_disaggregation_deployment.md)
or [SGLang PD deployment](../deployment/k8s/pd_disaggregation_sglang.md) guide.
For the vLLM PD Connector that uses `kv-transfer-config`, check whether `kv_role` is consistent with the P/D role and the Connector requirements,
and check the allocation and connectivity of `kv_port`.

### Coordinator Cannot Query the Conductor

**Symptom**

Conductor connection or query errors appear in the Coordinator log, and requests fall back to load balancing.

**Solution**

1. Check `conductor_service`, `http_server_port`, and the service status, and confirm that the Coordinator can access `/health` of the conductor.
2. When a new Coordinator interworks with an old conductor that supports only JSON, set `kv_conductor_config.query_encoding` to `json`.
   Do not write this field under the old `prefill_kv_event_config`.
3. If the log shows that the query circuit breaker is triggered, fix the connection or query error, wait for the 30-second cooldown window to end, and then check whether subsequent queries recover.

### HBM Hits Are Normal but CPU/Disk Is Always Zero

**Symptom**

The Conductor query already has HBM hits, but in requests expected to use the CPU/Disk cache, no hits on the corresponding medium are observed.

**Solution**

1. Check `store_backend` and the pool / medium event port configuration, and confirm that the corresponding service has started and completed registration.
2. Confirm that both engine offload events and backend storage confirmation events have reached the conductor.
3. YuanRong CPU/Disk uses the node base port without adding `dp_rank`; check the node IP, the model ID, and the target DP.
4. If all events are present but there are still no hits, refer to the cross-medium matching description and check whether the prefix is contiguous and whether the continued lookup belongs to the same `(instance_id, dp_rank)`.
