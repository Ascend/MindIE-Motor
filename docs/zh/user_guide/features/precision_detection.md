# 精度检测特性

## 特性介绍

精度检测特性用于发现Decode实例在推理过程中出现的token级输出质量异常，例如大段重复、乱码和生僻字异常。

### 工作原理

精度检测功能开启后，Coordinator 在选定 Decode 实例后抢占该实例的采样窗口，仅对抢占成功的请求注入 logprobs、top_logprobs 和 return_token_ids。响应完成后，采集的 token id 与 logprob 进入后台检测；检测结果按最终的 P/D 实例组累计。

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

以下运行约束依据当前实现整理。硬件、引擎版本和特性组合的验证范围需在发布前由测试结果确认。

| 约束维度 | 要求 |
|----------|------|
| 硬件 | <ul><li>Atlas 800I A2推理服务器</li><li>Atlas 800I A3超节点服务器</li><li>Ascend950 PR&950 DT系列产品</li></ul> |
| 部署场景 | <ul><li>仅**支持** PD 分离、CDP/Hybrid 等经过 Coordinator Router 转发 Decode 请求的部署形态；</li><li>**不支持**未经过 Coordinator Router 的直连推理请求。</li></ul> |
| 引擎 | 当前仅支持 vLLM 引擎。`user_config.json` 中 `motor_engine_*_config.engine_type` 须为 `vllm`；引擎须接受 `logprobs`、`top_logprobs`、`return_token_ids` 请求参数，并在响应中返回可对齐的 token id 与 logprob。 |
| 特性互斥 | 用户主动请求 logprobs 时，采样仍会强制 return_tokens_as_token_ids=true，可能改变 token 的表示形式；需要依赖原始文本 token 的客户端须验证兼容性。 |
| 软件依赖 | 运行环境须安装 msprobe，且 msprobe.response_anomaly.detector.ILLDetector 可导入。 |
| 其他限制 | <ul><li>设置precision_check_enabled=true 后，仅抢占采样窗口成功的 Decode 请求会注入 logprobs；请求失败也会消耗本次窗口，不回退、不转让。</li><li>Scheduler 客户端不可用时，Coordinator 启动阶段会将 sampling_manager 置为 None，本轮进程不会启用完整采样链路。</li><li>检测异常时采用 fail-open：msprobe 执行失败、top-k 与 token 数量不对齐、Scheduler ZMQ 失败等场景不会中断用户请求，也不会误触发恢复。</li><li>精度拨测使用固定问题“相对论的发明人是谁”，响应中需包含“爱因斯坦”才认为单次拨测通过。</li><li>自动恢复只由 Controller 侧 precision_auto_recovery_enabled 控制，不依赖 observability 告警展示开关。</li><li>本版本仅保证用户请求的 logprobs 数量不被 Motor 的额外采样改变。采样请求仍会强制 return_tokens_as_token_ids=true，用户主动请求 logprobs 时，token 表示形式可能与未采样请求不同。</li><li>精度检测对象在进程启动时装配；修改嵌套的 precision_detection_config 后需要重启 Coordinator Worker 才能确保全部字段生效。</li></ul> |

## 特性使用

### 使用场景

| 维度 | 说明 |
|------|------|
| 部署形态 | PD 分离等经过 Coordinator Router 转发 Decode 请求的部署形态。 |
| 检测对象 | Decode 输出 token 序列及对应 logprob。 |
| 检测粒度 | PD 实例组；PD 分离使用 (p_instance_id, d_instance_id)，Hybrid 场景使用 (None, union_id) |
| 异常类型 | logprobs_count=1 支持大段重复；大于等于3 额外支持乱码；大于等于5 额外支持生僻字。 |
| 处置方式 | Coordinator 上报告警，Controller 可选自动终止 D/P 实例。 |

### 环境准备

msprobe **不在 Motor 包的依赖清单中**（`requirements.txt` 未声明），因此是否需要单独安装取决于运行环境：

<ul><li>**使用仓库提供的 Motor 镜像**（`docker/mindie-motor-vllm/` 下的 Dockerfile）：镜像已预装 msprobe 并锁定版本 `mindstudio-probe==26.1.0.post1`，无需另行安装。</li><li>**仅在自有环境中安装 Motor 包（wheel）**：该环境需要自行安装配套 msprobe，版本应与发布所用镜像保持一致。</li></ul>

