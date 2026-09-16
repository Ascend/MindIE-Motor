# 精度检测特性

## 特性介绍

精度检测特性用于发现Decode实例在推理过程中出现的token级输出质量异常，例如大段重复、乱码和生僻字异常。

### 工作原理

精度检测功能开启后，Coordinator会在Decode请求中注入logprobs、top_logprobs和return_token_ids，从推理响应中采集token id与logprob，再按实例组维度定时送检。

当同一个实例组连续多次被检测为异常时，Coordinator会通过内部Router对目标实例组发起固定问答拨测。拨测完成后，Coordinator上报精度异常告警到Controller；如果Controller侧开启自动恢复，Controller会终止告警中的Decode实例，并在告警携带Prefill实例ID时同步终止Prefill实例。

Coordinator的请求处理链路会发生以下变化：

1. Router 选定 Decode 实例后，SampleController 通过 Scheduler ZMQ 按 Decode 实例原子抢占采样窗口。
2. 仅抢占成功的请求注入 logprobs、top_logprobs 和 return_token_ids；同一 Decode 实例每 interval_seconds 最多注入一次。
3. Router 从流式或非流式响应中缓存完整的 prompt_token_ids、output_token_ids、logprobs 和 topk_logprobs，并在返回用户前恢复其原始 logprobs 数量：用户未请求时删除，用户请求较少时裁剪，用户请求较多时不缩减。
4. 用户响应结束后，完整样本进入受跟踪的后台任务；PrecisionReporter 和 MsprobeChecker 不阻塞用户响应及 TPOT。
5. PrecisionReporter 通过 Scheduler 按 (p_instance_id, d_instance_id) 记录跨 Worker 的连续异常次数；Hybrid 使用 (None, union_id)。
6. 连续异常达到 precision_issue_threshold 后，InternalRouterProbe 固定问答拨测目标实例组。
7. PrecisionAlarm 构造精度异常告警并上报 Controller。
8. Controller 根据 precision_auto_recovery_enabled 决定是否调用恢复服务终止实例；终止成功后 Controller 上报 CLEAR 并通知 Coordinator 清理 Scheduler 活动状态。
9. 活动告警下，Coordinator 连续 precision_clear_threshold 次有效正常检测后，自动上报 CLEAR 清除告警（不依赖 auto-recovery，适用于关闭自动恢复或告警仍活动的场景）。
10. 若部署 CCAE Reporter，Controller 侧 Reporter 会调用 Controller 既有接口 /controller/terminate_instance 终止 D 实例；请求体可携带 p_instance_id 和 precision_alarm_clear=true，由 Controller 在终止 P/D 实例组后附加清除精度告警，并继续向 CCAE 成功上报 controlStatus=Completed 共 10 次，之后才停止该 precision task 的上报。

### 约束与限制

**依据原文上下文内容重组，请进行人工校验。**

| 约束维度 | 要求 |
|----------|------|
| 硬件 | **内容缺失，需要人工补齐。** |
| 部署场景 | <ul><li>仅**支持** PD 分离、CDP/Hybrid 等经过 Coordinator Router 转发 Decode 请求的部署形态；</li><li>**不支持**未经过 Coordinator Router 的直连推理请求。</li></ul> |
| 引擎 | <ul><li>推理引擎**必须支持** return_token_ids、logprobs 和 top_logprobs 返回字段；（**建议明确写当前验证过的推理引擎**）</li><li>不支持上述字段的引擎无法使用本特性。</li></ul> |
| 特性互斥 | **内容缺失，需要人工补齐。** |
| 软件依赖 | 运行环境须安装 msprobe，且 msprobe.response_anomaly.detector.ILLDetector 可导入。 |
| 其他限制 | <ul><li>precision_check_enabled=true 后，仅抢占采样窗口成功的 Decode 请求会注入 logprobs；请求失败也会消耗本次窗口，不回退、不转让。</li><li>Scheduler 客户端不可用时，Coordinator 启动阶段会将 sampling_manager 置为 None，本轮进程不会启用完整采样链路。</li><li>检测异常时采用 fail-open：msprobe 执行失败、top-k 与 token 数量不对齐、Scheduler ZMQ 失败等场景不会中断用户请求，也不会误触发恢复。</li><li>精度拨测使用固定问题“相对论的发明人是谁”，响应中需包含“爱因斯坦”才认为单次拨测通过。</li><li>自动恢复只由 Controller 侧 precision_auto_recovery_enabled 控制，不依赖 observability 告警展示开关。</li><li>本版本仅保证用户请求的 logprobs 数量不被 Motor 的额外采样改变。采样请求仍会强制 return_tokens_as_token_ids=true，用户主动请求 logprobs 时，token 表示形式可能与未采样请求不同；该兼容性待后续方案确认。</li><li>精度检测对象在进程启动时装配；修改嵌套的 precision_detection_config 后需要重启 Coordinator Worker 才能确保全部字段生效。</li></ul> |

