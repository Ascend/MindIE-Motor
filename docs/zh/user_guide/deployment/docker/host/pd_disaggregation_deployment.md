# 基于Docker的多容器PD分离服务部署

本文档指导用户基于docker组件部署PD分离服务。用户在**宿主机执行一条指令创建容器并部署服务**，但容器的生命周期需要用户手动管理。

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

     MindIE Motor已提供常用模型（deepseek_v4_flash、deepseek_v4_pro、GLM 5.1等）的[PD分离配置示例](../../../../../../model_configs/vllm/models/README.md)，基于模型和服务器类型选择配置示例，并**将配置文件拷贝至/home/examples/config目录**下。
     >[!NOTE]说明
     >上述Motor配置仅给出一种可行的模型切分示例，**用户可根据实际情况调整模型划分策略**，关注以下表中参数即可，但必须满足以下公式的限制：
     >
     > 1个P实例占用的NPU卡数 = single_p_instance_pod_num（占用几个pod，跨几机） × p_pod_npu_num （每个pod占用的NPU数）= data_parallel_size × tensor_parallel_size

      | 配置项 | 取值类型 | 取值范围 | 配置说明 |
      | --- | --- | --- | --- |
      | p_instances_num | int | ≥1 | Prefill 实例数量。 |
      | d_instances_num | int | ≥1| Decode 实例数量。 |
      | single_p_instance_pod_num | int | ≥1 | 1 个 P 实例拆成几个 Pod。|
      | single_d_instance_pod_num | int | ≥1 | 1 个 D 实例拆成几个 Pod。 |
      | p_pod_npu_num | int | ≥1，单 Pod 最大 16 卡 | 每个 P Pod 使用的 NPU 卡数。 |
      | d_pod_npu_num | int | ≥1，单 Pod 最大 16 卡 | 每个 D Pod 使用的 NPU 卡数。 |
      | data_parallel_size | int | ≥1 | 数据并行（DP）数 |
      | tensor_parallel_size | int | ≥1 | 张量并行（TP）数 |

3. <a id="step0003"></a>同步启动脚本配置。

    将整个/home/examples目录，**拷贝至所有服务器的相同路径**下。

## 启动服务

1. 获取服务启动命令

    执行如下命令：

    ```bash
    python3 /home/examples/deployer/docker_deploy.py --config_dir /home/examples/config --show-commands
    ```

    回显如下（一台服务器部署一个推理容器）。下例为 2 个 Prefill、2 个 Decode，每个实例 1 个容器、2 张卡：

    ```text
    预计使用 4 台服务器。

    服务器 1：管理面
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-ctrl \
      --role coordinator,controller \
      --pod-ip "<本机IP>" \
      --nic-name "<本机网卡>"

    服务器 1：Prefill p0
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-p0 \
      --role prefill \
      --instance-name p0 \
      --pod-ip "<本机IP>" \
      --nic-name "<本机网卡>" \
      --coordinator-ip "<服务器1 IP>" \
      --controller-ip "<服务器1 IP>"

    服务器 2：Prefill p1
    ...

    服务器 3：Decode d0
    ...

    服务器 4：Decode d1
    ...
    ```

