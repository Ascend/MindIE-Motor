# PD分离服务部署（基于SGLang推理引擎）

本文指导开发者基于 MindIE Motor 的 SGLang 推理引擎，完成 Kubernetes 环境下的 PD 分离部署。

完整部署流程：1. 获取镜像；2. 准备配置文件；3. 服务部署与验证。

下面以 DeepSeek-V4-Flash 模型、Atlas 800I A3 超节点服务器为例，演示部署全流程。

## 获取镜像

- 基本检查

  1、部署节点已完成[环境准备](../../environment_preparation.md)。

  2、确保模型权重路径可读取，且 NPU 卡数满足标准配置要求。

  3、使用RBG后端时，集群须预先安装`RoleBasedGroup`、`RoleInstanceSet`、`RoleInstance`、`RoleBasedGroupScalingAdapter`和`CoordinatedPolicy` CRD及对应controller。启用默认Gang Scheduling时还须安装Volcano。具体预先安装准备参见[RBG部署说明](../../../../../examples/deployer/lib/rbg/README.md)。可执行以下命令确认资源已注册：

  ```bash
  kubectl api-resources | grep -E 'rolebasedgroup|roleinstanceset|roleinstance|coordinatedpolic'
  ```

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

### 使用RBG方式部署

在`user_config.json`的`motor_deploy_config`中合并以下字段，其余SGLang Prefill、Decode和MF Store配置保持不变：

```json
   "motor_deploy_config": {
     "deploy_mode": "rbg",
     "job_id": "mindie-motor-sglang",
     "rbg_config": {
       "name": "sglang",
       "logical_service_name": "sglang",
       "restart_policy_by_role": {
         "prefill": "RecreateRoleInstanceOnPodRestart",
         "decode": "RecreateRoleInstanceOnPodRestart"
       }
     }
   }
```

