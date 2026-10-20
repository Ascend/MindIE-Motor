# 复杂组网 deployer 改法

Ascend 950PR&950DT系列产品上用 host RoCE 做 HYBM 池化、Prefill 和 Decode 跨节点时，相对当前 `master` 的 `examples/deployer`，需要在**自己的部署副本**里改下面这些地方。改动绑现场网卡、DNS、卡号和大页，不直接合入主干。

下文的 IP、节点名、卡号、hugepage 大小、hixlep 路径都是一套现场例子（Prefill 在 `motor-master-109`，Decode 在 `motor-worker-104`，命名空间 `mindie-motor-dsv4-roce`）。换环境时换成自己的值。

第 1～12 节是这套跨节点 host RoCE。同一台机器上 Prefill 和 Decode 一起跑、容器不开特权时，用第 13 节，不要套第 3、6、8 节；同机多个 P/D 实例的端口配置见第 14 节。

`deploy.py` 和 `delete.sh` 不用改。

## 1. 放开 memlock

容器默认 memlock 只有 64MB。Prefill 打开 `MF_HYBM_RDMA_SWAP_SPACE_SIZE=2048` 时，要 pin 大约 2GB 的 RoCE swap MR。锁不住这段内存，HCOMM / swap 起不来，后面的 put 会失败。

在 `examples/deployer/startup/boot.sh` 里，engine 角色（`encode`、`prefill`、`decode`、`union`）执行 `engine.sh` 之前加上：

```bash
ulimit -l unlimited
echo "memlock=$(ulimit -l)"
```

改完后在引擎容器里执行 `ulimit -l`，应看到 `unlimited`。

## 2. 把 memcache 协议改成 device_rdma

当前 `master` 在Ascend 950PR&950DT系列产品上把 LocalService 协议写成 `device_urma`（更早的模板默认是 `device_sdma`）。这套池化走的是 HYBM RoCE，不是 UB。

- `device_sdma` 在Ascend 950PR&950DT系列产品上会直接拒绝，报 `A5 or x86 not support batch extend copy`。
- `device_urma` 对不上当前 host RoCE。

在 `examples/deployer/startup/common.sh` 的 `sync_mmc_local_config` 里，该产品对应分支把协议改写从 `device_urma` 换成 `device_rdma`：

```bash
sed -E -i.bak "s|^(ock\\.mmc\\.local_service\\.protocol)[[:space:]]*=.*|\\1 = device_rdma|" "$_dst"
```

模板可以先改成 `device_urma`，运行时仍会被上面这条 sed 盖成 `device_rdma`。两份模板都要改默认值：

- `startup/roles/kv_store_backends/memcache/mmc-local-inprocess.conf`
- `startup/roles/kv_store_backends/memcache/mmc-local-standalone.conf`

```text
ock.mmc.local_service.protocol = device_urma
```

inprocess 模板还要把每个 worker 的 DRAM 从 `0GB` 改成现场池大小。这套例子是 `100GB`，并在 `env.json` 里写 `MMC_A5_INPROCESS_DRAM_SIZE=100GB`。`0GB` 会建成 `hbm{0},dram{0}`，prefix block 没有地方放。

同一函数里，如果设置了 `KV_CACHE_STORE_PORT`，把 conf 里的 `ock.mmc.meta_service_url` 端口改成这个端口。`user_config.json` 的 `kv_cache_store_config.port` 只影响 MetaService 监听，LocalService 仍读 conf，两边不一致时池化连不上。

## 3. 引擎容器加特权

当前 `master` 只在 ASCEND_GLOBAL_RESOURCE_CONFIG 的 host-nic overlay（`uboe` / `roce` / `ub_rtp:device`）下给引擎加特权。对齐同事 Docker 的 `--privileged` 时，Ascend 950PR&950DT系列产品的引擎 Pod 一律打开特权，否则 TsdOpen 看不见 host RoCE 设备。

在 `examples/deployer/lib/generator/engine.py` 的 `apply_a5_engine_pod_config` 里，该产品对应分支追加 hostPath 之后、判断 overlay 之前，给容器加上：

```python
sec = container.setdefault(C.SECURITY_CONTEXT, {})
sec[C.PRIVILEGED] = True
sec["allowPrivilegeEscalation"] = True
```

