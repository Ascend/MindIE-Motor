# docker-only部署多容器PD指南

## 特性介绍

本文档描述在**不使用 Kubernetes deployer**、仅用 **Docker 容器 + 宿主机挂载配置** 的方式部署多容器 MindIE Motor PD 推理服务的**端到端流程**，同时适用于 PD 混部和 PD 分离。

| 部署模式 | Engine 容器 | 跨角色 KV 传输 | 启动方式 |
| :--- | :--- | :--- | :--- |
| PD 分离 | Prefill 和 Decode 容器 | 需要 | 分别拉起 P/D 实例，按需拉起 KV Cache Store |
| PD 混部 | union 容器 | 不需要 | 每个 union 实例按其占用节点分别拉起 |

## 部署流程

### 准备 examples

`examples` 获取方式见[快速入门](../../quick_start.md)的“服务部署”章节。从镜像拷贝至宿主机后，将后续 `prepare.sh` 中的 `EXAMPLES_PATH` 设置为该目录的绝对路径。

### 准备user_config.json和env.json配置文件

根据部署模式准备 `user_config.json` 和 `env.json`。配置字段的完整说明请参考 [user_config 全量参数说明](../k8s/config_reference.md)。

Coordinator、Controller 和 Engine 容器部署在不同节点时可使用默认端口；同一节点部署多个角色时，推荐显式配置以下端口：

- Coordinator 推理、管理和可观测端口分别使用 `1025`、`1026`、`1027`。
- Controller 管理和可观测端口使用 `2026`、`2027`。
- union / Prefill NodeManager 从 `3026` 起规划；Decode NodeManager 从 `4026` 起规划。

#### PD 分离配置

```json
{
  "motor_deploy_config": {
    ...
    "p_instances_num": 1,
    "d_instances_num": 1,
    "single_p_instance_pod_num": 2,
    "single_d_instance_pod_num": 4
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
    "hybrid_instances_num": 1,
    "single_hybrid_instance_pod_num": 2,
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
    },
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

`env.json` 使用 `motor_engine_union_env`。PD 混部不需要 `kv_transfer_config`。

### 端口规划

| 组件 | 推荐端口 | 说明 |
| :--- | :--- | :--- |
| Coordinator 推理 | 1025 | 使用 host 网络，通过 Coordinator 节点 IP 访问 |
| Coordinator 管理 | 1026 | 同节点内须避免被其他角色占用 |
| Coordinator 可观测 | 1027 | `/metrics` |
| Controller 管理 | 2026 | 同节点部署时避开 1026 |
| Controller 可观测 | 2027 | 同节点部署时避开 1027 |
| union / Prefill NodeManager | 3026 起 | 按实例和节点规划 |
| Decode NodeManager | 4026 起 | 与 Prefill 区分 |

### 准备CONFIGMAP_PATH

准备阶段需将配置文件、启动脚本拷贝到环境变量**CONFIGMAP_PATH**对应目录下，并通过set_env_docker.py加载环境变量。准备阶段脚本**prepare.sh**示例(**EXAMPLES_PATH**、**CONFIGMAP_PATH**、**USER_CONFIG_PATH**、**ENV_PATH**需修改为实际路径)：

```shell
EXAMPLES_PATH="xxx" # 主机examples部署脚本路径
CONFIGMAP_PATH="xxx" # 服务启动脚本路径，需挂载到容器内
USER_CONFIG_PATH="xxx" # user_config.json路径
ENV_PATH="xxx" # env.json路径

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

### Docker启动服务

准备启动脚本start_docker.sh，脚本示例（**CONFIGMAP_PATH**、**WEIGHT_MOUNT_PATH**需修改为实际绝对路径，**IMAGE_NAME**需修改为实际镜像名）。**WEIGHT_MOUNT_PATH**需与`user_config.json`中`weight_mount_path`及模型路径保持一致：

