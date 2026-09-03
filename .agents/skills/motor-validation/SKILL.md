---
name: motor-validation
description: Motor validation entry point for deployed-service readiness, functional behavior, AISBench performance workloads, answer-accuracy evaluation, and performance analysis. Route ordinary post-deployment validation requests through this Skill before selecting an atomic validation workflow.
---

# Motor validation dispatcher

这是 Motor 部署后验证的统一入口，只选择并编排最小必要验证，不把 readiness、功能正确性
和性能归因混成一个结论。

## 路由

| 意图 | Skill |
|---|---|
| Coordinator readiness、最小冒烟 | [`motor-validation-smoke`](../motor-validation-smoke/SKILL.md) |
| inference、metrics、tracing 或已部署 feature 行为 | [`motor-validation-functional`](../motor-validation-functional/SKILL.md) |
| AISBench 性能压测、打流、QPS、TTFT、TPOT、prefix-cache workload，且不以参考答案/evaluator 为目标 | [`motor-validation-benchmark`](../motor-validation-benchmark/SKILL.md) |
| 带参考答案的数据集、AISBench 原生 evaluator、答案精度或跨服务精度对比 | [`motor-validation-accuracy`](../motor-validation-accuracy/SKILL.md) |
| 已有有效 benchmark 的常规性能分析/归因，且不是“明确未达目标但原因未知”的异常诊断 | [`motor-validation-performance`](../motor-validation-performance/SKILL.md) |
| 集群级扩缩容/RAS/GPQA/性能验收套组 | [`motor-smoke-suite`](../motor-smoke-suite/SKILL.md) |
| 性能明确未达兼容目标/基线且原因未知，或请求失败、Pod 异常、Service 不可达、readiness 超时 | [`motor-diagnosis`](../motor-diagnosis/SKILL.md) |

路由命中后读取并完整遵循对应原子 Skill 的 `SKILL.md`。用户显式点名带父路由入口约束
的受限子 Skill 时仍先经本入口确认目标，不能绕过父路由直接执行。

不要用 `AISBench` 或数据集名称单独路由：有参考答案和 evaluator 的正确性目标优先进入
accuracy；只产生负载和性能指标时进入 benchmark。一个请求同时要求两类结果时拆成两个
独立验证阶段，不让两个原子 Skill 共同拥有同一结果。

## 编排规则

- 新部署的默认最小链路是 readiness；只有用户目标需要时才追加 functional、accuracy 或 benchmark。
- benchmark 或 accuracy 前必须由 `motor-validation-smoke` 证明当前 Coordinator `ready=true`。
- benchmark 产生负载和原始证据；常规性能分析由 `motor-validation-performance` 完成，
  明确未达兼容目标/基线且原因未知时返回 `motor-diagnosis`。
- 某一验证 PASS 只证明它自己的判据，不外推 accuracy、stability、reliability 或完整
  业务可用性。
- 验证不授权改配置、restart、redeploy、scale 或安装/升级压测工具。

报告所选原子 Skill、选择原因、实际目标、证据、结论和未覆盖维度。