这套跨节点现场不要设置 `ASCEND_GLOBAL_RESOURCE_CONFIG`。设成 `roce:device` 会滤掉 host 侧 `roce:host` 端点。Prefill 的 TransferEngine 会失败，Decode 也会找不到 `/etc/hixlep/ub_endpoint_npu_*.json`。单机非特权的写法在第 13 节，不要把那边的 `max_active_channels` 抄到这套 host 端点上。

## 4. 额外挂载

### 4.1 生成器注入的 hostPath

在 `examples/deployer/lib/constant.py` 的 `A5_HOST_PATH_VOLUMES` 里，保留原来的 `/etc/hixlep`（volume 名 `hixlep`），再加：

| volume 名 | 宿主机路径 | type |
|-----------|------------|------|
| `hixlep-roce` | 现场 hixlep 目录，例子是 `/home/z00931220/hixlep` | `DirectoryOrCreate` |
| `dev-infiniband` | `/dev/infiniband` | `Directory` |
| `dev-ummu` | `/dev/ummu` | `Directory` |
| `dev-uburma` | `/dev/uburma` | `Directory` |

Prefill 读的是 `/home/.../hixlep`，不是 `/etc/hixlep`。没有 `hixlep-roce`，或者没有整棵 `/home`，Prefill 读不到端点文件。

`A5_HOST_PATH_VOLUMES_HOST_NIC` 里去掉重复的 `npu-smi-bin`。引擎模板已经用 volume 名 `npu-smi` 挂了同一个路径，两个名字挂到同一 `mountPath` 会被 API 拒绝。

不要再加 `/dev/hisi_hdc`、`/dev/davinci_manager`。设备能看见，解决不了 native `RaGetIfNum count: 0`。

### 4.2 引擎模板上的 hostPath

`examples/deployer/yaml_template/infer_service_template.yaml` 里，Encode、Prefill、Decode、Union 都加上下面这些挂载。`volumeMounts` 和 `volumes` 成对写。

| volume 名 | 宿主机 | 容器 | 说明 |
|-----------|--------|------|------|
| `host-home` | `/home` | `/home` | Prefill 读 hixlep |
| `ascend-firmware` | `/usr/local/Ascend/firmware` | 同路径，只读 | |
| `host-sbin` | `/usr/local/sbin` | 同路径 | |
| `npu-slog` | `/var/log/npu` | `/usr/slog` | |
| `root-host` | `/root/host` | 同路径 | |
| `hccn-tool-bin` | `/usr/local/Ascend/driver/tools/hccn_tool` | 同路径 | |

`hccn-tool-bin` 如果宿主机上是文件，`hostPath.type` 留空；是目录再写 `Directory`。

## 5. 使用 host 网络

host 网 RoCE 要看到宿主机网卡。在 `examples/deployer/yaml_template/infer_service_template.yaml` 中，分别给 Prefill 和 Decode 的 **Pod spec** 增加 `hostNetwork: true` 和 `dnsPolicy: ClusterFirstWithHostNet`。InferServiceSet 的层级如下，只增加这两个字段，保留原有角色定义、容器、调度及挂载配置：

```yaml
spec:
  template:
    roles:
      - name: prefill
        spec:
          template:
            spec:
              hostNetwork: true
              dnsPolicy: ClusterFirstWithHostNet
      - name: decode
        spec:
          template:
            spec:
              hostNetwork: true
              dnsPolicy: ClusterFirstWithHostNet
```

普通 Deployment/StatefulSet（包括 `multi_deployment` 的 `engine_template.yaml`）则放在 `spec.template.spec`：

```yaml
spec:
  template:
    spec:
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
```

两种写法都是设置 PodSpec，不能把普通 Deployment 的层级直接套在 InferServiceSet 顶层。`dnsPolicy` 指定 host 网络 Pod 使用集群 DNS；它不会自动修复节点到 DNS 的路由。端口适配开关 `MOTOR_INSTANCE_PORTS_ENABLE` 也不会自动开启 host 网络。