## 特性使用

### 使用场景

| 维度 | 说明 |
|------|------|
| 部署形态 | PD 分离、CDP/Hybrid 等经过 Coordinator Router 转发 Decode 请求的部署形态。 |
| 检测对象 | Decode 输出 token 序列及对应 logprob。 |
| 检测粒度 | PD 实例组；PD 分离使用 (p_instance_id, d_instance_id)，Hybrid 场景使用 (None, union_id) |
| 异常类型 | logprobs_count=1 支持大段重复；大于等于3 额外支持乱码；大于等于5 额外支持生僻字。 |
| 处置方式 | Coordinator 上报告警，Controller 可选自动终止 D/P 实例。 |

### 使用样例

**依据原文上下文内容重组，请进行人工校验。**

1. 确认推理引擎支持 `return_token_ids` 和 `logprobs` 返回字段。
2. 确认运行环境已安装 msprobe，且 msprobe.response_anomaly.detector.ILLDetector 可导入。（**这一步是环境准备，有现成的文档还需要提供跳转链接，否则提供安装方法**）
3. 在 user_config.json 的 motor_coordinator_config 中增加 precision_detection_config，开启精度检测功能。

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

   **依据原文上下文内容重组，请进行人工校验。**

   | 配置项 | 类型 | 取值范围 | 必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | precision_check_enabled | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | false | 精度检测总开关。关闭时不注入 `logprobs`、不采样、不检测，性能零额外开销 |
   | interval_seconds | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 30.0 | 每个实例组允许送检一条完整请求样本的最小间隔，单位秒 |
   | logprobs_count | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 1 | 注入到 Decode 请求的 top-k 宽度；值越大检测能力越强，引擎侧开销也越高 |
   | precision_issue_threshold | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 10 | 同一实例组连续检测异常达到该次数后触发拨测与告警 |
   | precision_clear_threshold | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 10 | 活动告警下连续有效正常样本达到该次数后上报清除告警 |
   | probe_max_attempts | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 3 | 精度拨测请求次数 |
   | probe_timeout_seconds | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 600.0 | 单次拨测超时时间，单位秒 |

4. （可选）如需根据精度告警终止实例，则需要在 Controller 配置中开启 precision_auto_recovery_enabled配置。

   ```json
   {
     "motor_controller_config": {
       "precision_auto_recovery_enabled": true
     }
   }
   ```

   **依据原文上下文内容重组，请进行人工校验。**

   | 配置项 | 类型 | 取值范围 | 必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | precision_auto_recovery_enabled | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | 内容缺失，需要人工补齐。 | false | Controller 收到 `alarm_id=0xFC001009` 的精度告警后，是否自动终止告警中的 D/P 实例 |

   >[!NOTE] 说明
   >精度检测分为 Coordinator 侧检测开关和 Controller 侧自动恢复开关，两者相互独立；只开启 Coordinator 开关时会检测并上报告警；只有同时开启 Controller 自动恢复时，Controller 才会根据精度告警终止实例。