```shell
# 默认不开启特权容器，如需开启，将--privileged=false改为--privileged=true
CONFIGMAP_PATH="xxx" # CONFIGMAP_PATH需与prepare.sh保持一致，且必须使用绝对路径
IMAGE_NAME="xxx" # 镜像名
WEIGHT_MOUNT_PATH="xxx" # 宿主机权重目录，必须使用绝对路径

if [ "$ENABLE_IPC_HOST" = "enable" ]; then
    SET_IPC_HOST_STR="--ipc=host"
fi

# 从环境变量读取可见卡，默认自动检测主机昇腾卡，用逗号拼接，如"0,1,2,3"
if [ -z "$ASCEND_VISIBLE_DEVICES" ]; then
    ASCEND_VISIBLE_DEVICES=$(ls /dev/davinci[0-9]* 2>/dev/null | sed 's/[^0-9]//g' | paste -sd "," -)
fi
ASCEND_DEVICES="--device=/dev/davinci_manager --device=/dev/devmm_svm --device=/dev/hisi_hdc"
# 循环挂载ASCEND_VISIBLE_DEVICES指定卡
IFS=',' read -ra ADDR <<< "$ASCEND_VISIBLE_DEVICES"
for i in "${ADDR[@]}"; do
    ASCEND_DEVICES="$ASCEND_DEVICES --device=/dev/davinci$i"
done

docker run -u root --rm --name $CONTAINER_NAME --net=host $SET_IPC_HOST_STR \
-e ASCEND_RUNTIME_OPTIONS=NODRV --privileged=false \
-e CONFIGMAP_PATH=$CONFIGMAP_PATH \
-e CONFIG_PATH=/usr/local/Ascend/pyMotor/conf \
-e ROLE=$ROLE \
-e JOB_NAME=$JOB_NAME \
-e COORDINATOR_SERVICE=$COORDINATOR_SERVICE \
-e CONTROLLER_SERVICE=$CONTROLLER_SERVICE \
-e POD_IP=$POD_IP \
-e KV_STORE_BACKEND=$KV_STORE_BACKEND \
-e KVS_MASTER_SERVICE=$KVS_MASTER_SERVICE \
-e KV_CACHE_STORE_PORT=$KV_CACHE_STORE_PORT \
-e KV_STORE_EVICTION_HIGH_WATERMARK_RATIO=$KV_STORE_EVICTION_HIGH_WATERMARK_RATIO \
-e KV_STORE_EVICTION_RATIO=$KV_STORE_EVICTION_RATIO \
-e DEFAULT_KV_LEASE_TTL=$DEFAULT_KV_LEASE_TTL \
$ASCEND_DEVICES \
-v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
-v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/ \
-v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
-v /usr/local/sbin:/usr/local/sbin \
-v /var/log/npu/:/usr/slog \
-v /mnt:/mnt \
-v $CONFIGMAP_PATH:$CONFIGMAP_PATH \
-v $WEIGHT_MOUNT_PATH:$WEIGHT_MOUNT_PATH:ro \
$IMAGE_NAME \
bash -c "source \$CONFIGMAP_PATH/boot.sh"
```

环境变量说明：

| 变量名                                 | 含义                        | 取值                                                                                                |
| :------------------------------------- | :-------------------------- | :-------------------------------------------------------------------------------------------------- |
| CONFIGMAP_PATH                         | 启动脚本路径                | 与2.2小节保持一致，需挂载到容器中                                                                   |
| IMAGE_NAME                             | 镜像名                      | 版本镜像，确保docker images能查询到                                                                 |
| WEIGHT_MOUNT_PATH                      | 模型权重宿主机路径          | 与`user_config.json`中`weight_mount_path`及模型路径保持一致，必须使用绝对路径                       |
| CONTAINER_NAME                         | 容器名                      | 不限                                                                                                |
| ASCEND_VISIBLE_DEVICES                 | 可见卡                      | 指定挂载卡，如"0,1,2,3"，默认自动检测主机昇腾卡                                                     |
| ENABLE_IPC_HOST                        | 是否使能--ipc=host          | enable或其他                                                                                        |
| ROLE                                   | 部署角色                    | coordinator / controller / union / prefill / decode / kv_store                                      |
| JOB_NAME                               | Engine 实例任务名           | union / prefill / decode 需设置，每个实例具有唯一性                                                 |
| COORDINATOR_SERVICE                    | Coordinator 地址            | 设置为 Coordinator 部署节点 IP                                                                      |
| CONTROLLER_SERVICE                     | Controller 地址             | 设置为 Controller 部署节点 IP                                                                       |
| POD_IP                                 | 容器 IP                     | 使用 host 网络，取值为宿主机 IP                                                                     |
| KV_STORE_BACKEND                       | KV Cache Store 后端         | PD 分离启用 Mooncake 时设置为`mooncake`；不启用或使用 PD 混部时设置为空                           |
| KVS_MASTER_SERVICE                     | KV Cache Store 地址         | PD 分离启用时设置为 KV Cache Store 所在节点 IP；不启用或使用 PD 混部时设置为空                      |
| KV_CACHE_STORE_PORT                    | KV Cache Store 端口         | 启用时设置有效端口，如 50088                                                                        |
| KV_STORE_EVICTION_HIGH_WATERMARK_RATIO | KV Cache Store 高水位比例   | 启用时取值 0～1                                                                                     |
| KV_STORE_EVICTION_RATIO                | KV Cache Store 逐出比例     | 启用时取值 0～1                                                                                     |
| DEFAULT_KV_LEASE_TTL                   | KV 对象默认租约 TTL（毫秒） | 配置值须大于`env.json` 中的 `ASCEND_CONNECT_TIMEOUT` 和 `ASCEND_TRANSFER_TIMEOUT`，默认 11000 |

