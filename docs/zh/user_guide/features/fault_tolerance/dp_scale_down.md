# DP 域缩容

## 特性介绍

DP 域缩容（减卡缩容）用于在大 EP、多 DP 推理实例内，当部分 DP rank 对应 NPU 或引擎进程不可恢复时，由 Motor 协调存活 rank 在**不整实例重启**的前提下降低有效 DP 规模并继续服务。

Motor 负责故障收集、缩容门禁（FtGate）、向 NodeManager 下发 FT 指令以及实例调度视图更新；vLLM / vLLM-Ascend 负责在引擎内执行 rank 移除、通信组重建与 EPLB 重分配。二者通过 `/v1/fault_tolerance/apply` 与 `/v1/fault_tolerance/status` 协作。

### 工作原理

1. NodeManager 轮询原生引擎 FT 状态，并结合 MindCluster ConfigMap、节点状态上报硬件/软件故障；Controller FaultManager 在收集窗口内汇总各 DP rank 证据。
2. 满足 DP 缩容条件且 FtGate 通过时，FaultManager 选择 `DpScaleDownStrategy`，将待移除的**原始 DP rank** 列表下发到各幸存 NodeManager，由引擎异步完成缩容。
3. 缩容提交后，Motor 在 `execution_deadline_sec` 内轮询幸存引擎直至 FT 状态恢复为 HEALTHY；失败或超时则进入整实例重配置/重调度等既有恢复路径。
4. 成功后实例进入 `SCALED_DOWN_RUNNING` 阶段，Coordinator 按更新后的 DP 视图继续调度；若开启 `enable_dp_scale_up`，且某 Pod 内全部 DP rank 均已移除，Motor 会停止对应 NodeManager，由集群回收空 Pod 资源。

### 约束与限制

| 约束维度 | 说明 |
| -------- | ---- |
| 硬件 | 仅 **Atlas 800I A3 超节点**；典型场景为大 EP、多 Pod 多 DP 部署。缩容依赖 MC2 / MegaMoE Mask 容错，当前仅在 A3 形态验证。 |
| 推理引擎 | 仅 **vLLM** 在开启引擎侧 `enable_fault_tolerance` 时具备 DP 域缩容执行能力；SGLang 不支持。**Prefill 角色不支持**本特性。引擎侧 `user_config` / `env.json` 及版本组合等**详细约束见下表「引擎使用约束」**。 |
| Motor 开关 | `motor_controller_config.fault_tolerance_config.enable_fault_tolerance` 与 `enable_dp_scale_down` 须同时为 true；关闭总 FT 开关时 DP 缩容不执行。 |
| 部署方式 | 推荐 K8s + Infer Operator / InferServiceSet。启用 DP 缩容且 Decode/Union 开启引擎 FT 时，Deployer 为推理 Pod 生成 `fault-scheduling: external-force-pod-failed` 与 `pod-rescheduling: on`，详见[实例重调度](./instance_rescheduling.md)。 |
| 特性互斥 | 与[容器快照](../container_snapshot.md)不可叠加；引擎 `fault_tolerance_config.auto_recovery` 须为 false（与 DP 缩容互斥，FtGate 校验）。 |
| 业务连续性 | 缩容执行期间实例会短暂摘流；成功后以较小 DP 规模继续服务，需评估可接受的最小 DP 数与专家冗余。 |

#### 引擎使用约束

下表为 Decode / Union 引擎及配套 `env.json` 须满足的条件。

