# Prefill Context Parallel (PCP) 与跨节点 PCP

## 概述

**Prefill Context Parallel (PCP)** 是将长序列的 prefill 计算切分到多个设备上并行执行的策略，用于支持超长上下文场景。PyMotor 完整集成了 PCP 以及跨节点的 PCP 编排能力。

- **单节点 PCP**：PCP rank 全部在同一节点的多张 NPU 上，PyMotor 通过 `engine_config` 中的 `prefill-context-parallel-size` 参数自动将配置透传给 vLLM 引擎。
- **跨节点 PCP**：PCP rank 分布在多个节点的 NPU 上，PyMotor 自动化处理节点注册、主从分配、通信地址注入等全流程。

> 关于 vLLM Ascend 跨节点 PCP 的底层原理与参数说明，请参考 vLLM 社区文档：
> 👉 **[长序列上下文并行的多节点部署](https://docs.vllm.ai/projects/vllm-ascend-cn/zh-cn/latest/tutorials/features/long_sequence_context_parallel_multi_node.html)**

## 跨节点 PCP 在 PyMotor 中的使用

### 用户配置

用户只需在 `engine_config` 中添加 `nnodes` 和 `master-port` 两个字段，其余参数由 PyMotor 自动管理：

```json
{
  "motor_engine_prefill_config": {
    "engine_type": "vllm",
    "engine_config": {
      "model": "/mnt/weight/your_model",
      "tensor_parallel_size": 16,
      "data_parallel_size": 1,
      "prefill-context-parallel-size": 2,
      "cp-kv-cache-interleave-size": 128,
      "nnodes": 2,
      "master-port": 7001
    }
  }
}
```

| 字段 | 说明 |
|------|------|
| `nnodes` | PCP 组包含的节点数。每个 PCP 组内 `nnodes` 个节点协同完成跨节点上下文并行 |
| `master-port` | PCP 主节点（`node_rank=0`）的通信端口 |
| `prefill-context-parallel-size` | 全局 PCP 并行度。PyMotor 会自动计算每节点贡献的 PCP rank 数 |
| `cp-kv-cache-interleave-size` | CP KV cache 交错粒度，控制 PCP rank 间 KV cache 的分片大小 |

### PyMotor 自动管理的参数

以下 vLLM 原生参数由 PyMotor 自动推导和注入，**无需用户手动配置**：

| 参数 | 自动管理方式 |
|------|-------------|
| `node-rank` | Controller 按 NodeManager 注册顺序分配（每 `nnodes` 个节点为一组，组内从 0 开始编号） |
| `master-addr` | 自动复用首注册节点（`node_rank=0`）的 IP 地址 |
| `headless` | `node_rank != 0` 的从节点自动追加，跳过 API 服务器启动 |
| `data-parallel-rank` | 由 Endpoint ID 决定 |
| `data-parallel-address` | 由 Controller 根据组装结果确定 |

### 控制面流程

1. **注册**：各节点 NodeManager 启动后向 Controller 注册，携带 `nnodes` 参数
2. **组装**：Controller 等待 `dp_size × nnodes` 个节点全部注册完成
3. **主从分配**：按注册顺序每 `nnodes` 个节点为一组，组内首个为主节点（`node_rank=0`），其余为从节点
4. **启动命令下发**：Controller 向各 NodeManager 下发差异化 StartCmdMsg，包含各自的 `node_rank` 和 `master_dp_ip`
5. **引擎拉起**：主节点启动完整 EngineCore + API Server；从节点仅启动 Worker 进程（headless 模式）

### DP 叠加 PCP

支持 `data_parallel_size > 1` 与跨节点 PCP 组合使用。例如 DP=4、PCP=2、每节点 16 卡时，总共需要 4 × 2 = 8 个节点。Controller 会等待全部 8 个节点到齐后统一组装并下发启动命令。

### 从节点 SimInference 处理

跨节点 PCP 的从节点不启动 API 服务器（headless 模式），MgmtEndpoint 的 `/status` 健康检查通过 NPU AICore 使用率监控实现，虚拟推理请求自动禁用。

### 调度模式（Coordinator 侧）

**推荐使用 `MooncakeConnectorV1`（非 layerwise）+ `cpcd_separate` 调度模式。** Layerwise connector 在 CP 场景下存在 KV transfer 的 block 切分兼容性问题，不推荐使用。

```json
"motor_coordinator_config": {
    "scheduler_config": {
        "deploy_mode": "cpcd_separate"
    }
}
```

| KV Connector | 调度模式 | 推荐？ | 说明 |
|-------------|---------|:---:|------|
| `MooncakeConnectorV1` | `cpcd_separate` | ✅ 推荐 | 非 layerwise connector 配合 CPCD 调度模式，block 切分与 CP 场景兼容 |
| `MooncakeLayerwiseConnector` | `pd_separate` | ❌ 不推荐 | Layerwise 按层拆分 KV 传输，与 CP 场景存在兼容性问题 |

> **注意**：若使用 `MooncakeConnectorV1` 时配为 `pd_separate`，会走 `SeparateCDPRouter` 路由，其 KV transfer 的 block 切分逻辑与 CP 场景不兼容，导致两类断言失败：
>
> - Prefill 侧：`assert len(selected_p_cp_groups) == len(selected_d_cp_groups)` — CP group 数量不匹配
> - Decode 侧：`assert num_external_tokens == 0` — `remote_block_ids` 为空但 `num_external_tokens > 0`，非 layerwise connector 的 block 分配与 CDP 调度不一致

### 引擎配置映射

CLI 参数与 `engine_config` 键名的完整映射关系详见：

👉 **[CLI 参数与 engine_config 映射指南](https://gitcode.com/Ascend/MindIE-PyMotor/blob/master/docs/zh/user_guide/operations/cli_to_engine_config_guide.md)**
