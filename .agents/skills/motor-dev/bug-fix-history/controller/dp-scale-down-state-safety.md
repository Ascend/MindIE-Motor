# [2026-09-15] DP 缩容等待状态和瞬时故障导致路由冻结或误缩容

- **现象 (Symptom)**：首个 DP `UNHEALTHY` / `DEAD` 到达后仍等待策略生成才摘流；故障证据自行消失后实例保持摘流；一次引擎查询抖动或重拉加载期不可达可能被提交为永久退役 rank；跨 Pod Decode 的心跳隔离先触发整实例重配置，后到的节点 reboot L6 无法升级到 ScaleP2D；重调度生成的半实例 `INITIAL` 又被误判为恢复成功。
- **根因 (Root cause)**：Coordinator 摘流耦合在缩容策略执行阶段，提前摘流后又遗漏了 apply 前的成功门禁；`WAITING_ENGINE_FAULT` 没有健康对账出口；传输异常被合成为 DEAD；原地重拉未等 readiness 就恢复 FT 轮询；恢复规划器把无引擎/硬件证据的 `INACTIVE` 当成重配置依据，失败重配置被当作终态，ScaleP2D 把 `INITIAL` 与真正恢复的 `ACTIVE` 混为一谈。
- **为什么会写出 (Why)**：把故障收集和策略执行误当成同一安全边界；把传输层可达性当成引擎权威状态，并只设计了策略成功/失败出口，遗漏了证据自愈和更高置信度硬件证据的升级出口；把“开始重建”误当成“重建完成”。
- **修复 (Fix)**：首个有效引擎故障上报立即进入 `WAITING_ENGINE_FAULT` 并摘流，apply 前再次确认摘流成功，失败时进入 fallback；收集窗口改为 Controller 可配置且默认 60 秒；健康对账恢复并发布最后提交拓扑；查询异常返回 unknown；轮询恢复失败阈值；重拉 readiness 前保持暂停；无引擎/硬件证据的心跳隔离先等待 Pod 重调度；后到的 L4-L6 硬件证据可将失败重配置升级为原生策略；ScaleP2D 仅把 `ACTIVE` D 视为恢复成功。
- **测试拦截 (Test interception)**：覆盖首个 DEAD/UNHEALTHY 立即摘流、apply 前摘流失败禁止缩容、开关关闭兼容、60 秒默认值与显式配置、等待状态恢复、查询超时、连续失败阈值、重拉 readiness、硬件关联窗口后的完整快照、无证据隔离等待重调度、失败重配置后升级 ScaleP2D、replacement id 已生成时仍检查 ScaleP2D、`INITIAL` 超时继续保 D，以及缩容发布成功和发布 pending 的分级日志。
- **场景 (Scenario)**：多 rank 分批上报、短时网络抖动、模型重拉加载、跨 Pod Decode 单节点 reboot，以及故障在策略启动前自行恢复。
- **关键词 (Keywords)**：DP scale-down, ScaleP2D, node reboot, reconfiguration, INITIAL, WAITING_ENGINE_FAULT