| 序号 | 使用约束 | 相关配置 / 环境 | 补充说明 |
| ---- | -------- | --------------- | -------- |
| 1 | **Motor 与引擎 FT 开关** | Controller：`enable_fault_tolerance`、`enable_dp_scale_down` 为 true；**Decode 或 Union** 的 `engine_config.enable_fault_tolerance` 为 true | Prefill 不支持 DP 域缩容 |
| 2 | **权重加载与量化** | 仅 vLLM 默认 `DefaultModelLoader`；**仅 W8A8**；删除 MTP / speculative 配置 | 转测示例可用 DSV4-Flash-W8A8；自定义加载器（如 runai_streamer）暂不支持 |
| 3 | **不支持 PCP** | `prefill_context_parallel_size` = 1，`enable_dsa_cp` = false | — |
| 4 | **部署角色与 CUDA Graph** | 仅 **Decode / Union**；`max_num_batched_tokens` ≤ `cudagraph_capture_sizes` 最大值 | 示例：`max_num_batched_tokens`: 32，`cudagraph_capture_sizes`: [4,8,12,16,20,24,28,32] |
| 5 | **外部负载均衡** | `data_parallel_external_lb` = true，`data_parallel_size_local` = 1 | 每 API Server 仅一个 Engine |
| 6 | **冗余专家** | `enable_eplb` = true，`eplb_config.num_redundant_experts` 足够大 | 可按「单 DP rank 专家槽位数 × 预期移除次数」估算 |
| 7 | **本地通信资源版本** | 在 `env.json` 的 **`motor_common_env`** 中配置 `"ASCEND_LOCAL_COMM_RES": "{\"version\":\"1.2\"}"` | 由 Deployer 注入各组件 Pod 的公共环境；缺失或版本不匹配可能导致缩容阶段通信资源异常 |
| 8 | **与 TASK_QUEUE 互斥** | `env.json` 的 `motor_engine_decode_env`（PD 混部为 `motor_engine_union_env`）中 `TASK_QUEUE_ENABLE` = 0 | TASK_QUEUE 异常退出会导致后续 Device 操作卡死 |
| 9 | **与 HCCL 异步错误处理互斥** | 同上，`HCCL_ASYNC_ERROR_HANDLING` = 0 | HCCL 看门狗可能在集合通信异常时直接杀进程 |
| 10 | **DP 同步走 CPU 通信组** | 开启容错后由框架自动设置 | DP 同步仅 Gloo（CPU），不能依赖 HCCL |
| 11 | **硬件超时代答** | A3 超节点默认约 23s | 可能偶发 Motor 缩容超时后走[实例重调度](./instance_rescheduling.md) |
| 12 | **故障感知与超时配平** | `cpu_distributed_timeout_seconds`、`additional_config` 中 FT 通信超时；`motor_engine_*_env` 中 `HCCL_EXEC_TIMEOUT`（示例 29） | 空闲 DP 无流量时感知慢。**正常缩容恢复小于 30s**；感知过长时最长约 **1 分钟**后走重调度；须同步评估 `execution_deadline_sec` |

#### RAS 能力边界（特性叠加）

| 粒度 | 能力 | 与 DP 域缩容叠加 |
| ---- | ---- | ---------------- |
| 服务级 | RAS-Monitor | 支持 |
| 实例级 | [虚推健康探测](../sim_inference.md) | 支持 |
| 实例级 | 引擎重拉（Engine Relaunch） | 支持 |
| 实例级 | [实例重调度](./instance_rescheduling.md) | 支持 |
| 实例级 | PD 分离降级混部 | 支持 |
| 实例级 | [ScaleP2D 故障恢复](./scale_p2d.md) | 支持 |
| 实例级 | [主备倒换](./standby.md) | 支持 |
| 实例级 | [D2D 权重加载](../startup_acceleration.md) | 支持 |
| 实例级 | [容器快照](../container_snapshot.md) | **不支持** |
| 请求级 | [请求重调度](./rescheduler.md) | 支持 |
| 请求级 | 故障实例熔断 | 支持 |
| 高阶 RAS | [精度检测](../precision_detection.md) | 支持 |

## 特性使用

### 环境准备

