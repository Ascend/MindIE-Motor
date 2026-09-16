# [2026-08-29] Admission timeout was retried as a transport failure

- **现象 (Symptom)**：PDHybrid 非流式请求在 `Program admission queue timeout` 后重新进入完整准入等待；5 次 transport retry 可将一次约 10 分钟的等待放大到约 50 分钟。
- **根因 (Root cause)**：`motor/coordinator/router/strategies/pd_hybrid.py` 的非流式通用异常分支没有像流式和 UnifiedPD 路径一样终止 `HTTPException`，因此本地准入 503 落入普通重试。
- **为什么会写出 (Why)**：重试分类只区分了不可重试的上游 HTTP/网络错误，遗漏了进入上游请求之前产生的本地 HTTP 控制面结果。
- **修复 (Fix)**：PDHybrid 非流式路径收到 `HTTPException` 时更新请求异常状态并立即原样抛出，不执行 transport retry。
- **测试拦截 (Test interception)**：`test_nonstream_program_admission_timeout_does_not_retry` 配置 3 次 transport retry，并断言准入函数只调用一次且原样返回 HTTP 503。
- **场景 (Scenario)**：启用 Progress-TTL、带稳定 Program identity 的非流式 P/Union 请求因容量不足持续排队至 `first_token_timeout`。
- **关键词 (Keywords)**：coordinator, program admission, timeout, retry, HTTP 503
