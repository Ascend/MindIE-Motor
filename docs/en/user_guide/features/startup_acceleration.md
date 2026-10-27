# D2D Weight Loading Feature

## Feature Introduction

D2D weight loading is a model weight startup acceleration capability provided by MindIE Motor. When a new instance starts, it can pull weights directly over the network from ready (ACTIVE) instances of the same role in the cluster, instead of loading all weights from disk, thereby shortening the startup time.

### How It Works

The working principle of the D2D weight loading feature is roughly divided into the following three steps:

1. Enable the D2D configuration in the `engine_config` of `user_config.json`.
2. When the first instance has no available peer, the engine loads weights from the local disk and provides the weight service externally through `listen_port` (seed mode).
3. For subsequent instances, the Controller automatically discovers ACTIVE instances of the **same role**, collects peer IPs (the peer discovery route is completed automatically by the Controller/NodeManager, so there is no need to fill in peer IPs manually), and delivers them to the NodeManager; the engine pulls the corresponding shard weights from the peers in netloader mode.

### Constraints and Limitations

| Constraint Dimension | Requirement |
|----------|------|
| Hardware | Atlas 800I A2, Atlas 800I A3 |
| Deployment scenario | PD disaggregation service deployment, PD aggregation service deployment |
| Engine | vLLM only |
| Feature mutual exclusion | None |
| Software dependency | The vLLM Ascend version must be 27.1 or later |
| Other restrictions | <ul><li>Peer matching: Only instances of the same role (Prefill to Prefill, Decode to Decode, and so on) whose status is ACTIVE are matched, and the instance itself is excluded.</li><li>Port: `listen_port` must be reachable within the cluster network and must not conflict with existing service ports.</li><li>Weight path: The first instance (seed) still needs access to the local model weight directory; subsequent instances can rely on D2D pulling.</li></ul> |

The following models have been verified for D2D startup acceleration in the MindIE Motor example configurations:

| Model | Configuration Directory (Reference) |
|------|----------|
| Qwen3-30B | model_configs/vllm/models/qwen_235b/ |
| DeepSeek-V3.1-w8a8-mtp | model_configs/vllm/models/deepseek_v3.1/ |
| DeepSeek-V4-Flash-w8a8-mtp | model_configs/vllm/models/deepseek_v4_flash/ |
| DeepSeek-V4-Pro-w4a8 | model_configs/vllm/models/deepseek_v4_pro/ |
| GLM-5.1-w4a8 | model_configs/vllm/models/glm_5.1/ |
| GLM-5.1-w8a8 | model_configs/vllm/models/glm_5.1/ |

## Using the Feature

### Environment Preparation

The service has been deployed according to [PD disaggregation service deployment](../deployment/k8s/pd_disaggregation_deployment.md) or [PD aggregation service deployment](../deployment/k8s/pd_aggregation_deployment.md), and the service is running normally. All subsequent operations are performed only on the management node (master node) of the K8s cluster.

### Usage Scenarios

- Fault rescheduling scenario: If a fault occurs after the initial instance is started, weights are automatically pulled from ACTIVE instances of the **same role** during rescheduling.

- Instance scale-out scenario: When P/D/U instances have been deployed and are running normally, if scale-out is required, the scaled-out nodes automatically pull weights from ACTIVE instances of the **same role**, without cold loading weights from disk.

Both scenarios share the same configuration and the same procedure:

- The fault rescheduling scenario is triggered automatically by the Controller, and no additional operation is required.

- The instance scale-out scenario requires the scale-out command to be executed.

### Usage Example

The D2D weight loading feature is configured through the `engine_config` field in the `user_config.json` configuration file. The Controller considers the D2D weight loading feature enabled only when all the following conditions are met:

- `model_loader_extra_config` exists and is a valid JSON object;

- `source` is set to `"auto"`;

- `listen_port` is configured.

Once the conditions are met, the engine automatically sets `load_format = "netloader"` when it starts.

**Procedure**

