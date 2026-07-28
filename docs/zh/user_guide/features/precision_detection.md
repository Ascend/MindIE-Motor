# 精度检测功能

## 特性介绍

精度检测功能用于发现 Decode 实例在推理过程中出现的 token 级输出质量异常，例如大段重复、乱码和生僻字异常。开启后，Coordinator 会在 Decode 请求中注入 `logprobs`、`top_logprobs` 和 `return_token_ids`，从推理响应中采集 token id 与 logprob，再按实例组维度定时送检。

当同一个实例组连续多次被检测为异常时，Coordinator 会通过内部 Router 对目标实例组发起固定问答拨测。拨测完成后，Coordinator 上报精度异常告警到 Controller；如果 Controller 侧开启自动恢复，Controller 会终止告警中的 Decode 实例，并在告警携带 Prefill 实例 ID 时同步终止 Prefill 实例。

更完整的实现说明见 [精度检测设计文档](../../design/fault_tolerance/precision_detection.md)。

## 适用场景

| 维度 | 说明 |
|------|------|
| 部署形态 | PD 分离、CDP/Hybrid 等经过 Coordinator Router 转发 Decode 请求的部署形态 |
| 检测对象 | Decode 输出 token 序列及对应 logprob |
| 检测粒度 | PD 实例组；PD 分离使用 `(p_instance_id, d_instance_id)`，Hybrid 场景使用 `(None, union_id)` |
| 异常类型 | `logprobs_count=1` 支持大段重复；`>=3` 额外支持乱码；`>=5` 额外支持生僻字 |
| 处置方式 | Coordinator 上报告警；Controller 可选自动终止 D/P 实例 |

**不适用于：**

- 未经过 Coordinator Router 的直连推理请求。
- 不支持 `return_token_ids`、`logprobs` 或 `top_logprobs` 的推理引擎。
- 未安装 `msprobe` 运行依赖的生产环境。

## 配置说明

精度检测分为 Coordinator 侧检测开关和 Controller 侧自动恢复开关。两者相互独立：只开启 Coordinator 开关时会检测并上报告警；只有同时开启 Controller 自动恢复时，Controller 才会根据精度告警终止实例。

### Coordinator 配置

在 `user_config.json` 的 `motor_coordinator_config` 中增加 `precision_detection_config`：

