# [2026-09-18] Render HTTP 错误只留下状态码，400 原因不可见

- **现象 (Symptom)**：sidecar 返回 HTTP 400 时，Coordinator 只打 `reason=Render returned HTTP 400`，看不到 pydantic / model 校验的 body。
- **根因 (Root cause)**：`VLLMRenderClient._post_json_unchecked` 在非 2xx 时只把 `status_code` 放进异常。
- **为什么会写出 (Why)**：把 sidecar 当不透明上游，假设状态码足够分流 fallback；实际 400 的判别信息全在响应体。
- **修复 (Fix)**：异常文案改成 `f"{operation} returned HTTP {status}: {response.text}"`，现有 `reason=%s` 会带出 body。
- **测试拦截 (Test interception)**：`test_render_http_error_includes_sidecar_body`。
- **场景 (Scenario)**：Render/Derender 返回 4xx/5xx 且带 JSON/text 错误体。
- **关键词 (Keywords)**：render, HTTP 400, sidecar body, VLLMRenderClient, reason
