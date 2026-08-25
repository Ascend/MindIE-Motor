# [2026-08-25] 增量实例刷新失败后未及时收敛

- **现象 (Symptom)**：Controller 向 Coordinator 发送 ADD、DEL、PAUSE 或 RESUME 失败时，该事件被直接丢弃，只能等待最长一个周期的定时 SET；SET 发送失败时仍可能把当前实例指纹标记为已同步。
- **根因 (Root cause)**：`EventPusher._event_consumer()` 忽略 `CoordinatorApiClient.send_instance_refresh()` 的布尔返回值，只处理抛出的异常，并无条件更新成功 SET 的指纹状态。
- **为什么会写出 (Why)**：把“HTTP 调用未抛异常”等同于“Coordinator 已接受刷新”，没有把业务失败返回纳入最终一致性状态机。
- **修复 (Fix)**：失败的增量刷新排队一次完整 SET 对账，连续失败合并为一个待处理 SET；SET 失败不递归排队，也不更新 `_last_sent_fingerprint`。
- **测试拦截 (Test interception)**：`test_event_pusher.py` 覆盖四类增量事件失败、连续失败合并、SET 失败不循环和不推进指纹。
- **场景 (Scenario)**：Coordinator 暂时不可达、返回冲突或拒绝刷新，随后恢复服务时。
- **关键词 (Keywords)**：Controller, EventPusher, incremental refresh, SET reconciliation, fingerprint
