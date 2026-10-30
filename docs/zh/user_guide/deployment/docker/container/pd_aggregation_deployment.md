# 基于Docker的容器内PD混部服务部署

本文档指导用户在已创建的容器内部署PD混部服务。容器由云化平台（例如 ModelArts）或用户创建，用户进入容器后执行一条指令拉起服务。

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

   MindIE Motor已提供[PD混部配置示例](../../../../../../examples/infer_engines/vllm/pd_hybrid/README.md)。实例数量用`hybrid_instances_num`，单个实例的容器数用`single_hybrid_instance_pod_num`，每个容器的卡数用`hybrid_pod_npu_num`，引擎配置在`motor_engine_union_config`，环境变量在`motor_engine_union_env`。**将配置文件拷贝至/home/examples/config目录**下。

   ```json
   {
     "motor_deploy_config": {
       "hybrid_instances_num": 2,
       "single_hybrid_instance_pod_num": 1,
       "hybrid_pod_npu_num": 2
     }
   }
   ```

   >[!NOTE]说明
   >上述Motor配置仅给出一种可行的模型切分示例，**用户可根据实际情况调整模型划分策略**，关注以下表中参数即可，但必须满足以下公式的限制：
   >
   > 1个混部实例占用的NPU卡数 = single_hybrid_instance_pod_num（占用几个pod，跨几机） × hybrid_pod_npu_num （每个pod占用的NPU数）= data_parallel_size × tensor_parallel_size

   | 配置项 | 取值类型 | 取值范围 | 配置说明 |
   | --- | --- | --- | --- |
   | hybrid_instances_num | int | ≥1 | 混部实例数量。 |
   | single_hybrid_instance_pod_num | int | ≥1 | 1 个混部实例拆成几个 Pod。 |
   | hybrid_pod_npu_num | int | ≥1，单 Pod 最大 16 卡 | 每个混部 Pod 使用的 NPU 卡数。 |
   | data_parallel_size | int | ≥1 | 数据并行（DP）数 |
   | tensor_parallel_size | int | ≥1 | 张量并行（TP）数 |

3. <a id="step0003"></a>同步启动脚本配置。

   将整个/home/examples目录，**拷贝至所有服务器的相同路径**下。

## 创建容器