```json
{
  "motor_coordinator_config": {
    "precision_detection_config": {
      "precision_check_enabled": true,
      "interval_seconds": 30.0,
      "logprobs_count": 5,
      "precision_issue_threshold": 10,
      "precision_clear_threshold": 10,
      "probe_max_attempts": 3,
      "probe_timeout_seconds": 600.0
    }
  }
}
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `precision_check_enabled` | `false` | 精度检测总开关。关闭时不注入 logprobs、不采样、不检测，性能零额外开销 |
| `interval_seconds` | `30.0` | 每个实例组允许送检一条完整请求样本的最小间隔，单位秒 |
| `logprobs_count` | `1` | 注入到 Decode 请求的 top-k 宽度；值越大检测能力越强，引擎侧开销也越高 |
| `precision_issue_threshold` | `10` | 同一实例组连续检测异常达到该次数后触发拨测与告警 |
| `precision_clear_threshold` | `10` | 活动告警下连续有效正常样本达到该次数后上报清除告警 |
| `probe_max_attempts` | `3` | 精度拨测请求次数 |
| `probe_timeout_seconds` | `600.0` | 单次拨测超时时间，单位秒 |

### Controller 配置

如果需要收到精度告警后自动终止实例，在 Controller 配置中开启：

```json
{
  "motor_controller_config": {
    "precision_auto_recovery_enabled": true
  }
}
```

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `precision_auto_recovery_enabled` | `false` | Controller 收到 `alarm_id=0xFC001009` 的精度告警后，是否自动终止告警中的 D/P 实例 |

## 部署流程

1. 确认推理引擎支持 `return_token_ids` 和 `logprobs` 返回字段。
2. 确认运行环境已安装 `msprobe`，且 `msprobe.response_anomaly.detector.ILLDetector` 可导入。
3. 在 Coordinator 配置中开启 `precision_detection_config.precision_check_enabled`。
4. 按需在 Controller 配置中开启 `precision_auto_recovery_enabled`。
5. 使用现有 deploy 脚本重新部署服务：

```bash
cd examples/deployer
python deploy.py --config_dir ../infer_engines/vllm
```

## 运行机制

开启后，Coordinator 的请求处理链路会发生以下变化：

1. Router 向每条 Decode 请求注入 `logprobs`、`top_logprobs` 和 `return_token_ids`。
2. Router 从流式或非流式响应中缓存 `prompt_token_ids`、`output_token_ids`、`logprobs` 和 `topk_logprobs`。
3. 请求完成后，`SampleController` 通过 Scheduler ZMQ 做实例组级出口门控，同一实例组每 `interval_seconds` 最多送检一条样本。
4. `PrecisionReporter` 调用 `MsprobeChecker` 检测样本，并通过 Scheduler 记录跨 Worker 的连续异常次数。
5. 连续异常达到 `precision_issue_threshold` 后，`InternalRouterProbe` 固定问答拨测目标实例组。
6. `PrecisionAlarm` 构造精度异常告警并上报 Controller。
7. Controller 根据 `precision_auto_recovery_enabled` 决定是否调用恢复服务终止实例；终止成功后 Controller 上报 CLEAR 并通知 Coordinator 清理 Scheduler 活动状态。
8. 活动告警下，Coordinator 连续 `precision_clear_threshold` 次**有效正常**检测后，自动上报 CLEAR 清除告警（不依赖 auto-recovery，适用于关闭自动恢复或告警仍活动的场景）。
9. 若部署 CCAE Reporter，Controller 侧 Reporter 会调用 Controller 既有接口 `/controller/terminate_instance` 终止 D 实例；请求体可携带 `p_instance_id` 和 `precision_alarm_clear=true`，由 Controller 在终止 P/D 实例组后附加清除精度告警，并继续向 CCAE 成功上报 `controlStatus=Completed` 共 10 次，之后才停止该 precision task 的上报。

## 验证方法

服务启动后，Coordinator 日志中出现以下关键字表示精度检测链路已启用：

```text
Precision check (token sampling): interval=...
exit_gate=scheduler_zmq streak=scheduler_zmq probe=internal_router
```

发送普通推理请求后，可在 Coordinator 日志中观察采样链路：

| 日志关键词 | 含义 |
|------------|------|
| `PrecisionSample: inject_logprobs` | Router 已向 Decode 请求注入采样参数 |
| `SampleController: confirmed (scheduler)` | Scheduler 放行了本实例组的一条样本 |
| `PrecisionSample: submit` | 样本已构造并提交给检测链路 |
| `MsprobeChecker: result` | msprobe 已返回检测结果 |
| `PrecisionReporter: threshold reached` | 连续异常达到阈值，开始拨测与告警 |
| `PrecisionAlarm: reporting alarm_id=0xFC001009` | 精度告警已上报 Controller |
| `Precision auto-recover: terminating D instance_id=` | Controller 已触发自动恢复 |

## 限制与约束

1. `precision_check_enabled=true` 后，每条 Decode 请求都会注入 logprobs；真正进入检测链路的频率由 `interval_seconds` 控制。
2. Scheduler 客户端不可用时，Coordinator 启动阶段会将 `sampling_manager` 置为 `None`，本轮进程不会启用完整采样链路。
3. 检测异常时采用 fail-open：msprobe 执行失败、top-k 与 token 数量不对齐、Scheduler ZMQ 失败等场景不会中断用户请求，也不会误触发恢复。
4. 精度拨测使用固定问题“相对论的发明人是谁”，响应中需包含“爱因斯坦”才认为单次拨测通过。
5. 自动恢复只由 Controller 侧 `precision_auto_recovery_enabled` 控制，不依赖 observability 告警展示开关。

## 日志与排查

| 现象 | 可能原因 | 建议 |
|------|----------|------|
| 启动后没有 `Precision check` 日志 | `precision_check_enabled=false` 或配置未加载 | 检查 `motor_coordinator_config.precision_detection_config` |
| 日志提示 Scheduler client unavailable | Coordinator 未连接 Scheduler | 检查 Scheduler 进程和 ZMQ 连接 |
| `sample incomplete` | 引擎返回 token_ids 但未返回 logprobs | 检查引擎是否支持 logprobs 参数 |
| `MsprobeChecker: msprobe not installed` | 环境缺少 msprobe | 安装 msprobe 或在测试中显式注入 mock checker |
| 一直检测不到生僻字 | `logprobs_count < 5` 或 token2category 映射缺失 | 调大 `logprobs_count`，检查 msprobe 映射文件 |
| 告警有但未终止实例 | Controller 未开启自动恢复 | 检查 `precision_auto_recovery_enabled` |
| 只终止 D 未终止 P | 告警中 `p_instance_id` 为空 | 检查部署模式和实例组 key 是否能解析 P 实例 |
