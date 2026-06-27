# docker-only部署单容器PD分离指南

## 特性介绍

本文档描述在**不使用 Kubernetes deployer**、仅用 **Docker 容器 + 宿主机挂载配置** 的方式部署单容器PyMotor PD分离推理的**端到端流程**。

## 部署流程

### 准备user_config.json和env.json配置文件

可从如下路径获取[user_config.json](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/infer_engines/vllm/user_config.json)和[env.json](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/examples/infer_engines/vllm/env.json)模板，本文主要介绍docker-only部署方式相关适配点，其他特性请参考[quick_start](../../quick_start.md)。

单容器场景，需指定单容器部署模式，并修改**user_config.json**配置文件中的默认端口：

- **motor_coordinator_config.api_config.coordinator_api_infer_port**：coordinator推理端口（默认1025）。
- **motor_coordinator_config.api_config.coordinator_api_mgmt_port**：coordinator管理端口（默认1026）。
- **motor_coordinator_config.api_config.coordinator_obs_port**：coordinator observability端口（默认1027）。
- **motor_controller_config.api_config.controller_api_port**：controller管理端口（默认1026）。
- **motor_nodemanger_config.api_config.node_manager_port**：nodemanger管理端口（默认1026）。
- **motor_deploy_config.deploy_mode**：取值**single_container**表示单容器场景，其他值表示多容器。

样例如下：

