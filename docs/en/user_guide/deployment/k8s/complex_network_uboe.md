# UBOE Networking Modifications

On Ascend 950PR&950DT products, when `uboe:device` is used for pooling and Prefill and Decode are placed on the same machine with 4 cards each, modify the following places in your own deployment copy, relative to `examples/deployer` of the current `master`. These values are tied to node names and are not merged into the mainline directly.

The configuration directory of this example is `model_configs/vllm/dsv4_uboe_memcache_k8s/`, and `deploy.py` is in `examples/deployer/`. Both Prefill and Decode are pinned to `motor-master`, and the namespace comes from `job_id`: `mindie-motor-dsv4-k8s`. The Coordinator NodePort in the example is `31015`.

This differs from the host RoCE setup: here `ASCEND_GLOBAL_RESOURCE_CONFIG` must be set to `uboe:device`, and privileges must **not** be enabled for the engine. A privileged container can see all `/dev/davinci*` devices, and Prefill and Decode on the same node would initialize the same set of physical cards.

## 1. Put Environment Variables in env.json

The protocol, proxy, and HCCL settings are written in `env.json`; do not paste the `set_*_env` functions in `startup/roles/*.sh` before `#!/bin/bash`. If they are pasted before the shebang, the kernel does not interpret the script as bash, and these functions are never called.

Write the following in all of `motor_common_env`, `motor_engine_prefill_env`, and `motor_engine_decode_env`:

```text
ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.protocol_desc":["uboe:device"]}
ASCEND_LOCAL_COMM_RES={"version":"1.3"}
```

Set the four proxy variables to empty strings. Write the local host, the management-plane IP, and the service-plane IP in `no_proxy` / `NO_PROXY`. This example uses `141.61.45.124`, `141.61.133.132`, `20.0.232.33`, and `20.0.240.83`; replace them with your own addresses in another environment.

Add the following for both Prefill and Decode:

| Variable | Prefill | Decode |
|------|---------|--------|
| `VLLM_RPC_TIMEOUT` | `3600000` | `3600000` |
| `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS` | `30000` | `30000` |
| `HCCL_EXEC_TIMEOUT` | `204` | `204` |
| `HCCL_CONNECT_TIMEOUT` | `120` | `120` |
| `HCCL_BUFFSIZE` | `512` | `2600` |
| `HCCL_ALGO` | `level0:fullmesh` | `level0:fullmesh` |
| `PYTORCH_NPU_ALLOC_CONF` | `expandable_segments:True` | Same as left |
| `TASK_QUEUE_ENABLE` | `1` | `1` |
| `OMP_NUM_THREADS` | `10` | `10` |

In the common items, this example also enables `VLLM_USE_V2_MODEL_RUNNER=1`. There is no need to write it again if the image already enables it by default.

## 2. Clear HTTP Proxies in Processes

Setting `HTTP_PROXY` in the image ENV to an empty string keeps the variable present, and libraries still go through the proxy.

In `startup/boot.sh`, after sourcing `common.sh` and before the role branches:

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
```

kubelet runs `probe/probe.sh` directly without sourcing `boot.sh`, so unset the variables in `probe.sh` as well. Add `trust_env=False` to the httpx client of `probe/probe.py`.

## 3. Do Not Enable Privileges for the UBOE Overlay

The current `master` adds `privileged: true` for engines on Ascend 950PR&950DT products when ASCEND_GLOBAL_RESOURCE_CONFIG is `uboe` / `roce` / `ub_rtp:device`. When P/D each occupy 4 cards on the same node, a privileged container sees all cards, and the two sides initialize the same set of physical cards.

In `apply_a5_engine_pod_config` of `lib/generator/engine.py`, change the code as follows when the overlay is enabled:

```python
sec = container.setdefault(C.SECURITY_CONTEXT, {})
sec[C.PRIVILEGED] = False
sec["allowPrivilegeEscalation"] = False
```

Keep it non-privileged so that the device plugin hides the NPUs that do not belong to this Pod. Do not follow the host RoCE setup and make all engines on Ascend 950PR&950DT products privileged.

## 4. Remove the Duplicate npu-smi Mount

Delete `npu-smi-bin` (`/usr/local/bin/npu-smi`) from `A5_HOST_PATH_VOLUMES_HOST_NIC` in `lib/constant.py`. The engine template already mounts the same path with the volume name `npu-smi`, and mounting two names to the same `mountPath` is rejected by the API.

## 5. memcache Port and Probe Timeout

`kv_cache_store_config.port` in `user_config.json` affects only the MetaService listener. LocalService still reads `mmc-local-*.conf`. In `sync_mmc_local_config` of `startup/common.sh`, if `KV_CACHE_STORE_PORT` is set, change the port of `ock.mmc.meta_service_url` in the conf file to the same value.

Change the `timeoutSeconds` of the startup probe to `30`:

- The startupProbe of the engine and kv-store in `yaml_template/infer_service_template.yaml` (kv-store was originally `3`)

- `yaml_template/controller_template.yaml`

- `yaml_template/coordinator_template.yaml`

- `yaml_template/kv_cache_store_template.yaml`

Change `name_space` in `log_collect/log_config.ini` to the namespace used here, which is `mindie-motor-dsv4-k8s` in the example. This affects only log collection filtering.

## 6. Networking-Related Items in user_config

This example:

- `hardware_type`: `Ascend950`

- Prefill and Decode each have 1 instance, 1 Pod, and 4 cards, all pinned to `motor-master`

- Weight directory `/mnt/share/weight`, model name `dsv`

- Prefill `data_parallel_size=4`, `tensor_parallel_size=1`, with prefix cache enabled

- The KV connector uses MooncakeHybrid with memcache, and `kv_port` is `36020` in the example

- Fill in the image name according to the on-site repository, and do not write it into the mainline

Change the node name, ports, and weight path when moving to another environment.

## 7. When Using Docker Only

`python3 deploy.py` does not use these files. When only `docker_deploy.py` is used:

- Use `ASCEND_RT_VISIBLE_DEVICES` for visible cards, and do not write `ASCEND_VISIBLE_DEVICES=0..n-1`. When a privileged container sees all cards, the logical numbering collides with the other side.

- Bind `/dev/ummu` and `/dev/uburma` with `-v` instead of `--device`.

- Remove `-v /driver:/driver` from the KVS container.

- Remove `-t` from `docker run` and keep only `-i`. In `lib/in_place_run.py`, do not open a PTY for the startup process; use a pipe and set `PYTHONUNBUFFERED=1` to avoid flooding the console with CANN slog output.

For this UBOE K8s setup, do not rely on section 7. Card isolation relies on the non-privileged setting and the device plugin described in section 3.
