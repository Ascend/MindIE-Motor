# Agentic 特性

## 功能

Prefill Context Parallel（PCP，上下文并行）将长文本的预填充计算分配到多张 NPU 卡上执行。
本页介绍单节点和跨节点 PCP 的配置、启动与验证方法。

## 使用前准备

- 准备已适配的 vLLM Ascend、MindIE Motor、模型权重和可用的 Ascend NPU。
- 按 [Deployer 部署说明](../../deployer/README.md)准备 Python 依赖、集群资源、镜像、模型挂载及配置文件。
  本页命令从仓库根目录执行，使用 `model_configs/vllm` 配置目录。
- 按实际拓扑核对 PCP 并行度、Pipeline Parallel（PP）、节点数和每节点设备数。
  Motor 要求 `pcp_size * pp_size` 能被 `nnodes` 整除。
- 跨节点时，同一 PCP 组的节点须连接到同一 Controller。通信端口 `master-port` 应空闲，
  且网络策略允许节点间访问。

## 配置说明

在 `user_config.json` 中找到要启用 PCP 的角色，在该角色的 `engine_config` 内设置以下字段。
下面以 Prefill 角色为例，数值须按实际模型和设备拓扑调整：

```text
{
  "motor_engine_prefill_config": {
    "engine_type": "vllm",
    "engine_config": {
      ...,
      "prefill-context-parallel-size": 2,
      "cp-kv-cache-interleave-size": 128,
      "nnodes": 2,
      "master-port": 7001
    }
  }
}
```

`...` 表示省略该角色的其他配置，不是 JSON 语法，不能原样保存。
保留现有模型、TP、DP 和 PP 配置，并按实际拓扑核对。

| 字段 | 作用 |
| --- | --- |
| `prefill-context-parallel-size` | PCP 并行度，按模型和设备拓扑选择。 |
| `cp-kv-cache-interleave-size` | PCP rank 间 KV Cache 交错粒度，按模型配置选择。 |
| `nnodes` | 跨节点 PCP 组的节点数；单节点可省略，默认为 `1`。 |
| `master-port` | 跨节点通信端口；单节点可省略，也接受 `master_port`。 |

Motor 自动分配主从节点编号和通信地址，无需手动填写 `node_rank`、`master_addr` 或 `headless`。

## 操作步骤

### 1. 修改并检查配置

**操作**：在仓库根目录，按上面的说明修改 `model_configs/vllm/user_config.json`。
按部署说明补齐 `env.json`，再检查两份文件的 JSON 语法：

```bash
python -m json.tool \
  model_configs/vllm/user_config.json > /dev/null &&
python -m json.tool \
  model_configs/vllm/env.json > /dev/null &&
echo 'JSON 语法检查通过'
```

**成功标志**：输出“JSON 语法检查通过”，且所选角色保留了模型和并行配置。
这一步只检查文件语法，拓扑与引擎兼容性还需在启动时核对。

**失败处理**：找不到文件时检查工作目录与配置目录；解析报错时检查引号、逗号，
并删除示意用的省略号。

### 2. 启动部署器

**操作**：在已准备好依赖的仓库根目录 shell 中执行：

```bash
python examples/deployer/deploy.py \
  --config_dir model_configs/vllm
```

**成功标志**：部署器打印 `Deploy complete.`，表示部署任务已提交完成。
继续下一步核对节点和引擎状态。

**失败处理**：若出现 `Deployment pre-check requires attention.`，按
[配置参数说明](../../../docs/zh/user_guide/configuration/config_reference.md)补齐推理 TLS
和管理面 API Key 配置，再重新执行。
其他报错按首处错误检查依赖、集群资源、镜像和模型挂载。

### 3. 核对节点与 PCP 状态

**操作**：查看各节点 Node Manager 和引擎的启动日志，按部署方式核对：

- 单节点：实际引擎启动参数中的 `prefill-context-parallel-size` 与配置一致，且引擎就绪。
- 跨节点：等待该 PCP 组所需节点注册，检查 `Cross-node PCP active` 日志中的节点编号和地址。
  主节点提供 API，从节点运行 Worker，不提供独立 API。

**成功标志**：单节点引擎或跨节点主节点引擎就绪，从节点进程存活，相关节点状态正常。
跨节点时，同一 PCP 组各节点均有上述日志，主节点为 `node_rank=0`，从节点为非零 rank，
且组内的 `master_addr` 均指向该组主节点。
单节点不要求出现跨节点日志。

**失败处理**：跨节点没有上述日志时，检查 `nnodes > 1`、`master-port`、节点注册和网络连通性。
引擎启动失败时，核对 NPU 数量、PCP/PP/节点数的整除约束，以及模型和引擎版本兼容性。

### 4. 验证真实请求

**操作**：在可访问服务的请求客户端，按[非流式请求示例](../../../docs/zh/user_guide/api/service_interfaces.md#openai-chat-completion-接口)
替换服务地址、推理端口、模型名及实际部署所需的鉴权和 TLS 参数，
向 Coordinator 的推理入口发送一次 `/v1/chat/completions` 请求。

**成功标志**：接口返回 HTTP `200`，响应中有符合接口约定的模型回复，且相关节点保持正常。
模型名应与实际部署的 `served_model_name` 一致。

**失败处理**：连接失败时检查地址、端口和网络，并确认请求没有发往 PCP 从节点；
`401/403` 检查鉴权，TLS 报错检查证书配置；未就绪或 `5xx` 检查服务日志和节点状态；
模型名报错时核对 `served_model_name`。

**尚未确认**：只有参数配置或启动日志，没有真实成功响应时，不能记录推理验证通过。

## 相关文档

- [PCP 配置、节点编排与 DP 组合说明](./context_parallelism.md)
- [Node Manager 跨节点 PCP 说明](../../../docs/zh/developer_guide/components/node_manager.md#跨节点-pcp)
- [PD 自动路由与 KV 传输说明](../../../docs/zh/design/pd_disaggregation.md#自动识别拓扑)
- [vLLM Ascend 多节点 PCP 文档](https://docs.vllm.ai/projects/vllm-ascend-cn/zh-cn/v0.18.0/tutorials/features/long_sequence_context_parallel_multi_node.html)
