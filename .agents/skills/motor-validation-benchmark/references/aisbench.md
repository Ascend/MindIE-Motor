# AISBench execution contract

AISBench CLI、参数名和产物 schema 会随版本变化。每次运行先读取已安装版本的
`ais_bench --help`，并以该 revision 的官方说明和真实产物为准；本 reference 不固定
可能漂移的完整命令行。

## 参数映射

构造命令前明确记录：endpoint、served model、dataset 或输入长度分布、输出长度、
request count、concurrency、request rate、stream、seed、warmup 和 output directory。
不支持的参数应 BLOCKED，不得静默丢弃。

## 运行约束

- 先执行更小的 smoke workload；正式 workload 必须由用户确认。
- 正式运行不启用会把 client 限制为单核/串行的 debug 模式。
- 长任务使用可监控后台执行，并保存完整 stdout/stderr 和退出码。
- 每次运行使用新目录，验证产物 mtime 落在本次时间窗，避免读取旧结果。
- 不自动下载数据集、安装依赖或修改共享 AISBench 源码。

## 结果读取

优先从原生结构化产物提取：总/成功/失败请求数、duration、achieved request rate、
concurrency、request/token throughput、TTFT、TPOT/ITL、E2E 和实际 token 分布。
字段缺失时报告 schema gap，不从日志摘要猜值。

负载机未达到目标 rate/concurrency 时结果描述客户端能力上限，不能作为 Motor 服务端
上限。比较两次运行前逐项核对环境、revision、拓扑、模型、workload 和 cache state。