5. 使用现有 deploy 脚本重新部署服务：

   ```bash
   cd examples/deployer
   python deploy.py --config_dir ../infer_engines/vllm
   ```

   Coordinator 日志中出现 Precision check 关键字表示精度检测链路已启用。

### 验证特性

服务启动后，Coordinator 日志中出现以下关键字表示精度检测链路已启用：

```text
Precision check (token sampling): interval=...
exit_gate=scheduler_zmq streak=scheduler_zmq probe=internal_router
```

发送普通推理请求后，可在 Coordinator 日志中观察采样链路：

| 日志关键词 | 含义 |
|------------|------|
| PrecisionSample: inject_logprobs | Router 已向 Decode 请求注入采样参数 |
| SampleController: confirmed (scheduler) | Scheduler 放行了本实例组的一条样本 |
| PrecisionSample: submit | 样本已构造并提交给检测链路 |
| MsprobeChecker: result | msprobe 已返回检测结果 |
| PrecisionReporter: threshold reached | 连续异常达到阈值，开始拨测与告警 |
| PrecisionAlarm: reporting alarm_id=0xFC001009 | 精度告警已上报 Controller |
| Precision auto-recover: terminating D instance_id= | Controller 已触发自动恢复 |

## 调优建议

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

| 关键参数 | 建议取值 | 说明 |
|----------|----------|------|
| logprobs_count | 内容缺失，需要人工补齐。 | `logprobs_count=1` 支持大段重复；`>=3` 额外支持乱码；`>=5` 额外支持生僻字；值越大检测能力越强，引擎侧开销也越高 |
| interval_seconds | 内容缺失，需要人工补齐。 | 每个实例组允许送检一条完整请求样本的最小间隔，默认 30.0 秒 |
| precision_issue_threshold | 内容缺失，需要人工补齐。 | 同一实例组连续检测异常达到该次数后触发拨测与告警，默认 10 |

## 常见问题

**依据原文上下文内容重组，请进行人工校验。**

### 启动后没有 Precision check 日志

**问题描述**

服务启动后，Coordinator 日志中没有出现 Precision check 关键字。

**原因分析**

`precision_check_enabled=false` 或配置未加载。

**解决步骤**

检查 motor_coordinator_config.precision_detection_config 配置是否正确加载。

### 日志提示 Scheduler client unavailable

**问题描述**

Coordinator 日志中出现 Scheduler client unavailable 提示。

**原因分析**

Worker 未连上 Mgmt 控制面 IPC。

**解决步骤**

检查 Mgmt 进程是否已 bind `scheduler_frontend` 以及 ZMQ 路径。

### 日志提示 sample incomplete

**问题描述**

Coordinator 日志中出现 sample incomplete 提示。

**原因分析**

引擎返回 `token_ids` 但未返回 `logprobs`。

**解决步骤**

检查引擎是否支持 `logprobs` 参数。

### 日志提示 MsprobeChecker: msprobe not installed

**问题描述**

Coordinator 日志中出现 MsprobeChecker: msprobe not installed 提示。

**原因分析**

环境缺少 msprobe。

**解决步骤**

安装 msprobe 或在测试中显式注入 mock checker。

### 一直检测不到生僻字

**问题描述**

开启精度检测后，始终无法检测到生僻字异常。

**原因分析**

`logprobs_count` 取值小于 5 或 token2category 映射缺失。

**解决步骤**

调大 `logprobs_count`，检查 msprobe 映射文件。

### 告警已上报但未终止实例

**问题描述**

精度告警已上报 Controller，但未触发实例终止。

**原因分析**

Controller 未开启自动恢复。

**解决步骤**

检查 `precision_auto_recovery_enabled` 是否已开启。

### 只终止 D 实例未终止 P 实例

**问题描述**

自动恢复时只终止了 Decode实例，未同步终止 Prefill 实例。

**原因分析**

告警中 `p_instance_id` 为空。

**解决步骤**

检查部署模式和实例组 key 是否能解析 P 实例。
