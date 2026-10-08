# xLLM 被按 vLLM Prometheus 格式抓取

## Symptom
xLLM union 已注册并可推理，Motor Obs 周期性报告原生指标解析失败。

## Root cause
`_fetch_instance_metrics` 对所有引擎执行相同的 `/metrics` 抓取。
xLLM 当前没有 vLLM Prometheus exposition 接口，响应不能被该解析器读取。

## Why
OpenAI 推理接口兼容不代表原生观测协议也兼容，接入遗漏了能力边界。

## Fix
xLLM 暂不参与原生 Prometheus 抓取；Motor 自有请求负载账本与 KV Conductor
目录继续工作。不要用虚构的零指标模拟引擎数据。

## Test interception
`test_xllm_does_not_scrape_vllm_metrics_in_a_mixed_cluster` 验证混合实例下
只抓取 vLLM，保留其结果，不请求 xLLM 指标。

## Scenario
单机或集群 xLLM union；也适用于与 vLLM 混合的实例集合。

## Keywords
xLLM, MetricsCollector, Prometheus, native engine, unsupported metrics