```json
{
  "motor_deploy_config": {
    ...
    "deploy_mode": "single_container"
  },
  "motor_controller_config": {
    ...
    "api_config": {
      "controller_api_port": 2026
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
    },
  },
  "motor_engine_decode_config": {
    ...
    "motor_nodemanger_config": {
      "api_config": {
        "node_manager_port": 3026
      }
    },
  },
  ...
}
```

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
cp -f $EXAMPLES_PATH/deployer/startup/mooncake_config.py $CONFIGMAP_PATH/mooncake_config.py
cp -f $EXAMPLES_PATH/deployer/startup/roles/* $CONFIGMAP_PATH/

# 将准备好的user_config.json和env.json配置文件拷贝到$CONFIGMAP_PATH目录下
cp -f $USER_CONFIG_PATH $CONFIGMAP_PATH/user_config.json
cp -f $ENV_PATH $CONFIGMAP_PATH/env.json

# 若环境变量已加载，但发生改动，需先清理旧的环境变量。
sed -i '/^function set_controller_env()/,/^}/d' $CONFIGMAP_PATH/controller.sh
sed -i '/^function set_coordinator_env()/,/^}/d' $CONFIGMAP_PATH/coordinator.sh
sed -i '/^function set_prefill_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_decode_env()/,/^}/d' $CONFIGMAP_PATH/engine.sh
sed -i '/^function set_common_env()/,/^}/d' $CONFIGMAP_PATH/common.sh
sed -i '/^function set_kv_pool_env()/,/^}/d' $CONFIGMAP_PATH/kv_pool.sh
sed -i '/^function set_kv_conductor_env()/,/^}/d' $CONFIGMAP_PATH/kv_conductor.sh
sed -i '/^function set_controller_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_coordinator_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_prefill_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_decode_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
sed -i '/^function set_kv_pool_env()/,/^}/d' $CONFIGMAP_PATH/all_combine_in_single_container.sh
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

准备启动脚本start_docker.sh，脚本示例(**CONFIGMAP_PATH**需修改为实际路径，**IMAGE_NAME**需修改为实际镜像名)：

```shell
# 默认不开启特权容器，如需开启，将--privileged=false改为--privileged=true
CONFIGMAP_PATH="xxx" # CONFIGMAP_PATH需与prepare.sh保持一致，且必须使用绝对路径
IMAGE_NAME="xxx" # 镜像名

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

docker run -u root --rm --name single_container \
-e ASCEND_RUNTIME_OPTIONS=NODRV --privileged=false \
-e CONFIGMAP_PATH=$CONFIGMAP_PATH \
-e CONFIG_PATH=/usr/local/Ascend/pyMotor/conf \
-e ROLE=SINGLE_CONTAINER \
-e KVP_MASTER_SERVICE=$KVP_MASTER_SERVICE \
-e KV_POOL_PORT=$KV_POOL_PORT \
-e KV_POOL_EVICTION_HIGH_WATERMARK_RATIO=$KV_POOL_EVICTION_HIGH_WATERMARK_RATIO \
-e KV_POOL_EVICTION_RATIO=$KV_POOL_EVICTION_RATIO \
-e DEFAULT_KV_LEASE_TTL=$DEFAULT_KV_LEASE_TTL \
-p $ENDPOINT_PORT_RANGE:$ENDPOINT_PORT_RANGE \
-p $KV_PORT_RANGE:$KV_PORT_RANGE \
$ASCEND_DEVICES \
-v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
-v /usr/local/Ascend/add-ons/:/usr/local/Ascend/add-ons/ \
-v /usr/local/sbin/npu-smi:/usr/local/sbin/npu-smi \
-v /usr/local/sbin:/usr/local/sbin \
-v /var/log/npu/:/usr/slog \
-v /mnt:/mnt \
$IMAGE_NAME \
bash -c "export POD_IP=\$(grep \$(hostname) /etc/hosts | cut -f1) && source \$CONFIGMAP_PATH/boot.sh"
```

环境变量说明：

| 变量名 | 含义 | 取值 |
| :--- | :--- | :--- |
| CONFIGMAP_PATH | 启动脚本路径 | 与2.2小节保持一致，需挂载到容器中 |
| IMAGE_NAME | 镜像名 | 版本镜像，确保docker images能查询到 |
| ASCEND_VISIBLE_DEVICES | 可见卡 | 指定挂载卡，如"0,1,2,3"，默认自动检测主机昇腾卡 |
| ENDPOINT_PORT_RANGE | endpoint端口映射区间 | 非host网络部署设置endpoint端口映射，起始端口默认值10000，先P后D，每dp端口偏移2，分别对应推理端口和管理端口 |
| KV_PORT_RANGE | kv_port映射端口区间 | 非host网络部署设置kv_port映射端口，起始端口user-config.json中motor_engine_prefill_config下kv_port值，先P后D，每实例端口偏移1 |
| KVP_MASTER_SERVICE | mooncake_master部署域名 | 若开启kv_pool，设置为任意非空字符串，如kvp_master，boot.sh会自动适配为容器ip；若不开启则设置为空 |
| KV_POOL_PORT | mooncake_master部署端口 | 若开启kv_pool，设置任意有效端口，如50088；若不开启则设置为空 |
| KV_POOL_EVICTION_HIGH_WATERMARK_RATIO | mooncake_master进程高水位比例 | 若开启kv_pool，取值0~1；若不开启则设置为空 |
| KV_POOL_EVICTION_RATIO | mooncake_master进程逐出比例 | 若开启kv_pool，取值0~1；若不开启则设置为空 |
| DEFAULT_KV_LEASE_TTL | 控制 KV 对象的默认租约 TTL（毫秒） | 配置值需大于env.json中vllm实例的环境变量`ASCEND_CONNECT_TIMEOUT`和`ASCEND_TRANSFER_TIMEOUT`。默认值11000；若不开启kv_pool则设置为空 |

启动服务示例（1P1D）：

```shell
# 若开启池化，KVP_MASTER_SERVICE设置为任意非空字符串,如kvp_master，不开启池化设置为空。
ASCEND_VISIBLE_DEVICES=0,1 KVP_MASTER_SERVICE="" KV_POOL_PORT=50088 KV_POOL_EVICTION_HIGH_WATERMARK_RATIO=0.9 KV_POOL_EVICTION_RATIO=0.1 DEFAULT_KV_LEASE_TTL=11000 sh start_docker.sh
```

### A5 环境额外修改内容

A5创建容器时，需做如下调整：

**网络**：使用 `--network host`，替代 `-p` 端口映射。

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
