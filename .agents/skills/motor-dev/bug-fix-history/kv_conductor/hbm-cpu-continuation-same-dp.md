# [2026-09-16] HBM→CPU 续查未按 (instance_id, dp_rank) 对齐，跨 DP 虚高 cpu_blocks

- **现象 (Symptom)**：YuanRong/Mooncake `IpOnly` 把同一条 CPU 尾边写到节点上每个 DP 后，`POST /query` 里无 HBM 的 DP 仍能拿到很大的 `cpu_blocks`（把别的 DP 的 NPU 前缀算进自己的 CPU）。
- **根因 (Root cause)**：`indexer/mod.rs` `lower_tier_lookup` 用 `edge_owners(last_seq, next_hash)` 开续查，不看 `TierBreakpoint.instance_id` / `dp_rank`。`cpu_end` 是查询序列上的绝对位置，`cpu_blocks = max(0, cpu_end - npu_end)` 在 `npu_end=0` 时把外源前缀整段算进去。
- **为什么会写出 (Why)**：走链时 `edge.contains(worker)` 已按完整 CPU `WorkerKey`，误以为开续查不必再对齐引擎 DP。HBM 与 CPU 的 `medium`/`backend_id` 不同，不能比整份 `WorkerKey`；`IpOnly` 后又让每个 DP 都拥有同一条续接边，断点位置会串 DP。
- **修复 (Fix)**：断点续查仅当 `w.instance_id == b.instance_id && w.dp_rank == b.dp_rank`；root 走查仍按首边 owner 无条件进行。
- **测试拦截 (Test interception)**：`test_hbm_cpu_continuation_requires_same_instance_and_dp` — 同节点 DP1 与另一实例持有相同 CPU 尾边，不得从 DP0 HBM 断点续查。
- **场景 (Scenario)**：节点级 CPU/Disk（Mooncake/Memcache/YuanRong `IpOnly`），多 DP 共享 CPU 边，仅部分 DP 有 HBM 前缀。
- **关键词 (Keywords)**：kv_conductor, lower_tier_lookup, continuation, instance_id, dp_rank, IpOnly, cpu_blocks。