- 已按 [K8s PD 分离部署](../../deployment/README.md#k8s-pd-disaggregation) 或 **Decode/Union** PD 混部完成 Motor 服务部署；集群节点为 **A3 超节点**，Controller、Coordinator 与各推理 Pod 正常注册。
- 目标实例使用3.2.0版本motor镜像拉起，模型为 **W8A8**、默认权重加载器，且**未启用 MTP**；引擎已按[引擎使用约束](#引擎使用约束)配置 `user_config` 与 `env.json`（见[配置示例](#配置示例)）。
- `env.json` 中在 **`motor_common_env`** 配置 `ASCEND_LOCAL_COMM_RES`（见引擎约束第 7 条）；`TASK_QUEUE_ENABLE`、`HCCL_ASYNC_ERROR_HANDLING`、`HCCL_EXEC_TIMEOUT` 等写入 **`motor_engine_decode_env`** 或 **`motor_engine_union_env`**（见第 8–12 条）。
- 若镜像内引擎尚未包含 DP 缩容相关实现，需在容器启动、执行 `engine.sh` **之前**应用v3.2.0版本 Motor deployer 提供的容错补丁。补丁合入后路径为：

  ```bash
  # 在 deployer/startup/boot.sh 中、source engine.sh 之前插入
  bash /path/to/deployer/patch/0.30.0/apply_patches.sh \
      /path/to/deployer/patch/0.30.0/fault_tolerance/vllm_v0.30.0_ft.patch \
      /path/to/deployer/patch/0.30.0/fault_tolerance/vllm_ascend_v0.30.0_ft.patch
  ```

  将 `/path/to/deployer` 替换为容器内 `examples/deployer` 的实际挂载路径。可通过环境变量 `PYTHON` 指定用于 `pip show` 定位安装目录的解释器，默认 `python3`。若同时使用容器快照补丁，须**先应用容错补丁，再应用快照补丁**。

- 集群具备 MindCluster 故障管理与（可选）Infer Operator 外部调度能力；缩容后空 Pod 回收依赖 `enable_dp_scale_up` 与集群调度器行为。

### 配置示例

DP 域缩容的 Controller 开关集中在 `motor_controller_config.fault_tolerance_config`；**不需要**在 `motor_engine_prefill_config` / `motor_engine_decode_config` 顶层重复 Motor 特性开关。引擎侧须在 **Decode 或 Union** 的 `engine_config` 中开启 vLLM 原生 FT 及缩容依赖项；**Prefill 不支持**本特性。

**Controller 侧**

```json
"motor_controller_config": {
  "fault_tolerance_config": {
    "enable_fault_tolerance": true,
    "fault_collection_timeout_seconds": 60,
    "enable_dp_scale_down": true,
    "enable_dp_scale_up": false,
    "dp_scale_down_config": {
      "request_timeout_sec": 10.0,
      "poll_interval_sec": 1.0,
      "execution_deadline_sec": 30
    }
  }
}
```

**引擎侧（Decode 示例，字段名可用 kebab-case）**

```json
"motor_engine_decode_config": {
  "engine_type": "vllm",
  "engine_config": {
    "enable_fault_tolerance": true,
    "data_parallel_external_lb": true,
    "data_parallel_size_local": 1,
    "prefill_context_parallel_size": 1,
    "enable_dsa_cp": false,
    "max_num_batched_tokens": 32,
    "cudagraph_capture_sizes": [4, 8, 12, 16, 20, 24, 28, 32],
    "enable_eplb": true,
    "eplb_config": {
      "num_redundant_experts": 128
    },
    "cpu_distributed_timeout_seconds": 30,
    "additional_config": {
      "ft_communication_abort_timeout": 30
    },
    "fault_tolerance_config": {
      "auto_recovery": false
    }
  }
}
```

**env.json（节选）**

与 `user_config.json` 一并交给 Deployer。`ASCEND_LOCAL_COMM_RES` 须写在 **`motor_common_env`**；角色相关变量写在 **`motor_engine_decode_env`**（PD 混部为 **`motor_engine_union_env`**）。

```json
{
  "motor_common_env": {
    "ASCEND_LOCAL_COMM_RES": "{\"version\":\"1.2\"}"
  },
  "motor_engine_decode_env": {
    "TASK_QUEUE_ENABLE": 0,
    "HCCL_ASYNC_ERROR_HANDLING": 0,
    "HCCL_EXEC_TIMEOUT": 29
  }
}
```

勿配置 MTP（如 `speculative_config` / `speculative-config` 的 `method: mtp`）。`num_redundant_experts` 须按模型与预期连续缩容次数规划。`data_parallel_size` 须大于 1 才有缩容意义。`env.json` 字段说明见各[部署指南](../../deployment/README.md)与 `model_configs` 典配。更多参数见 [user_config.json 配置参考](../../configuration/config_reference.md#motor_controller_config) 与 [motor_engine_prefill_config / motor_engine_decode_config](../../configuration/config_reference.md#motor_engine_prefill_configmotor_engine_decode_config)。

| 参数 | 说明 |
| ---- | ---- |
| `enable_dp_scale_down` | Controller 统一控制 DP 缩容，默认 false。 |
| `enable_dp_scale_up` | 缩容成功后是否在 Pod 内全部 DP rank 移除时停止 NodeManager 以回收空 Pod，默认 false；关闭时保留空 Pod，不影响卡级缩容结果。 |
| `dp_scale_down_config.request_timeout_sec` | Controller 调用 NodeManager FT 接口超时，须为 NodeManager 到引擎的默认 5 秒请求留出余量。 |
| `dp_scale_down_config.poll_interval_sec` | 等待引擎完成缩容的轮询间隔。 |
| `dp_scale_down_config.execution_deadline_sec` | Motor 侧等待引擎缩容恢复的最长时间；超时进入原故障恢复流程。 |

部署后，可通过下列命令确认推理 Pod 标签（仅当 Controller DP 缩容与对应角色引擎 FT 同时开启时生成）：

```bash
kubectl get pod -n {namespace} \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.fault-scheduling}{"\t"}{.metadata.labels.pod-rescheduling}{"\n"}{end}'
```

### 验证特性

> [!WARNING] 警告
> 故障注入与 DP 缩容验证会中断目标实例上的请求，请在测试环境进行，并保证仍有足够幸存 DP 容量。

1. 记录目标实例 ID 与各 Pod 的 DP 布局（可通过 Controller 实例查询或部署配置确认）。
2. 向Decode实例注入pkill -9 VLLM::Worker_DP 进程。
3. 查询 Controller DP 缩容运行态（管理面端口 `controller_api_port`，默认 `1026`）：

   ```bash
   curl "http://{controller-ip}:{port}/controller/fault_tolerance/status"
   curl "http://{controller-ip}:{port}/controller/fault_tolerance/status?instance_id={id}"
   ```

   响应中的 `phase` 会经历 `DECIDING`、`SCALING_DOWN`、`POST_SCALE_CLEANUP`、`SCALED_DOWN_RUNNING` 等状态；`enable_dp_scale_down` 为 false 时返回 `DISABLED`。
4. 观察 Controller / NodeManager 日志中 `DpScaleDownStrategy` 与 `scale-down` 相关记录，确认 `removed_dp_ranks` 与幸存 rank 符合预期。
5. 缩容完成后向 Coordinator 发送推理请求，确认实例在较小 DP 规模下仍可服务；若启用[虚推健康探测](../sim_inference.md)，NodeManager 会在 DP master 变更后迁移虚推 monitor。

## 常见问题

### FtGate 拒绝缩容

日志或运行态 `last_error` 中出现 `scale-down gate rejected`，常见 `reason_codes` 如下：

| reason_code | 含义 | 处理建议 |
| ----------- | ---- | -------- |
| `FEATURE_DISABLED` | Controller 未开启 `enable_dp_scale_down` 或总 FT 关闭 | 检查 `motor_controller_config.fault_tolerance_config`。 |
| `ENGINE_FT_REQUIRED` | 实例未声明引擎 FT 能力 | 在对应角色 `engine_config` 开启 `enable_fault_tolerance` 并重新部署。 |
| `EXTERNAL_LB_REQUIRED` | 未开启 external-lb | 设置 `data_parallel_external_lb` / `data-parallel-external-lb` 为 true。 |
| `AUTO_RECOVERY_CONFLICT` | 引擎 FT 自动恢复与 DP 缩容互斥 | 将引擎 `fault_tolerance_config.auto_recovery` 设为 false。 |
| `ENGINE_SCALE_DOWN_UNSUPPORTED` | 非 vLLM 或未开启 FT | 确认 `engine_type` 为 vLLM 且 FT 已启用。 |
| `EPLB_REQUIRED` | 未开启 EPLB | 设置 `enable_eplb` 并配置 `eplb_config`。 |
| `EXPERT_REDUNDANCY_REQUIRED` | 专家冗余数为 0 | 将 `eplb_config.num_redundant_experts` 设为大于 0。 |

### 缩容超时或回退整实例恢复

`execution_deadline_sec` 内引擎 FT 状态未回到 HEALTHY 时，Motor 会标记失败并进入 `RECONFIGURING` 等回退路径。对齐结论要求**正常缩容恢复时间小于 30 秒**；若因 A3 硬件超时代答（默认约 23s）、空闲 DP 无流量导致感知滞后或算子超时配平不当，可能偶发超时并走实例重调度。可适当增大 `execution_deadline_sec`（上限 600），并与 `cpu_distributed_timeout_seconds`、`ft-communication-abort-timeout`、`HCCL_EXEC_TIMEOUT` 一并评估；确认已应用 0.30.0 容错补丁。

### 与手动/自动实例扩缩容的区别

- **手动/ HPA 扩缩容**：修改 `p_instances_num` / `d_instances_num` 或 HPA replicas，增减的是 **Prefill/Decode 实例 Pod 个数**。
- **DP 域缩容**：同一实例内减少 **DP rank**；InferServiceSet replicas 不变，除非开启 `enable_dp_scale_up` 后集群回收空 Pod。

### 与容器快照同时开启

容器快照与 DP 域扩缩容互斥。若业务需要快照加速重调度，请勿在同一服务上开启 `enable_dp_scale_down`；参见[容器快照](../container_snapshot.md)约束表。
