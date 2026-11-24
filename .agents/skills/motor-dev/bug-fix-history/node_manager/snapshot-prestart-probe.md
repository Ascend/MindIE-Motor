# [2026-11-17] 恢复元数据未到齐时误探测旧 endpoint

- **现象 (Symptom)**：恢复后缺少 job_name，注册准备未能刷新配置 Pod IP；旧地址健康探测被拒绝，endpoint 从 WAIT2START 变为 ABNORMAL，控制面尚未就绪就累计异常自杀。
- **根因 (Root cause)**：`HeartbeatManager._refresh_native_engine_status` 只比较 endpoint IP 和配置 Pod IP。注册准备失败时两者同为旧值，过滤不生效；此时 start 尚未执行，generation 也没有改变。
- **为什么会写出 (Why)**：把 IP 是否变化当成恢复状态是否可用的判据，遗漏了元数据刷新可能延迟以及探测请求可能跨越 restore。
- **修复 (Fix)**：只扩展已有两处过滤条件，恢复后 start 未完成时跳过探测并拒绝在途结果。保留旧 IP 过滤、generation 校验和原有仲裁，不增加模块或改变保存协议。
- **测试拦截 (Test interception)**：扩展已有恢复等待测试，覆盖配置 IP 尚未刷新；新增跨恢复探测结果丢弃及 start 后真实故障仍可检测的测试。前两种场景在旧逻辑上失败，修复后通过。
- **场景 (Scenario)**：3.2.0 的最小修复，针对用户日志中旧 .59 地址在配置刷新成 .76 前已写入 ABNORMAL 的路径。完整 Sentinel/保存屏障重构独立保存在 master 基线的 refactor/snapshot 分支，尚未进行真实 NPU/CRIU 验收。
- **关键词 (Keywords)**：snapshot, restore, stale pod IP, probe, started_after_restore
