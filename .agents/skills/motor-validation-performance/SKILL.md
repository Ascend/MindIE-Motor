---
name: motor-validation-performance
description: Explicit atomic workflow under motor-validation for read-only performance triage from valid benchmark output, logs, metrics, and traces. Use for high TTFT/TPOT/E2E, low throughput, scheduling overhead, P/D imbalance, or ownership questions.
---

# Motor performance analysis framework

先证明结果有效和可比，再做归因。本 Skill 不自动发压测、不收 profiler、不改配置或
重启服务。

1. 记录每份证据的时间窗、revision、硬件、模型、拓扑、engine config、workload、
   warmup/cache 和来源。优先原始 benchmark、Coordinator/P/D logs、metrics 和 trace。
2. 核对 success/failed、实际 rate/concurrency 和 token 分布；条件不兼容时只报告绝对
   行为与 comparability gap。
3. 分开 startup、warmup、steady state、teardown，只对齐正式测量窗口。
4. 建立客户端症状：QPS/token throughput、TTFT、TPOT/ITL、E2E、tail 和 success rate。
5. 从匹配 revision 的源码确认 Motor scheduling 日志/metric 语义，再估计调度阶段规模；
   没有 request-correlated trace 时禁止把阶段值直接相加成每请求成本。
6. 检查 P/D worker、queue/running、prompt/generation TPS、engine latency、拓扑变化和
   handoff 证据。缺 P-D 跨段证据表示 transfer unresolved，不表示健康。
7. 对 Motor scheduling、engine service、transfer、client/load generator、environment
   分别列支持/反证/缺失证据和置信度，推荐最小判别测量。

需要新 workload 时路由 `motor-validation-benchmark`；请求失败、Pod 异常或 timeout 先走
`motor-diagnosis`。没有 operator/kernel/HCCL/profiler 证据时不得声称深层根因。