1. Modify the `user_config.json` configuration file.

   In `model_loader_extra_config` under `engine_config` of `motor_engine_*_config` for the corresponding role, fill in `source` and `listen_port`. The following uses Prefill as an example:

   ```json
   {
     "motor_engine_prefill_config": {
       "engine_type": "vllm",
       "engine_config": {
         "model": "/data01/models/DeepSeek-V3.1",
         "model_loader_extra_config": {
           "source": "auto",
           "listen_port": 10000
         }
       }
     }
   }
   ```

   **Table 1** Configuration parameter description

   | Configuration Item | Type | Value Range | Mandatory | Default Value | Description |
   |--------|------|----------|------|--------|------|
   | source | string | auto | Yes | "auto" | Fixed to `"auto"`, indicating that the peer address is filled in automatically by the Controller. |
   | listen_port | int | Port number | Yes | None | Start port on which this instance provides the weight service; the actual port of each device is `listen_port+device_rank` (including the DP offset). |
   | int8_cache | string | ["hbm", "dram", "no"] | Optional | "no" | Whether to enable the INT8 cache; when it is not enabled, all parameters are transferred directly. |
   | int8_cache_name | list | INT8 cache names | Optional | None | Internal int8 parameter names of vLLM Ascend; if configured, D2D transfers only the corresponding int8 parameters. By default, no filtering is performed and all parameters are transferred.|
   | output_prefix | string | File name prefix | Optional | None | If set, each rank produces {OUTPUT_PREFIX}{RANK}.txt, which contains the IP:Port pairing information of each rank.|

   >[!NOTE]NOTE
   >
   >- If speculative inference (such as `speculative-config` / MTP) is enabled for the model, the main model and the draft model share the same `source` and `listen_port` configuration, and no additional configuration is required. The draft weight service port is automatically offset by 10000 from `listen_port`; in this case, it is recommended to configure the port as < 55535 - device_rank.
   >- Configuration key names can be in lowercase (such as `listen_port`) or uppercase (such as `LISTEN_PORT`); the two are equivalent.

2. Deploy the first instance.

   Obtain the PD disaggregation inference service deployment configuration according to [PD disaggregation service deployment](../deployment/k8s/pd_disaggregation_deployment.md), and add `model_loader_extra_config` to the corresponding `user_config.json` as described in step 1 (configure Prefill / Decode / Union separately according to the actual role).

   Run the following command to deploy the first instance, and wait until the instance enters the ACTIVE state:

   ```bash
   python3 deploy.py --config_dir .../../model_configs/vllm/models/...
   ```

   >[!NOTE]NOTE
   >
   >`--config_dir` is the directory that contains `user_config.json` and `env.json`. The above `.../../model_configs/vllm/models/...` is a path example and must be replaced with the configuration directory corresponding to the engine and model actually used (refer to the configuration directories in the "tested models" table of this document).

3. Scale out or deploy a new instance of the same role.

   When you scale out or deploy a new instance of the same role, the Controller automatically delivers the peer IPs to the new instance.

   You can perform scale-out with the following command:

   ```bash
   python3 deploy.py --config_dir .../../model_configs/vllm/models/... --update_instance_num
   ```

4. <a id="step004"></a>Confirm the deployment result.

   If the following output appears in the log, the external service has started, and subsequent new instances can pull weights from this instance.

   ```text
   [netloader.py:664] Start elastic Netloader server, rank: 3, listen port: 10.244.66.130:12348, group: netloader
   [netloader.py:708] Elastic server started, rank: 3, group: netloader
   ```

### Verifying the Feature

When a new instance starts, it automatically pulls weights from the seed instance.

```text
[netloader.py:514] Netloader manifest build time: 0.8685483103618026, rank: 6, manifest=2682
[elastic.py:136] Start connection to server: 10.244.66.130:12351
[elastic.py:138] Finish connection to server: 10.244.66.130:12351
[elastic.py:252] Receive ack: label=JOIN_ACK name=10.244.169.204:38392 transfer_shape_count=2682
[elastic_load.py:505] [netloader_p2p] HCCL recv time: 3.1666774908080697, transfer=2682 group=netloader processed_layout=True addr=10.244.66.130:58045
[load.py:90] Netloader P2P load time: 3.5929042901843786, device_id: 6, group: netloader
[netloader.py:664] Start elastic Netloader server, rank: 6, listen port: 10.244.169.204:12351, group: netloader
[netloader.py:708] Elastic server started, rank: 6, group: netloader
```

## FAQs

### Port Conflict Causes the Elastic Server to Fail to Start

**Symptom**

The Elastic server fails to start, and the instance can no longer serve as a seed instance from which other instances pull weights.

**Cause Analysis**

`listen_port` (the actual port of each device is `listen_port+device_rank`, including the DP offset) conflicts with an existing service port in the cluster.

**Solution**

1. Referring to the port requirements in "Constraints and Limitations" of this document, change `model_loader_extra_config.listen_port` to avoid conflicts with existing service ports.
2. Redeploy the instance and confirm that `Elastic server started` appears in the log; for log details, see [step 4](#step004).

### HCCL Transfer of a New Model Is Stuck for a Long Time or Falls Back to Loading from Disk

**Symptom**

When a new model is tested, the HCCL transfer is stuck for a long time, or the feature fails and falls back to loading weights from disk.

**Cause Analysis**

This feature is strongly coupled with model weights, and unverified models may have compatibility issues with D2D weight transfer.

**Solution**

Submit an [ISSUE](https://gitcode.com/Ascend/MindIE-Motor/issues) to the official community with the model name and the instance logs.