按照规划在集群内的服务器上创建容器，一共需要创建三类容器。容器名与[启动服务](#启动服务)回显中的`--container-name`一致。`examples/deployer/lib/constant.py`中的模板可供参考：

| 容器类型 | 参考模板 | 配置说明 |
| --- | --- | --- |
| 管理面 | [ENTER_DOCKER_RUN_CTRL](https://gitcode.com/Ascend/MindIE-Motor/blob/all_in_one/examples/deployer/lib/constant.py#L495-L502) | 容器名`motor-ctrl`，创建在服务器 1。 |
| 混部 | Atlas 800I A2：[ENTER_DOCKER_RUN_A2](https://gitcode.com/Ascend/MindIE-Motor/blob/all_in_one/examples/deployer/lib/constant.py#L391-L419)<br>Atlas 800I A3：[ENTER_DOCKER_RUN_A3](https://gitcode.com/Ascend/MindIE-Motor/blob/all_in_one/examples/deployer/lib/constant.py#L421-L457)<br>Ascend 950（8卡）：[ENTER_DOCKER_RUN_A5](https://gitcode.com/Ascend/MindIE-Motor/blob/all_in_one/examples/deployer/lib/constant.py#L459-L493) | 1 个实例只占 1 个 容器时，容器名为`motor-u0`、`motor-u1`。1 个实例拆成多个容器时，容器名为`motor-u0-0`、`motor-u0-1`。每台服务器仅创建一个容器。 |
| kv_store | [ENTER_DOCKER_RUN_KVS](https://gitcode.com/Ascend/MindIE-Motor/blob/all_in_one/examples/deployer/lib/constant.py#L504-L514) | 开启池化时创建，容器名`motor-kvs`，放在服务器 1。 |

创建容器时，必须把服务启动脚本文件夹挂载进容器（例如：`-v /home/examples:/home/examples`）,创建完成后进入容器：

```bash
docker exec -it <容器名> bash
```

## 启动服务

默认情况下，一台服务器仅部署一个混部推理容器，如需**一台服务器部署多个混部推理实例（仅用于小模型）**，请参考[同一容器部署多个推理实例](#同一容器部署多个推理实例)。

1. 获取服务启动命令

   在已准备好`/home/examples`的服务器上执行。该命令只打印启动命令。

   ```bash
   python3 /home/examples/deployer/docker_deploy.py --config_dir /home/examples/config --show-commands --start
   ```

   回显如下：

   ```text
   预计使用 2 台服务器。

   服务器 1：管理面
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --start \
     --container-name motor-ctrl \
     --role coordinator,controller \
     --pod-ip "<本机IP>" \
     --nic-name "<本机网卡>"

   服务器 1：混部 u0
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --start \
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
     --start \
     --container-name motor-u1 \
     --role union \
     --instance-name u1 \
     --pod-ip "<本机IP>" \
     --nic-name "<本机网卡>" \
     --coordinator-ip "<服务器1 IP>" \
     --controller-ip "<服务器1 IP>"
   ```

   开启池化后，回显在服务器 1 增加容器名为`motor-kvs`的 kv_store 命令，管理面和混部命令增加`--kv-store-ip`。

2. 执行服务启动命令

   进入回显中对应的容器，参照以下规则**修改上述启动指令**后执行。每个混部容器仅执行一次。

   | 参数 | 说明 |
   | :--- | :--- |
   | --start | 在当前容器内拉起服务。 |
   | --container-name | 当前容器名，与创建容器时的名称一致。 |
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

开启池化后，再用不少于 128 token 的 prompt 发一次请求。请求成功后，在 kv_store 日志中查看`Keys`，由 0 变为大于 0 即表示 block 已入池。日志路径示例：

```text
/home/examples/motor_workspace/motor-kvs/kvs/log/docker-kv_store-<时间戳>.log
```

## 终止服务与重复部署

在终端执行`Ctrl+C`可终止服务，但不会退出容器。服务终止后会出现如下提示，便于用户重新部署。

```text
容器内服务已终止，可执行以下命令重新部署服务。
python3 /path/to/examples/deployer/docker_deploy.py --config_dir /path/to/examples/infer_engines/vllm --start --container-name motor-u0 --role union --instance-name u0 --pod-ip <本机IP地址> --nic-name <本机主网卡名称>
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

本分支不会通过 `--role render` 启动 Render，管理面 `--start` 也不会重启或停止 Render 容器。需要 Render 时，在部署管理面的服务器上自行创建名为 `motor-ctrl-vllm-render` 的容器。该容器不挂载 NPU，挂载可参考管理面模板 [ENTER_DOCKER_RUN_CTRL](https://gitcode.com/Ascend/MindIE-Motor/blob/all_in_one/examples/deployer/lib/constant.py#L495-L502)。

## 调优指导

### 如何调整容器挂载

创建容器的模板仅供参考。调整挂载时，在`docker run`命令中增删`-v`。云化平台创建容器时，在平台的挂载配置中调整。宿主机路径须真实存在。

### 同一容器部署多个推理实例

一个容器里可以执行多次执行`--start`，以此部署多个推理实例。

下面以 1 个容器部署 2 个混部实例为例，容器挂载卡`0,1,2,3`。混部 `u0`使用`0,1`，混部 `u1`使用`2,3`。

1. 创建容器

   在宿主机执行一次。这个容器挂上两个实例要用的卡。管理面仍单独创建一个容器，按[启动服务](#启动服务)的回显执行。

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --create \
     --container-name motor-hybrid \
     --devices 0,1,2,3    # 这个容器挂载两个实例要用的卡
   ```

2. 进入容器

   ```bash
   docker exec -it motor-hybrid bash
   ```

3. 执行混部 u0 启动命令

   额外添加以下五个参数。

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --start \
     --container-name motor-hybrid \
     --role union \
     --instance-name u0 \
     --pod-ip "<本机IP>" \
     --nic-name "<本机网卡>" \
     --coordinator-ip "<服务器1 IP>" \
     --controller-ip "<服务器1 IP>" \
     --devices 0,1  \                  # 本实例使用的卡号，2 张卡。同一容器里的其他实例不能重叠
     --node-manager-port 4026  \       # NodeManager 端口。同一容器里错开，例如 4026、4226
     --data-parallel-rpc-port 9000  \  # 数据并行 RPC 端口。同一容器里错开，例如 9000、9200
     --base-port 10000  \              # 引擎端点起始端口，会再占用后续一段端口。同一容器里错开，例如 10000、12000
     --kv-port 21000                   # KV 传输起始端口，会再占用后续一段端口。同一容器里错开，例如 21000、23000
   ```

4. 执行混部 u1 启动命令

   开一个新的窗口，进入容器后，执行以下命令，额外添加以下五个参数。

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --start \
     --container-name motor-hybrid \
     --role union \
     --instance-name u1 \
     --pod-ip "<本机IP>" \
     --nic-name "<本机网卡>" \
     --coordinator-ip "<服务器1 IP>" \
     --controller-ip "<服务器1 IP>" \
     --devices 2,3  \                  # 本实例使用的卡号，2 张卡。同一容器里的其他实例不能重叠
     --node-manager-port 4226  \       # NodeManager 端口。同一容器里错开，例如 4026、4226
     --data-parallel-rpc-port 9200  \  # 数据并行 RPC 端口。同一容器里错开，例如 9000、9200
     --base-port 12000  \              # 引擎端点起始端口，会再占用后续一段端口。同一容器里错开，例如 10000、12000
     --kv-port 23000                   # KV 传输起始端口，会再占用后续一段端口。同一容器里错开，例如 21000、23000
   ```
