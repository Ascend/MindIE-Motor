# Prefill Context Parallel (PCP) 与跨节点 PCP

## 概述

**Prefill Context Parallel (PCP)** 是将长序列的 prefill 计算切分到多个设备上并行执行的策略，用于
支持超长上下文场景。MindIE Motor 完整集成了 PCP 以及跨节点的 PCP 编排能力。

- **单节点 PCP**：PCP rank 全部在同一节点的多张 NPU 上，MindIE Motor 通过
  `engine_config` 中的 `prefill-context-parallel-size` 参数自动将配置透传给 vLLM 引擎。
- **跨节点 PCP**：PCP rank 分布在多个节点的 NPU 上，MindIE Motor 自动化处理节点注册、主从分配、通信地址注入等全流程。

> 关于 vLLM Ascend 跨节点 PCP 的底层原理与参数说明，请参考 vLLM 社区文档：
> 👉 **[长序列上下文并行的多节点部署](https://docs.vllm.ai/projects/vllm-ascend-cn/zh-cn/v0.18.0/tutorials/features/long_sequence_context_parallel_multi_node.html)**

## 跨节点 PCP 在 MindIE Motor 中的使用

### 用户配置

用户只需在 `engine_config` 中添加 `nnodes` 和 `master-port` 两个字段，其余参数由 MindIE Motor 自动管理：

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

|字段|说明|
|------|------|
|`nnodes`|PCP 组包含的节点数。每个 PCP 组内 `nnodes` 个节点协同完成跨节点上下文并行|
|`master-port`|PCP 主节点（`node_rank=0`）的通信端口|
|`prefill-context-parallel-size`|全局 PCP 并行度。Motor 会自动计算每节点贡献的 PCP rank 数|
|`cp-kv-cache-interleave-size`|CP KV cache 交错粒度，控制 PCP rank 间 KV cache 的分片大小|

### MindIE Motor 自动管理的参数

以下 vLLM 原生参数由 MindIE Motor 自动推导和注入，**无需用户手动配置**：

|参数|自动管理方式|
|------|-------------|
|`node-rank`|Controller 按 NodeManager 注册顺序分配（每 `nnodes` 个节点为一组，组内从 0 开始编号）|
|`master-addr`|自动复用首注册节点（`node_rank=0`）的 IP 地址|
|`headless`|`node_rank != 0` 的从节点自动追加，跳过 API 服务器启动|
|`data-parallel-rank`|由 Endpoint ID 决定|
|`data-parallel-address`|由 Controller 根据组装结果确定|

### 控制面流程

1. **注册**：各节点 NodeManager 启动后向 Controller 注册，携带 `nnodes` 参数
2. **组装**：Controller 等待 `dp_size × nnodes` 个节点全部注册完成
3. **主从分配**：按注册顺序每 `nnodes` 个节点为一组，组内首个为主节点（`node_rank=0`），其余为从节点
4. **启动命令下发**：Controller 向各 NodeManager 下发差异化 StartCmdMsg，包含各自的 `node_rank` 和 `master_dp_ip`
5. **引擎拉起**：主节点启动完整 EngineCore + API Server；从节点仅启动 Worker 进程（headless 模式）

### DP 叠加 PCP

支持 `data_parallel_size > 1` 与跨节点 PCP 组合使用，总节点数为 `data_parallel_size × nnodes`。
例如 DP=4、`nnodes`=2 时需要 8 个节点；PCP 并行度另按模型和设备拓扑配置。
Controller 等待所有节点注册后再下发启动命令。

### 从节点健康状态

跨节点 PCP 的从节点不启动 API 服务器（headless 模式），NodeManager 根据原生引擎进程状态维护从节点生命周期；只有主节点暴露业务端口并参与健康探测和请求调度。

### PD 分离与 KV 连接器能力

Coordinator 根据可用实例角色自动选择路由实现。P/D 实例的协同行为由 KV 连接器推导的能力决定；
Coordinator 不通过 `motor_coordinator_config.scheduler_config.deploy_mode` 选择此行为。
`motor_deploy_config.deploy_mode` 仍用于选择部署形态。拓扑识别与 KV 传输能力详见
[PD 分离说明](../../../docs/zh/design/pd_disaggregation.md)。

当前说明指出，Mooncake 的按层传输（layerwise）模式在 PCP 场景存在 KV 区块切分兼容风险，
因此不建议默认用于 PCP。使用前仍须按目标引擎版本和 NPU 环境确认；PD 自动路由与 KV 传输能力表
不能证明 PCP 兼容。

### 引擎配置映射

CLI 参数与 `engine_config` 键名的完整映射关系详见：

CLI 参数与 `engine_config` 的映射由
`motor/node_manager/core/services/native_engine/backends/vllm/config.py` 中的
`VLLMConfig` 维护。
