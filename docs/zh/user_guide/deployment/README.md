# 部署方式说明

推荐部署方案如下，两种方案**均可以基于 Motor 优秀的调度算法获得性能收益**：

1. k8s部署

    推荐已**具备K8s 环境、且对高阶可靠性有较强诉求**的用户使用。

    [基于k8s平台的PD分离服务部署（vLLM）](#k8s-pd-disaggregation)

    [基于k8s平台的PD分离服务部署（SGLang）](#k8s-pd-disaggregation-sglang)

2. docker only部署

    推荐**不具备 K8s 部署环境**的用户使用，该方案仅具备**基础可靠性能力**。

    [基于docker的PD分离服务部署（vLLM）](#docker-pd-disaggregation)

除推荐方案外，Motor还提供了 PD 混部、云平台部署、Slurm 流水线的部署方案，请参考[**全量部署方案**](#all-deployments)。

---

<a id="k8s-pd-disaggregation"></a>

# 基于k8s平台的PD分离服务部署（vLLM）

本文旨在指导开发者基于MindIE Motor的vLLM推理引擎完成Kubernetes环境下的PD分离部署，并提供生产环境配置优化实践指导。

## 前置准备

1. 按照以下要求准备环境。

    - 所有服务器均已完成[环境准备](../environment_preparation.md)。

2. 准备镜像。

    通过以下方式获取镜像，并将镜像加载至K8s集群的所有节点：

    - 方式一：下载官方完整的MindIE Motor镜像

        进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，在搜索框输入`motor`，单击“mindie-motor”卡片，在“镜像下载”页签根据设备型号下载对应的MindIE Motor镜像。

    - 方式二：在已有镜像中安装MindIE Motor

        基础镜像已安装CANN、vLLM、vLLM Ascend等组件，请参考[基于vllm-ascend/sglang镜像安装MindIE Motor](../maintenance/build_motor_image_from_vllm_ascend.md#基于vllm-ascendsglang镜像安装mindie-motor) 额外安装MindIE Motor。

    获取镜像后，请使用以下命令将镜像加载至服务器：

    ```bash
    docker load -i xxxx.tar
    ```

    待镜像导入后，请使用以下命令查看Docker镜像是否存在：

      ```bash
      docker images
      ```

3. 准备服务启动脚本。

    请将examples目录上传至K8s集群的master节点。examples可使用以下两种方式获取：

    - 方式一：下载官方完整的MindIE Motor镜像

        镜像内路径为`/tmp/motor/examples`，可执行以下命令获取：

        ```bash
        # 自定义一个存镜像名或镜像ID的变量IMAGE
        IMAGE="<镜像名或镜像ID>"
        # 创建一个未启动的临时容器，并记录容器ID
        cid=$(docker create "$IMAGE")
        # 把容器中/tmp/motor/examples目录复制到宿主机的 /examples
        docker cp "$cid:/tmp/motor/examples" /examples
        # 删除以上创建的临时容器$cid，清理资源，避免残留无用容器
        docker rm "$cid"
        ```

    - 方式二：在已有镜像中安装MindIE Motor

        git clone代码仓后，启动脚本位于`/examples`目录。

## 配置获取

   1. MindIE Motor已在下表中提供了常用模型的配置示例(deepseek_v4.1_flash、glm 5.2、glm 5.3等)，**用户修改少量配置后可直接使用**。

      <table>
        <tr><th style="font-size:18px" align="center" colspan="4">常用模型</th></tr>
        <tr>
          <th align="left">模型</th>
          <th align="center">Atlas 800I A2</th>
          <th align="center">Atlas 800I A3 超节点</th>
          <th align="center">Atlas 950系列服务器</th>
        </tr>
        <tr><td>DeepSeek-V4.1-Flash </td><td align="center">A2</td><td align="center">A3</td><td align="center">A5</td></tr>
        <tr><td>GLM-5.3-Flash </td><td align="center">A2</td><td align="center">A3</td><td align="center">A5</td></tr>
        <tr><td>GLM-5.2 </td><td align="center">A2</td><td align="center">A3</td><td align="center">A5</td></tr>
        <tr><td>GLM-5.3 </td><td align="center">A2</td><td align="center">A3</td><td align="center">A5</td></tr>
        <tr><td>DeepSeek-V4-Flash </td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_flash/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_flash/A3">A3</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_flash/A5">A5</a></td></tr>
        <tr><td>GLM-5.1 </td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5.1/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5.1/A3">A3</a></td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5.1/A5">A5</a></td></tr>
        <tr><td>DeepSeek-V3.1</td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v3.1/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v3.1/A3">A3</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v3.1/A5">A5</a></td></tr>
        <tr><td>DeepSeek-V4-Pro</td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_pro/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_pro/A3">A3</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_pro/A5">A5</a></td></tr>
        <tr><td>GLM-5</td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5/A3">A3</a></td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5/A5">A5</a></td></tr>
        <tr><td>Qwen3-235B</td><td align="center"><a href="../../../../model_configs/vllm/models/qwen_235b/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/qwen_235b/A3">A3</a></td><td align="center">—</td></tr>
      </table>

      对于未提供典型配置的模型，可参考 [MindIE Motor 配置自动生成指导](../../../../model_configs/vllm/models/README.md)，自动生成配置文件 `user_config.json` 与 `env.json`。

   2. 保存配置至服务器

      进入examples目录下，将所选模型目录下的 user_config.json、env.json 粘贴到./config目录下，再修改后保存：

      ```bash
      cd /examples
      mkdir -p ./config
      # 将所选模型目录下的 user_config.json、env.json 粘贴到./config目录下
      vim ./config/user_config.json
      vim ./config/env.json
      ```

## 服务部署与验证

以下操作均在**K8s集群的master节点**执行。

1. 使用以下命令启动服务。

    ```bash
    # 创建命名空间：<namespace>必须与user_config.json中job_id一致，默认为mindie-motor
    kubectl create namespace <namespace>

    # 进入部署脚本并启动服务
    cd /examples/deployer
    python3 deploy.py --config_dir ../config/
    ```

2. 使用以下命令查看服务状态。

    ```bash
    # 查看Pod状态：<namespace> 与上文job_id一致，默认为mindie-motor
    kubectl get pods -n <namespace> -o wide
    ```

    回显中各Pod/Deployment的命名可能随模板与engine_type变化，可按以下方式识别：

    | 运行时类型 | 说明 |
    | --- | --- |
    | mindie-motor-controller-xxxxx | 服务管理Pod，监控各实例健康状态。 |
    | mindie-motor-coordinator-xxxx | 请求调度Pod，业务请求入口。 |
    | vllm-dx-xxxx | D实例Pod，负责Decode推理。 |
    | vllm-px-xxxx | P实例Pod，负责Prefill推理。 |

    Pod状态为`Running`仅表示已成功调度并启动，是否业务就绪仍需结合日志进一步确认。

3. 使用以下命令查看日志。

    ```bash
    # 编辑 log_collect/log_config.ini：将 name_space 改为与 job_id 相同的名称（默认为mindie-motor）
    vim log_collect/log_config.ini
    # 持续跟踪并收集日志
    bash show_log.sh
    ```

4. 使用以下命令验证服务。

    ```bash
    # 替换占位符：<master_node_ip>为master节点IP，<served_model_name>为user_config.json中served_model_name字段值
    curl -X POST http://<master_node_ip>:31015/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{
        "model": "<served_model_name>",
        "messages": [{"role": "user", "content": "who are you?"}],
        "max_tokens": 36,
        "stream": true
      }'
    ```

    - 返回{"detail":"Service is not available"}：表示服务尚未就绪，多为P/D或Coordinator尚未完全就绪，等待后重试，并查看各Pod日志。

    - 返回流式JSON：推理正常。更多接口请参考[业务接口](../api/service_interfaces.md)。

5. 使用以下命令终止服务。

    ```bash
    # 卸载服务：<namespace>与上文job_id一致，默认为mindie-motor
    bash delete.sh <namespace>
    ```

如需查阅常用特性的开启方式，请参见[PD分离服务部署常用特性使用指导](./k8s/pd_disaggregation_deployment.md#特性配置)。

---

<a id="docker-pd-disaggregation"></a>

# 基于docker的PD分离服务部署（vLLM）

本文档指导用户基于docker组件部署PD分离服务。用户在**宿主机执行一条指令创建容器并部署服务**，但容器的生命周期需要用户手动管理。

## 准备镜像

通过以下方式获取镜像：

- 方式一：下载官方完整的MindIE Motor镜像

    进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，在搜索框输入`motor`，单击“mindie-motor”卡片，在“镜像下载”页签根据设备型号下载对应的MindIE Motor镜像。

- 方式二：在已有镜像中安装MindIE Motor

    基础镜像已安装CANN、vLLM、vLLM Ascend等组件，请参考[基于已有镜像安装MindIE Motor](../maintenance/build_motor_image_from_vllm_ascend.md) 额外安装MindIE Motor。

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

     MindIE Motor已提供常用模型（deepseek_v4_flash、deepseek_v4_pro、GLM 5.1等）的[PD分离配置示例](../../../../model_configs/vllm/models/README.md)，基于模型和服务器类型选择配置示例，并**将配置文件拷贝至/home/examples/config目录**下。

      <table>
        <tr><th style="font-size:18px" align="center" colspan="4">常用模型</th></tr>
        <tr>
          <th align="left">模型</th>
          <th align="center">Atlas 800I A2</th>
          <th align="center">Atlas 800I A3 超节点</th>
          <th align="center">Atlas 950系列服务器</th>
        </tr>
        <tr><td>DeepSeek-V4.1-Flash </td><td align="center">A2</td><td align="center">A3</td><td align="center">A5</td></tr>
        <tr><td>GLM-5.3-Flash </td><td align="center">A2</td><td align="center">A3</td><td align="center">A5</td></tr>
        <tr><td>GLM-5.2 </td><td align="center">A2</td><td align="center">A3</td><td align="center">A5</td></tr>
        <tr><td>GLM-5.3 </td><td align="center">A2</td><td align="center">A3</td><td align="center">A5</td></tr>
        <tr><td>DeepSeek-V4-Flash </td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_flash/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_flash/A3">A3</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_flash/A5">A5</a></td></tr>
        <tr><td>GLM-5.1 </td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5.1/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5.1/A3">A3</a></td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5.1/A5">A5</a></td></tr>
        <tr><td>DeepSeek-V3.1</td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v3.1/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v3.1/A3">A3</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v3.1/A5">A5</a></td></tr>
        <tr><td>DeepSeek-V4-Pro</td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_pro/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_pro/A3">A3</a></td><td align="center"><a href="../../../../model_configs/vllm/models/deepseek_v4_pro/A5">A5</a></td></tr>
        <tr><td>GLM-5</td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5/A3">A3</a></td><td align="center"><a href="../../../../model_configs/vllm/models/glm_5/A5">A5</a></td></tr>
        <tr><td>Qwen3-235B</td><td align="center"><a href="../../../../model_configs/vllm/models/qwen_235b/A2">A2</a></td><td align="center"><a href="../../../../model_configs/vllm/models/qwen_235b/A3">A3</a></td><td align="center">—</td></tr>
      </table>

      对于未提供典型配置的模型，可参考 [MindIE Motor 配置自动生成指导](../../../../model_configs/vllm/models/README.md)，自动生成配置文件 `user_config.json` 与 `env.json`。

3. 保存配置文件

    进入examples目录下，将所选模型目录下的 user_config.json、env.json 粘贴到./config目录下，再修改后保存：

    ```bash
    cd /home/examples
    mkdir -p ./config
    # 将所选模型目录下的 user_config.json、env.json 粘贴到./config目录下
    vim ./config/user_config.json
    vim ./config/env.json
    ```

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

4. <a id="step0003"></a>同步启动脚本配置。

    将整个/home/examples目录，**拷贝至所有服务器的相同路径**下。

## 启动服务

1. 获取服务启动命令

    在任意一台服务器，执行如下命令：

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

    【可选】如果需要**一台服务器部署多个推理容器**，请参考[同一服务器部署多个推理容器](./docker/host/pd_disaggregation_deployment.md#同一服务器部署多个推理容器)部署服务。

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
python3 /home/examples/deployer/docker_deploy.py --config_dir /home/examples/config --start --container-name motor-ctrl --role coordinator,controller --pod-ip <本机IP地址> --nic-name <本机主网卡名称>
运行日志：examples/motor_workspace/motor-ctrl/ctrl/log/docker-coordinator-controller-<时间戳>.log
```

要开启特性（如 KV 池化、Render），或查看调优指导，请参见[基于Docker的多容器PD分离服务部署](./docker/host/pd_disaggregation_deployment.md)。

---

<a id="k8s-pd-disaggregation-sglang"></a>

# PD分离服务部署（基于SGLang推理引擎）

本文指导开发者基于 MindIE Motor 的 SGLang 推理引擎，完成 Kubernetes 环境下的 PD 分离部署。

完整部署流程：1. 获取镜像；2. 准备配置文件；3. 服务部署与验证。

下面以 DeepSeek-V4-Flash 模型、Atlas 800I A3 超节点服务器为例，演示部署全流程。

## 获取镜像

- 基本检查

    1、部署节点已完成[环境准备](../environment_preparation.md)。

    2、确保模型权重路径可读取，且 NPU 卡数满足标准配置要求。

- 下载镜像

    1、进入[昇腾镜像仓库](https://www.hiascend.com/developer/ascendhub)，打开 **mindie-motor** 镜像仓。

    2、打开**镜像下载**页签，在右侧搜索框搜索 `sglang` ，选择镜像版本：`3.2.0-sglang0.5.21-a3-ubuntu22.04-py3.12`。

    3、按操作系统架构选择 ARM 或 x86 对应镜像并下载。

    4、下载后，请确认 Kubernetes 集群各工作节点都能使用该镜像。

## 准备配置文件

从 MindIE Motor 仓库获取本示例的配置文件：

1. 克隆仓库并进入仓库根目录：

    ```bash
    git clone https://gitcode.com/Ascend/MindIE-Motor.git
    cd MindIE-Motor
    ```

2. 配置文件位于 `model_configs/sglang/models/deepseek_v4_flash_0731/A3`，即该目录下的 `env.json` 与 `user_config.json`。

3. 按实际环境修改 `user_config.json` 中的以下字段：

    **`image_name`**：填写上一节下载的镜像名。

    **`weight_mount_path`**：模型权重在容器内的挂载目录。

    **`model-path`**：位于 Prefill、Decode 的 `engine_config` 中，填写挂载目录下的模型权重路径。

    **`served-model-name`**：本示例为 `deepseek-v4-flash`。

    **`job_id`**：本示例为 `mindie-motor-sglang`，后续将作为 Kubernetes 命名空间。

## 服务部署与验证

以下操作均在 Kubernetes 集群的 master 节点执行。

1. 获取启动脚本

    克隆仓库时已包含启动脚本，直接使用即可。脚本位于 `MindIE-Motor/examples/deployer`。

2. 启动服务

    ```bash
    # namespace 须与 user_config.json 中的 job_id 一致
    kubectl create namespace mindie-motor-sglang

    # --config_dir指定配置文件所在目录
    cd examples/deployer
    python3 deploy.py --config_dir ../../model_configs/sglang/models/deepseek_v4_flash_0731/A3
    ```

3. 查看状态

    ```bash
    kubectl get pods -n mindie-motor-sglang -o wide
    ```

    | 服务 Pod 名称                    | 说明                |
    | ---------------------------- | ----------------- |
    | `mindie-motor-controller-*`  | 服务管理              |
    | `mindie-motor-coordinator-*` | 请求调度与业务入口         |
    | `mf-store`                   | KV Cache 传输       |
    | `sglang-p*`                  | Prefill 实例        |
    | `sglang-d*`                  | Decode 实例         |
    | `kv-conductor-*`             | KV 亲和调度（未开启则不会出现） |

4. 查看日志

    ```bash
    # 将 name_space 改为 mindie-motor-sglang
    vim log_collect/log_config.ini
    # 采集日志并落盘
    bash show_log.sh
    ```

5. 验证服务

    ```bash
    # <node_ip>：暴露 Coordinator Infer NodePort 的节点 IP
    # <infer_port>：user_config.json 中的 coordinator_infer_node_port
    # <model>：engine_config 中的 served-model-name
    curl -s http://<node_ip>:<infer_port>/v1/models

    curl -X POST http://<node_ip>:<infer_port>/v1/chat/completions \
      -H "Content-Type: application/json" \
      -d '{
        "model": "<model>",
        "messages": [{"role": "user", "content": "你是谁？"}],
        "max_tokens": 360,
        "stream": true
      }'
    ```

    返回 `{"detail":"Service is not available"}`：服务尚未就绪，请等待 Prefill、Decode 与 MF Store 就绪后重试。

    返回流式 JSON：推理正常。更多接口见[业务接口](../api/service_interfaces.md)。

6. 终止服务

    ```bash
    # 在 examples/deployer 目录执行，命名空间与 job_id 一致
    bash delete.sh mindie-motor-sglang
    ```

---

<a id="all-deployments"></a>

# 全量部署方案

<table cellspacing="0" cellpadding="6" style="border-collapse:collapse;border-spacing:0;">
  <tr><th align="left" width="220">方案</th><th align="left" width="420">额外说明</th><th align="center" width="72">文档</th></tr>
  <tr><th align="left" colspan="3"><strong style="font-weight:700">K8s 部署</strong></th></tr>
  <tr><td align="left" width="220">PD 分离（vLLM）</td><td align="left"></td><td align="center" width="72"><a href="#k8s-pd-disaggregation">链接</a></td></tr>
  <tr><td align="left" width="220">PD 分离（SGLang）</td><td align="left"></td><td align="center" width="72"><a href="#k8s-pd-disaggregation-sglang">链接</a></td></tr>
  <tr><td align="left" width="220">PD 混部</td><td align="left"></td><td align="center" width="72"><a href="./k8s/pd_aggregation_deployment.md">链接</a></td></tr>
  <tr><th align="left" colspan="3"><strong style="font-weight:700">Docker 部署</strong></th></tr>
  <tr><td align="left" width="220">单容器</td><td align="left">1 台服务器、1 个容器内完成 PD 分离，适合权重小于 100GB 的小模型，不依赖 K8s。</td><td align="center" width="72"><a href="./docker/single_container.md">链接</a></td></tr>
  <tr><td align="left" width="220">PD 分离</td><td align="left"></td><td align="center" width="72"><a href="#docker-pd-disaggregation">链接</a></td></tr>
  <tr><td align="left" width="220">PD 混部</td><td align="left"></td><td align="center" width="72"><a href="./docker/host/pd_aggregation_deployment.md">链接</a></td></tr>
  <tr><td align="left" width="220">云平台 PD 分离</td><td align="left">容器已由云平台（例如 ModelArts）创建，进入容器后拉起 PD 分离服务。</td><td align="center" width="72"><a href="./docker/container/pd_disaggregation_deployment.md">链接</a></td></tr>
  <tr><td align="left" width="220">云平台 PD 混部</td><td align="left">容器已由云平台（例如 ModelArts）创建，进入容器后拉起 PD 混部服务。</td><td align="center" width="72"><a href="./docker/container/pd_aggregation_deployment.md">链接</a></td></tr>
  <tr><th align="left" colspan="3"><strong style="font-weight:700">Slurm 部署</strong></th></tr>
  <tr><td align="left" width="220">Slurm</td><td align="left">已有 Slurm 集群，按流水线作业提交并调度推理服务。</td><td align="center" width="72"><a href="./slurm/README.md">链接</a></td></tr>
  <tr><th align="left" colspan="3"><strong style="font-weight:700">调度器独立部署</strong></th></tr>
  <tr><td align="left" width="220">Coordinator 独立部署</td><td align="left"></td><td align="center" width="72"><a href="./standalone.md">链接</a></td></tr>
</table>