两种模式均先启动 Coordinator 和 Controller：

```shell
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" JOB_NAME="" ROLE="coordinator" POD_IP="<IP0>" CONTAINER_NAME="docker_coordinator" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" JOB_NAME="" ROLE="controller" POD_IP="<IP1>" CONTAINER_NAME="docker_controller" sh start_docker.sh
```

#### 启动 PD 分离实例

以下示例部署 1P1D：P 占用 `<IP0>`、`<IP1>`，D 占用 `<IP2>`～`<IP5>`。相同实例的多个容器需一起拉起。

```shell
# 若启用 KV Cache Store，先在对应节点启动；不启用时跳过。
ROLE=kv_store POD_IP="<IP2>" KV_STORE_BACKEND=mooncake KVS_MASTER_SERVICE="<IP2>" KV_CACHE_STORE_PORT=50088 KV_STORE_EVICTION_HIGH_WATERMARK_RATIO=0.9 KV_STORE_EVICTION_RATIO=0.1 DEFAULT_KV_LEASE_TTL=11000 CONTAINER_NAME="docker_kv_store" sh start_docker.sh

COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="p0" ROLE="prefill" POD_IP="<IP0>" CONTAINER_NAME="docker_p0_node0" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="p0" ROLE="prefill" POD_IP="<IP1>" CONTAINER_NAME="docker_p0_node1" sh start_docker.sh

COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP2>" CONTAINER_NAME="docker_d0_node0" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP3>" CONTAINER_NAME="docker_d0_node1" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP4>" CONTAINER_NAME="docker_d0_node2" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" KVS_MASTER_SERVICE="" ENABLE_IPC_HOST="" JOB_NAME="d0" ROLE="decode" POD_IP="<IP5>" CONTAINER_NAME="docker_d0_node3" sh start_docker.sh
```

启用 KV Cache Store 时，Prefill/Decode 启动命令中的 `KV_STORE_BACKEND` 也须设置为 `mooncake`，`KVS_MASTER_SERVICE` 设置为 KV Cache Store 节点 IP，并按需要设置 `ENABLE_IPC_HOST=enable`。

#### 启动 PD 混部实例

以下示例部署 1 个 union 实例，占用 `<IP0>`、`<IP1>` 两个节点：

```shell
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" JOB_NAME="u0" ROLE="union" POD_IP="<IP0>" CONTAINER_NAME="docker_u0_node0" sh start_docker.sh
COORDINATOR_SERVICE="<IP0>" CONTROLLER_SERVICE="<IP1>" JOB_NAME="u0" ROLE="union" POD_IP="<IP1>" CONTAINER_NAME="docker_u0_node1" sh start_docker.sh
```

### 服务验证

服务就绪后，在任意可访问 Coordinator 的机器执行以下命令。将 `<IP0>` 替换为 Coordinator 部署节点 IP，将 `model` 替换为 `user_config.json` 中配置的模型名称。

```bash
curl -X POST http://<IP0>:1025/v1/chat/completions \
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

若返回 `{"detail":"Service is not available"}`，表示服务尚未就绪，可稍后重试并查看 `docker logs docker_coordinator`。若返回流式 JSON，则说明推理正常。

>[!NOTE]说明
>
>HTTP 协议存在安全风险，生产环境建议开启 HTTPS。接口和 TLS 配置请参考[业务接口](../../api/service_interfaces.md)。

### A5 环境额外修改内容

A5创建容器时，需做如下调整：

**网络**：正文 `docker run` 已使用 `--net=host`，A5 场景继续使用 host 网络。

**额外挂载路径**：

| 宿主机路径 | 容器路径 | 说明 |
| :--- | :--- | :--- |
| `/dev/ummu` | `/dev/ummu` | A5 卡间 UB 互联内存设备，UB 内存池访问依赖此通路 |
| `/dev/uburma` | `/dev/uburma` | 服务器间 UB RDMA 通信设备节点 |
| `/usr/lib64` | `/usr/lib64` | 提供 `liburma` 等 UB 用户态通信库 |
| `/etc/hixlep` | `/etc/hixlep` | UB 链路拓扑结构 |
| `/etc/hccl_rootinfo.json` | `/etc/hccl_rootinfo.json` | HCCL 集群建链配置文件 |
| `/usr/local/bin/npu-smi` | `/usr/local/bin/npu-smi` | NPU 管理工具 |
| `/usr/local/dcmi` | `/usr/local/dcmi` | DCMI 库目录，npu-smi 查卡/管卡的前端接口 |

A5 启动示例片段（基于上述实例基础修改）：

```shell
ASCEND_DEVICES="--device=/dev/davinci_manager --device=/dev/hisi_hdc"
# 按 ASCEND_VISIBLE_DEVICES 循环追加 --device=/dev/davinci$i

docker run -u root --rm --name $CONTAINER_NAME \
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
