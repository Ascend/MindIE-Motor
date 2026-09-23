# [2026-09-15] NodeManager FT 代理契约漂移导致请求失败或退役状态不一致

- **现象 (Symptom)**：`retry` 在公共客户端被拒绝；畸形请求返回 500；跨 Pod 缩容向未退役本地 endpoint 的 Pod 提交空 `retired_endpoint_ids` 时 finalize 返回 400；退役 rank 仍出现在 metrics 目标中；提交前可能只更新部分组件；同 Pod 连续缩容 DP master 时复用固定 store 端口并触发 `EADDRINUSE`；DP0 退役后虚推仍绑定旧 rank，新 master 失去虚推检测。
- **根因 (Root cause)**：Controller、公共引擎客户端和 NM 白名单不一致，路由没有结构校验，并错误地假设 commit finalize 在每个 Pod 上都至少退役一个本地 endpoint；退役出口各自实现过滤和校验；`dp_store_port` 仅使用固定基础端口，没有纳入新 master 的全局 DP rank，也未在配置阶段预留完整 rank 偏移区间；虚推把 DP0 当成永久 master，未消费缩容提交后的 master rank。
- **为什么会写出 (Why)**：同一协议由多层重复声明，新增字段和状态时缺少端到端契约测试；把单 Pod 的局部退役列表误当成全局非空列表，并把“一 Pod 一个 DP”的端口假设带入了同 Pod 多 DP 场景。
- **修复 (Fix)**：统一 retry/scale_down 白名单和 HTTP 400 校验；finalize 的局部 `retired_endpoint_ids` 在 commit 时也允许为空；提交前预校验全部组件；metrics 排除 retired；投影复制 FT 能力；Controller 下发内部 `dp_master_rank`，NodeManager 按 `base_port + rank` 生成实际 store 端口并在转发引擎前移除内部字段，启用缩容代理时预校验 `base_port + dp_size - 1`；finalize 提交新 master rank，NodeManager 将虚推 monitor 重绑定到本地新 master，非 master 节点清理旧 monitor。
- **测试拦截 (Test interception)**：覆盖 retry apply、畸形 payload、commit finalize 空本地退役列表、预校验失败无状态变更、retired metrics、能力快照深拷贝，以及 DP0→DP1→DP2 连续切主、内部字段剥离、运行时与配置期端口越界和虚推 target 迁移。
- **场景 (Scenario)**：引擎快速恢复、错误控制面请求、跨 Pod 缩容事务 finalize、PreStop 排水以及同 Pod 多 DP 连续切主。
- **关键词 (Keywords)**：NodeManager, FT proxy, finalize empty list, DP master, dp_store_port, EADDRINUSE, virtual inference migration
