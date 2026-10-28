# UBG Networking Modification

On 950PR&950DT products, when a single node has 16 cards, Prefill uses `0-7`, Decode uses `8-15`, and the communication protocol is `ub_rtp:device`, make the following changes in **your own Docker startup copy** relative to the current `master`. This approach does not go through the K8s `deploy.py`. The patches are bound to the on-site card IDs, NICs, and ports, and are not merged into the trunk directly.

The IP, NICs, image name, and card IDs below are examples from the run on `141.63.88.64` on 2026-09-20. The model is `Qwen3-30B-A3B-W8A8`. Replace them with your own values when you change machines.

## 1. Making Decode Use Physical Cards 8-15

When `deploy_mode=multi_deployment`, `NodeManager._calc_visible_device_ids` of the current `master` does **not** add `device_offset`. Decode still selects cards according to `0,1,2,3` / `4,5,6,7`. The container, however, mounts `davinci0` through `davinci15` for `ub_rtp`, so Decode and Prefill see the same set of EID / phyId, the Mooncake `CreateChannel` call fails, and the logs show a `devPhyId` or EID conflict in `local/remote`.

Do not merge the entire `service.py` into the trunk. Keep a patch outside the image and modify `_calc_visible_device_ids`:

- Read `ASCEND_RT_VISIBLE_DEVICES` first.

- When the role is `decode` and this variable is empty or its first card ID is `0`, use `MOTOR_DECODE_VISIBLE_DEVICES` instead. The example is `8,9,10,11,12,13,14,15`.

- Slice this physical card list by DP rank, and do not renumber from `0` again.

After the change, the Decode log should contain:

```text
Device IDs: 8,9,10,11
Device IDs: 12,13,14,15
```

This file must be overwritten in both containers. On site, write it through the UpperDir of the Docker overlay instead of relying on `docker exec` (exec hangs when the container is stuck):

```text
/usr/local/python3.11.10/lib/python3.11/site-packages/motor/node_manager/core/services/native_engine/service.py
```

The path varies with the Python version in the image, so confirm it in the container first. For a container that is already running, you can modify the same path with `nsenter` by container PID. Restart the process after the modification, because changing the file alone does not replace the code that has already been loaded.

The visible cards in `env.json` and `engine.sh` must be consistent with the patch:

- Prefill: `ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7`

- Decode: `ASCEND_RT_VISIBLE_DEVICES=8,9,10,11,12,13,14,15`

## 2. The Container Still Needs to See All Cards

`ub_rtp:device` locates devices by physical card. If only the `--device` of Decode is narrowed to `8-15`, the protocol stack still accesses `davinci0`. In the startup script, **both** Prefill and Decode mount `davinci0` through `davinci15`, and additionally mount the following devices if they exist:

- `/dev/davinci_manager`

- `/dev/hisi_hdc`

- `/dev/devmm_svm`

- `/dev/ummu`

- `/dev/uburma`

Start the container with `--privileged`, `--network=host`, and `--cgroupns=host`. The visible card list remains `0-7` / `8-15` from section 1; do not set `ASCEND_RT_VISIBLE_DEVICES` to all 16 cards.

## 3. Separating Decode Ports from Prefill

On the same machine, the two engines cannot share the NodeManager, the DP RPC port, the service port, or the KV port. In this example, the Decode startup parameters are:

| Parameter | Decode | Corresponding Setting on the Prefill Side |
|------|--------|-------------------|
| `--node-manager-port` | `4026` | Must not be the same as the Prefill management port |
| `--data-parallel-rpc-port` | `9100` | A separate port range |
| `--base-port` | `30200` | The Prefill example uses `10000` / `10002` |
| `--kv-port` | `20500` | A separate port range |

The Coordinator inference port in this example is `30000`, and the model name is `qwen`. In `user_config.json`, `deploy_mode` is `multi_deployment`, `data_parallel_size=2`, and `tensor_parallel_size=4`.

## 4. HCCL Ports and ub_rtp

Set the on-site NIC in the common section, Prefill, and Decode of `env.json`. Example:

- `HCCL_IF_IP=141.63.88.64`

- `HCCL_SOCKET_IFNAME=enp34s0f1`

- `ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.protocol_desc":["ub_rtp:device"]}`

- `ASCEND_LOCAL_COMM_RES={"version":"1.3"}`

Separate the HCCL socket ports by role so that Prefill and Decode do not contend for the same range:

- Prefill: `HCCL_NPU_SOCKET_PORT_RANGE=16000-16999`

- Decode: `HCCL_NPU_SOCKET_PORT_RANGE=17000-17999`

For `gpu_memory_utilization` of Decode, this example uses `0.92`. With `0.95`, the remaining memory is insufficient and the engine cannot start. Change this value only when the weights or the memory headroom change.

## 5. UBG Patches for vLLM Ascend 0.27.1

The vLLM Ascend in the image also requires a set of patches for UBG / `ub_rtp`. Write them into the UpperDir as well; they are not merged into the Motor trunk. The on-site directory is `motor_patches/`, which includes at least:

- `worker/worker.py`: adds `get_resolved_kv_cache_layout`

- `patch/platform/patch_kv_cache_coordinator.py`: adapts `num_prefill_lookahead` with `inspect.signature`

- `utils.py`: adds `shared_by`

- `attention/`, `quantization/methods/w8a8/`, `distributed/kv_transfer/utils/mooncake_transfer_engine.py`

These files target the `v0.27.1` image used on site. Compare them against the original files before changing the image, and do not overwrite if they do not match.

## 6. Startup Order and What Not to Do

Start the Controller first, then Prefill, then Decode, then apply the patches in section 1 and section 5, and finally start the processes. If `npu-smi` or `TsdOpen` still occupies the device after `kill -9`, the next round cannot start; run `docker restart` before starting again. When the container is already stuck, do not modify files with `docker exec`; modify the UpperDir instead.

HTTP 200 only indicates that the request has entered the Coordinator. To check whether pooling takes effect, look for `KV cache transfer ... took ... ms` in the Decode log and for the absence of EID / Connect timeout.
