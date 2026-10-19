# [2026-09-19] DP 请求统计遗漏非零 worker 和重复窗口

- **现象 (Symptom)**：发送 1200 条请求但 dp_stats 日志仅统计到 200 多条。
- **根因 (Root cause)**：`scheduler_connection_manager.py` 只开启 worker 0 输出，而 `DpStatsLogger` 使用进程内 Counter；`dp_stats.py` 清空窗口计数后对相同非零窗口去重；`scheduler_client.py` 断开连接时取消定时任务但不输出尾部窗口。
- **为什么会写出 (Why)**：把共享 token 快照和本地请求增量混用同一种输出策略，并将增量误当成可去重的状态值。
- **修复 (Fix)**：所有推理 worker 输出本地计数并附 worker 索引；仅对无请求且状态不变的窗口去重；正常断开时补打尾部窗口。requests 跨 worker 求和，active_tokens 不求和；强制终止仍可能丢失未输出计数。
- **测试拦截 (Test interception)**：`test_emit_window_preserves_repeated_nonzero_windows` 验证重复窗口；`test_all_workers_emit_1200_allocations_including_final_window` 验证 5 个 worker 的 1200 次分配完整输出，包含相同窗口、尾部补打和重复断开。
- **场景 (Scenario)**：多 worker 分流、稳定负载产生相同窗口，或正常退出时最后一个窗口尚未到期。
- **关键词 (Keywords)**：dp_stats, worker-local Counter, repeated window, graceful disconnect
