# UBG 组网改法

Ascend 950PR&950DT系列产品上，单机 16 张卡、Prefill 用 `0-7`、Decode 用 `8-15`、通信协议是 `ub_rtp:device` 时，相对当前 `master` 要在**自己的 Docker 启动副本**里做下面这些改动。这套不走 K8s `deploy.py`。补丁绑现场卡号、网口和端口，不直接合入主干。

下文的 IP、网口、镜像名、卡号都是 `141.63.88.64` 上 2026-09-20 那次的例子。模型是 `Qwen3-30B-A3B-W8A8`。换机器时换成自己的值。

## 1. 让 Decode 用物理卡 8-15

`deploy_mode=multi_deployment` 时，当前 `master` 的 `NodeManager._calc_visible_device_ids` **不会**加 `device_offset`。Decode 仍按 `0,1,2,3` / `4,5,6,7` 选卡。容器又为了 `ub_rtp` 挂了 `davinci0` 到 `davinci15`，于是 Decode 和 Prefill 看到同一组 EID / phyId，Mooncake `CreateChannel` 失败，日志里是 `local/remote` 的 `devPhyId` 或 EID 冲突。

不要把整份 `service.py` 合进主干。在镜像外放一份补丁，改 `_calc_visible_device_ids`：

- 优先读 `ASCEND_RT_VISIBLE_DEVICES`。
- 角色是 `decode`，并且这个变量为空或第一个卡号是 `0` 时，改用 `MOTOR_DECODE_VISIBLE_DEVICES`。例子是 `8,9,10,11,12,13,14,15`。
- 按 DP rank 在这份物理卡列表里切片，不要再从 `0` 重新编号。

改完后 Decode 日志应出现：

```text
Device IDs: 8,9,10,11
Device IDs: 12,13,14,15
```

两台容器都要盖到这份文件。现场用 Docker overlay 的 UpperDir 写入，不依赖 `docker exec`（容器卡住时 exec 会挂住）：

```text
/usr/local/python3.11.10/lib/python3.11/site-packages/motor/node_manager/core/services/native_engine/service.py
```

路径随镜像里的 Python 版本变，先在容器里确认。已经在跑的容器可以用 `nsenter` 按容器 PID 改同一路径，改完要重启进程，只改文件不会换掉已经加载的代码。

`env.json` 和 `engine.sh` 里的可见卡要和补丁一致：

- Prefill：`ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7`
- Decode：`ASCEND_RT_VISIBLE_DEVICES=8,9,10,11,12,13,14,15`

## 2. 容器仍要看见全部卡

`ub_rtp:device` 会按物理卡找设备。只把 Decode 的 `--device` 收成 `8-15`，协议栈仍然会去碰 `davinci0`。启动脚本里 Prefill 和 Decode **都**挂 `davinci0` 到 `davinci15`，另外挂上存在的这些设备：

- `/dev/davinci_manager`
- `/dev/hisi_hdc`
- `/dev/devmm_svm`
- `/dev/ummu`
- `/dev/uburma`

容器用 `--privileged`、`--network=host`、`--cgroupns=host`。可见卡列表仍然是第 1 节的 `0-7` / `8-15`，不要把 `ASCEND_RT_VISIBLE_DEVICES` 写成全部 16 张。

## 3. Decode 的端口和 Prefill 错开

同一台机器上两套引擎不能共用 NodeManager、DP RPC、服务端口和 KV 端口。这套例子里 Decode 启动参数是：

| 参数 | Decode | Prefill 侧对应关系 |
|------|--------|-------------------|
| `--node-manager-port` | `4026` | 不要和 Prefill 的管理口相同 |
| `--data-parallel-rpc-port` | `9100` | 单独一段 |
| `--base-port` | `30200` | Prefill 例子是 `10000` / `10002` |
| `--kv-port` | `20500` | 单独一段 |

Coordinator 推理口例子是 `30000`，模型名 `qwen`。`user_config.json` 里 `deploy_mode` 用 `multi_deployment`，`data_parallel_size=2`，`tensor_parallel_size=4`。

## 4. HCCL 端口和 ub_rtp

`env.json` 的公共项、Prefill、Decode 都写上现场网卡。例子：

- `HCCL_IF_IP=141.63.88.64`
- `HCCL_SOCKET_IFNAME=enp34s0f1`
- `ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.protocol_desc":["ub_rtp:device"]}`
- `ASCEND_LOCAL_COMM_RES={"version":"1.3"}`

HCCL socket 端口按角色分开，避免 Prefill 和 Decode 抢同一段：

- Prefill：`HCCL_NPU_SOCKET_PORT_RANGE=16000-16999`
- Decode：`HCCL_NPU_SOCKET_PORT_RANGE=17000-17999`

Decode 的 `gpu_memory_utilization` 这套例子用 `0.92`。写成 `0.95` 时剩余显存不够，引擎起不来。权重和显存余量变了再改这个数。

## 5. vLLM Ascend 0.27.1 的 UBG 补丁

镜像里的 vLLM Ascend 还要对 UBG / `ub_rtp` 打一组补丁，同样写进 UpperDir，不合入 Motor 主干。现场目录是 `motor_patches/`，至少包括：

- `worker/worker.py`：补 `get_resolved_kv_cache_layout`
- `patch/platform/patch_kv_cache_coordinator.py`：用 `inspect.signature` 适配 `num_prefill_lookahead`
- `utils.py`：补 `shared_by`
- `attention/`、`quantization/methods/w8a8/`、`distributed/kv_transfer/utils/mooncake_transfer_engine.py`

这些文件对着现场用的 `v0.27.1` 镜像。换镜像前先对一下原文件，对不上就不要覆盖。

## 6. 启动顺序和不要做的事

先起 Controller，再 Prefill，再 Decode，然后打第 1 节和第 5 节的补丁，最后拉起进程。`kill -9` 时如果 `npu-smi` 或 `TsdOpen` 还占着设备，下一轮会起不来，应先 `docker restart` 再拉。容器已经卡死时不要用 `docker exec` 改文件，改 UpperDir。

HTTP 200 只说明请求进了 Coordinator。池化是否生效，看 Decode 日志里的 `KV cache transfer ... took ... ms`，以及没有 EID / Connect timeout。
