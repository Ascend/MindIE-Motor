# [2026-11-04] Messages Prefill 空 KV 参数仍进入重试或混部兜底

- **现象 (Symptom)**：vLLM handoff 的 Messages P 响应缺少 `kv_transfer_params` 或该值为 `null` 后，日志报 `Missing kv_transfer_params`，但仍出现 `retry=True`、`UnifiedPD fallback`。
- **根因 (Root cause)**：`strategies/unified_pd.py` 的 `_request_prefill_result` 把已有协议检查错误转成普通可重试 502；流式与非流式调用方在判断 `retry=False` 之前，先检查混部兜底，因此仅禁用重试也不能终止整个请求。
- **为什么会写出 (Why)**：把协议校验失败等同于请求终止，忽略了外层重试和兜底仍可发起新的推理请求。
- **修复 (Fix)**：仅将 Messages handoff 的 null/缺失 KV 参数分类为 `MissingMessagesPrefillKVError`；公共错误处理完成 attempt 清理、标记 EXCEPTION 后直接抛出，跳过重试等待及两类混部兜底，保留 HTTP 502。
- **测试拦截 (Test interception)**：`test_unified_pd_messages_missing_ticket_fails_without_retry_or_hybrid_fallback` 参数化流式/非流式和缺失/null，配置三次重试并强制允许混部兜底；断言 502、P 请求一次、D 请求零次、仅分配并释放 P、未构造混部路由。旧版 `dd3b616` 四例失败，修复后通过。有效 Messages ticket 的既有时序用例验证正常进入 D。通过官方 `bash tests/run_tests.sh --serial` 入口，目标文件 81 例、路由模块 388 例通过。
- **场景 (Scenario)**：原生 vLLM handoff，入口 `/v1/messages`，P 返回 JSON 空 KV 参数或序列化时省略该字段。测试使用模拟引擎 HTTP 响应，不代表已在客户真实模型上复现。Trigger/CDP 先请求 D，不适用此拦截位置。
- **关键词 (Keywords)**：coordinator, messages, kv_transfer_params, terminal, hybrid fallback
