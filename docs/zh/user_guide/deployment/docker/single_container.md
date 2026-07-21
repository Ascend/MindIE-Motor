# docker-only部署单容器PD指南

## 特性介绍

本文档描述在**不使用 Kubernetes deployer**、仅用 **Docker 容器 + 宿主机挂载配置** 的方式部署单容器 MindIE Motor PD 推理服务的**端到端流程**，同时适用于 PD 混部和 PD 分离。

| 部署模式 | Engine 实例 | 跨角色 KV 传输 | 单容器内拉起进程 |
| :--- | :--- | :--- | :--- |
| PD 分离 | Prefill 和 Decode 实例 | 需要 | Coordinator、Controller、Prefill/Decode NodeManager |
| PD 混部 | union 实例 | 不需要 | Coordinator、Controller、union NodeManager |

## 部署流程

以`/mnt/motor`作为根路径，目录结构如下：

```text
/mnt/motor/
├── prepare.sh
├── start_motor.sh
├── start_docker.sh
├── user_config.json
├── env.json
├── examples/
└── configmap/ # 该目录下的文件都是自动生成的
    ├── boot.sh
    ├── common.sh
    ├── hccl_tools.py
    ├── all_combine_in_single_container.sh
    ├── controller.sh
    ├── coordinator.sh
    ├── engine.sh
    ├── kv_conductor.sh
    ├── kv_cache_store.sh
    ├── kv_store_backends.mooncake.mooncake.sh
    ├── kv_store_backends.mooncake.mooncake_config.py
    ├── kv_store_backends.memcache.memcache.sh
    ├── kv_store_backends.memcache.memcache_meta_service.py
    ├── kv_store_backends.memcache.mmc-local.conf
    ├── mf_store.sh
    ├── user_config.json
    └── env.json
```

### 准备 examples

`examples` 获取方式见[快速入门](../../quick_start.md)的“服务部署”章节。从镜像拷贝至宿主机后，将后续 `prepare.sh` 中的 `EXAMPLES_PATH` 设置为该目录的绝对路径。

### 准备user_config.json和env.json配置文件

根据部署模式准备 `user_config.json` 和 `env.json`。配置字段的完整说明请参考 [user_config 全量参数说明](../k8s/config_reference.md)。

两种模式均须配置：

- `motor_deploy_config.deploy_mode`：必须设置为 `single_container`。
- Coordinator 推理、管理和可观测端口分别推荐使用 `1025`、`1026`、`1027`。
- Controller 管理和可观测端口推荐使用 `2026`、`2027`，避免与 Coordinator 冲突。
- NodeManager 端口须配置在对应 engine section 的 `motor_nodemanger_config.api_config` 下。

#### PD 分离配置

```json
{
  "motor_deploy_config": {
    ...
    "deploy_mode": "single_container",
    "p_instances_num": 1,
    "d_instances_num": 1
  },
  "motor_controller_config": {
    ...
    "api_config": {
      "controller_api_port": 2026,
      "observability_api_port": 2027
    }
  },
  "motor_coordinator_config": {
    ...
    "api_config": {
      "coordinator_api_infer_port": 1025,
      "coordinator_api_mgmt_port": 1026,
      "coordinator_obs_port": 1027
    }
  },
  "motor_engine_prefill_config": {
    ...
    "motor_nodemanger_config": {
      "api_config": {
        "node_manager_port": 3026
      }
    }
  },
  "motor_engine_decode_config": {
    ...
    "motor_nodemanger_config": {
      "api_config": {
        "node_manager_port": 4026
      }
    }
  },
  ...
}
```

`env.json` 分别使用 `motor_engine_prefill_env` 和 `motor_engine_decode_env`。同时须在 P/D engine 配置中正确设置 `kv_transfer_config`；启用 KV Cache Store 时，还需准备对应环境变量。

#### PD 混部配置

