# 基于云平台的PD分离服务部署

本文档**专用于云平台场景（例如ModelArts平台）的Motor部署方案指导**，如果无需云平台进行容器管理，请参考[宿主机部署docker服务指导](../host/pd_disaggregation_deployment.md)。在云平台部署中，业务容器由用户自行创建，并在容器中启动Motor主进程。

## 准备镜像

1. 通过以下方式获取镜像：

   - 方式一：下载官方完整的MindIE Motor镜像

     进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，在搜索框输入`motor`，单击“mindie-motor”卡片，在“镜像下载”页签根据设备型号下载对应的MindIE Motor镜像。

   - 方式二：在已有镜像中安装MindIE Motor

     基础镜像已安装CANN、vLLM、vLLM Ascend等组件，请参考[基于已有镜像安装MindIE Motor](../../../maintenance/build_motor_image_from_vllm_ascend.md) 额外安装MindIE Motor。

2. 获取镜像后，请使用以下命令**将镜像加载至每一台服务器**：

   ```bash
   docker load -i xxxx.tar
   ```

## 准备服务启动脚本

在任意一台服务器上，执行以下操作：

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

   MindIE Motor已在[常用模型配置连接](../../README.md#常用模型)中提供了常用模型的配置示例(deepseek_v4.1_flash、glm 5.2、glm 5.3等)，请选择与模型及硬件匹配的配置示例，修改少量配置后可直接使用。对于未提供典型配置的模型，可参考 [MindIE Motor 配置自动生成指导](../../../../../../model_configs/vllm/models/README.md)，自动生成配置文件 `user_config.json` 与 `env.json`。

   - 开启池化功能（可选）

     请参考[KV池化能力](../../../features/inference_scheduling/kv_cache_store/README.md)文档来修改user_config.json配置文件。并在创建容器小节中额外部署一个KV_store容器。

   - 开启Render功能（可选）

     请在 `/home/examples/config/user_config.json` 中增加以下配置：

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

     并在创建容器小节中额外部署一个Render容器。

   - 开启KV亲和功能（可选）

     请参考 [KV Cache亲和性调度](../../../features/inference_scheduling/kvcache_affinity.md)文档来修改user_config.json配置文件。并在创建容器小节中额外部署一个KV Conductor容器。

3. 保存配置文件。

   进入examples目录下，将所选模型目录下的 user_config.json、env.json 粘贴到./config目录下，再修改后保存：

   ```bash
   cd /home/examples
   mkdir -p ./config
   # 将所选模型目录下的 user_config.json、env.json 粘贴到./config目录下
   vim ./config/user_config.json
   vim ./config/env.json
   ```

   >[!NOTE] 说明
   >上述Motor配置仅给出一种可行的模型切分示例，**用户可根据实际情况调整模型划分策略**，关注以下表中参数即可，但必须满足以下公式的限制：
   >
   >1个P实例占用的NPU卡数 = single_p_instance_pod_num（占用几个pod，跨几机） × p_pod_npu_num（每个pod占用的NPU数）= data_parallel_size × tensor_parallel_size

   | 配置项 | 取值类型 | 取值范围 | 配置说明 |
   | --- | --- | --- | --- |
   | p_instances_num | int | ≥1 | Prefill 实例数量。 |
   | d_instances_num | int | ≥1 | Decode 实例数量。 |
   | single_p_instance_pod_num | int | ≥1 | 1 个 P 实例拆成几个 Pod。 |
   | single_d_instance_pod_num | int | ≥1 | 1 个 D 实例拆成几个 Pod。 |
   | p_pod_npu_num | int | ≥1，单 Pod 最大 16 卡 | 每个 P Pod 使用的 NPU 卡数。 |
   | d_pod_npu_num | int | ≥1，单 Pod 最大 16 卡 | 每个 D Pod 使用的 NPU 卡数。 |
   | data_parallel_size | int | ≥1 | 数据并行（DP）数 |
   | tensor_parallel_size | int | ≥1 | 张量并行（TP）数 |

4. <a id="step0003"></a>同步启动脚本配置。

   将整个/home/examples目录，**拷贝至所有服务器的相同路径**下。

## 查看部署计划

1. 在任意服务器，执行以下命令。

   ```bash
   python3 /home/examples/deployer/docker_deploy.py --config_dir /home/examples/config --show-commands --start
   ```

2. 回显示例如下：

   「1、创建容器」代表容器规划，请按照该指引清单在对应服务器创建容器，对应操作请参考[创建容器](#创建容器)小节的指引。

   「在各个容器中执行以下启动命令」代表在各个容器启动服务的命令，对应操作请参考[启动服务](#启动服务)小节的指引。

   ```output
   容器内部署。先在对应服务器创建容器，再进入该容器执行启动命令。
   本命令只打印计划，不创建容器，也不启动服务。

   预计使用 2 台服务器。

   1、创建容器：

   服务器 1：
   - motor-ctrl：管理面容器。
   - motor-kvs：池化后端。
   - motor-ctrl-vllm-render：Render分词器。
   - motor-kvc：KV Cache索引。
   - motor-p0：Prefill推理容器。

   服务器 2：
   - motor-d0：Decode推理容器。

   在各个容器中执行以下启动命令。

   服务器 1：
     创建管理面容器 motor-ctrl
     在容器内执行以下启动命令
     python3 /home/examples/deployer/docker_deploy.py \
       --config_dir /home/examples/config \
       --start \
       --container-name motor-ctrl \
       --role coordinator,controller \
       --pod-ip "<本机IP>" \
       --nic-name "<本机网卡>" \
       --kv-store-ip "<本机IP>" \
       --kv-conductor-ip "<KV Conductor所在服务器IP>" \
       --render-ip "<Render所在服务器IP>"

   服务器 1：
     创建kv_store容器 motor-kvs
     在容器内执行以下启动命令
     python3 /home/examples/deployer/docker_deploy.py \
       --config_dir /home/examples/config \
       --start \
       --container-name motor-kvs \
       --role kv_store \
       --pod-ip "<本机IP>" \
       --nic-name "<本机网卡>" \
       --coordinator-ip "<本机IP>" \
       --controller-ip "<本机IP>" \
       --kv-conductor-ip "<KV Conductor所在服务器IP>"

   服务器 1：
     创建Render容器 motor-ctrl-vllm-render
     在容器内执行以下启动命令
     python3 /home/examples/deployer/docker_deploy.py \
       --config_dir /home/examples/config \
       --start \
       --container-name motor-ctrl-vllm-render \
       --role render

   服务器 1：
     创建KV Conductor容器 motor-kvc
     在容器内执行以下启动命令
     python3 /home/examples/deployer/docker_deploy.py \
       --config_dir /home/examples/config \
       --start \
       --container-name motor-kvc \
       --role kv_conductor \
       --pod-ip "<本机IP>" \
       --nic-name "<本机网卡>" \
       --coordinator-ip "<服务器1 IP>" \
       --controller-ip "<服务器1 IP>"

   服务器 1：
     创建Prefill p0容器 motor-p0
     在容器内执行以下启动命令
     python3 /home/examples/deployer/docker_deploy.py \
       --config_dir /home/examples/config \
       --start \
       --container-name motor-p0 \
       --role prefill \
       --instance-name p0 \
       --pod-ip "<本机IP>" \
       --nic-name "<本机网卡>" \
       --coordinator-ip "<服务器1 IP>" \
       --controller-ip "<服务器1 IP>" \
       --kv-store-ip "<服务器1 IP>" \
       --kv-conductor-ip "<KV Conductor所在服务器IP>"

   服务器 2：
     创建Decode d0容器 motor-d0
     在容器内执行以下启动命令
     python3 /home/examples/deployer/docker_deploy.py \
       --config_dir /home/examples/config \
       --start \
       --container-name motor-d0 \
       --role decode \
       --instance-name d0 \
       --pod-ip "<本机IP>" \
       --nic-name "<本机网卡>" \
       --coordinator-ip "<服务器1 IP>" \
       --controller-ip "<服务器1 IP>" \
       --kv-store-ip "<服务器1 IP>" \
       --kv-conductor-ip "<KV Conductor所在服务器IP>"
   ```

   `motor-kvc` 不要填写 `--instance-name`。开启 KV 亲和后，管理面、kv_store、Prefill 和 Decode 都要填写 `--kv-conductor-ip`，地址是实际创建 `motor-kvc` 的服务器。开启 Render 后，管理面要填写 `--render-ip`。管理面的 `--start` 和退出不会启动或停止这两个容器。

## 创建容器

按照容器创建计划，在对应服务器上创建以下容器：

| 容器类型 | 参考模板 | 配置说明 |
| --- | --- | --- |
| 管理容器 | [ENTER_DOCKER_RUN_CTRL](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/lib/constant.py#L611-L622) | 容器名`motor-ctrl`，只需创建一个。所在服务器以回显为准。 |
| P/D实例推理容器 | Atlas 800I A2：[ENTER_DOCKER_RUN_A2](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/lib/constant.py#L448-L480)<br>Atlas 800I A3：[ENTER_DOCKER_RUN_A3](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/lib/constant.py#L482-L522)<br>Ascend 950（8卡）：[ENTER_DOCKER_RUN_A5](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/lib/constant.py#L524-L561)<br>Ascend 950（16卡）：[ENTER_DOCKER_RUN_A5_POD16](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/lib/constant.py#L563-L609) | 1个实例只占1个容器时，容器名为`motor-p0`、`motor-d0`。1个实例拆成多个容器时，容器名为`motor-p0-0`、`motor-p0-1`。每台服务器仅创建一个容器。 |
| kv_store（可选） | [ENTER_DOCKER_RUN_KVS](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/lib/constant.py#L624-L638) | 回显列出 `motor-kvs` 时创建，只需一个。所在服务器以回显为准。 |
| Render（可选） | [ENTER_DOCKER_RUN_CTRL](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/lib/constant.py#L611-L622) | 回显列出 `motor-ctrl-vllm-render` 时创建，只需一个。所在服务器以回显为准。 |
| KV Conductor（可选） | [ENTER_DOCKER_RUN_CTRL](https://gitcode.com/Ascend/MindIE-Motor/blob/master/examples/deployer/lib/constant.py#L611-L622) | 回显列出 `motor-kvc` 时创建，只需一个。所在服务器以回显为准。 |

创建时请满足以下条件：

- 将宿主机`/home/examples`按相同路径挂载进容器：`-v /home/examples:/home/examples`。权重、驱动、日志目录按模板挂载，宿主机路径须真实存在。

## 启动服务

按照部署计划，进入对应容器执行启动命令，并按下表修改参数。

**表 1** 容器内部署命令参数解释

| 参数 | 说明 |
| :--- | :--- |
| --pod-ip | 当前服务器的IP地址。<br>可执行`ip -4 route get x.x.x.x`命令查看，`src`字段后面是`--pod-ip`值。 |
| --nic-name | 当前服务器对应`--pod-ip`的网卡名。<br>可执行`ip -4 route get x.x.x.x`命令查看，`dev`字段后面是`--nic-name`值。 |
| --coordinator-ip/--controller-ip | Coordinator/Controller所在服务器的IP。部署推理服务、kv_store、kv_conductor时填写。分容器部署管理服务时，Controller填写`--coordinator-ip`，Coordinator填写`--controller-ip`。 |
| --kv-store-ip | kv_store所在服务器的IP地址。开启池化后，部署管理面（coordinator/controller）和Prefill/Decode时必填。`--role kv_store`可省略，默认使用`--pod-ip`。 |
| --kv-conductor-ip | KV Conductor 所在服务器的 IP。开启 KV 亲和后，管理面、kv_store、Prefill 和 Decode 必填，按实际创建 `motor-kvc` 的机器修改。`--role kv_conductor` 不填，使用 `--pod-ip`。 |
| --render-ip | Render 所在服务器的 IP。开启 Render 后，管理面必填，按实际创建 Render 容器的机器修改。 |

上述命令执行后，窗口将启动对应服务，用户在屏幕上可观察到日志打印。

默认情况下，一台服务器仅部署一个Prefill/Decode推理容器，如需**一台服务器部署多个P/D推理实例（仅用于小模型）**，请参考[同一容器部署多个推理实例](#同一容器部署多个推理实例)。

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
python3 /home/examples/deployer/docker_deploy.py --config_dir /home/examples/config --start --container-name motor-ctrl --role coordinator,controller --pod-ip <本机IP地址> --nic-name <本机主网卡名称>
运行日志：examples/motor_workspace/motor-ctrl/ctrl/log/docker-coordinator-controller-<时间戳>.log
```

## 调优指导

### 如何调整容器挂载

创建容器的模板仅供参考。调整挂载时，在`docker run`命令中增删`-v`。云化平台创建容器时，在平台的挂载配置中调整。宿主机路径须真实存在。

### 同一容器部署多个推理实例

一个容器里可以多次执行`--start`，以此部署多个推理实例。

下面以1个容器部署1个Prefill和1个Decode为例，容器挂载卡`0,1,2,3`。Prefill `p0`使用`0,1`，Decode `d0`使用`2,3`。

1. 创建容器。

   在宿主机执行一次。这个容器挂上两个实例要用的卡。管理面仍单独创建一个容器，按[查看部署计划](#查看部署计划)的回显执行。

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --create \
     --container-name motor-pd \
     --devices 0,1,2,3    # 这个容器挂载两个实例要用的卡
   ```

2. 进入容器。

   ```bash
   docker exec -it motor-pd bash
   ```

3. 执行Prefill 启动命令。

   额外添加以下五个参数：

   - --devices：本实例使用的卡号，同一容器里的其他实例不能重叠。
   - --node-manager-port：NodeManager端口，同一容器里错开，例如：4026、4226。
   - --data-parallel-rpc-port：数据并行RPC端口。同一容器里错开，例如：9000、9200。
   - --base-port：引擎端点起始端口，会再占用后续一段端口。同一容器里错开，例如：10000、12000。
   - --kv-port：KV传输起始端口，会再占用后续一段端口。同一容器里错开，例如：21000、23000。

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --start \
     --container-name motor-pd \
     --role prefill \
     --instance-name p0 \
     --pod-ip "<本机IP>" \
     --nic-name "<本机网卡>" \
     --coordinator-ip "<服务器1 IP>" \
     --controller-ip "<服务器1 IP>" \
     --devices 0,1  \
     --node-manager-port 4026  \
     --data-parallel-rpc-port 9000  \
     --base-port 10000  \
     --kv-port 21000
   ```

4. 执行Decode 启动命令。

   开一个新的窗口，进入容器后，执行以下命令，额外添加以下五个参数：

   - --devices：本实例使用的卡号，同一容器里的其他实例不能重叠。
   - --node-manager-port：NodeManager端口，同一容器里错开，例如：4026、4226。
   - --data-parallel-rpc-port：数据并行RPC端口。同一容器里错开，例如：9000、9200。
   - --base-port：引擎端点起始端口，会再占用后续一段端口。同一容器里错开，例如：10000、12000。
   - --kv-port：KV传输起始端口，会再占用后续一段端口。同一容器里错开，例如：21000、23000。

   ```bash
   python3 /home/examples/deployer/docker_deploy.py \
     --config_dir /home/examples/config \
     --start \
     --container-name motor-pd \
     --role decode \
     --instance-name d0 \
     --pod-ip "<本机IP>" \
     --nic-name "<本机网卡>" \
     --coordinator-ip "<服务器1 IP>" \
     --controller-ip "<服务器1 IP>" \
     --devices 2,3  \
     --node-manager-port 4226  \
     --data-parallel-rpc-port 9200  \
     --base-port 12000  \
     --kv-port 23000
   ```
