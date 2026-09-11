# Slurm 服务部署

本文介绍如何使用 `examples/deployer/slurm_deploy.py`，通过 Slurm 和 Apptainer 部署
MindIE Motor。部署前请先完成[环境准备](./environment_preparation.md)。

## 部署入口

在 `examples/deployer` 目录执行：

```bash
python3 slurm_deploy.py start \
  --config_dir /path/to/model/config \
  --partition <partition-name> \
  --coordinator-service <coordinator-address> \
  --controller-service <controller-address>
```

`--config_dir` 指向包含 `user_config.json` 和 `env.json` 的目录。也可以分别指定两个文件：

```bash
python3 slurm_deploy.py start \
  --config /path/to/user_config.json \
  --env /path/to/env.json \
  --partition <partition-name> \
  --coordinator-service <coordinator-address> \
  --controller-service <controller-address>
```

## 参数配置

命令行参数优先于环境变量，环境变量优先于 `slurm_deploy.py` 中的默认值。

| 命令行参数 | 环境变量 | 默认值 | 说明 |
|------------|----------|--------|------|
| `--partition` | `PARTITION` | `<partition>` | Slurm 分区 |
| `--device` | `DEVICE` | `npu` | Engine 作业使用的 GRES 设备名 |
| `--coordinator-cpus` | `COORDINATOR_CPUS` | `64` | Coordinator task 申请的 CPU 数 |
| `--controller-cpus` | `CONTROLLER_CPUS` | `8` | Controller task 申请的 CPU 数 |
| `--kv-store-cpus` | `KV_STORE_CPUS` | `8` | KV Store task 申请的 CPU 数 |
| `--kv-conductor-cpus` | `KV_CONDUCTOR_CPUS` | `8` | KV Conductor task 申请的 CPU 数 |
| `--mf-store-cpus` | `MF_STORE_CPUS` | `8` | MF Store task 申请的 CPU 数 |
| `--encode-cpus` | `ENCODE_CPUS` | `16` | 每个 Encode task 申请的 CPU 数 |
| `--prefill-cpus` | `PREFILL_CPUS` | `16` | 每个 Prefill task 申请的 CPU 数 |
| `--decode-cpus` | `DECODE_CPUS` | `16` | 每个 Decode task 申请的 CPU 数 |
| `--union-cpus` | `UNION_CPUS` | `16` | 每个 Union task 申请的 CPU 数 |
| `--distribution-path` | `SLURM_DISTRIBUTION_PATH` | `/tmp` | 计算节点上的分发根目录 |
| `--log-path` | `SLURM_LOG_PATH` | `./slurm_workspace` | 计算节点上的日志根目录 |
| `--coordinator-service` | `COORDINATOR_SERVICE` | `<coordinator-ip>` | Coordinator 地址 |
| `--controller-service` | `CONTROLLER_SERVICE` | `<controller-ip>` | Controller 地址 |
| `--kvs-master-service` | `KVS_MASTER_SERVICE` | `<kvs-master-ip>` | KV Store 地址 |
| `--kv-conductor-service` | `KV_CONDUCTOR_SERVICE` | `<kv-conductor-ip>` | KV Conductor 地址 |
| `--mf-store-service` | `MF_STORE_SERVICE` | `<mf-store-ip>` | MF Store 地址 |
| `--ascend-mf-store-port` | `ASCEND_MF_STORE_PORT` | `50089` | MF Store 监听端口 |

例如，可以使用环境变量配置站点信息：

```bash
export PARTITION=<partition-name>
export DEVICE=npu
export COORDINATOR_CPUS=64
export CONTROLLER_CPUS=8
export KV_STORE_CPUS=8
export KV_CONDUCTOR_CPUS=8
export MF_STORE_CPUS=8
export ENCODE_CPUS=16
export PREFILL_CPUS=16
export DECODE_CPUS=16
export UNION_CPUS=16
export SLURM_DISTRIBUTION_PATH=/tmp
export SLURM_LOG_PATH=./slurm_workspace
export COORDINATOR_SERVICE=<coordinator-address>
export CONTROLLER_SERVICE=<controller-address>

python3 slurm_deploy.py start --config_dir /path/to/model/config
```

相对形式的分发路径和日志路径都会根据执行命令时的当前目录转换为绝对路径。计算节点必须能够
创建这两个路径。ConfigMap 在计算节点和容器内使用同一个绝对路径。日志路径独立配置，修改
`SLURM_DISTRIBUTION_PATH` 不会改变日志的落盘位置。

Slurm 作业固定以计算节点本地的 `/tmp` 作为初始工作目录，再由 `slurm_job.sh` 在每个节点创建
分发目录和日志目录。因此，作业启动不依赖提交节点当前目录在计算节点上存在。

## 节点配置

Coordinator、Controller、KV Store、KV Conductor 和 MF Store 需要运行在与服务地址对应的
固定节点上。部署脚本先使用 `getent hosts` 查找节点名，再读取 `scontrol show node -o`。

无法自动找到节点时，可以设置以下环境变量：

- `COORDINATOR_NODE`
- `CONTROLLER_NODE`
- `KVS_MASTER_NODE`
- `KV_CONDUCTOR_NODE`
- `MF_STORE_NODE`

`COORDINATOR_INFER_SERVICE` 和 `COORDINATOR_OBS_SERVICE` 自动使用
`COORDINATOR_SERVICE`，不需要单独配置。

## 启动流程

