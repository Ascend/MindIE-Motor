# [2026-09-28] 恢复前旧心跳的 503 误触发新实例重注册

- **现象 (Symptom)**：快照恢复的正常 register/start 成功，但旧实例心跳返回 503 后，以新实例 ID 触发 reregister；不完整的组装记录阻挡下一次同 job_name 的 register。
- **根因 (Root cause)**：`HeartbeatManager._report_heartbeat_loop` 只在异常返回时读取 started 状态；HTTP 请求及其重试期间 start 可以完成，`RegisterManager` 已换成新身份。
- **为什么会写出 (Why)**：把响应时的生命周期状态当作请求发出时的状态，忽略快照可能保留在途请求。
- **修复 (Fix)**：在读取心跳字段前记录 `sent_after_restore_start`。仅恢复路径的 503 要求本次请求在 start 完成后采样；冷启动分支不变，不跳过循环末尾 sleep。
- **测试拦截 (Test interception)**：`test_503_only_reregisters_current_post_start_heartbeat` 参数化覆盖发送后才 start、请求跨越 restore、恢复后正常 503、尚未 start、冷启动 503；心跳测试文件 41 项通过。
- **场景 (Scenario)**：干净冷启动快照恢复后先 register 再 start，旧心跳延迟返回。不是同一进程任意多代实例切换的通用 generation 锁方案，也不清理 Controller 已有残留记录。
- **关键词 (Keywords)**：snapshot, heartbeat, 503, reregister, start race