```json
{
  "motor_deploy_config": {
    ...
    "deploy_mode": "single_container",
    "hybrid_instances_num": 1,
    "single_hybrid_instance_pod_num": 1,
    "hybrid_pod_npu_num": 2
  },
  "motor_controller_config": {
    ...
    "api_config": {
      "controller_api_port": 2026,
      "observability_api_port": 2027
    }
  },
  "motor_coordinator_config": {
    ...
    "api_config": {
      "coordinator_api_infer_port": 1025,
      "coordinator_api_mgmt_port": 1026,
      "coordinator_obs_port": 1027
    }
  },
  "motor_engine_union_config": {
    "engine_type": "vllm",
    ...
    "motor_nodemanger_config": {
      "api_config": {
        "node_manager_port": 3026
      }
    }
  },
  ...
}
```

`env.json` 使用 `motor_engine_union_env` 配置 union 实例环境变量。PD 混部不需要 `kv_transfer_config`。

### 端口规划

| 组件 | 推荐端口 | 说明 |
| :--- | :--- | :--- |
| Coordinator 推理 | 1025 | 通过 `-p 31015:1025` 暴露到宿主机 |
| Coordinator 管理 | 1026 | 容器内部管理接口 |
| Coordinator 可观测 | 1027 | 可按需通过 `-p 31017:1027` 暴露 |
| Controller 管理 | 2026 | 避开 Coordinator 管理端口 |
| Controller 可观测 | 2027 | 避开 Coordinator 可观测端口 |
| union / Prefill NodeManager | 3026 起 | 按实例规划 |
| Decode NodeManager | 4026 起 | 与 Prefill 区分 |

若修改 `coordinator_api_infer_port`，`docker run` 端口映射的容器侧端口须同步修改。

### 准备configmap

准备阶段需将配置文件、启动脚本拷贝到环境变量**CONFIGMAP_PATH**对应目录下，并通过set_env_docker.py加载环境变量。准备阶段脚本**prepare.sh**示例(**EXAMPLES_PATH**、**CONFIGMAP_PATH**、**USER_CONFIG_PATH**、**ENV_PATH**需修改为实际路径)：

以下以`/mnt/motor`作为根路径为例

```shell
EXAMPLES_PATH="/mnt/motor/examples/"
CONFIGMAP_PATH="/mnt/motor/configmap/"
USER_CONFIG_PATH="/mnt/motor/user_config.json"
ENV_PATH="/mnt/motor/env.json"

mkdir -p $CONFIGMAP_PATH

# 容器启动脚本boot.sh，其运行时会调用startup目录下其他脚本，需要将其统一拷贝到$CONFIGMAP_PATH目录下。
cp -f $EXAMPLES_PATH/deployer/startup/boot.sh $CONFIGMAP_PATH/boot.sh
cp -f $EXAMPLES_PATH/deployer/startup/common.sh $CONFIGMAP_PATH/common.sh
cp -f $EXAMPLES_PATH/deployer/startup/hccl_tools.py $CONFIGMAP_PATH/hccl_tools.py
cp -f $EXAMPLES_PATH/deployer/startup/roles/*.sh $CONFIGMAP_PATH/
cp -f $EXAMPLES_PATH/deployer/startup/roles/kv_store_backends/mooncake/mooncake.sh $CONFIGMAP_PATH/kv_store_backends.mooncake.mooncake.sh
cp -f $EXAMPLES_PATH/deployer/startup/roles/kv_store_backends/mooncake/mooncake_config.py $CONFIGMAP_PATH/kv_store_backends.mooncake.mooncake_config.py
cp -f $EXAMPLES_PATH/deployer/startup/roles/kv_store_backends/memcache/memcache.sh $CONFIGMAP_PATH/kv_store_backends.memcache.memcache.sh
cp -f $EXAMPLES_PATH/deployer/startup/roles/kv_store_backends/memcache/memcache_meta_service.py $CONFIGMAP_PATH/kv_store_backends.memcache.memcache_meta_service.py
cp -f $EXAMPLES_PATH/deployer/startup/roles/kv_store_backends/memcache/mmc-local.conf $CONFIGMAP_PATH/kv_store_backends.memcache.mmc-local.conf

# 将准备好的user_config.json和env.json配置文件拷贝到$CONFIGMAP_PATH目录下
cp -f $USER_CONFIG_PATH $CONFIGMAP_PATH/user_config.json
cp -f $ENV_PATH $CONFIGMAP_PATH/env.json

# 若环境变量已加载，但发生改动，需先清理旧的环境变量。
sed -i '/^function set_controller_env()/,/^}/d' $CONFIGMAP_PATH/controller.sh
sed -i '/^function set_coordinator_env()/,/^}/d' $CONFIGMAP_PATH/coordinator.sh
sed -i '/^function set_prefill_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_decode_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_union_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_common_env()/,/^}/d' $CONFIGMAP_PATH/common.sh
sed -i '/^function set_kv_store_env()/,/^}/d' $CONFIGMAP_PATH/kv_cache_store.sh
sed -i '/^function set_kv_conductor_env()/,/^}/d' $CONFIGMAP_PATH/kv_conductor.sh
sed -i '/^function set_controller_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_coordinator_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_prefill_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_decode_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_union_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_kv_store_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_kv_conductor_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/./,$!d' $CONFIGMAP_PATH/common.sh

# 加载user_config.json和env.json中的环境变量，并作用于容器启动脚本。
python $EXAMPLES_PATH/deployer/startup/set_env_docker.py --configmap_path $CONFIGMAP_PATH
```