`rbg_config`字段说明（完整配置参见[配置参数说明](../../configuration/config_reference.md#motor_deploy_config)）：

| 字段 | 含义 |
|------|------|
| `name` | RoleBasedGroup资源名；未配置时使用`logical_service_name`，两者均未配置时最终使用`vllm`。 |
| `logical_service_name` | 业务Service的逻辑名称，默认`vllm`。这两个名称均须为不超过63字符的Kubernetes DNS label；本示例均显式设为`sglang`。 |
| `restart_policy_by_role` | 按引擎Role（此处为`prefill`、`decode`）设置Pod重启时是否重建整个RoleInstance；可选`None`或`RecreateRoleInstanceOnPodRestart`。开启DP域缩容的Role必须使用`None`。 |
| `gang_scheduling` | 是否启用组级Gang Scheduling；首次部署默认`true`，启用时须使用Volcano。 |
| `scheduler_name` | Pod调度器名称；首次部署默认`volcano`。当前A3示例有硬件调度注解，必须使用`volcano`。 |
| `coordinated_policy` | 可选的Role协调扩缩容/滚动更新策略；首次部署省略时生成默认同名CoordinatedPolicy。HPA自动扩缩容另由顶层`scaling_policy`配置。 |

这里只叠加RBG专属字段。必须保留所选模型及机型典配中的`hardware_type`、每Pod NPU数、实例Pod数、引擎并行参数和MF Store配置，不能统一改成示例值。省略`gang_scheduling`和`scheduler_name`时，首次部署默认启用Volcano Gang Scheduling。RBG会创建Controller、Coordinator、Prefill、Decode和MF Store等Role。

当前SGLang示例使用A3（`800I_A3`）。RBG会生成`sp-block`硬件调度注解，因此必须使用`scheduler_name: volcano`，不能改成`default-scheduler`。当前不支持选择超节点亲和调度策略；如需将Prefill或Decode部署到指定节点，可分别通过`prefill_node_selector`或`decode_node_selector`配置Kubernetes `nodeSelector`。若后续使用A2或A5典配，同样应保留对应目录下的机型资源和网络参数；A5也必须使用Volcano。

RBG方式不支持ScaleP2D和容器快照。启用Gang Scheduling时所有启用的Role须使用相同的scheduler，且不能配置Role依赖。完整前置条件和限制参见[RBG部署说明](../../../../../examples/deployer/lib/rbg/README.md)。

## 服务部署与验证

以下部署与验证命令在 Kubernetes 集群的 master 节点执行；组件写入的日志、诊断文件和缓存需到对应组件所在节点查看。

1. 获取启动脚本

   克隆仓库时已包含启动脚本，直接使用即可。脚本位于 `MindIE-Motor/examples/deployer`。

2. 启动服务

   ```bash
   # namespace 须与 user_config.json 中的 job_id 一致
   kubectl create namespace mindie-motor-sglang

   # --config_dir指定配置文件所在目录
   cd examples/deployer
   # 可先增加--dry-run，仅生成并检查资源清单
   python3 deploy.py --config_dir ../../model_configs/sglang/models/deepseek_v4_flash_0731/A3 --dry-run
   python3 deploy.py --config_dir ../../model_configs/sglang/models/deepseek_v4_flash_0731/A3
   ```

   所有部署后端（含 RBG）在启动和 `--dry-run` 预览前均会执行[部署前安全预检](README.md#deployment-security-precheck)。仅在隔离测试环境需要沿用现有配置时，可在原命令末尾追加 `--skip-check`。

3. 查看状态

   ```bash
   kubectl get pods -n mindie-motor-sglang -o wide
   ```

   RBG方式还需检查RoleBasedGroup和RoleInstance：

   ```bash
   kubectl get rolebasedgroup,roleinstance,coordinatedpolicy -n mindie-motor-sglang
   kubectl get pods,svc -n mindie-motor-sglang -o wide
   kubectl describe rolebasedgroup sglang -n mindie-motor-sglang
   ```

   若配置了顶层`scaling_policy`，还应检查HPA及ScalingAdapter：

   ```bash
   kubectl get rolebasedgroupscalingadapter,hpa -n mindie-motor-sglang
   ```

   | 服务 Pod 名称                    | 说明                |
   | ---------------------------- | ----------------- |
   | `mindie-motor-controller-*`  | 服务管理              |
   | `mindie-motor-coordinator-*` | 请求调度与业务入口         |
   | `mindie-motor-mf-store`                   | KV Cache 传输       |
   | `sglang-p*`                  | Prefill 实例        |
   | `sglang-d*`                  | Decode 实例         |
   | `mindie-motor-kv-conductor-*`             | KV 亲和调度（未开启则不会出现） |

4. 查看日志

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

   启用推理 TLS 时，请按[HTTPS 验证方式](README.md#verify-inference-tls)使用 HTTPS 地址和 `curl --cacert`。以下 HTTP 示例适用于推理 TLS 关闭的隔离测试环境。

   - node_ip：暴露Coordinator Infer NodePort的节点IP地址。
   - infer_port：user_config.json中的coordinator_infer_node_port。
   - model：engine_config中的served-model-name。

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

   返回流式 JSON：推理正常。更多接口见[业务接口](../../api/service_interfaces.md)。

6. 终止服务

   ```bash
   # 在 examples/deployer 目录执行，命名空间与 job_id 一致
   bash delete.sh mindie-motor-sglang
   ```

## RBG扩缩容

手动扩缩容时，修改`p_instances_num`或`d_instances_num`后执行：

```bash
python3 deploy.py \
  --config_dir ../../model_configs/sglang/models/deepseek_v4_flash_0731/A3 \
  --update_instance_num
```

若命令提示安全预检未通过，请参见[部署前安全预检](README.md#deployment-security-precheck)完成安全配置。仅在隔离测试环境需要沿用现有配置时，可在原命令末尾追加 `--skip-check`。

RBG方式会更新在线RoleBasedGroup中对应Role的`replicas`。若目标Role已存在deployer管理的HPA或RoleBasedGroupScalingAdapter，脚本会拒绝手动扩缩容。完整操作参见[手动扩缩容](../../features/scaling_deployment/manual_scaling.md)。

自动扩缩容通过`user_config.json`顶层的`scaling_policy`生成HPA并启用对应Role的ScalingAdapter，配置、停用及验证方法参见[自动弹性扩缩容](../../features/scaling_deployment/auto_scaling.md#rbg自动扩缩容)。可通过以下命令检查资源：

```bash
kubectl get rolebasedgroupscalingadapter,hpa -n mindie-motor-sglang
kubectl describe hpa -n mindie-motor-sglang
```
