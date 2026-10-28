# 基于Docker的多容器PD混部服务部署

本文档指导用户基于docker组件部署PD混部服务。用户在**宿主机执行一条指令创建容器并部署服务**，但容器的生命周期需要用户手动管理。

## 准备镜像

通过以下方式获取镜像：

- 方式一：下载官方完整的MindIE Motor镜像

  进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，在搜索框输入`motor`，单击“mindie-motor”卡片，在“镜像下载”页签根据设备型号下载对应的MindIE Motor镜像。

- 方式二：在已有镜像中安装MindIE Motor

  基础镜像已安装CANN、vLLM、vLLM Ascend等组件，请参考[基于已有镜像安装MindIE Motor](../../../maintenance/build_motor_image_from_vllm_ascend.md) 额外安装MindIE Motor。

获取镜像后，请使用以下命令**将镜像加载至每一台服务器**：

   ```bash
   docker load -i xxxx.tar
   ```

## 准备服务启动脚本

在**任意一台服务器**，执行以下操作

1. 获取启动脚本。

   执行以下命令，从镜像内部拷贝`examples`目录至宿主机（以/home/examples路径为例）：

   ```bash
   # 填写上一步加载的镜像名称
   IMAGE="<镜像名或镜像ID>"
   # 创建一个临时容器并记录容器ID
   cid=$(docker create "$IMAGE")
   # 拷贝examples文件夹至宿主机
   docker cp "$cid:/tmp/motor/examples" /home/examples
   # 删除临时容器
   docker rm "$cid"
   ```

2. 准备配置文件。

   MindIE Motor已提供[PD混部配置示例](../../../../../../model_configs/vllm/pd_hybrid/README.md)。实例数量用`hybrid_instances_num`，单个实例的容器数用`single_hybrid_instance_pod_num`，每个容器的卡数用`hybrid_pod_npu_num`，引擎配置在`motor_engine_union_config`，环境变量在`motor_engine_union_env`。**将配置文件拷贝至/home/examples/config目录**下。

   ```json
   {
     "motor_deploy_config": {
       "hybrid_instances_num": 2,
       "single_hybrid_instance_pod_num": 1,
       "hybrid_pod_npu_num": 2
     }
   }
   ```

   >[!NOTE] 说明
   >上述Motor配置仅给出一种可行的模型切分示例，用户可根据实际情况调整模型划分策略，关注以下表中参数即可，但必须满足以下公式的限制：
   >
   >1个混部实例占用的NPU卡数 = single_hybrid_instance_pod_num（占用几个pod，跨几机） × hybrid_pod_npu_num（每个pod占用的NPU数）= data_parallel_size × tensor_parallel_size

   | 配置项 | 取值类型 | 取值范围 | 配置说明 |
   | :--- | :--- | :--- | :--- |
   | hybrid_instances_num | int | ≥1 | 混部实例数量。 |
   | single_hybrid_instance_pod_num | int | ≥1 | 1 个混部实例拆成几个 Pod。 |
   | hybrid_pod_npu_num | int | ≥1，单 Pod 最大 16 卡 | 每个混部 Pod 使用的 NPU 卡数。 |
   | data_parallel_size | int | ≥1 | 数据并行（DP）数。写在 `motor_engine_union_config.engine_config`。 |
   | tensor_parallel_size | int | ≥1 | 张量并行（TP）数。写在 `motor_engine_union_config.engine_config`。 |

3. <a id="step0003"></a>同步启动脚本配置。

   将整个/home/examples目录，**拷贝至所有服务器的相同路径**下。

## 启动服务

1. 获取服务启动命令

   执行如下命令：

   ```bash
   python3 /home/examples/deployer/docker_deploy.py --config_dir /home/examples/config --show-commands
   ```

   回显如下：

   ```text
   预计使用 2 台服务器。

   服务器 1：管理面
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --container-name motor-ctrl \
     --role coordinator,controller \
     --pod-ip "<本机IP>" \
     --nic-name "<本机网卡>"

   服务器 1：混部 u0
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --container-name motor-u0 \
     --role union \
     --instance-name u0 \
     --pod-ip "<本机IP>" \
     --nic-name "<本机网卡>" \
     --coordinator-ip "<服务器1 IP>" \
     --controller-ip "<服务器1 IP>"

   服务器 2：混部 u1
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --container-name motor-u1 \
     --role union \
     --instance-name u1 \
     --pod-ip "<本机IP>" \
     --nic-name "<本机网卡>" \
     --coordinator-ip "<服务器1 IP>" \
     --controller-ip "<服务器1 IP>"
   ```

