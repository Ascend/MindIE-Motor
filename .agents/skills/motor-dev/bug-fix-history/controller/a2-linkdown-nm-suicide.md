# [2026-09-02] A2 linkdown 被降成 L2，P/D 实例无法自杀恢复

- **现象 (Symptom)**：Atlas 800I_A2 PD 分离出现 `0x81078603` / `CardNetworkUnhealthy` / PreSeparateNPU 后，FaultManager 把 L6 降成 L2，策略空转。Decode 若走 ScaleP2D 只杀 P、不 stop D。
- **根因 (Root cause)**：有活跃业务时 PreSeparateNPU 一律降 L2；L6 Decode 固定 ScaleP2D。节点级故障还会把同机另一角色一起隔离。
- **为什么会写出 (Why)**：把 PreSeparate 当成「业务还在、只能自愈」，没区分 A2 linkdown 必须整实例退出；ScaleP2D 假设 D 已经挂了。
- **修复 (Fix)**：A2 该故障码保持 L6；按 NPU `device_id` 归属实例（无 device_id 时 fail-closed）；仅 A2 隔离码的 P / D / 多 Pod union 走 `NmSuicideStrategy`。stop 不可达当已退出；stop 非断连失败 `mark_failed`；旧 instance id 不向 Coordinator 发 DEL。
- **测试拦截 (Test interception)**：`test_fault_types.py` 卡归属与 A2 判定；`test_strategy.py` A2 Decode/Prefill linkdown→NmSuicide、非隔离 Prefill L6→None、其它 Decode L6→ScaleP2D；`test_nm_suicide.py` 停全部 NM / 不可达不失败 / 5xx mark_failed / 跳过已替换 id；`test_event_pusher.py` 跳过旧 id DEL。
- **场景 (Scenario)**：`hardware_type` 为 `800I_A2`/`800I-A2` 的 PD 分离，P 或 D（含 `single_*_instance_pod_num>1`）发生 linkdown。
- **关键词 (Keywords)**：A2, linkdown, 0x81078603, NmSuicide, PreSeparateNPU
