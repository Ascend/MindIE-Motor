# [2026-09-15] NodeManager FT 代理契约漂移导致请求失败或退役状态不一致

- **现象 (Symptom)**：`retry` 在公共客户端被拒绝；畸形请求返回 500；退役 rank 仍出现在 metrics 目标中；提交前可能只更新部分组件。
- **根因 (Root cause)**：Controller、公共引擎客户端和 NM 白名单不一致，路由没有结构校验，退役出口各自实现过滤和校验。
- **为什么会写出 (Why)**：同一协议由多层重复声明，新增字段和状态时缺少端到端契约测试。
- **修复 (Fix)**：统一 retry/scale_down 白名单和 HTTP 400 校验；提交前预校验全部组件；metrics 排除 retired；投影复制 FT 能力。
- **测试拦截 (Test interception)**：覆盖 retry apply、畸形 payload、预校验失败无状态变更、retired metrics 和能力快照深拷贝。
- **场景 (Scenario)**：引擎快速恢复、错误控制面请求、缩容 finalize 以及 PreStop 排水。
- **关键词 (Keywords)**：NodeManager, FT proxy, retry, retired endpoint, HTTP 400