2. 执行服务启动命令

    请参照以下规则**修改上述启动指令**，**在不同服务器的宿主机执行服务启动命令**。

    | 参数 | 说明 |
    | :--- | :--- |
    | --pod-ip | 当前服务器的IP地址。<br>可执行`ip -4 route get 1.1.1.1`命令查看，`src`字段后面是`--pod-ip`值。 |
    | --nic-name | 当前服务器对应的主网卡名。<br>可执行`ip -4 route get 1.1.1.1`命令查看，`dev`字段后面是`--nic-name`值。 |
    | --coordinator-ip/--controller-ip | 服务器 1 的 IP。管理面部署在服务器 1。 |
    | --kv-store-ip | 服务器 1 的 IP。开启池化时 kv_store 也部署在服务器 1，与管理面同机。 |

    【可选】如果需要**一台服务器部署多个推理容器**，请参考[同一服务器部署多个推理容器](#同一服务器部署多个推理容器)部署服务。

---

## 开启KV池化（可选）

不开启池化可跳过本节。

开启后，还需要单独部署一个 kv_store 容器。执行[启动服务](#启动服务)中的 `--show-commands` 后，回显会在服务器 1 增加容器名为 `motor-kvs` 的 kv_store 命令，管理面和 Prefill、Decode 命令增加 `--kv-store-ip`。user_config.json 的池化配置见 [KV池化能力](../../../features/kv_cache_store/README.md)。

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
python3 /path/to/examples/deployer/docker_deploy.py --config_dir /path/to/model_configs/vllm --start --container-name motor-ctrl --role coordinator,controller --pod-ip <本机IP地址> --nic-name <本机主网卡名称>
运行日志：examples/motor_workspace/motor-ctrl/ctrl/log/docker-coordinator-controller-<时间戳>.log
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

### 同一服务器部署多个推理容器

记录[启动服务](#启动服务)里 `--show-commands` 的回显，按下面修改。下面以 1 台 8 卡服务器部署 2 个 Prefill、2 个 Decode 为例。每个实例 1 个容器、2 张卡，卡号依次为 `0,1`、`2,3`、`4,5`、`6,7`。

1. 管理面启动命令

    无需修改，按回显执行。

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-ctrl \
      --role coordinator,controller \
      --pod-ip "<本机IP>" \
      --nic-name "<本机网卡>"
    ```

2. kv_store 启动命令

    未开启池化时，回显没有这条命令。开启池化后无需修改，按回显执行。

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-kvs \
      --role kv_store \
      --pod-ip "<本机IP>" \
      --nic-name "<本机网卡>" \
      --coordinator-ip "<本机IP>" \
      --controller-ip "<本机IP>"
    ```

3. Prefill 推理容器启动命令

    额外添加以下五个参数。

    `motor-p0`：

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-p0 \
      --role prefill \
      --instance-name p0 \
      --pod-ip "<本机IP>" \
      --nic-name "<本机网卡>" \
      --coordinator-ip "<服务器1 IP>" \
      --controller-ip "<服务器1 IP>" \
      --devices 0,1  \                  # 本容器使用的卡号，2 张卡。同一台服务器上不能重叠
      --node-manager-port 4026  \       # NodeManager 端口。同一台服务器上错开，例如 4026、4126
      --data-parallel-rpc-port 9000  \  # 数据并行 RPC 端口。同一台服务器上错开，例如 9000、9100
      --base-port 10000  \              # 引擎端点起始端口，会再占用后续一段端口。同一台服务器上错开，例如 10000、11000
      --kv-port 21000                   # KV 传输起始端口，会再占用后续一段端口。同一台服务器上错开，例如 21000、22000
    ```

    `motor-p1`：

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-p1 \
      --role prefill \
      --instance-name p1 \
      --pod-ip "<本机IP>" \
      --nic-name "<本机网卡>" \
      --coordinator-ip "<服务器1 IP>" \
      --controller-ip "<服务器1 IP>" \
      --devices 2,3  \                  # 本容器使用的卡号，2 张卡。同一台服务器上不能重叠
      --node-manager-port 4126  \       # NodeManager 端口。同一台服务器上错开，例如 4026、4126
      --data-parallel-rpc-port 9100  \  # 数据并行 RPC 端口。同一台服务器上错开，例如 9000、9100
      --base-port 11000  \              # 引擎端点起始端口，会再占用后续一段端口。同一台服务器上错开，例如 10000、11000
      --kv-port 22000                   # KV 传输起始端口，会再占用后续一段端口。同一台服务器上错开，例如 21000、22000
    ```

4. Decode 推理容器启动命令

    额外添加以下五个参数。

    `motor-d0`：

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-d0 \
      --role decode \
      --instance-name d0 \
      --pod-ip "<本机IP>" \
      --nic-name "<本机网卡>" \
      --coordinator-ip "<服务器1 IP>" \
      --controller-ip "<服务器1 IP>" \
      --devices 4,5  \                  # 本容器使用的卡号，2 张卡。同一台服务器上不能重叠
      --node-manager-port 4226  \       # NodeManager 端口。同一台服务器上错开，例如 4226、4326
      --data-parallel-rpc-port 9200  \  # 数据并行 RPC 端口。同一台服务器上错开，例如 9200、9300
      --base-port 12000  \              # 引擎端点起始端口，会再占用后续一段端口。同一台服务器上错开，例如 12000、13000
      --kv-port 23000                   # KV 传输起始端口，会再占用后续一段端口。同一台服务器上错开，例如 23000、24000
    ```

    `motor-d1`：

    ```bash
    python3 /home/examples/deployer/docker_deploy.py \
      --config_dir /home/examples/config \
      --container-name motor-d1 \
      --role decode \
      --instance-name d1 \
      --pod-ip "<本机IP>" \
      --nic-name "<本机网卡>" \
      --coordinator-ip "<服务器1 IP>" \
      --controller-ip "<服务器1 IP>" \
      --devices 6,7  \                  # 本容器使用的卡号，2 张卡。同一台服务器上不能重叠
      --node-manager-port 4326  \       # NodeManager 端口。同一台服务器上错开，例如 4226、4326
      --data-parallel-rpc-port 9300  \  # 数据并行 RPC 端口。同一台服务器上错开，例如 9200、9300
      --base-port 13000  \              # 引擎端点起始端口，会再占用后续一段端口。同一台服务器上错开，例如 12000、13000
      --kv-port 24000                   # KV 传输起始端口，会再占用后续一段端口。同一台服务器上错开，例如 23000、24000
    ```
