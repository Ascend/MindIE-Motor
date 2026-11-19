# [2026-11-04] vLLM 返回计数成功，Coordinator 却返回 502

- **现象**：P+D Handoff 下，原生接口返回 `{"input_tokens":7}` 后，客户端收到 `Missing kv_transfer_params`。
- **根因**：v3.2.0 基线 `dd3b616a` 的 `inference_server.py:657` 将计数送入 Messages 推理处理器。Prefill 协议要求 KV 交接字段。
- **为什么会写出**：两个 API 的输入结构相似，但输出和资源需求不同。入口测试模拟了 dispatch，且只检查 HTTP 200。
- **修复**：独立入口按当前模型和实例状态直接访问 vLLM 的原生计数端点。
- **测试拦截**：`tests/coordinator/api_server/test_count_tokens.py` 从 HTTP 入口核对原始请求、顶层计数字段、目标角色和零负载分配。
- **场景**：P+D 的正常计数响应，以及仅 P、禁用混部回退的独立计数。
- **关键词**：Anthropic、count_tokens、KV handoff、502、Prefill。

## 主干恢复暂停兼容性

引入 `Instance.recovery_paused` 后，实例可以保持 ACTIVE 和 NORMAL 以继续处理在途请求。计数代理绕过调度分配，而 `get_unblocked_instances()` 只过滤熔断实例，因此原筛选条件会把新的计数请求发给恢复暂停的实例。

计数代理排除恢复暂停的实例。端点筛选回归用例构造了仍健康且未熔断的暂停实例。其他匹配 P 端点均不可用时，用例验证返回 503，且没有请求发往暂停实例或 U 池。

## 截止时间与连接池清理

截止时间恰好落在 HTTPcore 关闭响应时，`asyncio.wait_for` 直接取消任务，可以打断 AnyIO 屏蔽区内的清理。连接池只有一个槽位时，下一个请求会在池中等待至超时。

Count Tokens 改用 AnyIO 截止范围。HTTPcore 完成受保护的关闭后，路由检查截止取消并返回 504。回归用例在真实单槽 HTTPX 连接池的响应关闭阶段延迟操作，验证关闭完成、首个请求返回 504，以及后续请求能使用连接池。
