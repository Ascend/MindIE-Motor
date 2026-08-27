---
name: motor-validation-benchmark
description: Explicit atomic workflow under motor-validation for repeatable AISBench online-serving workloads. Use for 压测、AISBench、打流、QPS、TTFT、TPOT or prefix-cache workload; attribution belongs to motor-validation-performance.
---

# Motor benchmark

读取 `references/aisbench.md`。本 Skill 产生受控在线负载和可复核证据，不负责性能
瓶颈归因，不安装或升级压测环境。

## 后端选择

| 请求 | 后端 |
|---|---|
| 固定/变长、数据集、rate/concurrency、稳定阶段 | 原生 `ais_bench`（默认） |
| 原生 AISBench 无法表达的 prefix 构造/逐 DP warmup | 已确认版本的 prefix wrapper |
| 高 TTFT/TPOT、低吞吐或回归归因 | `motor-validation-performance` |
| 请求失败、Pod 不健康、服务不可达 | 先 `motor-validation-smoke` / `motor-diagnosis` |

记录 `EXECUTION_BACKEND=native|prefix-wrapper` 及选择原因。原生能力够用时禁止为了方便
选择 wrapper。

## 事实解析与 capability gate

从当前 config、live K8s 和负载机解析 served model、namespace、inference endpoint、
`max_model_len`、P/D/DP/实例拓扑、硬件与软件 revision。以下输入必须由用户提供或确认：
正式 request count、输入/输出分布、concurrency、arrival rate、数据集/生成方式和基线。

执行前必须满足：

1. `input_len + output_len <= max_model_len`；分布按最大值检查，运行后核对实际 token。
2. `motor-validation-smoke` 已观察到 `ready=true`，负载机可访问推理端点，served model 精确匹配。
3. 记录 Python、AISBench 版本/revision、`--help` 和产物 schema；只探测，不安装升级。
4. 使用独立、用户认可的 run directory；不修改 tracked AISBench/wrapper 源码，不共享
   可变工作目录。
5. API key 只从环境或 secret reference 获取，不写入生成脚本、配置或报告。

## 执行

先展示正式命令、smoke workload、输出目录、运行时临时改动和停止条件。smoke 请求数和
并发均小于正式 workload。正式 native run 禁止 `--debug`。长任务只启动一个受监控
job，超时后不得重复发起同一负载。

wrapper 若硬编码 debug、credential 或修改共享安装，仅允许在独立运行副本中做最小
临时修正并记录 diff；无法安全隔离时 fail closed。每次 repetition 单独运行和归档。

## 结果有效性 gate

原生 AISBench JSON/JSONL/CSV 是权威产物。正式结果必须同时满足：

- 明确提取 total/success/failed，不能把 `total_req` 当成功数；
- 必需字段非空、无 sentinel 值，无重复 Bad Request、全失败或旧产物；
- wrapper 汇总与原生产物一致；
- 记录目标与实际 rate、平均/最大 concurrency；未达到目标标记 `client-limited`；
- 实际输入/输出 token 分布满足 context gate；
- prefix 构造比例不冒充观测 hit rate；counter reset、混合流量或覆盖不全时 hit rate
  标记 unavailable。

## 证据与报告

保存用户指定的 run directory：manifest、精确命令、脱敏 config、环境/revision 指纹、
dataset 参数和 checksum、完整原生产物、wrapper 产物、相关原始 metrics。报告成功失败
数、时长、实际 concurrency/rate、QPS/token throughput、TTFT、TPOT/ITL、E2E、token
分布、产物路径和可比性缺口。

只有硬件、模型/tokenizer、revision、拓扑、engine config、workload、warmup/cache、
客户端环境和 benchmark 后端/版本一致时才比较基线。
