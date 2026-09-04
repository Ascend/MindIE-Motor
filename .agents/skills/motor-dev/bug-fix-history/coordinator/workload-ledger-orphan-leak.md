# [2026-09-04] SHM CAS 分配成功后被取消，active_tokens 永久泄漏

- **现象 (Symptom)**：压测中客户端断连/`infer_timeout` 后，个别 endpoint 的 SHM `active_tokens` 持续偏高且无对应 release 日志；负载感知调度逐渐避开这些"虚高"端点。旧 ZMQ ALLOCATE_ONLY 时代的同类 repro 在 Rust 重构（PR822）后失效，但症状类别仍在。
- **根因 (Root cause)**：三个窗口共同导致"账本已 CAS 提交、本地待释放记录缺失、释放静默 no-op"。
  1. **W1（主窗口）** `unified_pd.py::_create_attempt`：P 已分配后，D 腿 `_prepare_attempt_resource` 期间被 `asyncio.CancelledError` 打断（`stream_response.py` 用 `stream_task.cancel()` 投递），`except Exception` 接不住（Py3.8+ `CancelledError` 继承 `BaseException`），`p_resource` 是局部变量、`AttemptContext` 尚未创建，引用永久丢失。
  2. **W2（窄窗口）** `_prepare_attempt_resource` / `base.py::prepare_resource`：CAS 已提交后 `add_req_attempt_workload`/`add_req_workload` 这行 await 被取消（仅在 `RequestManager._lock` 争用时可达——`asyncio.Lock.acquire()` 无争用快路径没有 await），无回滚。
  3. **W3（兜底缺失）** `request_manager.py::del_req_info` 把残留 workload 记录静默删除；而 `WorkloadActionHandler.compute_and_update(RELEASE_TOKENS)` 依赖该记录计算释放量，记录没了释放就静默跳过。
- **为什么会写出 (Why)**：两个认知盲区。① `except Exception` 直觉上"接住一切"，但 `CancelledError` 是 `BaseException`——凡是"资源已提交、清理靠 except"的代码块都必须用 `BaseException` 或 finally；② 本地记录（`_req_workload_dict`）既是"待释放量"又是"唯一线索"，丢记录 = 丢释放能力，但没有任何终局对账兜底。
- **修复 (Fix)**：commit `02ade6fa`。
  - W1：`_create_attempt` 改 `except BaseException`，P 腿释放改后台提交 + `_drain_release_tasks()`（drain 被再次取消时 shielded gather 兜底），消息用 `{e!r}`。
  - W2：两处 `add_req_*` 包 `try/except BaseException` → ERROR 日志 + `_rollback_allocated_workload`（内部 shield）→ re-raise。
  - W3：`RequestManager` 新增 `_req_workload_owner`（key → (instance_id, endpoint_id)）与 `pop_residual_workloads()`；`BaseRouter._manage_request_context` 的 finally 在 `CancelScope(shield=True)` 内先 `_reclaim_residual_workloads()`（先 drain 在途释放防双扣，再逐条 `update_workload(RELEASE_TOKENS)` 回收 + ERROR 日志）再 `del_req_info`；`del_req_info` 残留改为 ERROR 金丝雀日志（正常不应触发）。
- **测试拦截 (Test interception)**：`test_unified_pd_create_attempt_releases_p_when_d_allocation_cancelled`（W1，旧代码 FAILED）、`test_prepare_resource_rolls_back_when_bookkeeping_cancelled` + `test_unified_pd_prepare_attempt_resource_rolls_back_when_bookkeeping_cancelled`（W2，旧代码 FAILED）、`test_reclaim_residual_workloads_*` 三条 + `test_pop_residual_workloads_*` + `test_del_req_info_logs_orphan_workload`（W3）。
- **场景 (Scenario)**：流式请求在"P 已选中、D 正在选点"的窗口内客户端断连或 `infer_timeout` 触发；KV affinity 候选查询越慢窗口越宽。释放语义变更时注意：残留记录只会在 `finalize_release`（账本 ACK 后）删除，这是 reclaim 不双扣的前提。
- **关键词 (Keywords)**：active_tokens leak, CancelledError BaseException, cas_add rollback, pop_residual_workloads, orphan workload
