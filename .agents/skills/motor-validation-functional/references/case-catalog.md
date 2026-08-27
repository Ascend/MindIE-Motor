# Functional case catalog

| Case | 最小动作 | PASS |
|---|---|---|
| inference-nonstream | 对 inference Service 发一个小型非流式请求 | HTTP 成功、模型匹配、返回非空且协议结构有效 |
| inference-stream | 发一个小型流式请求并读完整 SSE | 分片格式有效、正常结束、无中途协议错误 |
| metrics-after-request | 唯一请求后读取目标 metrics | 预期 series 存在且时间/label 能关联本次目标 |
| tracing-sampled-request | 注入 sampled traceparent 后查 backend | 找到相同 trace id 且 span 链与请求匹配 |

只选择用户目标所需的最小 case set。具体请求结构、模型参数和期望 feature 必须来自
当前部署能力与用户目标，不能由 catalog 写死。认证信息只引用环境或 secret，不写进
tracked 文件或输出。
