---
name: motor-validation
description: Motor validation entry point for deployed-service readiness, functional behavior, AISBench workloads, and performance analysis. Route ordinary post-deployment validation requests through this Skill before selecting an atomic validation workflow.
---

# Motor validation dispatcher

这是 Motor 部署后验证的统一入口，只选择并编排最小必要验证，不把 readiness、功能正确性
和性能归因混成一个结论。

## 路由

| 意图 | Skill |
|---|---|
| Coordinator readiness、最小冒烟 | [`motor-validation-smoke`](../motor-validation-smoke/SKILL.md) |
| inference、metrics、tracing 或已部署 feature 行为 | [`motor-validation-functional`](../motor-validation-functional/SKILL.md) |
| AISBench、打流、QPS、TTFT、TPOT、prefix-cache workload | [`motor-validation-benchmark`](../motor-validation-benchmark/SKILL.md) |
| 高 TTFT/TPOT/E2E、低吞吐、P/D 不平衡或性能归因 | [`motor-validation-performance`](../motor-validation-performance/SKILL.md) |
| 请求失败、Pod 异常、Service 不可达或 readiness 超时 | [`motor-diagnosis`](../motor-diagnosis/SKILL.md) |

路由命中后读取并完整遵循对应原子 Skill 的 `SKILL.md`。用户显式调用原子 Skill 时可以
直接进入该流程。

## 编排规则

- 新部署的默认最小链路是 readiness；只有用户目标需要时才追加 functional 或 benchmark。
- benchmark 前必须由 `motor-validation-smoke` 证明当前 Coordinator `ready=true`。
- benchmark 产生负载和原始证据；性能归因由 `motor-validation-performance` 完成。
- 某一验证 PASS 只证明它自己的判据，不外推 accuracy、stability、reliability 或完整
  业务可用性。
- 验证不授权改配置、restart、redeploy、scale 或安装/升级压测工具。

报告所选原子 Skill、选择原因、实际目标、证据、结论和未覆盖维度。