在 Coordinator Worker 所在容器或 Python 环境中确认可导入：

```bash
python -c "from msprobe.response_anomaly.detector import ILLDetector; print('ILLDetector import OK')"
```

需自行安装时：

```bash
python -m pip install "mindstudio-probe==26.1.0.post1"
```

离线环境可改用已获取并核对版本的 wheel。详细安装步骤参见 [msProbe 安装指南](https://gitcode.com/Ascend/msprobe/blob/f5900e0670ab5406b75d79635dde062d80ad84f0/docs/zh/msprobe_install_guide.md)。

安装后还需检查该版本随包提供的 `response_anomaly/configs/config.yaml`、`configs/mtype_config.json` 和 `token2category`，以及目标模型对应的映射。仅有 `import OK` 不能证明目标模型能够正常检测；应继续完成下文的真实请求验证。详细输入要求参见 [msProbe 推理异常检测说明](https://www.hiascend.com/document/detail/en/mindstudio/2610/TITools/msProbe/docs/en/user_guide/response_anomaly_instruct.md)。

### 使用样例

1. 确认推理引擎支持 `return_token_ids` 和 `logprobs` 返回字段。

   向推理入口发送带采样参数的请求，检查响应是否包含 token id 与 logprob：

   ```bash
   curl -X POST "http://<服务IP>:<服务端口>/v1/chat/completions" \
     -H "Content-Type: application/json" \
     -d '{
       "model": "<model-name>",
       "messages": [{"role": "user", "content": "hello"}],
       "max_tokens": 16,
       "logprobs": true,
       "return_token_ids": true
     }'
   ```

   响应 JSON 的 `choices[0].logprobs` 非空，且包含 token id 相关字段，即表示 vLLM 引擎能力满足要求。若字段缺失，须先升级或调整引擎配置。

2. 在 user_config.json 的 motor_coordinator_config 中增加 precision_detection_config，开启精度检测功能。

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

   以下字段均为选填；省略时使用默认值。启用检测时须显式设置 `precision_check_enabled=true`。计数参数按整数配置，时间参数支持正整数或正浮点数。

   | 配置项 | 类型 | 取值范围 | 必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | precision_check_enabled | boolean | true / false | 否 | false | 精度检测总开关。关闭时不进行额外采样注入与检测；不影响用户自己请求 logprobs |
   | interval_seconds | number | > 0 | 否 | 30.0 | 每个 Decode 实例允许一条请求注入采样参数的最小间隔，单位秒；共用 D 的不同 P/D 组共享窗口 |
   | logprobs_count | integer | ≥ 1，且不超过引擎允许的 logprobs 上限 | 否 | 1 | 注入到 Decode 请求的 top-k 宽度；用户请求更宽时保留用户宽度 |
   | precision_issue_threshold | integer | ≥ 1 | 否 | 10 | 同一实例组连续检测异常达到该次数后触发拨测与告警 |
   | precision_clear_threshold | integer | ≥ 1 | 否 | 10 | 活动告警下连续有效正常样本达到该次数后上报清除告警 |
   | probe_max_attempts | integer | ≥ 1 | 否 | 3 | 精度拨测的尝试次数 |
   | probe_timeout_seconds | number | > 0 | 否 | 600.0 | 单次拨测超时时间，单位秒 |

3. （可选）如需根据精度告警终止实例，则需要在 Controller 配置中开启 precision_auto_recovery_enabled配置。

   ```json
   {
     "motor_controller_config": {
       "precision_auto_recovery_enabled": true
     }
   }
   ```

   | 配置项 | 类型 | 取值范围 | 必填 | 默认值 | 说明 |
   |--------|------|----------|------|--------|------|
   | precision_auto_recovery_enabled | boolean | true / false | 否 | false | Controller 收到 `alarm_id=0xFC001009` 的精度告警后，是否自动终止告警中的 D/P 实例 |

   >[!NOTE] 说明
   >精度检测分为 Coordinator 侧检测开关和 Controller 侧自动恢复开关，两者相互独立；只开启 Coordinator 开关时会检测并上报告警；只有同时开启 Controller 自动恢复时，Controller 才会根据精度告警终止实例。

4. 使用现有 deploy 脚本重新部署服务：

   ```bash
   cd examples/deployer
   python deploy.py --config_dir ../infer_engines/vllm
   ```

   Coordinator 日志中出现 Precision check 关键字表示精度检测链路已启用。

### 验证特性

服务启动后，Coordinator 日志中出现以下关键字表示精度检测链路已启用：

```text
Precision check (token sampling): interval=...
entry_admission=scheduler_zmq streak=scheduler_zmq probe=internal_router
```

该日志为**单行**，此处按字段折行展示；`interval` 之后的 `logprobs_count`、`threshold`、`probe_attempts`、`probe_timeout` 字段已省略。

启动日志仅表明链路已装配，不表示 msprobe 已成功执行检测。使用已部署的模型发送正常推理请求，等待完整响应结束，并检查 `MsprobeChecker: result`；若出现 `fail-open`、依赖缺失或映射错误，应先解决对应问题。

`SampleController: claimed` 与 `PrecisionSample: submit` 只在 DEBUG 级别输出；`PrecisionSample: inject_logprobs` 在改变了客户端请求宽度时按 INFO 输出，宽度未变化时按 DEBUG 输出。需要看到完整注入记录时，在 `motor_coordinator_config.logging_config` 中把 `"log_level"` 设为 `"DEBUG"`；默认 INFO 下不能以 `claimed`、`submit` 缺失判定功能失败。

| 日志关键词 | 含义 |
|------------|------|
| PrecisionSample: inject_logprobs | Router 已向 Decode 请求注入采样参数 |
| SampleController: claimed (scheduler) | Scheduler 为该 Decode 实例放行了一条采样请求（DEBUG） |
| PrecisionSample: submit | 样本已构造并提交后台检测（DEBUG） |
| MsprobeChecker: result | msprobe 已返回检测结果 |
| PrecisionReporter: threshold reached | 连续异常达到阈值，开始拨测与告警 |
| PrecisionAlarm: reporting alarm_id=0xFC001009 | 精度告警已上报 Controller |
| Precision recovery: terminating D instance_id= | Controller 已触发自动恢复，正在终止该 Decode 实例（终止 Prefill 时打印 terminating P instance_id=） |

## 调优建议

以下为首次启用时的配置起点，实际阈值应结合负载、模型和允许的检测开销调整；不是性能或检出率保证。

| 关键参数 | 建议取值 | 说明 |
|----------|----------|------|
| logprobs_count | 仅检测重复从 1 开始；需乱码检测用 3；需生僻字检测用 5 | 检测能力还依赖引擎实际返回的 top-k 和模型映射；增大宽度会增加采样请求开销 |
| interval_seconds | 先用默认 30.0 秒 | 按 D 实例控制入口采样频率；减小会增加开销，增大会延长发现异常的时间 |
| precision_issue_threshold | 先用默认 10 | 调小会更快触发拨测，也更易受短时异常影响；按 P/D 组累计，不能将阈值乘采样间隔视为确定的发现时延 |
| precision_clear_threshold | 先用默认 10 | 调小会更快清除告警；无效检测不算正常样本，不会推进清除计数 |
| probe_timeout_seconds | 先用默认 600.0 秒，再依据模型响应时间评估 | 这是单次拨测超时，不是用户请求超时；避免低于模型正常生成所需时间 |

## 常见问题

### 启动后没有 Precision check 日志

**问题描述**

服务启动后，Coordinator 日志中没有出现 Precision check 关键字。

**原因分析**

`precision_check_enabled=false` 或配置未加载。

**解决步骤**

检查 motor_coordinator_config.precision_detection_config 配置是否正确加载。

### 日志提示 Precision check enabled but scheduler client unavailable

**问题描述**

Coordinator 日志中出现以下告警，表示已启用精度检测但 Scheduler 客户端尚未就绪，本轮进程不会启用采样链路：

```text
Precision check enabled but scheduler client unavailable; disabling precision sampling until scheduler connects
```

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

按“环境准备”在 Coordinator Worker 实际使用的 Python 环境中安装配套 msprobe，并重新发送请求，确认出现 `MsprobeChecker: result`。mock checker 仅用于开发测试，不能作为生产检测能力的替代。

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
