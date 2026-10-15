# [2026-10-12] HBM 淘汰误清待确认 offload，目录残留 Device 命中

- **现象 (Symptom)**：引擎先上报 CPU offload、再淘汰同一哈希的 HBM block，而 pool 尚未确认时，查询仍返回 NPU 命中；后续 Host 确认也无法消除该 Device 驻留。
- **根因 (Root cause)**：`motor/kv_conductor/src/indexer/mod.rs` 的 `evict_pending_blocks()` 对所有介质查询同一 offload 缓存。HBM removal 消耗待确认的 CPU/Disk 内容后跳过树删除，导致 HBM 目录未被清理。
- **为什么会写出 (Why)**：将下层介质的两阶段确认状态误当成 HBM 驻留状态；相同 block hash 可以同时存在于多个介质，淘汰一个副本不应丢弃另一个副本的确认信息。
- **修复 (Fix)**：Npu/Unknown removal 直接返回全部待删除哈希，按 HBM 分派规则清理树，保留 offload/pending_pool 等下层匹配状态。CPU/Disk 的原有淘汰逻辑保持原样。
- **测试拦截 (Test interception)**：`indexer::tests::test_hbm_removal_preserves_unconfirmed_offload` 覆盖 Npu/Unknown 与 Cpu/Disk 组合，断言 HBM 淘汰后无命中，随后 pool 确认仍能恢复正确的下层命中。该测试在原版失败、修复后通过。
- **场景 (Scenario)**：引擎 offload 与 pool 确认来自独立订阅流；HBM 淘汰先于 pool 确认到达。高频全量快照可能暂时掩盖目录残留，因此回归应同时覆盖增量事件顺序。
- **关键词 (Keywords)**：HBM eviction, pending offload, two-phase matching, Host affinity, stale residency
