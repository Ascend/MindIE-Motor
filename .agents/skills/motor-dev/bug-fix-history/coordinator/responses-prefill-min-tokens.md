# [2026-10-27] Responses Prefill 未发送最小生成 token 约束

- **现象 (Symptom)**：vLLM PD handoff 中，Responses 的 P 请求包含 `max_output_tokens=1`，却没有 `min_tokens=1`；Chat/Completion 的 P 请求已包含该约束。
- **根因 (Root cause)**：`motor/coordinator/router/adapters/pd_protocol.py:188` 的 `VllmProtocolAdapter.build_prefill_request` 在 Responses 分支主动删除 `min_tokens`。
- **为什么会写出 (Why)**：Responses 使用独立的最大输出字段，不代表应删除引擎扩展的最小生成约束；原测试反而断言 `min_tokens` 缺失，未校验两类 API 的共同 Prefill 约束。
- **修复 (Fix)**：将 Responses Prefill 的 `min_tokens` 固定为 `1`，保留 `max_output_tokens=1` 和同步 Prefill 行为；原请求及 Decode 的客户参数保持不变。
- **测试拦截 (Test interception)**：`tests/coordinator/router/test_pd_protocol_adapter.py` 对未传、`0`、`2` 三种输入验证 P 固定为 `1`、原请求不变，以及 D 保留输出预算和原始最小 token 字段。商发 `v3.2.0` 基线 `5d34c29` 配新测试为 P 三例失败、D 三例通过；修复后通过 `bash tests/run_tests.sh --serial tests/coordinator/router/test_pd_protocol_adapter.py`，共 40 例通过。
- **场景 (Scenario)**：Coordinator 经原生 vLLM Responses handoff 构造 Prefill 请求。此修复保证 Coordinator 构造并发送约束；目标引擎需支持 Responses 的 `min_tokens` 并将其映射到采样参数。单测验证范围为 Motor 的 P/D 请求构造。
- **关键词 (Keywords)**：coordinator, Responses, Prefill, min_tokens, EOS
