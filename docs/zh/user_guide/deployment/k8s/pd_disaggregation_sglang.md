# PD分离服务部署（基于SGLang推理引擎）

本文指导开发者基于MindIE Motor的SGLang推理引擎，完成Kubernetes环境下的PD分离部署。

完整部署流程：

1. 获取镜像。

2. 准备配置文件。

3. 服务部署与验证。

下面以DeepSeek-V4-Flash模型、Atlas 800I A3超节点服务器为例，演示部署全流程。

## 获取镜像

- 基本检查

  1. 部署节点已完成[环境准备](../../environment_preparation.md)。

  2. 确保模型权重路径可读取，且NPU卡数满足标准配置要求。

- 下载镜像

  1. 进入[昇腾镜像仓库](https://www.hiascend.com/developer/ascendhub)，打开 **mindie-motor** 镜像仓。

  2. 打开**镜像下载**页签，在右侧搜索框搜索`sglang`，选择镜像版本：`3.2.0-sglang0.5.21-a3-ubuntu22.04-py3.12`。

  3. 按操作系统架构选择ARM或x86对应镜像并下载。

  4. 下载后，请确认Kubernetes集群各工作节点都能使用该镜像。

## 准备配置文件

从MindIE Motor仓库获取本示例的配置文件：

1. 克隆仓库并进入仓库根目录：

   ```bash
   git clone https://gitcode.com/Ascend/MindIE-Motor.git
   cd MindIE-Motor
   ```

2. 配置文件位于`model_configs/sglang/models/deepseek_v4_flash_0731/A3`，即该目录下的`env.json`与`user_config.json`。

3. 按实际环境修改`user_config.json`中的以下字段。

   - image_name：填写上一节下载的镜像名。

   - weight_mount_path：模型权重在容器内的挂载目录。

   - model-path：位于Prefill、Decode的`engine_config`中，填写挂载目录下的模型权重路径。

   - served-model-name：本示例为`deepseek-v4-flash`。

   - job_id：本示例为`mindie-motor-sglang`，后续将作为Kubernetes命名空间。

## 服务部署与验证

以下部署与验证命令在 Kubernetes 集群的 master 节点执行；组件写入的日志、诊断文件和缓存需到对应组件所在节点查看。

1. 获取启动脚本。

   克隆仓库时已包含启动脚本，直接使用即可。脚本位于 `MindIE-Motor/examples/deployer`。

2. 启动服务。

   ```bash
   # namespace 须与 user_config.json 中的 job_id 一致
   kubectl create namespace mindie-motor-sglang

   # --config_dir指定配置文件所在目录
   cd examples/deployer
   python3 deploy.py --config_dir ../../model_configs/sglang/models/deepseek_v4_flash_0731/A3
   ```

3. 查看状态。

   ```bash
   kubectl get pods -n mindie-motor-sglang -o wide
   ```

   | 服务 Pod 名称                    | 说明                |
   | ---------------------------- | ----------------- |
   | mindie-motor-controller-*  | 服务管理              |
   | mindie-motor-coordinator-* | 请求调度与业务入口         |
   | mindie-motor-mf-store                   | KV Cache传输       |
   | sglang-p*                  | Prefill实例        |
   | sglang-d*                  | Decode实例         |
   | mindie-motor-kv-conductor-*             | KV Cache亲和性调度（未开启则不会出现） |

4. 查看日志。

   **查看容器输出**

   ```bash
   # 在 examples/deployer 目录下
   # 将 name_space 改为 mindie-motor-sglang
   vim log_collect/log_config.ini
   # 采集日志并落盘
   bash show_log.sh
   ```

   `show_log.sh` 采集 Pod 容器的标准输出和标准错误，保存在部署端 `examples/deployer/log_collect/log/`。

   **查看组件日志、诊断文件和缓存**

   使用已有 `plog-path` hostPath 日志卷的标准模板部署时，计算 Pod 内各组件写入的日志文件、诊断文件和缓存默认位于其所在宿主机的 `<宿主机日志根>/<模型名>/<service_id>/<Pod名>/<容器名>/`。宿主机日志根默认与 `env.json` 中的 `motor_common_env.MOTOR_LOG_ROOT_PATH` 相同，未配置时为 `/root/ascend/log`；`multi_deployment` 可通过 `logging_config.host_log_dir` 指定宿主机日志根。`service_id` 在环境生成阶段按 `<job_id>_<北京时间YYYYMMDDhhmmss>` 生成。挂载适用范围、组件子目录及控制面目录见[日志与诊断文件目录](../../maintenance/log_directory.md)。

5. 验证服务。

   - node_ip：暴露Coordinator Infer NodePort的节点IP地址。
   - infer_port：user_config.json中的coordinator_infer_node_port。
   - model：engine_config中的served-model-name。

   ```bash
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

   返回`{"detail":"Service is not available"}`：服务尚未就绪，请等待Prefill、Decode与MF Store就绪后重试。

   返回流式JSON：推理正常。更多接口见[业务接口](../../api/service_interfaces.md)。

6. 终止服务。

   ```bash
   # 在 examples/deployer 目录执行，命名空间与 job_id 一致
   bash delete.sh mindie-motor-sglang
   ```
