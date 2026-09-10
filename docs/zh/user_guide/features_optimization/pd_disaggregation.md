# PD 分离

## 特性介绍

PD 分离（Prefill & Decode 分离）将大语言模型推理的预填充（Prefill）与解码（Decode）两个阶段拆分到不同实例上运行。Prefill 阶段对输入 prompt 执行完整前向传播，生成初始隐藏状态（Hidden States），为计算密集型；Decode 阶段基于 Prefill 结果逐步生成后续 token，为访存密集型（以 KV Cache 等内存访问为主）。本仓库采用多机 PD 分离部署方案，通过 K8s Service 为 Coordinator 暴露推理入口，使用多个 Deployment 分别部署 Controller、Coordinator 以及 Server（P 实例与 D 实例各若干 Pod）。

### 工作原理

**依据原文上下文内容重组，请进行人工校验。**

PD 分离将推理流程拆分为两个独立阶段，分别在独立实例上执行：

1. **Prefill 阶段**：P 实例接收用户请求，对输入 prompt 执行完整前向传播，生成初始隐藏状态。每个新输入序列都需执行一次 Prefill。
2. **KV Cache 传输**：P 实例将计算后的 KV Cache 通过 kv_transfer_config 传输至 D 实例。
3. **Decode 阶段**：D 实例基于 Prefill 结果逐步生成后续 token，每步仅计算最新 token 的激活与 attention，单步计算量较小，但需反复执行直至生成结束。

### 核心功能

提高 NPU 利用率，减轻 Prefill 与 Decode 分时复用带来的相互干扰，在相同时延下提升整体吞吐。Prefill 处理新请求的同时 Decode 可持续处理已有请求的解码，整体处理能力更高，尤其在高并发场景下有助于降低时延。

### 约束与限制

**依据原文上下文内容重组，请进行人工校验。**

| 约束维度 | 要求 |
|----------|------|
| 硬件 | 内容缺失，需要人工补齐。 |
| 部署场景 | 支持多机 PD 分离部署。 |
| 引擎 | 基于 vLLM 引擎。 |
| 特性互斥 | 内容缺失，需要人工补齐。 |
| 软件依赖 | 需部署 Controller、Coordinator、P 实例与 D 实例；使用 K8s Service 暴露 Coordinator 推理入口。 |
| 其他限制 | 内容缺失，需要人工补齐。 |

## 特性使用

### 环境准备

- 已部署 K8s 集群，具备 `kubectl` 权限。
- 已安装 MindIE Motor 推理服务组件。
- 详细部署步骤请参考《[PD 分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md)》。

### 使用样例

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

PD 分离部署通过 `user_config.json` 配置 Prefill 和 Decode 实例数，并使用 `deploy.py` 完成部署。详细操作步骤请参考《[PD 分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md)》。

### 验证特性

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

1. 确认 P 实例和 D 实例均启动成功：

   ```bash
   kubectl get pod -A -owide
   ```

   预期输出：P 实例（prefill）和 D 实例（decode）均处于 Running 状态。

2. 发送推理请求验证：

   ```bash
   curl -X POST http://{coordinator-ip}:1025/v1/chat/completions \
     -H "Content-Type: application/json" \
     -d '{"model": "your-model", "messages": [{"role": "user", "content": "hello"}]}'
   ```

   预期输出：返回 HTTP 200，响应体包含 `choices` 字段。

## 常见问题

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认**

### P 实例与 D 实例之间无法传输 KV Cache

**问题描述**：P 实例与 D 实例之间无法传输 KV Cache，推理失败。

**原因分析**：`kv_transfer_config` 中 `kv_role` 配置错误，或 `kv_port` 不一致。

**解决步骤**：

1. 检查 `kv_transfer_config` 中 `kv_role` 是否正确：P 为 `kv_producer`，D 为 `kv_consumer`。
2. 检查 `kv_port` 是否一致。

### P 实例或 D 实例启动失败

**问题描述**：P 实例或 D 实例启动失败。

**原因分析**：配置文件中 `user_config.json` 的实例数配置错误或引擎配置不完整。

**解决步骤**：

1. 检查 `user_config.json` 中 `p_instances_num` 和 `d_instances_num` 是否正确。
2. 检查引擎 `model` 和 `max_model_len` 配置是否完整。
3. 查看引擎日志：`kubectl logs <pod-name>`。
