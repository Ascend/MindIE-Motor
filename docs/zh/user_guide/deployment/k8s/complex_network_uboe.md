# UBOE 组网改法

Ascend 950PR&950DT系列产品上用 `uboe:device` 做池化，Prefill 和 Decode 落在同一台机器、各 4 张卡时，相对当前 `master` 的 `examples/deployer`，在自己的部署副本里改下面这些地方。这些值和节点名绑在一起，不直接合入主干。

这套例子的配置目录是 `model_configs/vllm/dsv4_uboe_memcache_k8s/`，`deploy.py` 在 `examples/deployer/`。Prefill 和 Decode 都钉在 `motor-master`，命名空间来自 `job_id`：`mindie-motor-dsv4-k8s`。Coordinator NodePort 例子是 `31015`。

和 host RoCE 那套不同：这里要设 `ASCEND_GLOBAL_RESOURCE_CONFIG` 为 `uboe:device`，并且 **不要** 给引擎开特权。特权容器能看见全部 `/dev/davinci*`，同一节点上的 Prefill 和 Decode 会初始化同一批物理卡。

## 1. 环境变量放在 env.json

协议、代理和 HCCL 写在 `env.json`，不要把 `set_*_env` 函数贴到 `startup/roles/*.sh` 的 `#!/bin/bash` 前面。贴在 shebang 之前，内核不会按 bash 解释脚本，这些函数也没有被调用。

`motor_common_env`、`motor_engine_prefill_env`、`motor_engine_decode_env` 都写上：

```text
ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.protocol_desc":["uboe:device"]}
ASCEND_LOCAL_COMM_RES={"version":"1.3"}
```

代理四个变量设成空字符串。`no_proxy` / `NO_PROXY` 写上本机、管理面 IP 和业务面 IP。这套例子里有 `141.61.45.124`、`141.61.133.132`、`20.0.232.33`、`20.0.240.83`，换环境换成自己的地址。

Prefill 和 Decode 再加：

| 变量 | Prefill | Decode |
|------|---------|--------|
| `VLLM_RPC_TIMEOUT` | `3600000` | `3600000` |
| `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS` | `30000` | `30000` |
| `HCCL_EXEC_TIMEOUT` | `204` | `204` |
| `HCCL_CONNECT_TIMEOUT` | `120` | `120` |
| `HCCL_BUFFSIZE` | `512` | `2600` |
| `HCCL_ALGO` | `level0:fullmesh` | `level0:fullmesh` |
| `PYTORCH_NPU_ALLOC_CONF` | `expandable_segments:True` | 同左 |
| `TASK_QUEUE_ENABLE` | `1` | `1` |
| `OMP_NUM_THREADS` | `10` | `10` |

公共项里这套例子还开了 `VLLM_USE_V2_MODEL_RUNNER=1`。镜像已经默认打开时不必再写。

## 2. 清掉进程里的 HTTP 代理

镜像 ENV 里的 `HTTP_PROXY` 写成空字符串后变量还在，库仍会走代理。

`startup/boot.sh` 在 source `common.sh` 之后、按角色分支之前：

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
```

kubelet 直接跑 `probe/probe.sh`，不 source `boot.sh`，所以 `probe.sh` 里同样 unset。`probe/probe.py` 的 httpx 客户端加 `trust_env=False`。

## 3. UBOE overlay 不要开特权

当前 `master` 在 ASCEND_GLOBAL_RESOURCE_CONFIG 为 `uboe` / `roce` / `ub_rtp:device` 时给Ascend 950PR&950DT系列产品的引擎加 `privileged: true`。同一节点上 P/D 各占 4 张卡时，特权容器看见全部卡，两边会初始化同一批物理卡。

在 `lib/generator/engine.py` 的 `apply_a5_engine_pod_config` 里，overlay 打开时改成：

```python
sec = container.setdefault(C.SECURITY_CONTEXT, {})
sec[C.PRIVILEGED] = False
sec["allowPrivilegeEscalation"] = False
```

保持非特权，让设备插件藏住不属于这个 Pod 的 NPU。不要照 host RoCE 那套把Ascend 950PR&950DT系列产品的引擎一律改成特权。

## 4. 去掉重复的 npu-smi 挂载

`lib/constant.py` 的 `A5_HOST_PATH_VOLUMES_HOST_NIC` 里删掉 `npu-smi-bin`（`/usr/local/bin/npu-smi`）。引擎模板已经用 volume 名 `npu-smi` 挂了同一路径，两个名字挂到同一个 `mountPath` 会被 API 拒绝。

## 5. memcache 端口和探活超时

`user_config.json` 里的 `kv_cache_store_config.port` 只影响 MetaService 监听。LocalService 仍读 `mmc-local-*.conf`。在 `startup/common.sh` 的 `sync_mmc_local_config` 里，如果设置了 `KV_CACHE_STORE_PORT`，把 conf 里的 `ock.mmc.meta_service_url` 端口改成同一个值。

startup probe 的 `timeoutSeconds` 改成 `30`：

- `yaml_template/infer_service_template.yaml` 里引擎和 kv-store 的 startupProbe（kv-store 原来是 `3`）
- `yaml_template/controller_template.yaml`
- `yaml_template/coordinator_template.yaml`
- `yaml_template/kv_cache_store_template.yaml`

`log_collect/log_config.ini` 的 `name_space` 改成这次的命名空间，例子是 `mindie-motor-dsv4-k8s`。这只影响日志采集过滤。

## 6. user_config 里和组网相关的项

这套例子：

- `hardware_type`：`Ascend950`
- Prefill、Decode 各 1 个实例、各 1 个 Pod、各 4 张卡，都钉在 `motor-master`
- 权重目录 `/mnt/share/weight`，模型名 `dsv`
- Prefill `data_parallel_size=4`，`tensor_parallel_size=1`，开 prefix cache
- KV connector 用 MooncakeHybrid 加 memcache，`kv_port` 例子是 `36020`
- 镜像名按现场仓库填写，不要写进主干

节点名、端口、权重路径换环境时改掉。

## 7. 只用 Docker 时

`python3 deploy.py` 不走这些文件。只用 `docker_deploy.py` 时：

- 可见卡用 `ASCEND_RT_VISIBLE_DEVICES`，不要写 `ASCEND_VISIBLE_DEVICES=0..n-1`。特权容器看见全部卡时，逻辑编号会和另一侧撞车。
- `/dev/ummu`、`/dev/uburma` 用 `-v` 绑定，不用 `--device`。
- KVS 容器去掉 `-v /driver:/driver`。
- 去掉 `docker run -t`，只留 `-i`。`lib/in_place_run.py` 不要给启动进程开 PTY，用管道并设置 `PYTHONUNBUFFERED=1`，避免 CANN slog 刷屏。

K8s 这条 UBOE 不要靠第 7 节。卡隔离靠第 3 节的非特权和设备插件。