执行方式：

```bash
sh prepare.sh
```

执行完成后，在`/mnt/motor/configmap/`目录下会生成一些脚本

### 准备Motor启动脚本

准备 `start_motor.sh` 脚本。两种模式共用该脚本；PD 混部不启用 KV Cache Store，将 `KVS_MASTER_SERVICE` 保持为空。PD 分离按需配置 KV Cache Store。

```sh
CONFIGMAP_PATH="/mnt/motor/configmap" # CONFIGMAP_PATH需与prepare.sh保持一致，且必须使用绝对路径
CONFIG_PATH=/usr/local/Ascend/pyMotor/conf

ROLE=SINGLE_CONTAINER

# mooncake池化配置
# PD 分离启用 Mooncake KV Cache Store 时，将 KV_STORE_BACKEND 设为 mooncake，
# KVS_MASTER_SERVICE 设为任意非空字符串；不启用或使用 PD 混部时均设置为空。
KV_STORE_BACKEND=""
KVS_MASTER_SERVICE=""
KV_CACHE_STORE_PORT=50088
KV_STORE_EVICTION_HIGH_WATERMARK_RATIO=0.9
KV_STORE_EVICTION_RATIO=0.1
DEFAULT_KV_LEASE_TTL=11000

source $CONFIGMAP_PATH/boot.sh
```

环境变量说明：

| 变量名 | 含义 | 取值 |
| :--- | :--- | :--- |
| KV_STORE_BACKEND | KV Cache Store 后端 | PD 分离启用 Mooncake 时设置为 `mooncake`；不启用或使用 PD 混部时设置为空 |
| KVS_MASTER_SERVICE | Mooncake KV Cache Store 地址 | PD 分离启用时设置任意非空字符串，启动脚本会适配为容器 IP；不启用或使用 PD 混部时设置为空 |
| KV_CACHE_STORE_PORT | Mooncake KV Cache Store 端口 | 启用时设置有效端口，如 50088 |
| KV_STORE_EVICTION_HIGH_WATERMARK_RATIO | KV Cache Store 高水位比例 | 启用时取值 0～1 |
| KV_STORE_EVICTION_RATIO | KV Cache Store 逐出比例 | 启用时取值 0～1 |
| DEFAULT_KV_LEASE_TTL | KV 对象默认租约 TTL（毫秒） | 配置值须大于 `env.json` 中的 `ASCEND_CONNECT_TIMEOUT` 和 `ASCEND_TRANSFER_TIMEOUT`，默认 11000 |

### 准备Docker启动脚本

准备启动脚本`start_docker.sh`，脚本示例（**CONFIGMAP_PATH**、**WEIGHT_MOUNT_PATH**需修改为实际绝对路径，**IMAGE_NAME**需修改为实际镜像名）。**WEIGHT_MOUNT_PATH**需与`user_config.json`中`weight_mount_path`及模型路径保持一致：