2. 执行服务启动命令

   请参照以下规则**修改上述启动指令**，**在不同服务器的宿主机执行服务启动命令**

   | 参数 | 说明 |
   | :--- | :--- |
   | --pod-ip | 当前服务器的IP地址。<br>可执行`ip -4 route get 1.1.1.1`命令查看，`src`字段后面是`--pod-ip`值。 |
   | --nic-name | 当前服务器对应的主网卡名。<br>可执行`ip -4 route get 1.1.1.1`命令查看，`dev`字段后面是`--nic-name`值。 |
   | --coordinator-ip/--controller-ip | 服务器 1 的 IP。管理面部署在服务器 1。 |
   | --kv-store-ip | 服务器 1 的 IP。开启池化时 kv_store 也部署在服务器 1，与管理面同机。 |

## 推理验证

在**部署管理服务的服务器上**发送如下推理请求，即可进行推理验证：

```bash
curl -X POST http://<coordinator管理服务所在服务器的IP地址>:1025/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<模型名称，必须和user_config.json文件中的配置一致>",
    "messages": [{"role": "user", "content": "who are you?"}],
    "max_tokens": 36,
    "stream": true
  }'
```

若返回以下内容，表示服务尚未就绪，可稍后重试。

```Output
{"detail":"Service is not available"}
```

## 终止服务与重复部署

在终端执行`Ctrl+C`可终止服务，但不会退出容器。服务终止后会出现如下提示，便于用户重新部署。

```text
容器内服务已终止，可执行以下命令重新部署服务。
python3 /path/to/examples/deployer/docker_deploy.py --config_dir /path/to/model_configs/vllm --start --container-name motor-u0 --role union --instance-name u0 --pod-ip <本机IP地址> --nic-name <本机主网卡名称>
运行日志：examples/motor_workspace/motor-u0/u0/log/docker-union-u0-<时间戳>.log
```

## 启用Render特性（可选）

在 `/home/examples/config/user_config.json` 中增加以下配置：

```json
{
  ...
  "motor_coordinator_config": {
    "render_config": {
      "enable": true
    }
  },
  ...
}
```

按[启动服务指导](#启动服务)部署管理面服务时，会额外创建一个名为 `<container-name>-vllm-render` 的容器，并在其中启动 vLLM Render。当管理面服务被终止时，render服务会被自动终止；当管理面服务被重启时，render服务被自动重启。Render退出后，管理面服务继续运行。

若 `<container-name>` 或 `<container-name>-vllm-render` 已经存在，终端部署会询问是否删除后继续。这一步在端口检查之前：上次容器没清掉时，它占用的端口会随容器一起释放，不再被当成端口冲突。回答 `y` 会同时删除管理面容器和 Render 容器，再重新创建。回答 `N`，或在非交互环境执行，则中止且不覆盖。

## 调优指导

### 如何调整容器挂载

部署脚本会为用户自动创建容器，容器将自动挂载用户的服务启动脚本以及权重文件的路径。如需自定义容器的创建指令，可以手动调整 `examples/deployer/lib/constant.py` 文件中的 docker-run 模板。

### 同一服务器部署多个推理容器时错开端口

同一服务器上部署多个推理容器时，须先通过`--devices`为本容器分配NPU，再规划并错开本容器端口。

`--devices`填写本容器使用的卡号。同一服务器上的多个推理容器须使用不同卡号，卡数须与该容器所需NPU数量一致。

错开端口时，在启动命令中增加下列参数：

| 参数 | 说明 |
| :--- | :--- |
| --node-manager-port | 实际运行时的NodeManager端口，用于引擎节点管理。 |
| --data-parallel-rpc-port | 实际运行时的数据并行RPC端口。 |
| --kv-port | 实际运行时的KV传输端口。 |
| --base-port | 实际运行时的引擎端点起始端口。 |

以混部实例u0的2个容器部署在同一服务器为例。2个容器分别使用卡0与卡1，端口规划如下

| --container-name | --devices | --node-manager-port | --data-parallel-rpc-port | --base-port |
| :--- | :--- | :--- | :--- | :--- |
| motor-u0-0 | 0 | 4026 | 9000 | 10000 |
| motor-u0-1 | 1 | 4126 | 9100 | 11000 |

执行以下命令部署容器`motor-u0-0`：

```bash
python3 docker_deploy.py --config_dir ../../model_configs/vllm \
  ... \
  --devices 0 \
  ... \
  --node-manager-port 4026 \
  --data-parallel-rpc-port 9000 \
  --base-port 10000
```

执行以下命令部署容器`motor-u0-1`：

```bash
python3 docker_deploy.py --config_dir ../../model_configs/vllm \
  ... \
  --devices 1 \
  ... \
  --node-manager-port 4126 \
  --data-parallel-rpc-port 9100 \
  --base-port 11000
```

>[!NOTE] 说明
>开启池化时，以上命令分别增加不同的`--kv-port`，例如`21000`与`22000`。未开启池化不要传`--kv-port`。
