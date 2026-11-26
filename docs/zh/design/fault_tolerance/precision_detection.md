# 精度检测

## 概述

Motor 集成 vLLM-Ascend Runtime Guard 的异常计数，并把 Decode 侧的新增结果透传为精度异常告警。检测器是否命中由引擎决定。Motor 读取 `/metrics` 上的计数，确认增加后上报 `alarm_id=0xFC001009`。

| 项 | 说明 |
|----|------|
| 用户指南 | [精度检测功能](../../user_guide/features/maintenance_testing_security/precision_detection.md) |
| 计数来源 | vLLM-Ascend Runtime Guard，`incident_type` 区分 `token_repeat`、`logits_finite`、`spec_acceptance` |
| 消费与上报 | `motor/coordinator/metrics/runtime_guard_pipeline.py` |
| 告警报文 | `motor/common/alarm/precision_issue_alarm.py` |
| 活动状态 | Scheduler 上的精度告警状态，key 为 `(None, decode_instance_id)` |
| 自动恢复 | `motor/controller/core/recovery_service.py` |

## 链路

```text
Decode 引擎 Runtime Guard
  → endpoint /metrics 计数
  → MetricsCollector 抓取
  → RuntimeGuardPipeline 确认新增
  → 上报 Precision Anomaly Alarm
  → Controller 按 precision_auto_recovery_enabled 决定是否终止该 Decode
```

可观测进程启动后即挂上这条处理。检测器默认关闭，没有计数就不会告警。本链路不修改用户请求，不调用软件故障入口，不做 DP 缩容。

出现新增时，Coordinator 对该 Decode endpoint 做一次拨测，并把失败次数写入告警附加信息。拨测不决定是否上报。Prefill 由当次调度选择，不写入告警。

## 告警与清除

**告警**

告警归属 Decode 实例。`p_instance_id` 为空。附加信息在原有计数字段后包含 `abnormal_ports`。

活动告警不重复抬起。当前各 endpoint 连续无新增达到 `precision_clear_threshold`（默认 10）后，用原 MOI 上报清除。已知 endpoint 从抓取名单消失时，不把该实例记为正常。

`precision_auto_recovery_enabled` 默认关闭。打开后，Controller 终止告警中的 Decode 实例；终止成功则清除告警，并通知 Coordinator 删除活动状态。

**清除**

清除记录与抬起使用同一 MOI：`service name=Coordinator, service ip=<POD_IP>, instanceId=<Decode实例ID>`。

| 路径 | 行为 |
|------|------|
| 连续无新增 | Coordinator 上报 CLEAR |
| 自动恢复成功 | Controller 终止 Decode 后清除，并通知 Coordinator |
| 外部精度恢复 | 复用 Controller 已有恢复接口；本告警的 Prefill 为空 |
