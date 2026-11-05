# [2026-09-07] Precision 告警测试未覆盖 raise 与 clear 的稳定 moi 契约

- **现象 (Symptom)**：precision 告警测试断言 `moi` 与 `location` 完全相等，但告警 payload 的 `moi` 还包含 PD 实例标识，旧断言在三个身份回退场景失败。
- **根因 (Root cause)**：测试沿用了 service/IP 位置字段的旧形状，没有按 `build_precision_moi` 的 PD 分组标识契约构造期望值，也没有覆盖 POD IP 变化后的清除 payload。相关告警 ID、reason ID 与主备切换事件字段的部分测试期望还直接读取生产常量或同一生产配置表，生产定义与期望可能一起变化，无法拦截外部契约漂移。
- **为什么会写出 (Why)**：只关注告警位置文本，遗漏了 raise 与 clear 需要通过同一个 moi 关联的生命周期语义；同时没有检查测试期望是否独立于被测定义。
- **修复 (Fix)**：更新完整的 precision moi 期望值；覆盖显式 IP、环境缓存与测试间环境隔离，并参数化验证 `p_instance_id` 有值和缺失时 raise→clear 仍复用原 moi。告警 ID 与事件 reason ID 使用独立字面量；reason enum 使用完整成员映射；主备切换事件用独立参数期望表验证组件 ID、名称和 probable cause。
- **测试拦截 (Test interception)**：`test_precision_alarm_identity_fallbacks`、`test_build_precision_moi_*` 和 `test_precision_alarm_clear_reuses_raise_moi_after_pod_ip_changes` 覆盖完整 moi、缓存读取与清除状态字段。使用 `bash tests/run_tests.sh` 对五组生产定义突变作了实际验证：三个告警 ID 一组命中 7 个断言失败；六个 reason 值一组命中 6 个映射失败；主备切换事件的 ID、名称和 probable cause 分别单独突变时，每组两个参数用例均失败。每组后生产文件均恢复为变异前字节，最终相关生产目录无差异；alarm 与 precision 集成目标最终 62 项通过，覆盖率 99.52%。
- **场景 (Scenario)**：Coordinator 产生 precision 告警后 Pod IP 改变，随后发送对应的清除告警；或同一进程连续处理不同测试/环境的 PD 告警。
- **关键词 (Keywords)**：precision alarm、moi、POD_IP、raise clear、PD instance

## 补充（2026-11-03）：无 Prefill 时说明字段采用规范化空值

- **现象 (Symptom)**：`p_instance_id=None` 时，结构化字段 `p_instance_id` 已是空字符串，但 `additional_information` 和北向格式化结果仍暴露 Python 字面量 `None`。
- **根因 (Root cause)**：说明文本直接插入构造器原始参数，没有复用已归一化的 `self.p_instance_id`。
- **修复 (Fix)**：说明文本改用 `self.p_instance_id`；结构化字段、MOI 与 Store 的活动告警和清除规则保持原样。
- **测试拦截 (Test interception)**：单测用独立完整字符串断言 payload 与 `deserialize_incoming_record(...).format()`；Store 闭环覆盖无 P 的 raise、同 MOI 去重、查找、北向输出和复用原 MOI 的 clear，并检查清除后的北向输出。
- **场景 (Scenario)**：Coordinator 产生没有 Prefill 实例标识的 precision 告警，并由 Controller 观测及后续清除。
- **关键词 (Keywords)**：precision alarm、missing Prefill、additional information、empty p_instance_id、AlarmStore
