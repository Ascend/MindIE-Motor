# Anthropic Count Tokens 在 v3.2.0 的实现与验收

`POST /v1/messages/count_tokens` 只计算输入的 token 数。Coordinator 将一次计数请求发给一个原生 vLLM 业务端点，由服务模型自己的 tokenizer、chat template 和多模态 processor 解释消息。对外接口仍使用 Anthropic 的请求和响应格式。

## 故障与接口边界

在 `upstream/v3.2.0` 基线 `dd3b616a` 上，即使 P 正确地返回 `200 {"input_tokens":7}`，Handoff 路由仍将它当作 Prefill 响应，因缺少 `kv_transfer_params` 返回 502。仅有 P 且禁用混部回退时，推理拓扑检查会提前返回 503。Union 请求虽然可以成功，仍会占用一次推理资源分配。以上现象可用真实 Coordinator ASGI 路由、模拟 Scheduler 和引擎在单机重现。

## 一个请求如何执行

1. `InferenceServer` 注册专用路由，沿用入口 API Key 和已配置的限流、请求体大小中间件。解析 UTF-8 JSON，要求对象包含非空模型名和非空消息数组；引擎负责校验具体的消息块。客户端可以省略 `max_tokens`。
2. 如 Worker 已启用 token 或图像混淆，返回 501。该版本的 Render 没有 Anthropic 计数合同，直接转发明文会破坏混淆边界。
3. 每个 Worker 的事件循环持有一个 `CountTokensProxy`。校验通过后申请本地在途名额，满额立即返回 429。端点选择、HTTP 转发和响应体读取都在名额内。成功、异常、任务取消和超时均在 `finally` 中释放名额。
4. 从当前 SchedulerClient 的实例视图发现角色与同模型 vLLM 实例。缓存冷启动沿用客户端的预热机制。优先匹配 P 池；没有匹配 P 池时查 U。调度器缺少所需实例或熔断视图、或者已匹配的 P 池没有可用端点时返回 503。D、E 不参与选择。注册模型名的比较沿用独立部署的大小写规则，发送给引擎的模型名保持原文。
5. 只考虑未熔断且未暂停接收新请求的 ACTIVE 实例和 NORMAL 端点。`Instance.get_all_endpoints()` 排除 headless 端点并遵守多端点开关。Worker 在本次请求的候选端点中轮询，每次读取当前视图，避免复用已经下线的地址。
6. 通过 `HTTPClientPool` 复用到端点 `ip:business_port` 的异步连接和现有 Infer TLS 配置。固定 POST 原路径，按原始字节发送请求体，设置 JSON Content-Type。允许转发 `anthropic-version`、`anthropic-beta` 和 `x-request-id`。复用 `TracerManager` 解析并注入 W3C `traceparent` 和 `tracestate`。入口的 API Key、Authorization、Cookie 不复制到引擎请求。
7. 计数代理请求未压缩的响应，成功响应体上限为 64 KiB。限内保留状态码、Content-Type 和完整响应体，包括 `input_tokens` 以外的可选字段。响应超限或引擎返回不符合请求的内容编码时返回 502。每个请求最多发送一次；整个调用不创建 `RequestInfo`，不调用 `select_and_allocate`，也不产生 KV 交接、Decode 请求或 workload 释放。

| 代码位置 | 职责 |
| --- | --- |
| `motor/coordinator/api_server/inference_server.py` | 区分 Messages 和 Count Tokens，鉴权、校验、混淆检查、总超时 |
| `motor/coordinator/api_server/count_tokens.py` | Worker 并发上限、实例筛选、轮询、转发、响应处理 |
| `motor/coordinator/router/upstream_error.py` | 有界响应体读取和上游错误映射 |
| `motor/config/coordinator.py` | 配置默认值、JSON 加载与校验 |

## 配置和故障行为

已有配置文件可以直接使用默认值。需要调整时，在 `user_config.json` 的 `motor_coordinator_config` 下配置：

```json
{
  "count_tokens_config": {
    "timeout_seconds": 30.0,
    "max_inflight": 32
  }
}
```

`timeout_seconds` 是从进入计数 handler 读取请求体到完成代理的总时限。到达时限后，连接池完成响应关闭再返回 504。连接阶段还受 `exception_config.connect_timeout` 限制。`max_inflight` 是每个 Worker 的转发上限。例如 4 个 Worker、每个 32 个名额，最多同时转发 128 个计数请求。该限制独立于推理负载和网关通用限流。这两个默认值是发布初值，需要结合目标模型的最大提示长度、多模态开销和 Worker 数量调整。

| 条件 | 客户端结果 |
| --- | --- |
| 非法 JSON、缺少模型或消息 | 400 |
| 网关鉴权失败 | 沿用入口 401 / 403 |
| 计数转发并发满 | 429 |
| 启用 token 或图像混淆 | 501 |
| Scheduler 未连接、缺少所需视图或没有合适端点 | 503 |
| 引擎连接或传输失败 | 502；超时为 504 |
| 上游成功响应超过 64 KiB 或使用意外的内容编码 | 502 |
| 引擎返回 4xx / 5xx | 保留引擎状态码。完整错误体按原文返回，保留 Content-Type、Retry-After。超过 `upstream_error_body_max_bytes` 时返回有界 JSON 错误 |
| 目标镜像未实现原生计数接口 | 保留引擎错误（例如 404），便于区分引擎版本与服务拓扑问题 |

注册信息提供引擎类型、模型名和健康状态，不能证明镜像实现了计数接口，也不能证明各副本的模板和 tokenizer 文件相同。部署负责保证同一模型池使用一致的镜像、模板和 tokenizer。该版本使用本地实例池和原生接口，无额外能力探测、跨角色重试或远程 tokenizer 服务。WARNING 日志区分模型未注册与匹配池无可用端点，并记录上游传输失败的端点和异常类型。DEBUG 日志记录引擎状态码和请求字节数。

## 验收

代码回归通过仓库入口运行：

```bash
bash tests/run_tests.sh --serial tests/coordinator/api_server/test_count_tokens.py
bash tests/run_tests.sh tests/coordinator/ tests/config/ tests/examples/deployer/test_update_config_whitelist.py
```

路由用例从真实 ASGI 入口发送请求。P+D、仅 P 且禁用混部回退、仅 U 三种场景都要求恰有一次原生转发、收到完整计数响应、零推理负载分配。还覆盖多 P 轮询、熔断、异常和 headless 端点、模型不匹配、调度视图缺失、链路上下文、响应体上限、超时后的连接池清理及名额释放。普通 Messages 和 OpenAI 路由继续使用原有用例。

目标镜像的联调应使用相同请求体、相同服务模型和对应的网关/引擎凭据。在允许直连的测试环境，可将以下请求分别发往 Coordinator 和所选 P（Union 环境使用 U），根据部署配置添加认证请求头和 TLS 证书：

```bash
curl -sS -i -H 'Content-Type: application/json' \
  --data-binary @count-request.json "${BASE_URL}/v1/messages/count_tokens"
```

`count-request.json` 的最小内容为 `{"model":"<实际服务模型名>","messages":[{"role":"user","content":"Hello"}]}`。先比较两次 200 响应的顶层 `input_tokens`，再用 system、多轮、tools 以及镜像支持的图像请求重复比对。在测试环境只保留 P 并关闭混部回退，计数仍应成功。检查 D 的访问记录没有该请求，再通过普通 `/v1/messages` 检查推理路径。随版本上线前，用真实流量分布确定超时、并发和 P 侧 CPU 余量。
