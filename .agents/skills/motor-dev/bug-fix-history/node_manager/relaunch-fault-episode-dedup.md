# [2026-09-29] Relaunch 后同一 endpoint 再次异常未触发恢复

- **现象 (Symptom)**：首次 engine relaunch 成功后，endpoint 再次从 NORMAL 变为 ABNORMAL，NodeManager 持续打印异常计数达到阈值，但不再向 Controller 报告 engine death，因而不触发第二轮恢复。
- **根因 (Root cause)**：`motor/node_manager/core/daemon.py::_finish_engine_restart` 在 readiness 成功后只恢复 EngineFtManager，没有解除 suicide freeze 或清除 `_reported_abnormal_ep_ids`。freeze 期间 `_check_suicide_condition` 提前返回，错过 NORMAL 分支的去重清理；同一 endpoint 再次异常时被旧 episode 的去重标记跳过。
- **为什么会写出 (Why)**：实现假设 arbitration 一定会在 relaunch 恢复 NORMAL 后执行一次非冻结检查，却忽略了 readiness 成功与 freeze deadline 是两套独立生命周期，恢复和再次异常都可能发生在 freeze 窗口内。
- **修复 (Fix)**：relaunch readiness 成功后，在 suicide lock 内解除 freeze、重置计数/自杀标志并清除 endpoint 异常上报去重集合，使成功恢复明确结束旧 fault episode。
- **测试拦截 (Test interception)**：`test_successful_relaunch_allows_same_endpoint_fault_to_be_reported_again` 构造旧 endpoint 已上报且处于 freeze 的状态，完成成功 relaunch 后立即注入同 endpoint ABNORMAL，断言产生新的 Controller 软件故障报告并开启新 episode 的 freeze。
- **场景 (Scenario)**：启用 engine relaunch；首次异常已成功上报；relaunch 成功后 endpoint 在原 freeze deadline 到期前再次异常，docker-only 且 `container_restart_managed=false` 时现象尤其明显。
- **关键词 (Keywords)**：NodeManager, engine relaunch, suicide freeze, abnormal dedup, repeated recovery
