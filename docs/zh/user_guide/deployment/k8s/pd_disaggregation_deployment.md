# PD分离服务部署（基于vLLM推理引擎）

本文旨在指导开发者基于MindIE Motor的vLLM推理引擎完成Kubernetes环境下的PD分离部署，并提供生产环境配置优化实践指导。

## 前置准备

1. 按照以下要求准备环境。
   - 所有服务器均已完成[环境准备](../../environment_preparation.md)。
   - 使用RBG后端时，集群须预先安装`RoleBasedGroup`、`RoleInstanceSet`、`RoleInstance`、`RoleBasedGroupScalingAdapter`和`CoordinatedPolicy` CRD及对应controller。启用默认Gang Scheduling时还须安装Volcano。具体预先安装准备参见[RBG部署说明](../../../../../examples/deployer/lib/rbg/README.md)。可执行以下命令确认资源已注册：

     ```bash
     kubectl api-resources | grep -E 'rolebasedgroup|roleinstanceset|roleinstance|coordinatedpolic'
     ```

2. 准备镜像。

   通过以下方式获取镜像，并将镜像加载至K8s集群的所有节点：

   - 方式一：下载官方完整的MindIE Motor镜像

      进入[昇腾官方镜像仓库](https://www.hiascend.com/developer/ascendhub)，在搜索框输入`motor`，单击“mindie-motor”卡片，在“镜像下载”页签根据设备型号下载对应的MindIE Motor镜像。
   - 方式二：在已有镜像中安装MindIE Motor

      基础镜像已安装CANN、vLLM、vLLM Ascend等组件，请参考[基于vllm-ascend/sglang镜像安装MindIE Motor](../../maintenance/build_motor_image_from_vllm_ascend.md#自行制作mindie-motor镜像) 额外安装MindIE Motor。

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
        <tr><td>DeepSeek-V4-Flash </td><td align="center"><a href="../../../../../model_configs/vllm/models/deepseek_v4_flash/A2">A2</a></td><td align="center"><a href="../../../../../model_configs/vllm/models/deepseek_v4_flash/A3">A3</a></td><td align="center"><a href="../../../../../model_configs/vllm/models/deepseek_v4_flash/A5">A5</a></td></tr>
        <tr><td>GLM-5.1 </td><td align="center"><a href="../../../../../model_configs/vllm/models/glm_5.1/A2">A2</a></td><td align="center"><a href="../../../../../model_configs/vllm/models/glm_5.1/A3">A3</a></td><td align="center"><a href="../../../../../model_configs/vllm/models/glm_5.1/A5">A5</a></td></tr>
        <tr><td>DeepSeek-V3.1</td><td align="center"><a href="../../../../../model_configs/vllm/models/deepseek_v3.1/A2">A2</a></td><td align="center"><a href="../../../../../model_configs/vllm/models/deepseek_v3.1/A3">A3</a></td><td align="center"><a href="../../../../../model_configs/vllm/models/deepseek_v3.1/A5">A5</a></td></tr>
        <tr><td>DeepSeek-V4-Pro</td><td align="center"><a href="../../../../../model_configs/vllm/models/deepseek_v4_pro/A2">A2</a></td><td align="center"><a href="../../../../../model_configs/vllm/models/deepseek_v4_pro/A3">A3</a></td><td align="center"><a href="../../../../../model_configs/vllm/models/deepseek_v4_pro/A5">A5</a></td></tr>
        <tr><td>GLM-5</td><td align="center"><a href="../../../../../model_configs/vllm/models/glm_5/A2">A2</a></td><td align="center"><a href="../../../../../model_configs/vllm/models/glm_5/A3">A3</a></td><td align="center"><a href="../../../../../model_configs/vllm/models/glm_5/A5">A5</a></td></tr>
        <tr><td>Qwen3-235B</td><td align="center"><a href="../../../../../model_configs/vllm/models/qwen_235b/A2">A2</a></td><td align="center"><a href="../../../../../model_configs/vllm/models/qwen_235b/A3">A3</a></td><td align="center">—</td></tr>
      </table>

      对于未提供典型配置的模型，可参考 [MindIE Motor 配置自动生成指导](../../../../../model_configs/vllm/models/README.md)，自动生成配置文件 `user_config.json` 与 `env.json`。

   2. 保存配置至服务器

      进入examples目录下，将所选模型目录下的 user_config.json、env.json 粘贴到./config目录下，再修改后保存：

      ```bash
      cd /examples
      mkdir -p ./config
      # 将所选模型目录下的 user_config.json、env.json 粘贴到./config目录下
      vim ./config/user_config.json
      vim ./config/env.json
      ```

### 使用RBG方式部署

在`./config/user_config.json`的`motor_deploy_config`中合并以下字段，其余P/D引擎配置保持不变：

```json
      "motor_deploy_config": {
        "deploy_mode": "rbg",
        "job_id": "mindie-motor",
        "rbg_config": {
          "name": "vllm",
          "logical_service_name": "vllm",
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
| `logical_service_name` | 业务Service的逻辑名称，默认`vllm`。这两个名称均须为不超过63字符的Kubernetes DNS label。 |
| `restart_policy_by_role` | 按引擎Role（此处为`prefill`、`decode`）设置Pod重启时是否重建整个RoleInstance；可选`None`或`RecreateRoleInstanceOnPodRestart`。开启DP域缩容的Role必须使用`None`。 |
| `gang_scheduling` | 是否启用组级Gang Scheduling；首次部署默认`true`，启用时须使用Volcano，且同一RBG中所有启用Role的每实例硬件调度策略必须一致。 |
| `scheduler_name` | Pod调度器名称；首次部署默认`volcano`。仅在关闭Gang Scheduling且没有昇腾硬件调度注解时可用`default-scheduler`。 |
| `coordinated_policy` | 可选的Role协调扩缩容/滚动更新策略；首次部署省略时生成默认同名CoordinatedPolicy。HPA自动扩缩容另由顶层`scaling_policy`配置。 |

这里只叠加RBG专属字段。必须保留所选模型及机型典配中的`hardware_type`、`p_pod_npu_num`、`d_pod_npu_num`、`single_p_instance_pod_num`、`single_d_instance_pod_num`及引擎并行参数，不能统一改成示例值。省略`gang_scheduling`和`scheduler_name`时，首次部署默认启用Volcano Gang Scheduling。启用Gang Scheduling时，同一RBG中所有启用Role的每实例硬件调度策略必须一致；对生成不同`sp-block`规格的典配，渲染校验将失败。

| 机型 | RBG配置要求 |
|------|-------------|
| A2（`800I_A2`） | 保留A2典配中的8卡/Pod及实例Pod数。可使用默认Volcano；如集群不使用Gang Scheduling，可显式配置`gang_scheduling: false`和`scheduler_name: default-scheduler`。 |
| A3（`800I_A3`） | 保留A3典配中的NPU数和实例Pod数。RBG会生成`sp-block`硬件调度注解，必须使用`scheduler_name: volcano`。如Prefill与Decode生成的每实例`sp-block`规格不同（例如分别为16和32），默认Gang Scheduling会在渲染阶段校验失败。可关闭Gang以通过该项校验，但仍须结合PodGroup及调度插件条件确认硬件调度是否可用。 |
| A5（`Ascend950`） | 保留A5典配中的NPU数、实例Pod数及A5网络配置。RBG会生成`sp-block`和A5硬件调度注解，必须使用`scheduler_name: volcano`。如各启用Role生成的每实例硬件调度策略不同，默认Gang Scheduling会在渲染阶段校验失败。 |

当前不支持选择超节点亲和调度策略；如需将Prefill或Decode部署到指定节点，可分别通过`prefill_node_selector`或`decode_node_selector`配置Kubernetes `nodeSelector`。

RBG方式不支持ScaleP2D和容器快照。启用Gang Scheduling时所有启用的Role须使用相同的scheduler和每实例硬件调度策略，且不能配置Role依赖。开启DP域缩容的角色必须将`restart_policy_by_role`配置为`None`。完整前置条件和限制参见[RBG部署说明](../../../../../examples/deployer/lib/rbg/README.md)。

## 服务部署与验证

以下部署与验证命令在**K8s集群的master节点**执行；组件写入的日志、诊断文件和缓存需到对应组件所在节点查看。

1. 使用以下命令启动服务。

    ```bash
    # 创建命名空间：<namespace>必须与user_config.json中job_id一致，默认为mindie-motor
    kubectl create namespace <namespace>

    # 进入部署脚本并启动服务
    cd /examples/deployer
    # 可先增加--dry-run，仅生成并检查资源清单
    python3 deploy.py --config_dir ../config/ --dry-run
    python3 deploy.py --config_dir ../config/
    ```

2. 使用以下命令查看服务状态。

   ```bash
   # 查看Pod状态：<namespace> 与上文job_id一致，默认为mindie-motor
   kubectl get pods -n <namespace> -o wide
   ```

   RBG方式还须确认RoleBasedGroup、RoleInstance、Service和协调策略均已创建：

   ```bash
   kubectl get rolebasedgroup,roleinstance,coordinatedpolicy -n <namespace>
   kubectl get pods,svc -n <namespace> -o wide
   kubectl describe rolebasedgroup vllm -n <namespace>
   ```

   若配置了顶层`scaling_policy`，还应检查HPA及ScalingAdapter：

   ```bash
   kubectl get rolebasedgroupscalingadapter,hpa -n <namespace>
   ```

   回显中各Pod/Deployment的命名可能随模板与engine_type变化，可按以下方式识别：

   | 运行时类型 | 说明 |
   | --- | --- |
   | vllm-0-motor-controller-xxxxx | 服务管理Pod，监控各实例健康状态。 |
   | vllm-0-motor-coordinator-xxxx | 请求调度Pod，业务请求入口。 |
   | vllm-0-kv-store-0-xxxx | 共享KV Cache Pod，供 P/D 实例复用（不参与推理计算）。 |
   | vllm-dx-xxxx | D实例Pod，负责Decode推理。 |
   | vllm-px-xxxx | P实例Pod，负责Prefill推理。 |

   Pod状态为`Running`仅表示已成功调度并启动，是否业务就绪仍需结合日志进一步确认。

3. 查看日志。

   **查看容器输出**

   ```bash
   # 在 examples/deployer 目录下
   # 编辑 log_collect/log_config.ini：将 name_space 改为与 job_id 相同的名称（默认为mindie-motor）
   vim log_collect/log_config.ini
   # 持续跟踪并收集日志
   bash show_log.sh
   ```

   `show_log.sh` 采集 Pod 容器的标准输出和标准错误，保存在部署端 `examples/deployer/log_collect/log/`。

   **查看组件日志、诊断文件和缓存**

   使用已有 `plog-path` hostPath 日志卷的标准模板部署时，计算 Pod 内各组件写入的日志文件、诊断文件和缓存默认位于其所在宿主机的 `<宿主机日志根>/<模型名>/<service_id>/<Pod名>/<容器名>/`。宿主机日志根默认与 `env.json` 中的 `motor_common_env.MOTOR_LOG_ROOT_PATH` 相同，未配置时为 `/root/ascend/log`；`multi_deployment` 可通过 `logging_config.host_log_dir` 指定宿主机日志根。`service_id` 在环境生成阶段按 `<job_id>_<北京时间YYYYMMDDhhmmss>` 生成。挂载适用范围、组件子目录及控制面目录见[日志与诊断文件目录](../../maintenance/log_directory.md)。

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
   - 返回流式JSON：推理正常。更多接口请参考[业务接口](../../api/service_interfaces.md)。

5. 使用以下命令终止服务。

   ```bash
   # 卸载服务：<namespace>与上文job_id一致，默认为mindie-motor
   bash delete.sh <namespace>
   ```

   RBG手动扩缩容仍使用`deploy.py --update_instance_num`；自动扩缩容通过顶层`scaling_policy`生成HPA并启用对应Role的ScalingAdapter。详细配置和停用方法分别参见[手动扩缩容](../../features/scaling_deployment/manual_scaling.md)和[自动弹性扩缩容](../../features/scaling_deployment/auto_scaling.md#rbg自动扩缩容)。

---

## 特性配置

[配置获取](#配置获取)中获取的user_config.json与env.json是基础部署配置。若要开启或调整主备倒换、异常实例重启、服务限流、KV 亲和性调度、KV 池化等能力，可参照本节内容做最小配置修改。

### 主备倒换

创建多个Controller和Coordinator并运行在不同的服务器上，当某一台服务器出现异常（宕机、重启、维护升级），部署于其他服务器的Controller和Coordinator组件将接管集群，避免单个服务器异常导致业务请求无法调度。

**特性原理**

创建多个管理面和业务面的Pod，分布于多台服务器。当主Pod异常时，选择一套备份Pod承担当前业务。对外表现上整个服务集群没有任何异常。

**开启特性**

>[!NOTE] 说明
>须提前部署ETCD（服务端至少需要3个副本，以保证ETCD集群的可靠性）。该特性详细文档请参考[主备倒换特性](../../features/fault_tolerance/standby.md)。

  ```json
  "motor_controller_config": {
    "standby_config": { "enable_master_standby": true }
  },
  "motor_coordinator_config": {
    "standby_config": { "enable_master_standby": true }
  }
  ```

**关闭特性**

删除standby_config参数配置块，或将enable_master_standby参数设为：false。

### 异常实例重启

P/D实例出现异常时，重启推理实例，避免实例长时间处于异常状态，影响集群推理功能。

**特性原理**

推理主进程异常退出等情况时，Motor 自动重启对应实例。重启期间吞吐能力下降，重启后恢复正常。
**开启特性**

默认开启，无需额外操作。

**关闭特性**

该特性不支持关闭。

### 服务限流

限制推理入口Coordinator在单位时间内的最大请求数，防止服务过载。

**特性原理**

Coordinator记录一段时间内接收到的请求数量，达到阈值后停止接收外部请求。

**开启特性**

  ```json
  "motor_coordinator_config": {
    "rate_limit_config": {
      "enable_rate_limit": true,
      "max_requests": 10000,
      "window_size": 60
    }
  }
  ```

上述配置表示60秒内最多处理10000条请求。

**关闭特性**

删除rate_limit_config参数配置块，或将enable_rate_limit参数设置为：false。

>[!NOTE] 说明
>该特性详细文档请参考[服务限流](../../features/fault_tolerance/rate_limiting.md)。字段说明请参考 [user_config.json配置文件全量参数说明](../../configuration/config_reference.md#motor_coordinator_config)中的rate_limit_config字段。

### 虚拟推理健康探测

NodeManager负责原生引擎的就绪探测、进程监管和虚推健康探测：

- **就绪探测**：轮询原生引擎业务端口的`/health`，在startup_timeout窗口内保持STARTING，成功后才允许调度。
- **进程存活**：监管原生引擎进程组，主进程或工作进程异常退出时触发实例恢复。
- **vLLM虚推**：对DP0实例发送轻量级推理请求，并结合AI Cube利用率识别静默故障；达到失败阈值后降级状态，但不直接终止进程。
- **SGLang 健康探测**：使用SGLang原生生成式`GET /health`，不创建Motor虚推monitor。
- **软件故障**：引擎提供`/fault_tolerance/status`时，可由EngineFtManager补充软件故障上报。

**开启特性**

  ```json
  "motor_engine_prefill_config": {
    "health_check_config": {
      "enable_virtual_inference": true,
      "npu_usage_threshold": 10,
      "health_collector_timeout": 5,
      "health_collector_timeout_retry_attempts": 3,
      "startup_timeout": 1800
    }
  }
  ```

**关闭特性**

将enable_virtual_inference参数设为`false`，不会关闭SGLang原生生成式`GET /health`。

**日志级别限制**

vLLM虚推仅允许在最终引擎环境ASCEND_GLOBAL_LOG_LEVEL=3（未配置时默认为ERROR）时开启。该特性详细文档请参考[虚拟推理健康探测](../../features/fault_tolerance/sim_inference.md)。

### KV Cache亲和调度

将具有相同前缀的请求调度到同一实例，复用已有KV Cache，减少Prefill耗时。

**特性原理**

MindIE Motor的KV Cache亲和性调度能力依赖 Mooncake 社区的 Mooncake Conductor 组件，允许调度器根据 KV Cache 位置优先将请求调度到缓存了对应 KV 的实例，从而减少 KV Cache 跨实例传输开销，提升推理吞吐与响应速度。

**开启特性**

- motor_coordinator_config字段：开启KV Cache亲和性调度。
- motor_engine_prefill_config字段：开启KV事件发布、开启prefix cache特性。
- kv_conductor_config字段：kv conductor配置，与motor_engine_prefill_config处于同一层级。

```json
"motor_coordinator_config": {
  "scheduler_config": { "scheduler_type": "kv_cache_affinity" }
},
"motor_engine_prefill_config": {
  "engine_config": {
    "kv-events-config": {
      "publisher": "zmq",
      "enable_kv_cache_events": true,
      "endpoint": "tcp://*:5557",
      "topic": "kv-events",
      "replay_endpoint": "tcp://*:6667"
    },
    "enable-prefix-caching": true
  }
},
"kv_conductor_config": {
  "kvevent_instance": { "mooncake_master": { "type": "Mooncake" } },
  "http_server_port": 13333
}
```

**关闭特性**

删除上述配置项。

>[!NOTE] 说明
>需要确保镜像中已安装KV Conductor组件，该特性详细文档请参考[KV Cache亲和性调度](../../features/inference_scheduling/kvcache_affinity.md)。

### PD异构（PR/DT）调度

Ascend 950PR&950DT系列产品分为PR、DT两类，两者的内存存在差异，DT的内存带宽更大、性能更好，更适合部署Decode；Prefill可部署在PR上。因此**PR + DT 组网**时，需要把P实例调度到PR节点，D实例调度到DT节点。

**特性原理**

在PD分离的Prefill/Decode Pod上追加`huawei.com/npu.chip.name`到`nodeSelector`，与节点上已有的芯片标签匹配。

**特性约束**

开启后Prefill只能调度到PR节点、Decode只能调度到DT节点，同角色恢复/扩容也只能使用对应类型的空闲节点。

| 特性 | PR+DT 异构 | 说明 |
|------|------------|------|
| ScaleP2D（缩P保D） | 不支持 | 停掉P后释放的是PR节点，D仍只能调度到DT，无法占用这些节点完成恢复。 |
| MindCluster实例重调度、容器快照默认重调度 | 受限 | P只能再调度到空闲PR，D只能再到空闲DT，对应类型没有空闲节点时无法恢复。 |
| 自动弹性扩缩容 | 受限 | 只能按角色在PR/DT上分别增减，不能靠缩P把PR资源转给D扩容。扩P需要空闲PR，扩D需要空闲DT。 |
| 手动扩缩容 | 受限 | 只能按角色在PR/DT上分别增减，不能靠缩P把PR资源转给D扩容。扩P需要空闲PR，扩D需要空闲DT。 |
| 异常实例原地重启、引擎重拉 | 支持 | 仍在原Pod/原节点，不换卡类型。 |
| Controller/Coordinator主备倒换 | 支持 | 管理面不使用PR/DT的`nodeSelector`。 |
| Coordinator故障场景请求重调度 | 支持 | 将请求转到其他健康实例，不改变P/D的节点类型绑定。 |
| 故障隔离、虚推健康探测、token 级重推 | 支持 | 不依赖跨PR/DT占节点。 |
| KV Cache亲和调度、KV池化、服务限流、Tracing | 支持 | 与卡类型调度无关。 |

建议在PR+DT组网下保持`enable_scale_p2d`为`false`。实例重调度与弹性扩缩容仅在对应类型节点有空闲容量时按原能力工作。

**开启特性**

在Prefill/Decode各自配置中填写对应字段；不填则不加该条`nodeSelector`。

  ```json
  "motor_engine_prefill_config": {
    "npu_chip_name": "Ascend950PR"
  },
  "motor_engine_decode_config": {
    "npu_chip_name": "Ascend950DT"
  }
  ```

**关闭特性**

删除上述字段或不填。

>[!NOTE] 说明
>仅Kubernetes PD分离生效。`hardware_type`为`Ascend950`时基础`nodeSelector`为`accelerator: huawei-npu`；芯片名标签是额外一条，全PR或全DT组网无需填写。基于Docker部署无调度器，按[基于Docker的多容器PD分离服务部署指导](../docker/host/pd_disaggregation_deployment.md#启动服务)的打印结果部署即可（即Prefill命令放到 PR上，Decode命令放到DT上）。

### KV池化能力

通过MultiConnector将KV缓存卸载到共享池，支持跨实例复用，降低显存压力。

**特性原理**

允许P/D实例通过KV缓存池共享KV Cache，P实例将计算好的KV Cache推入缓存池，D实例从缓存池拉取并复用，从而在PD分离场景下提升显存利用率和推理吞吐。

**开启特性**

该特性详细文档请参考[KV池化能力](../../features/inference_scheduling/kv_cache_store/README.md)。

**关闭特性**

改用非MultiConnector的单一connector，并删除kv_cache_pool_config参数配置块。
