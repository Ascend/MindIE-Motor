# 基于Docker的单容器服务部署

本文档指导用户基于Docker在单机上完成推理服务的部署，1台服务器、1个容器内完成整个PD分离的部署，适用于小模型（权重小于100GB）场景，该部署方式不依赖K8s。大模型或多机部署可参见[宿主机部署：PD 分离](host/pd_disaggregation_deployment.md)。KV池化需要单独的kv_store容器，请使用宿主机部署并参考[开启KV池化（可选）](./host/pd_disaggregation_deployment.md#开启kv池化可选)。

>[!NOTE] 说明
> 单容器部署中，Prefill与Decode在同1个容器、同1台机器上，不支持 Ascend 950PR 与 950DT 混合组网。混合组网请使用[宿主机部署：PD 分离](./host/pd_disaggregation_deployment.md#启动服务)，Prefill 命令放到 950PR 上，Decode 命令放到 950DT 上。

## 部署流程示意图

![部署流程示意图](../../../imgs/docker_deploy_flow.png)

**首次部署**：

- 部署前需要准备部署脚本和配置文件；
- 执行“容器创建命令”，系统会自动创建容器并进入容器内部；
- 系统在容器内自动执行部署服务，随后等待服务部署完毕；
- 若用户通过Ctrl+C终止服务，不会退出容器，而是回到可重新部署的状态。

**重复部署**：

- 在容器已存在的前提下，用户可直接执行部署命令；
- 系统再次等待服务部署完毕，完成新一轮部署。

## 镜像准备

通过以下方式获取镜像，并加载到部署服务器：

- 方式一：下载官方完整的MindIE Motor镜像

  进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，在搜索框输入`motor`，单击“mindie-motor”卡片，在“镜像下载”页签根据设备型号下载对应的MindIE Motor镜像。
- 方式二：在已有镜像中安装MindIE Motor

  基础镜像已安装CANN、vLLM、vLLM Ascend等组件，请参考[基于vllm-ascend/sglang镜像安装MindIE Motor](../../maintenance/build_motor_image_from_vllm_ascend.md#基于vllm-ascendsglang镜像安装mindie-motor) 额外安装MindIE Motor。

获取镜像后，请使用以下命令将镜像加载至服务器：

   ```bash
   docker load -i xxxx.tar
   ```

待镜像导入后，请使用以下命令查看Docker镜像是否存在：

   ```bash
   docker images
   ```

## 准备服务部署脚本

获取服务部署脚本`examples` 目录：

- 方式一：下载官方完整的MindIE Motor镜像

     镜像内路径为`/tmp/motor/examples`，可执行以下命令获取：

     ```bash
     # 自定义一个存镜像名或镜像ID的变量IMAGE
     IMAGE="<镜像名或镜像ID>"
     # 创建一个未启动的临时容器，并记录容器ID
     cid=$(docker create "$IMAGE")
     # 把容器中/tmp/motor/examples目录复制到宿主机的当前目录
     docker cp "$cid:/tmp/motor/examples" ./examples
     # 删除以上创建的临时容器$cid，清理资源，避免残留无用容器
     docker rm "$cid"
     ```

- 方式二：在已有镜像中安装MindIE Motor

     git clone代码仓后，部署脚本位于`MindIE-Motor/examples`目录。

## 配置服务化参数

1. 准备配置文件。

    MindIE Motor已提供常用模型（deepseek_v4_flash、deepseek_v4_pro、GLM 5.1等）的[PD分离配置示例](../../../../../examples/infer_engines/vllm/models/README.md)，用户修改少量配置后可直接使用。

    对于未提供典型配置的模型，请参考[MindIE Motor配置自动生成指导](../../../../../examples/infer_engines/vllm/models/README.md)，自动生成user_config.json与env.json配置文件。

2. 配置端口与部署模式。

   单容器里管理面和推理进程共用host网络，需要改部署模式并错开端口，否则会冲突，参数详细说明请参考[user_config.json配置文件全量参数说明](../../configuration/config_reference.md)：

   ```json
   {
     "motor_deploy_config": {
       "deploy_mode": "single_container",
       ...
     },
     ...
     "motor_controller_config": {
       "api_config": {
         "controller_api_port": 2026,
         "observability_api_port": 2027
       }
     },
     ...
     "motor_engine_prefill_config": {
       "motor_nodemanger_config": {
         "api_config": { "node_manager_port": 3026 }
       },
       ...
     },
     "motor_engine_decode_config": {
       "motor_nodemanger_config": {
         "api_config": { "node_manager_port": 4026 }
       },
       ...
     }
   }
   ```

   deploy_mode：必须为single_container。

3. 同步部署脚本配置。

    将准备好的配置文件（user_config.json、env.json）存放在 examples/infer_engines/vllm 目录下。

## 启用Render特性（可选）

本分支的 `docker_deploy.py` 不会自动创建 `{NAME}-vllm-render`，`--start` 也不会重启 Render。需要 Render 时请自行创建该容器。

同名主容器已经存在时，在终端执行部署命令会询问是否删除后继续。这一步在端口检查之前，避免把上次容器占用的端口当成新的冲突。回答 `y` 会删除该容器再重新创建；回答 `N`，或在非交互环境执行，则中止且不删除。

## 启动服务

在部署服务器`examples/deployer`目录下执行以下命令启动服务：

```bash
python3 docker_deploy.py --config_dir ../infer_engines/vllm \
  --container-name motor-single --devices 0,1 \
  --pod-ip <本机 IP地址> --nic-name <本机主网卡名称>
```

上述命令执行后，窗口将自动创建容器、进入容器以及启动服务，用户在屏幕上可观察到日志打印。`--devices`需覆盖Prefill和Decode使用的卡。

**表 1** 启动参数说明

| 参数 | 说明 |
| :--- | :--- |
| --config_dir | user_config.json和env.json配置文件所在目录。 |
| --container-name | 容器名，可自定义。 |
| --devices | 本容器挂载的NPU卡号，如`0,1`。如不填写，默认挂载服务器所有卡。 |
| --pod-ip | 当前服务器的IP地址。<br>可执行`ip -4 route get 1.1.1.1`命令查看，`src`字段后面是`--pod-ip`值。 |
| --nic-name | 当前服务器对应`--pod-ip`的网卡名。<br>可执行`ip -4 route get 1.1.1.1`命令查看，`dev`字段后面是`--nic-name`值。 |

## 推理验证

在本机发送如下推理请求，即可进行推理验证：

```bash
curl -X POST http://127.0.0.1:1025/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<模型名称，必须和user_config.json文件中的配置一致>",
    "messages": [{"role": "user", "content": "who are you?"}],
    "max_tokens": 36,
    "stream": true
  }'
```

若返回以下内容，则表示服务尚未就绪，可稍后重试。

```json
{"detail":"Service is not available"}
```

## 终止服务与重复部署

在终端执行**Ctrl+C**可终止服务，但不会退出容器。服务终止后会出现如下提示，便于用户重新部署。

```text
容器内服务已终止，可执行以下命令重新部署服务。
python3 /path/to/examples/deployer/docker_deploy.py --config_dir /path/to/examples/infer_engines/vllm --start --container-name motor-single --pod-ip <本机 IP地址> --nic-name <本机主网卡名称>
运行日志：examples/motor_workspace/motor-single/single/log/docker-single-<时间戳>.log
```

用户可直接复制上述指令重新部署服务。
