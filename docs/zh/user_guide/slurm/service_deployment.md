# Slurm 服务部署

本章介绍如何在已完成[环境准备](./environment_preparation.md)的 Slurm 集群上部署 MindIE Motor。部署脚本位于 `examples/slurm_deployer`；进入该目录后执行部署命令。

## 部署流程

```text
准备 json 配置 → start 在容器内生成 ConfigMap → 提交作业 → 查看日志和状态 → stop/clean
```

## 本章导航

- [1. 部署目录与运行机制](#1-部署目录与运行机制)
- [2. 配置部署参数](#2-配置部署参数)
- [3. 执行部署](#3-执行部署)
- [4. 作业角色与资源](#4-作业角色与资源)
- [5. 常用操作与排查](#5-常用操作与排查)

## 1. 部署目录与运行机制

### 1.1 目录结构

```text
examples/slurm_deployer/
├── deploy.sh
├── conf/                   # 用户配置目录
│   ├── user_config.json   # 部署前自行拷贝/准备，仓库不内置
│   └── env.json           # 部署前自行拷贝/准备，仓库不内置
└── script/
    ├── prepare.sh          # start 时由容器内部调用
    ├── srun_motor.sh
    ├── run_motor.sh
    └── lib/             # deploy.sh 使用的内部 Shell 模块
        ├── config.sh
        ├── validation.sh
        ├── slurm.sh
        ├── jobs.sh
        ├── lifecycle.sh
        └── start.sh
```

每次部署前，将现场准备好的 `user_config.json`、`env.json` 拷贝到 `conf/` 下（可参考 `examples/features/config_sample.json` 或 `examples/infer_engines/` 下对应模型典配）。如需覆盖镜像内的脚本或后端配置，也将文件直接放入 `conf/`，启动时会一并复制到容器内的 `/configmap`。`script/prepare.sh` 是部署器自带的准备脚本，不需要复制到 `conf/`。

Slurm 脚本会显式使用 Apptainer 的 `--no-mount tmp`，避免宿主机 `/tmp` 覆盖镜像内的 `/tmp/motor/examples` 和补丁目录。容器临时文件使用镜像内的 `/tmp` 及 `--writable-tmpfs`，宿主机仅挂载 `conf/`、`script/prepare.sh`、模型目录和日志目录；`/configmap` 只存在于容器临时文件系统中。

### 1.2 运行机制

```text
deploy.sh start
  → sbatch script/srun_motor.sh <role>
    → srun script/run_motor.sh
      → apptainer instance start
      → 在容器内执行 /slurm_prepare.sh
      → 生成容器内 /configmap
      → source /configmap/boot.sh
```

### 1.3 部署前检查

#### 确认各节点上的路径一致

Slurm 会在计算节点上执行 `script/run_motor.sh`，因此所有计算节点都必须使用相同的部署目录结构，并能访问相同路径下的以下文件和目录：

- Apptainer `.sif` 镜像（`image_name`）。
- 模型权重目录（`weight_mount_path`）。
- `examples/slurm_deployer` 目录及其脚本、`conf/` 用户配置目录。

如果这些路径只在提交作业的节点上存在，作业可能会提交成功，但会在计算节点上因找不到脚本、镜像或权重而启动失败。

#### 确认节点地址可以解析为 Slurm 节点名

`deploy.sh` 会对 `COORDINATOR_SERVICE`、`CONTROLLER_SERVICE` 等地址执行 `getent hosts`，再将解析出的节点名传给 `sbatch -w`，把管理面作业绑定到指定节点。因此，每个地址都必须能解析出对应的节点名，并且该节点名必须与 `slurm.conf` 中的 `NodeName` 完全一致。

节点地址可以使用主机名、IPv4 或 IPv6。建议优先使用已配置 DNS 或 `/etc/hosts` 的主机名；使用 IP 地址时，也要确保反向解析能够返回 Slurm 配置中的节点名。

如果无法配置反向解析，请参见下文“`deploy.sh`”小节，使用 `*_NODE` 变量直接指定 Slurm 节点名。

## 2. 配置部署参数

### 2.1 deploy.sh

| 变量 | 说明 |
|------|------|
| `COORDINATOR_SERVICE` | Coordinator 节点地址（主机名、IPv4 或 IPv6） |
| `COORDINATOR_INFER_SERVICE` | 可选，Coordinator 推理服务地址；未设置时默认使用 `COORDINATOR_SERVICE` |
| `COORDINATOR_OBS_SERVICE` | 可选，Coordinator 观测服务地址；未设置时默认使用 `COORDINATOR_SERVICE` |
| `CONTROLLER_SERVICE` | Controller 节点地址（主机名、IPv4 或 IPv6） |
| `KVS_MASTER_SERVICE` | 可选，kv_store 节点地址（主机名、IPv4 或 IPv6）；启用 KV Store 或 KV Conductor 时必须设置 |
| `KV_CONDUCTOR_SERVICE` | 可选，KV Conductor 独立地址；启用 KV Conductor 时必须设置 |
| `KV_CONDUCTOR_NODE` | 可选，KV Conductor 对应的 Slurm `NodeName`；服务地址无法解析时必须设置 |
| `MF_STORE_SERVICE` | mf_store 节点地址（主机名、IPv4 或 IPv6；`engine_type=sglang` 时使用） |
| `PARTITION` | `start` 提交作业使用的 Slurm 分区名，须与 `slurm.conf` 中 `PartitionName` 一致 |

Coordinator 与 Controller 使用不同节点。

#### 服务地址与 Slurm 节点名

`*_SERVICE` 和 `*_NODE` 用途不同：

- `*_SERVICE` 是容器内访问服务使用的地址，可以是主机名、IPv4 或 IPv6 地址。
- `*_NODE` 是 Slurm 调度使用的节点名，只用于 `sbatch -w` 绑定节点。

通常脚本会通过 DNS 或 `/etc/hosts` 将 `*_SERVICE` 解析为 Slurm 节点名。如果服务地址无法反向解析，可以直接指定：

```bash
COORDINATOR_NODE=coordinator-node
CONTROLLER_NODE=controller-node
KVS_MASTER_NODE=kvs-node
MF_STORE_NODE=mf-store-node       # 仅 engine_type=sglang 时需要
KV_CONDUCTOR_NODE=kv-conductor-node # KV_CONDUCTOR_SERVICE 独立时可设置
```

这里的 Slurm 节点名是 `slurm.conf` 中 `NodeName` 配置项的值，例如：

```text
NodeName=compute-node-1 CPUs=... Gres=npu:8 State=UNKNOWN
```

上例中的 `compute-node-1` 就是 Slurm 节点名，也可以通过 `sinfo -N` 查看。两者可以相同，也可以不同，例如 `COORDINATOR_SERVICE` 使用 IPv6 地址，而 `COORDINATOR_NODE` 使用 `slurm.conf` 中的主机名。

`KV_CONDUCTOR_SERVICE` 必须独立于 `KVS_MASTER_SERVICE` 配置。启用 `kv_conductor` 时，脚本会优先使用 `KV_CONDUCTOR_NODE` 绑定作业；未设置时通过 `KV_CONDUCTOR_SERVICE` 解析对应的 Slurm `NodeName`。脚本不会将 `KVS_MASTER_SERVICE` 作为 Conductor 地址的后备值。

### 2.2 conf/user_config.json

Slurm 使用统一的多角色启动流程，不读取 K8s 的 `deploy_mode` 字段。按现场填写 `image_name`、`weight_mount_path` 以及规模：

| 字段 | 说明 |
|------|------|
| `e_instances_num` / `single_e_instance_pod_num` / `e_pod_npu_num` | Encode |
| `p_instances_num` / `single_p_instance_pod_num` / `p_pod_npu_num` | Prefill |
| `d_instances_num` / `single_d_instance_pod_num` / `d_pod_npu_num` | Decode |
| `hybrid_instances_num` / `single_hybrid_instance_pod_num` / `hybrid_pod_npu_num` | union |

机数与卡数均大于 0 时提交对应引擎作业。

附加组件：

- **kv_store**：prefill 或 union 的 `kv_connector` 为 `MultiConnector`，或配置了非空 `kv_cache_store_config`
- **kv_conductor**：`kv_conductor_config.http_server_port` 非 0
- **mf_store**：`engine_type` 为 `sglang`

将准备好的 `user_config.json` / `env.json` 拷贝到 `conf/` 下（本目录不附带样例文件）。

`kv_cache_store_config` 中的 `port`、`backend`、淘汰比例、`default_kv_lease_ttl`、`config_store_port` 和 `metrics_port` 会由 `deploy.sh` 读取并传入对应容器；缺省值与 K8s deployer 一致：`port=50088`、`backend=memcache`、`default_kv_lease_ttl=11000`，Memcache 的 `config_store_port=50089`、`metrics_port=50090`。使用 Mooncake 时，`eviction_high_watermark_ratio` 和 `eviction_ratio` 必须显式配置。启用 `kv_conductor` 时，其端口读取 `kv_conductor_config.http_server_port`，`KV_CONDUCTOR_SERVICE` 必须单独配置。

#### Memcache 后端配置

Memcache 的配置分为两层，不能只在 `user_config.json` 中设置 `backend`：

1. `user_config.json` 中的 `kv_cache_store_config.backend` 选择池化后端，必须设置为 `memcache`。
2. `user_config.json` 中各引擎 `AscendStoreConnector` 对象内部的 `kv_connector_extra_config.backend` 也必须设置为 `memcache`，并与全局配置保持一致。
3. `mmc-local-*.conf` 配置 Memcache LocalService 的实际运行参数，包括 DRAM 池大小、通信协议和 SSD/UBSIO 参数。

`mmc-local-*.conf` 默认来自 Motor 镜像。需要自定义时，将对应文件直接放入宿主机 `conf/`，启动时会覆盖镜像默认配置：

```text
conf/kv_store_backends.memcache.mmc-local-inprocess.conf
conf/kv_store_backends.memcache.mmc-local-standalone.conf
```

启动时，每个 Apptainer 实例都会在容器内执行准备流程：

```bash
bash deploy.sh start [配置目录]
# 未指定时使用 conf/
bash deploy.sh start
# 使用自定义目录
bash deploy.sh start /path/to/conf
```

准备流程会先从镜像复制默认脚本和 Memcache 配置，再将宿主机 `conf/` 下的用户文件复制到容器内 `/configmap`，最后执行 `set_env_docker.py`。因此用户不需要手动创建或维护宿主机 `conf/configmap`。

配置文件的选择规则如下：

- `local_service_mode = "inprocess"`：编辑 `mmc-local-inprocess.conf`，并配置每个引擎进程的 `ock.mmc.local_service.dram.size`。
- `local_service_mode = "standalone"`：编辑 `mmc-local-standalone.conf`。此模式由独立 LocalService 管理 DRAM 池，引擎侧通常保持 `dram.size = 0GB`。
- 未设置 `local_service_mode`：由硬件类型决定默认模式。建议显式设置该字段；如果不设置，应同时检查两个配置文件。

其中，`backend` 只负责选择 Memcache 后端，不能替代 `mmc-local-*.conf` 中的池参数配置。启动时，脚本还会根据实际计算节点动态替换配置中的 KV Store 地址和 `backend_id`，不要手动写死节点 IP。用户放入 `conf/` 的同名文件会覆盖镜像默认文件。

推理入口：`http://<COORDINATOR_SERVICE>:<coordinator_api_infer_port>`。当 `COORDINATOR_SERVICE` 为 IPv6 地址时，URL 中必须加方括号，例如 `http://[2001:db8::10]:11025`。

容器与宿主机共用网络命名空间。Coordinator、Controller 与引擎作业可能调度到同一节点，必须在 `user_config.json` 中显式填写互不重叠的端口，不要留空走默认口。默认口会冲突：Coordinator 为 `1025` / `1026` / `1027`，Prefill NodeManager 为 `1026`。

端口错开示例：

| 组件 | 字段 | 样例 |
|------|------|------|
| Coordinator 推理 | `motor_coordinator_config.api_config.coordinator_api_infer_port` | `11025` |
| Coordinator 管理 | `motor_coordinator_config.api_config.coordinator_api_mgmt_port` | `11026` |
| Coordinator 观测 | `motor_coordinator_config.api_config.coordinator_obs_port` | `11027` |
| Controller 管理 | `motor_controller_config.api_config.controller_api_port` | `12026` |
| Controller 观测 | `motor_controller_config.api_config.observability_api_port` | `12027` |
| Prefill NodeManager | `motor_engine_prefill_config.motor_nodemanger_config.api_config.node_manager_port` | `13026` |
| Decode NodeManager | `motor_engine_decode_config.motor_nodemanger_config.api_config.node_manager_port` | `14026` |

union 将 NodeManager 端口写在 `motor_engine_union_config.motor_nodemanger_config.api_config.node_manager_port`。encode 同理。同一节点上引擎业务口（默认从 `10000` 起）以及 `kv_port` 也须与上表不重叠。

```json
"motor_controller_config": {
  "api_config": {
    "controller_api_port": 12026,
    "observability_api_port": 12027
  }
},
"motor_coordinator_config": {
  "api_config": {
    "coordinator_api_infer_port": 11025,
    "coordinator_api_mgmt_port": 11026,
    "coordinator_obs_port": 11027
  }
}
```

### 2.3 conf/env.json

填写各角色环境变量。`POD_IP`、`HOST_IP` 和 `SGLANG_HOST_IP` 不需要写入 `env.json`，由 `script/run_motor.sh` 在每个计算节点上自动设置。

#### 本机通信 IP 的选择顺序

Slurm 会先在计算节点的宿主机上启动 `run_motor.sh`。该脚本在进入 Apptainer 容器前，按以下顺序选择当前节点的通信 IP：

1. 如果宿主机环境中已经设置了 `POD_IP`，优先使用该值。
2. 否则选择第一个处于启用状态的全局 IPv4 地址。
3. 如果没有可用 IPv4，则选择第一个处于启用状态的全局 IPv6 地址。
4. 如果 IPv4 和 IPv6 都没有可用的全局地址，脚本退出并报错。

选出的地址随后通过 `apptainer exec --env POD_IP=...` 传入容器，并同时用于 `HOST_IP`、`SGLANG_HOST_IP` 和 `HCCL_IF_IP`。脚本还会根据该地址找到对应网卡，设置 `GLOO_SOCKET_IFNAME`、`HCCL_SOCKET_IFNAME` 和 `TP_SOCKET_IFNAME`。进入容器后，脚本先执行 `/slurm_prepare.sh` 生成临时 `/configmap`，再由 `apptainer exec` 执行 `source /configmap/boot.sh` 启动容器内的 Motor 服务。

正常情况下不需要用户预先配置 `POD_IP`。由于每个计算节点都会独立执行 `run_motor.sh`，各节点会得到自己的地址；不要在 `deploy.sh` 中统一写死 `POD_IP`，否则多个节点可能使用同一个地址。只有在节点存在多块业务网卡、自动选择结果不正确时，才建议手动指定 `POD_IP`。

## 3. 执行部署

### 3.1 准备配置并启动

```bash
cd examples/slurm_deployer
bash deploy.sh start
```

不带参数执行 `bash deploy.sh` 可从菜单选择 `start` / `stop` / `clean`。修改 `conf/` 下的配置后，下一次执行 `start` 会自动在每个容器内重新生成运行配置。也可以将其他目录作为第二个参数传给 `start`，该目录必须在所有计算节点上可见且包含 `user_config.json` 和 `env.json`。

`start` 提交 Slurm 作业并在每个实例内生成临时 `/configmap`，日志目录为 `logs/<YYYYMMDD_HHMMSS>/<role>/`。

```bash
squeue -p <partition-name>
```

权重加载完成后即可请求推理接口。

## 4. 作业角色与资源

每次 `start` 提交 coordinator、controller。其余按配置提交：

| 角色 | 资源 |
|------|------|
| kv_store | 1 节点，绑 `KVS_MASTER_SERVICE` |
| kv_conductor | 1 节点，绑 `KV_CONDUCTOR_SERVICE` 对应的 Slurm 节点 |
| mf_store | 1 节点，绑 `MF_STORE_SERVICE` |
| union / encode / prefill / decode | `-N <机数> --gres=npu:<卡数>`，循环实例数 |

引擎作业的 `--cpus-per-task` 在 `deploy.sh` 中配置。管理面作业使用 `-w` 绑定节点；引擎作业由分区按 NPU 调度。

## 5. 常用操作与排查

### 5.1 查看作业和日志

使用 `squeue -p <partition-name>` 查看作业是否已进入运行状态，其中 `<partition-name>` 替换为 `deploy.sh` 中 `PARTITION` 的值。每次 `start` 生成一个时间戳目录，日志位于 `logs/<YYYYMMDD_HHMMSS>/<role>/`。作业长时间处于 `PD` 状态时，使用以下命令查看原因：

```bash
scontrol show job <job-id>
sinfo -N -o "%N %G %T %P"
```

重点检查：分区是否与 `PARTITION` 配置一致、目标节点是否处于 `idle`、节点上的 NPU 数量是否满足 `--gres=npu:<卡数>`，以及 `.sif` 和权重路径是否存在。

### 5.2 停止和清理

```bash
bash deploy.sh stop     # 取消本次部署提交的作业
bash deploy.sh clean    # 作业停止后清理 logs 和临时文件
```

`stop` 会读取本次 `start` 记录的 Job ID，只取消本次部署提交的作业，不会取消同一分区中的其他作业。
`clean` 要求先成功执行 `stop`；如果 Job ID 记录仍存在，`clean` 会拒绝执行，避免清理停止作业所需的记录。