执行 `start` 后，部署脚本按以下顺序工作：

1. 读取并校验 `user_config.json` 和 `env.json`。
2. 判断 Engine 类型以及 KV Store、KV Conductor、MF Store 是否启用。
3. 在提交节点准备一次 ConfigMap，并生成统一的 `service_id`。
4. 将 ConfigMap 打包后写入生成的 `slurm_job.sh`。
5. 分别提交 Coordinator、Controller、可选存储服务和 Engine 作业。
6. 每个作业在分配到的节点上创建 deployment 目录。
7. `sbcast` 将生成的脚本发送到作业使用的所有节点。
8. 每个节点解压 ConfigMap，并使用 Apptainer 启动对应角色。

容器不会再次准备 ConfigMap，因此同一次启动的所有角色使用相同的 `service_id`。

## 提交节点文件

执行 `start` 时，会在当前目录创建：

```text
./slurm_workspace/
├── configmap/
├── slurm_deployment.json
└── slurm_job.sh
```

- `configmap/`：本次启动准备好的配置和脚本。
- `slurm_deployment.json`：记录所有服务的 Job ID 和本次部署参数，用于停止服务和后续扩缩容。
- `slurm_job.sh`：包含完整 ConfigMap 数据的作业脚本。

再次执行普通 `start` 时会重新生成这些内容。执行 `stop` 不会删除或改写它们。

ConfigMap 数据写入作业脚本后，脚本大小不能超过 Slurm 的 `MaxScriptSize`。部署脚本按默认
4 MiB 上限提前检查。

## 计算节点文件

`SLURM_DEPLOYMENT_ID` 由 `user_config.json` 中的 `job_id`、启动时间和随机后缀组成。
每个计算节点上的文件结构如下：

```text
<distribution-path>/<deployment-id>/
├── mindie_motor_<slurm-job-id>.sh
└── mindie_motor_<slurm-job-id>_<task-id>/
    └── configmap/

<log-path>/<deployment-id>/
└── <role>_<slurm-job-id>_task<task-id>_<node-name>.log
```

Job ID 和 Task ID 用于隔离不同作业和不同节点上的运行目录。ConfigMap 以只读方式挂载到
Apptainer 容器中的相同绝对路径。标准输出和标准错误写入独立日志目录下的 `.log` 文件。

任务退出时会删除本 task 的 ConfigMap 和分发脚本，日志文件保留。日志目录位于每个计算节点，
查看日志时需要登录对应节点。如需长期保留日志，应将日志路径放在可靠的本地存储上。

## Engine 作业

Slurm 不读取 `deploy_mode`，而是根据以下字段提交 Engine 作业：

| 角色 | 实例数 | 每实例节点数 | 每节点 NPU 数 |
|------|--------|--------------|----------------|
| Encode | `e_instances_num` | `single_e_instance_pod_num` | `e_pod_npu_num` |
| Prefill | `p_instances_num` | `single_p_instance_pod_num` | `p_pod_npu_num` |
| Decode | `d_instances_num` | `single_d_instance_pod_num` | `d_pod_npu_num` |
| Union | `hybrid_instances_num` | `single_hybrid_instance_pod_num` | `hybrid_pod_npu_num` |

每实例节点数或每节点 NPU 数小于等于 0 时，不提交该角色，并在终端输出跳过原因。

## Engine 扩缩容

修改原 `user_config.json` 中的 Engine 实例数后，在启动部署时使用的同一目录下执行：

```bash
python3 slurm_deploy.py start \
  --config_dir /path/to/model/config \
  --update_instance_num
```

扩容会从当前最大实例编号继续提交 Engine 作业，缩容会从最大实例编号开始取消作业。
Coordinator、Controller 和存储服务不会重启，
`SLURM_DEPLOYMENT_ID` 也不会改变。

扩缩容时只允许修改 `e_instances_num`、`p_instances_num`、`d_instances_num` 或
`hybrid_instances_num`。如果配置的其他内容发生变化，或本地记录的实例与已准备配置不一致，
命令会直接报错，不会自动推测或重建状态。

## KV Store、KV Conductor 和 MF Store

KV Store、KV Conductor 和 MF Store 是否启动由 `user_config.json` 决定。启用 KV Conductor
不会自动启用 KV Store；`KVS_MASTER_SERVICE` 可以指向本次部署的 KV Store，也可以指向已有
服务。

Memcache 使用以下两个配置文件：

```text
kv_store_backends.memcache.mmc-local-inprocess.conf
kv_store_backends.memcache.mmc-local-standalone.conf
```

源文件位于 `examples/deployer/startup/roles/kv_store_backends/memcache/`。启动时会将它们复制到
ConfigMap，并随作业脚本发送到计算节点。容器启动后会生成：

```text
/usr/local/Ascend/pyMotor/conf/mmc-local-inprocess.conf
/usr/local/Ascend/pyMotor/conf/mmc-local-standalone.conf
```

## 查看和停止

查看 Slurm 作业：

```bash
squeue -p <partition-name>
scontrol show job <job-id>
```

停止本次部署：

```bash
python3 slurm_deploy.py stop
```

`stop` 对 `slurm_deployment.json` 中的 Job ID 执行 `scancel --quiet`。重复执行不会因为作业已经结束
而报错。`stop` 不修改提交节点上的 workspace。计算节点上的任务退出时会清理自己的 ConfigMap
和分发脚本，并保留 `.log` 文件。