host网络下同一宿主机上运行多个 P/D 实例时，参见[同机多实例 P/D 的端口配置](#14-同机多实例-pd-的端口配置)。

若现场 hostNetwork Pod 访问不了 kube-dns 的 ClusterIP（本例是 `10.96.0.10`），Decode 可能因此起不来。先核对集群 DNS 和路由；确需按本例规避时，在 `boot.sh` 的 engine 分支里，将 `/etc/resolv.conf` 中该 ClusterIP 换成本集群 CoreDNS Pod 的 IP，并放在最前面。

先查出 CoreDNS Pod IP，不要照抄例子里的地址：

```bash
kubectl get pods -n kube-system -o wide | grep coredns
```

现场 109 上是 `10.244.113.66` 和 `10.244.113.67`。改写只影响引擎进程，kubelet 的探活不 source `boot.sh`。

## 6. Prefill 使用 2Mi hugepage

只给 Prefill 加大页。Decode 只走 Mooncake 时一般不加。K8s 要求 hugepage 的 request 和 limit 相等。

`infer_service_template.yaml` 的 Prefill 容器：

```yaml
resources:
  requests:
    memory: "64Gi"
    cpu: "16"
    hugepages-2Mi: 416Gi
    huawei.com/Ascend910: 1
  limits:
    hugepages-2Mi: 416Gi
    huawei.com/Ascend910: 1
volumeMounts:
  - name: dshm
    mountPath: /dev/shm
  - name: hugepage-2mi
    mountPath: /hugepages-2Mi
volumes:
  - name: dshm
    emptyDir:
      medium: Memory
      sizeLimit: 4Gi
  - name: hugepage-2mi
    emptyDir:
      medium: HugePages-2Mi
```

`416Gi` 是这套机器的例子。改之前先看节点上的大页，以及 K8s 是否已经把 `hugepages-2Mi` 报成 allocatable。模板写了不等于调度器认。现场 OS 有 2Mi 大页，但当时 allocatable 是 0，Pod 会 Pending。

```bash
kubectl describe node <节点名> | grep -A2 hugepages
```

## 7. 去掉镜像里的 HTTP 代理

镜像 ENV 带了 `HTTP_PROXY`。`set_common_env` 写成空字符串后，变量还在，库仍会走代理。

`startup/boot.sh` 在 source `common.sh` 之后、按角色分支之前：

```bash
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
```

kubelet 直接执行 `probe/probe.sh`，不 source `boot.sh`，所以 `probe.sh` 里同样 unset 这四个变量。

`probe/probe.py` 的 httpx 客户端加上 `trust_env=False`，避免探活打到 Pod IP 或 Service 时被代理截走：

```python
client_kwargs = {"headers": headers, "timeout": timeout, "trust_env": False}
```

`env.json` 的 `motor_common_env` 里把四个代理变量设成空字符串，`no_proxy` / `NO_PROXY` 写上本机、节点管理 IP 和业务 IP。

## 8. 按物理卡号覆盖可见设备

特权容器能看见节点上全部 NPU。镜像里的 `_calc_visible_device_ids` 不读 `ASCEND_RT_VISIBLE_DEVICES` 时，Prefill 会按逻辑卡 `0,1,2,3` 把兄弟进程的卡占掉。

不要把整份 `service.py` 合进主干。在两台节点的本地盘放同一份补丁，例如 `/mnt/motor_patch/service.py`（`/mnt` 不是共享盘，109 和 104 都要有）。`boot.sh` 的 engine 分支在 source `engine.sh` 之前：

```bash
MOTOR_SERVICE_PATCH="/mnt/motor_patch/service.py"
MOTOR_SERVICE_DEST="/usr/local/python3.11.10/lib/python3.11/site-packages/motor/node_manager/core/services/native_engine/service.py"
if [ -f "$MOTOR_SERVICE_PATCH" ]; then
    cp -f "$MOTOR_SERVICE_PATCH" "$MOTOR_SERVICE_DEST"
    rm -f "$(dirname "$MOTOR_SERVICE_DEST")/__pycache__/service."*.pyc
fi
```

镜像里的 site-packages 路径随 Python 版本变，先在容器里确认 `service.py` 的实际位置。

补丁改的是 `NativeEngineService._calc_visible_device_ids`：先读 `ASCEND_RT_VISIBLE_DEVICES`，没有再读 `ASCEND_VISIBLE_DEVICES`，用这份物理卡列表替换算出来的逻辑卡号。现场 Prefill 固定 `0,2,4,6`，Decode 固定 `1,3,5,7`，和 `env.json` 里的 `ASCEND_RT_VISIBLE_DEVICES` 一致。换卡位时两处一起改。

Docker 旁路不要写 `ASCEND_VISIBLE_DEVICES=0..n-1`。特权容器看见全部卡时，逻辑编号会和另一侧 Prefill 撞车。`docker_deploy.py` 里改成：

```text
-e ASCEND_RT_VISIBLE_DEVICES=<物理卡号列表>
```

## 9. 探活超时

host 网络和 RoCE 初始化更慢。把 startup probe 的 `timeoutSeconds` 改成 `30`：

- `yaml_template/infer_service_template.yaml` 里 Encode、Prefill、Decode 的 startupProbe
- `yaml_template/controller_template.yaml`
- `yaml_template/coordinator_template.yaml`
- `yaml_template/kv_cache_store_template.yaml`（这里原来是 `3`）

kv-store 如果必须落在某一台机器上，在它的 Pod 上加 `nodeSelector`。例子是 `kubernetes.io/hostname: motor-master-109`。不要把这个主机名写进通用模板后合入主干。

## 10. 只走 Docker 时的附加项

K8s 的 `deploy.py` 不走这些文件。只用 `docker_deploy.py` 时：

- `lib/constant.py` 的 `ENTER_DOCKER_RUN_A5`：`/dev/ummu`、`/dev/uburma` 从 `--device` 改成 `-v` 绑定；需要 overlay 时再绑一份 `native_engine/service.py`。
- `ENTER_DOCKER_RUN_KVS` 去掉 `-v /driver:/driver`。
- `docker_deploy.py` 去掉 `docker run -t`，只留 `-i`。`isatty()` 为真时，CANN slog 会刷在启动终端上；K8s Pod 默认没有 TTY，不会出现这个问题。
- `lib/in_place_run.py` 不要给 `start_motor` 开 PTY。用管道，并设置 `PYTHONUNBUFFERED=1`。

## 11. 配套的 env.json 和 user_config.json

这两份不在 `examples/deployer` 里，和上面的脚本一起用。现场目录是 `model_configs/vllm/dsv4_roce_pd/`。

`env.json`：

- 公共：`MMC_A5_INPROCESS_DRAM_SIZE=100GB`，代理按第 7 节清空。
- Prefill：`ASCEND_RT_VISIBLE_DEVICES=0,2,4,6`，以及 `MF_HYBM_RDMA_USE_HCOMM=1`、`MF_HYBM_RDMA_SWAP_SPACE_SIZE=2048`、`MF_HYBM_RDMA_FORCE_UNREGISTERED=1`。
- Decode：`ASCEND_RT_VISIBLE_DEVICES=1,3,5,7`，不要加那三个 HYBM 开关。
- 不要设 `ASCEND_GLOBAL_RESOURCE_CONFIG`。单机非特权的写法在第 13 节。

`user_config.json` 按自己的集群填。这套例子是：

- 命名空间 `mindie-motor-dsv4-roce`
- Prefill、Decode 各 1 个实例，每个 Pod 4 张卡；Prefill 钉 109，Decode 钉 104
- Prefill 开 prefix cache，KV connector 用 MooncakeHybrid 加 AscendStore memcache，hixlep 指向 `/home/z00931220/hixlep`
- Decode 关 prefix cache，只留 MooncakeHybrid，hixlep 指向 `/etc/hixlep`
- kv_cache_store 用 memcache inprocess，端口 `50088`，和 `KV_CACHE_STORE_PORT`、conf 里的 meta URL 一致

`log_collect/log_config.ini` 的 `name_space` 改成这次部署的命名空间。这是采集脚本的过滤条件，不是部署逻辑。

## 12. 部署后怎么确认

只从装了集群管理 kubeconfig 的那台机器部署。不要动别人的命名空间和已经占卡的容器。

```bash
cd examples/deployer
bash delete.sh <本次命名空间>
kubectl delete ns <本次命名空间> --wait=true
python3 deploy.py --config_dir <本次 user_config 目录>
```

请求打 Coordinator 的 NodePort。镜像里有代理时，curl 加 `--noproxy '*'`。

池化是否生效，看 Prefill 日志里的 `kvpool hit tokens`，不要用 Prefix cache hit rate 判断。

## 13. 单机非特权 RoCE

同一台机器上 Prefill 和 Decode 都跑、容器不能开特权时，用这一节。下面按 `worker-112` 上已经跑通的部署来写。主机名、IP、网口、端点目录、端口、库文件名都是这台机器的例子，换环境换成自己的值。

这一节和前面的差别：

- 容器 `privileged: false`、`allowPrivilegeEscalation: false`，只加 `NET_ADMIN` 和 `IPC_LOCK`。
- 不把整棵 `/dev/infiniband` 挂进引擎。设备插件注册 `devic.es/roce`，Prefill 和 Decode 各申请 1。
- 不覆盖 `service.py`。启动命令把调度器分到的物理卡改写成逻辑卡 `0..N-1`。
- Prefill 和 Decode 都申请 `hugepages-2Mi: 16Gi`。
- `ASCEND_GLOBAL_RESOURCE_CONFIG` 只写 `{"comm_resource_config.max_active_channels":2}`。不要写成 `roce:device`。
- Prefill、Decode、Controller、Coordinator、KV Store 放在同一节点。同机 `hostNetwork`，两边端口必须错开。

### 13.1 用设备插件提供 RoCE 设备

在业务节点部署 generic-device-plugin，资源名 `roce`，Pod 里申请的名字是 `devic.es/roce`。这台机器注册 2 份，每份都映射同一组设备：`/dev/infiniband/uverbs0`～`uverbs7` 和 `/dev/infiniband/rdma_cm`。Prefill、Decode 各拿 1 份。节点上这些设备节点要真实存在，没有的不要写进路径列表。

插件放在 `kube-system`，`nodeSelector` 固定到业务节点，例子是 `kubernetes.io/hostname: worker-112`。插件本身 `privileged: false`、`allowPrivilegeEscalation: false`，挂载 `/var/lib/kubelet/device-plugins` 和 `/dev/infiniband`。镜像先导入该节点，`imagePullPolicy` 设成 `Never`。这台机器的镜像是 `squat/generic-device-plugin:dts-c5e0633438d1`。

插件参数的核心是：

```text
--device {"name":"roce","groups":[{"count":2,"paths":["/dev/infiniband/uverbs0","/dev/infiniband/uverbs1","/dev/infiniband/uverbs2","/dev/infiniband/uverbs3","/dev/infiniband/uverbs4","/dev/infiniband/uverbs5","/dev/infiniband/uverbs6","/dev/infiniband/uverbs7","/dev/infiniband/rdma_cm"]}]}
```

部署后确认该节点 allocatable 里 `devic.es/roce` 至少为 2。不够的话两个引擎 Pod 会有一个 Pending。

### 13.2 改引擎 Pod

在部署副本的 `examples/deployer/yaml_template/infer_service_template.yaml` 里改 Prefill 和 Decode。角色如果拆在别的模板文件里，就改那几份，不要改完再合入主干。

Prefill 和 Decode 的 Pod spec 均按第 5 节的资源层级设置并保留：

```yaml
hostNetwork: true
dnsPolicy: ClusterFirstWithHostNet
```

访问不了 kube-dns 的 ClusterIP 时，再按第 5 节把引擎的 `resolv.conf` 改成 CoreDNS Pod IP。这台机器的启动命令里没有这段改写。

容器安全上下文和资源：

```yaml
securityContext:
  privileged: false
  allowPrivilegeEscalation: false
  capabilities:
    add:
      - NET_ADMIN
      - IPC_LOCK
resources:
  requests:
    hugepages-2Mi: 16Gi
    devic.es/roce: 1
  limits:
    hugepages-2Mi: 16Gi
    devic.es/roce: 1
```

`16Gi` 是这台机器的例子。hugepage 的 request 和 limit 必须相等。改之前先看节点 allocatable，模板写了不等于调度器认。

额外挂载。三个 provider 文件只读，`hostPath.type` 用 `File`：

| volume 名 | 宿主机 | 容器 |
|-----------|--------|------|
| `roce-provider-0` | `/etc/libibverbs.d/hrn5.driver` | 同路径，只读 |
| `roce-provider-1` | `/usr/lib64/libhrn5-rdmav34.so` | 同路径，只读 |
| `roce-provider-2` | `/usr/lib64/libibv_extend.so.4` | 同路径，只读 |
| `dev-ummu` | `/dev/ummu` | `/dev/ummu` |
| `dev-uburma` | `/dev/uburma` | `/dev/uburma` |

库文件名随驱动变。先在宿主机上确认 provider 和这两个 so 的实际路径。

Prefill、Decode 的启动命令在进入 `boot.sh` 之前，用调度器写进 `ASCEND_VISIBLE_DEVICES` 的物理卡做两件事：生成逻辑卡，并让父进程带上第一张物理卡对应的端点。`export` 必须和 `boot.sh` 在同一个 shell 里，模板里用的是 `source`。四卡时逻辑卡是 `0,1,2,3`。端点目录换成自己的 hixlep 目录，文件里的协议字段是 `roce`。

```bash
allocated_devices="${ASCEND_VISIBLE_DEVICES//npu-/}"
if ! [[ "$allocated_devices" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
    echo "Invalid scheduler NPU allocation" >&2
    exit 2
fi
IFS=, read -ra allocated_list <<< "$allocated_devices"
export ASCEND_RT_VISIBLE_DEVICES="$(seq -s, 0 $((${#allocated_list[@]}-1)))"
export ASCEND_LOCAL_COMM_RES="$(cat /mnt/share/y00943510/hixlep/ub_endpoint_npu_${allocated_list[0]}.json)"
export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common:$LD_LIBRARY_PATH
source /mnt/configmap/boot.sh
```

`boot.sh` 里不要再安装选卡修复包，用镜像里的 Motor。不要按第 8 节把 `service.py` 盖进 site-packages。

Controller、Coordinator、KV Store 的 Pod 加上同一台机器的 `nodeSelector`。例子是 `kubernetes.io/hostname: worker-112`。Prefill 和 Decode 的节点在 `user_config.json` 的 `prefill_node_selector`、`decode_node_selector` 里写，这套例子两个都是 `worker-112`。

### 13.3 错开同机端口

`user_config.json` 里 Prefill、Decode 各 1 个实例、1 个 Pod、4 张卡，`data_parallel_size` 为 4，`tensor_parallel_size` 为 1。`hardware_type` 用 `Ascend950`。镜像按现场的 Ascend 950PR&950DT系列产品镜像填。

同机 `hostNetwork` 时，下面这些端口两边不要共用：

| 项 | Prefill | Decode |
|----|---------|--------|
| `data_parallel_rpc_port` | `19000` | `19001` |
| `node_manager_port` | `11026` | `12026` |
| `base_port` | `11000` | `12000` |
| `ASCEND_BASE_PORT` | `22000` | `24000` |
| `HIXL_LISTEN_PORT` | `26666` | `26766` |
| Mooncake `kv_port` | `20001` | `20011` |

`ASCEND_BASE_PORT` 和 `HIXL_LISTEN_PORT` 写在 `env.json` 对应角色里，其余写在 `user_config.json`。`motor_nodemanger_config` 是现有配置键名的实际拼写。

这套例子里 P/D 都没有 `speculative-config`，并且 `no-enable-prefix-caching` 为 true。KV connector 用 MooncakeHybrid 加 AscendStore，`ascend_local_comm_res_path` 指向端点目录，例子是 `/mnt/share/y00943510/hixlep`。

`kv_cache_store_config` 用 mooncake standalone：`global_segment_size` 例子是 `10GB`，`local_buffer_size` 是 `1GB`，`default_kv_lease_ttl` 显式写成 `60000` 毫秒。部署器默认租约是 `11000` 毫秒，不改的话和这套不一致。

`env.json` 里 Prefill 和 Decode 都写上：

- `HCCL_IF_IP` 用这台机器的业务地址，例子是 `141.61.45.112`
- `GLOO_SOCKET_IFNAME`、`HCCL_SOCKET_IFNAME`、`TP_SOCKET_IFNAME` 用同一块网口，例子是 `enp34s0f1`
- `HCCL_BUFFSIZE=512`，`HCCL_ALGO=level0:fullmesh`
- `HCCL_EXEC_TIMEOUT=204`，`HCCL_CONNECT_TIMEOUT=120`
- `ASCEND_THREAD_POOL_SIZE=1`
- `ASCEND_GLOBAL_LOG_LEVEL=3`
- `MC_STORE_USE_HUGEPAGE=1`
- `ASCEND_GLOBAL_RESOURCE_CONFIG={"comm_resource_config.max_active_channels":2}`

不要把 `ASCEND_GLOBAL_RESOURCE_CONFIG` 设成 `roce:device`。

### 13.4 打开本机 RoCE 转发

Prefill 和 Decode 在同一台机器上时，RoCE 流量要从本机地址出去再回到本机。这台机器把 `net.ipv4.ip_forward` 设成 `1`，并为每块 RoCE 网口准备了专用路由表：经网关指向本机地址的 `/32` 路由，加上优先级 `0` 的策略规则：源地址等于目的地址，且报文从本地回环接口进入。系统 `lookup local` 的优先级从 `0` 调到 `1`，避免抢在这些规则前面。同 bond 的对端写成永久邻居项，MAC 用现场网关的值。对应接口打开 `accept_local=1`。

网口名、地址和路由表号都是现场值。做成开机以及网口 up 时自动执行，避免重启后转发丢了。

### 13.5 不要和跨节点做法混用

不要同时做第 3 节的一律特权、第 4 节的整棵 `/dev/infiniband`、第 6 节只给 Prefill 的 `416Gi` 大页、第 8 节的 `service.py` 物理卡补丁。那些是跨节点、容器能看见全部卡时的做法。这一节由调度器分卡，逻辑卡从 0 编号。

## 14. 同机多实例 P/D 的端口配置

同一节点上运行多个使用 `hostNetwork` 的 Prefill/Decode Pod 时，各实例共享宿主机端口空间。
仅区分 Prefill 和 Decode 的基础端口，不能区分同一角色的 P0、P1。这里介绍按实例编号生成端口的可选部署能力。

### 14.1 适用范围

- Kubernetes InferServiceSet 部署，使用 MindCluster 注入的 `ROLE` 和 `INSTANCE_INDEX`。
- 每个实例一个 Pod，P/D 的基础端口均显式配置。
- 用户自行确认节点、实例放置、NPU、RoCE 设备、大页及网络条件满足部署要求。
- 此功能只生成实例配置，继续使用 Motor 原生部署器及 NodeManager。

默认不启用。多 Pod 组成一个实例、Docker、Slurm、encode 和 union 混部不属于本次端口适配的支持范围；这些角色不会进入 P/D 转换分支，仍走原流程。混部不能直接套用本例，应单独规划端口并验证。
同一节点部署多个 InferServiceSet 时，还需要给不同服务规划不同的基础端口；实例编号只在各自服务内有意义。

### 14.2 配置方法

在 `env.json` 的 `motor_common_env` 中增加：

```json
{
  "MOTOR_INSTANCE_PORTS_ENABLE": "1",
  "MOTOR_INSTANCE_PORT_STRIDE": "100",
  "MOTOR_INSTANCE_KV_PORT_STRIDE": "100",
  "MOTOR_INSTANCE_ASCEND_PORT_STRIDE": "4000",
  "MOTOR_INSTANCE_STORE_PORT_STRIDE": "200"
}
```

四个 stride 分别作用于管理/API/DP RPC 端口、Connector KV 端口、Ascend 基础端口和 Store HIXL 端口。
偏移规则为：`实例实际端口 = 当前角色基础端口 + INSTANCE_INDEX × stride`。
默认 stride 与上例相同；端口或偏移非法时拒绝启动，不截断、不回绕。

部署器自动把 `startup/instance_config.py` 分发到 ConfigMap。启用后使用这个仓内工具，无需共享挂载个人目录中的脚本。
`MOTOR_INSTANCE_CONFIG_ADAPTER` 可显式指定兼容的部署适配脚本，属于运维配置，应只指向可信文件。

在 `user_config.json` 的 P/D `motor_nodemanger_config` 中均设置：

```json
{
  "api_config": {"node_manager_port": 11026},
  "endpoint_config": {"base_port": 11000},
  "port_allocator_config": {"enable": false}
}
```

上例是 Prefill，Decode 需要自己的基础端口，例如 `12026` / `12000`。
同时显式设置 P/D `engine_config.data_parallel_rpc_port` 和所用 Connector 的 `kv_port`。
`motor_nodemanger_config` 是现有配置键的实际拼写。显式实例端口方案要求关闭端口分配器；未关闭时拒绝转换。

P/D 各自的环境中还需要设置基础 `ASCEND_BASE_PORT` 和 `HIXL_LISTEN_PORT`。
端口规划不仅要保证表中的起始值不同，还应预留引擎、底层通信实际使用的连续端口范围。

### 14.3 单机 2P1D 示例

在第 13 节非特权 RoCE 条件上，千问 Qwen3-8B 使用两个 P、一个 D，各实例一个 Pod、一个 NPU，TP=DP=PP=1。第 13 节的两份 RoCE 配额对应原 1P1D，本例三个实例需要至少三份。host 网络按第 5 节配置，开启后再设置下面的实例端口：

| 参数 | P0 | P1 | D0 |
| --- | ---: | ---: | ---: |
| NodeManager 管理端口 | 11026 | 11126 | 12026 |
| 引擎 API 起始端口 | 11000 | 11100 | 12000 |
| DP RPC | 19000 | 19100 | 19001 |
| `ASCEND_BASE_PORT` | 22000 | 26000 | 24000 |
| `HIXL_LISTEN_PORT` | 26666 | 26866 | 26766 |
| Connector `kv_port` | 30001 | 30101 | 30002 |

配置只填写 P0/D0 的基础值。P1 的有效值由实例编号计算，不需要手工复制一份 Prefill 配置。

### 14.4 配置同步、探针与退出

1. `common.sh` 从 ConfigMap 原始 JSON 生成当前实例的有效配置。
2. 配置写入 `$CONFIG_PATH/user_config.json`，并写入同目录的 `instance-ports.json`。
3. 后台同步每次重新从原始 JSON 转换，保留新字段且不累计端口偏移。
4. `engine.sh` 在角色环境初始化后设置实例通信端口，避免被角色基础值覆盖。
5. 探针从 `CONFIG_PATH` 读取实例有效配置。
6. PreStop 校验实例标记的角色、编号、路径、管理端口，随后只访问当前实例的 NodeManager。

首次转换失败会阻止启动。后台转换失败保留上一份有效配置并停止该同步循环，错误写入日志，需修正配置并重新启动相应 Pod。
PreStop 遇到损坏或不匹配的实例标记时拒绝访问共享配置中的其他端口。

没有启用实例转换且没有实例标记时，保持原有配置同步和 PreStop 配置搜索顺序。
`instance-ports.json` 是实例运行状态，不应作为 ConfigMap 输入，也不应跨实例复制。

### 14.5 RoCE 与独立 Store 的配套条件

端口修复不替代设备和网络配置。若 P0、P1、D0 各申请 `devic.es/roce: 1`，目标节点至少需要三份空闲可分配配额。
设备插件公布的配额是共享设备资源计数，不代表新增物理网卡，也不代表带宽隔离。

非特权 RoCE 还需按实际驱动/provider 配置设备访问、匹配的动态库、网络能力和内存锁定能力。
使用独立 Store 大页时，Pod 需有匹配的 hugepage requests/limits；仅设置 `MC_STORE_USE_HUGEPAGE=1` 不会申请 Kubernetes 大页额度。
同机 RoCE 回环是否可用应独立测试，宿主机策略路由和网关属于现场网络配置。

某一 A5/B100 镜像的 Qwen3-8B 验证中，大批 dense KV 传输出现失败；保留 RoCE 和 standalone Store，
在 P/D `ASCEND_GLOBAL_RESOURCE_CONFIG` 中合并下面参数后，约 900MiB 的独立写入、清零、读回和完整 SHA256 校验通过：

```json
{
  "comm_resource_config.max_active_channels": 2,
  "transfer_config.max_transfer_count_per_batch": 16
}
```

这是已测组合的通信库配置规避，不是 Motor 修复底层通信库，也不是全局推荐默认值。
应保留原有 JSON 中其他必要参数，并在所用 CANN/HIXL 版本上确认支持情况、功能和性能。

### 14.6 验收

先执行原生部署器 dry-run，核对生成 YAML 的资源、挂载及节点选择，再按正常部署流程提交。
启动后确认三个实例实际配置中的端口符合规划，Coordinator `/readiness` 返回 `ready=true`。

功能验收应同时检查：

- 两个 P 都实际处理请求，D 返回正确内容；分别覆盖普通请求、流式请求和两个 P 同时工作的请求。
- 重复长输入的外部 Store 写入及回读计数有增量，并对应到本轮请求日志。
- 传输失败、超时和内部重试日志没有被 HTTP 200 掩盖。
- Pod 没有意外重启，重新部署后结果可重复。

为了区分本地 prefix cache 与外部 Store，可在功能验证配置中使用轮询调度并暂时关闭引擎本地 prefix caching。
这些设置用于检查两个 P 和真实外部回读，不是生产调度或性能配置的统一要求。