```shell
# 默认不开启特权容器，如需开启，将--privileged=false改为--privileged=true
CONFIGMAP_PATH="/mnt/motor/configmap" # CONFIGMAP_PATH需与prepare.sh保持一致，且必须使用绝对路径
IMAGE_NAME="xxx" # 镜像名
WEIGHT_MOUNT_PATH="xxx" # 宿主机权重目录，必须使用绝对路径

ASCEND_DEVICES="--device=/dev/davinci_manager --device=/dev/devmm_svm --device=/dev/hisi_hdc"

docker run -u root --rm --name single_container \
-e ASCEND_RUNTIME_OPTIONS=NODRV --privileged=false \
$ASCEND_DEVICES \
-v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
-v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/ \
-v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
-v /usr/local/sbin:/usr/local/sbin \
-v /var/log/npu/:/usr/slog \
-v /mnt:/mnt \
-v $CONFIGMAP_PATH:$CONFIGMAP_PATH \
-v $WEIGHT_MOUNT_PATH:$WEIGHT_MOUNT_PATH:ro \
-p 31015:1025 \
-p 31017:1027 \
$IMAGE_NAME \
bash -c "export POD_IP=\$(grep \$(hostname) /etc/hosts | cut -f1) && source /mnt/motor/start_motor.sh"
```

**注意：挂载路径要包含/mnt**

### 启动Docker

脚本会根据 `user_config.json` 自动拉起 union 实例或 Prefill/Decode 实例。

PD 分离启动示例（1P1D）：

```shell
ASCEND_VISIBLE_DEVICES=0,1 sh start_docker.sh
```

PD 混部启动示例（1 个 union 实例）：

```shell
ASCEND_VISIBLE_DEVICES=0,1 sh start_docker.sh
```

### 服务验证

服务就绪后，在宿主机执行以下命令。将 `<IP>` 替换为宿主机 IP 或 `127.0.0.1`，将 `model` 替换为 `user_config.json` 中配置的模型名称。

```bash
curl -X POST http://<IP>:31015/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3-8B",
    "messages": [
      {
        "role": "user",
        "content": "who are you?"
      }
    ],
    "max_tokens": 36,
    "stream": true
  }'
```

若返回 `{"detail":"Service is not available"}`，表示服务尚未就绪，可稍后重试并查看 `docker logs single_container`。若返回流式 JSON，则说明推理正常。

>[!NOTE]说明
>
>HTTP 协议存在安全风险，生产环境建议开启 HTTPS。接口和 TLS 配置请参考[业务接口](../../api/service_interfaces.md)。

### A5 环境额外修改内容

A5创建容器时，需做如下调整：

**网络**：使用 `--network host`，替代 `-p` 端口映射。此时服务验证使用 `http://<IP>:1025/v1/chat/completions`。

**额外挂载路径**：

| 宿主机路径 | 容器路径 | 说明 |
| :--- | :--- | :--- |
| `/dev/ummu` | `/dev/ummu` | A5 卡间 UB 互联内存设备，UB 内存池访问依赖此通路 |
| `/dev/uburma` | `/dev/uburma` | 服务器间 UB RDMA 通信设备节点 |
| `/usr/lib64` | `/usr/lib64` | 提供 `liburma` 等 UB 用户态通信库 |
| `/etc/hixlep` | `/etc/hixlep` | UB 链路拓扑结构 |
| `/etc/hccl_rootinfo.json` | `/etc/hccl_rootinfo.json` | HCCL 集群建链配置文件 |
| `/usr/local/bin/npu-smi` | `/usr/local/bin/npu-smi` | NPU 管理工具 |
| `/usr/local/dcmi` | `/usr/local/dcmi` | DCMI 库目录 |

A5 启动示例片段：

```shell
ASCEND_DEVICES="--device=/dev/davinci_manager --device=/dev/hisi_hdc"

docker run -u root --rm --name single_container \
  --network host \
  ... \
  -v /usr/local/bin/npu-smi:/usr/local/bin/npu-smi \
  -v /usr/lib64:/usr/lib64 \
  -v /etc/hixlep:/etc/hixlep \
  -v /etc/hccl_rootinfo.json:/etc/hccl_rootinfo.json \
  -v /usr/local/dcmi:/usr/local/dcmi \
  -v /dev/ummu:/dev/ummu \
  -v /dev/uburma:/dev/uburma \
  ... \
```
