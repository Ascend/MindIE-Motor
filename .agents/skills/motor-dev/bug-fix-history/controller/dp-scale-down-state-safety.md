# [2026-09-15] DP 缩容等待状态和瞬时故障导致路由冻结或误缩容

- **现象 (Symptom)**：故障证据自行消失后实例保持摘流；一次引擎查询抖动或重拉加载期不可达可能被提交为永久退役 rank。
- **根因 (Root cause)**：`WAITING_ENGINE_FAULT` 没有健康对账出口；传输异常被合成为 DEAD；原地重拉未等 readiness 就恢复 FT 轮询。
- **为什么会写出 (Why)**：把传输层可达性当成引擎权威状态，并只设计了策略成功/失败出口，遗漏了证据自愈出口。
- **修复 (Fix)**：健康对账恢复并发布最后提交拓扑；查询异常返回 unknown；轮询恢复失败阈值；重拉 readiness 前保持暂停。
- **测试拦截 (Test interception)**：覆盖等待状态恢复、查询超时、连续失败阈值、重拉 readiness 和硬件关联窗口后的完整快照。
- **场景 (Scenario)**：多 rank 分批上报、短时网络抖动、模型重拉加载以及故障在策略启动前自行恢复。
- **关键词 (Keywords)**：DP scale-down, WAITING_ENGINE_FAULT, false DEAD, engine relaunch, serving overlay
