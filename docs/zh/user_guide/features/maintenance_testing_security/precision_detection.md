# 精度检测特性

## 特性介绍

精度检测用于发现 Decode 推理过程中的运行时异常，并把结果上报为精度异常告警。检测由 vLLM-Ascend Runtime Guard 完成。Motor 不实现检测算法，也不修改用户请求。Coordinator 读取 Decode endpoint 的 `/metrics`，计数增加后透传上报 `Precision Anomaly Alarm`，告警 ID 为 `0xFC001009`。

本特性没有单独的总开关。三种检测器默认都是关闭的，需要在 Decode 引擎配置里按名称打开。Coordinator 启动时读取同一份配置，至少一个 Decode 检测器开启时才处理 Runtime Guard 指标；全部关闭或未配置时不处理。普通指标采集不受影响。

### 工作原理

1. Decode 引擎按 `additional_config.runtime_config.detector.<name>` 运行已打开的检测器。命中后，引擎把对应 `incident_type` 的异常计数写入 `/metrics`。
2. Coordinator 可观测进程按指标采集周期抓取 Decode endpoint。连续 10 次抓到可读文本后做一次窗口判定。计数相对上次已确认值增加，视为一次新增；第一次看到正数也算新增。
3. 出现新增时，Coordinator 对该 Decode endpoint 做一次拨测，并把失败次数写入告警附加信息。拨测不决定是否上报。
4. Coordinator 上报精度异常告警。告警归属该 Decode 实例，`p_instance_id` 为空。附加信息中的 `abnormal_ports` 是本轮出现新增的业务端口。`precision_issue_count` 是本轮计数增量。
5. 该告警已活动时不重复上报。当前各 endpoint 连续无新增达到 `precision_clear_threshold` 后，上报清除。
6. `precision_auto_recovery_enabled` 为 `true` 时，Controller 终止告警中的 Decode 实例。该开关默认 `false`，关闭时只保留告警。

Prefill 可以带同一份检测器配置。本特性只消费 Decode 的计数。

### 约束与限制

| 约束维度 | 要求 |
|----------|------|
| 硬件 | <ul><li>Atlas 800I A2 推理服务器</li><li>Atlas 800I A3 超节点服务器</li><li>Ascend 950 PR 系列产品</li></ul> |
| 部署场景 | 经过 Coordinator 管理的 Decode 实例。直连引擎不经过本特性。 |
| 引擎 | vLLM，且已安装带 Runtime Guard 的 vLLM-Ascend。检测器配置写在 `motor_engine_decode_config.engine_config.additional_config`，修改后重启 Decode 和 Coordinator 生效。 |
| 告警粒度 | Decode 实例。端口只写在附加信息里，用于定位 endpoint。 |
| 恢复 | 自动恢复只终止告警中的 Decode 实例，不终止 Prefill，不缩单个 DP，也不走软件故障入口。 |
| 其他限制 | <ul><li>抓取失败、文本无法解析、计数非法时，该次不计入 10 次窗口，已有基线保留。</li><li>整页没有 Runtime Guard 计数时，不能当作正常，基线会被清掉。</li><li>以前见过、本轮消失的 endpoint 会挡住清除。</li><li>修改检测器配置后需要重启对应 Decode 和 Coordinator。修改 `precision_clear_threshold` 或 `probe_timeout_seconds` 后需要重启 Coordinator。</li></ul> |

## 特性使用

### 使用场景

| 维度 | 说明 |
|------|------|
| 部署形态 | PD 分离等由 Coordinator 抓取 Decode `/metrics` 的部署。 |
| 检测对象 | Decode 引擎导出的 Runtime Guard 异常计数。 |
| 检测粒度 | Decode 实例。同一实例上多个 endpoint 的新增会写进同一条告警的 `abnormal_ports`。 |
| 异常类型 | `token_repeat`、`logits_finite`、`spec_acceptance`。只打开配置里 `enabled=true` 的检测器。 |
| 处置方式 | Coordinator 上报告警。Controller 可选终止该 Decode 实例。 |

### 环境准备

- 推理服务由 Motor 部署，Decode 使用 vLLM，镜像或环境中的 vLLM-Ascend 包含 Runtime Guard。
- Coordinator 使用包含 Decode 检测器配置的同一份配置文件，可观测进程已启动并能够访问 Decode endpoint 的 `/metrics`。
- 需要告警后自动终止实例时，Controller 可连通，并准备打开 `precision_auto_recovery_enabled`。

### 使用样例

检测器写在 Decode 的引擎配置中，不写在 `precision_detection_config`。三种检测器互相独立，未写出的检测器保持关闭。修改后重启 Decode 和 Coordinator。

```json
{
  "motor_engine_decode_config": {
    "engine_config": {
      "additional_config": {
        "runtime_config": {
          "detector": {
            "token_repeat": { "enabled": true },
            "logits_finite": { "enabled": false },
            "spec_acceptance": { "enabled": false }
          }
        }
      }
    }
  }
}
```

只打开其中一种时，可以只写该检测器。其余字段省略时使用下表默认值。

#### token_repeat

看输出 token 是否在滑动窗口内反复出现。`incident_type` 为 `token_repeat`。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enabled | bool | false | 是否打开该检测器。 |
| window | int | 32 | 滑动窗口长度，最小 1。 |
| repeat_sum_threshold | int | 64 | 窗口内重复分数之和超过该值记为一次越限，最小 0。 |
| min_tokens | int | 32 | 至少累计这么多内容 token 后才允许告警。`0` 表示不设这段预热。 |
| consecutive_hits | int | 1 | 需要连续多少次越限才上报，最小 1。 |
| ignore_token_ids | int 数组 | `[]` | 这些 token id 不计入窗口。 |

#### logits_finite

看采样前 logits 是否出现 NaN 或 Inf。`incident_type` 为 `logits_finite`。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enabled | bool | false | 是否打开该检测器。 |
| item_sync | bool | false | `false`：异步读取有限性结果。`true`：在采样前同步读取，结果对上语法处理前的 logits，同步开销更大。 |

#### spec_acceptance

看投机解码的接受率和接受长度是否偏离配置区间。未开启投机解码时，打开该检测器也不会产生有效计数。`incident_type` 为 `spec_acceptance`。

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enabled | bool | false | 是否打开该检测器。 |
| window | int | 10 | 滚动窗口长度，最小 1。 |
| low_threshold | float | 0.3 | 接受率下限，取值 0 到 1。 |
| len_low_threshold | float | 1.4 | 接受率处于低位时使用的长度比，最小 0。 |
| high_threshold | float | 0.96 | 接受率上限，取值 0 到 1。 |
| len_high_threshold | float | 2.8 | 接受率处于高位时使用的长度比，最小 0。 |

三种检测器同时打开、并改掉默认阈值时，可以写成：

```json
{
  "motor_engine_decode_config": {
    "engine_config": {
      "additional_config": {
        "runtime_config": {
          "detector": {
            "token_repeat": {
              "enabled": true,
              "window": 32,
              "repeat_sum_threshold": 64,
              "min_tokens": 32,
              "consecutive_hits": 1,
              "ignore_token_ids": []
            },
            "logits_finite": {
              "enabled": true,
              "item_sync": false
            },
            "spec_acceptance": {
              "enabled": true,
              "window": 10,
              "low_threshold": 0.3,
              "len_low_threshold": 1.4,
              "high_threshold": 0.96,
              "len_high_threshold": 2.8
            }
          }
        }
      }
    }
  }
}
```

Coordinator 侧只保留清除和拨测超时。省略时分别使用 10 和 600 秒。`precision_check_enabled` 不控制本特性。

```json
{
  "motor_coordinator_config": {
    "precision_detection_config": {
      "precision_clear_threshold": 10,
      "probe_timeout_seconds": 600.0
    }
  }
}
```

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| precision_clear_threshold | int | 10 | 活动告警下，当前各 endpoint 连续无新增达到该次数后上报清除。 |
| probe_timeout_seconds | float | 600.0 | 单次拨测超时，单位秒。拨测固定尝试 1 次。 |

如需在告警后终止 Decode 实例，打开 Controller 的自动恢复。省略或为 `false` 时只上报告警。

```json
{
  "motor_controller_config": {
    "precision_auto_recovery_enabled": true
  }
}
```

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| precision_auto_recovery_enabled | bool | false | 收到 `0xFC001009` 后是否终止告警中的 Decode 实例。 |

>[!NOTE] 说明
>检测器开关在引擎配置，告警清除在 Coordinator，自动终止在 Controller。三者独立。只打开检测器时，计数增加就会上报告警；只有再打开自动恢复，Controller 才会终止 Decode。

### 查看告警

告警名称为 `Precision Anomaly Alarm`。用 `instanceId` 定位 Decode 实例，用附加信息中的 `abnormal_ports` 定位 endpoint。

清除记录使用与抬起相同的 MOI。MOI 形式为 `service name=Coordinator, service ip=<POD_IP>, instanceId=<Decode实例ID>`。

### 验证特性

不需要查看 Decode 启动参数，也不需要直接访问 Decode 的 `/metrics`。确认配置已写入 `user_config.json` 并完成对应重启后，看 Coordinator 可观测进程的 INFO 日志和精度告警即可。

日志前缀是 `[PrecisionDetection]`。只看到 `schedule` 或 `ingest`，只说明 Coordinator 已在消费该 Decode 的抓取结果，不能说明检测器已打开。

| 日志 | 含义 |
|------|------|
| `[PrecisionDetection] schedule instance=%s endpoints=%d missing=%d` | 指标线程已把该 Decode 的本轮抓取交给检测。`missing` 大于 0 表示有 endpoint 没抓到文本。 |
| `[PrecisionDetection] ingest instance=%s model=%s endpoints=%d missing=%d` | 检测处理已开始。 |
| `[PrecisionDetection] collecting instance=%s endpoint=%s port=%s progress=%d/%d` | 可读抓取还没满 10 次，尚未做窗口判定。 |
| `[PrecisionDetection] window instance=%s endpoint=%s port=%s no runtime guard series` | 文本可读，但没有检测器计数。配置里的检测器未打开，或引擎没有导出计数。 |
| `[PrecisionDetection] window instance=%s endpoint=%s port=%s delta=%s samples=%s generation_reset=%s` | 本窗口已判定。`delta=0` 表示没有新增；`delta` 大于 0 表示有新增。`samples` 形如 `token_repeat=1`，用来区分是哪种检测器。 |
| `[PrecisionDetection] raise reported instance=%s ok=%s auto_recovery_cleared=%s` | 告警已提交。`ok=True` 表示对端返回成功，或本次按配置跳过上报。 |

检测器已打开且本窗口没有新增时，应能看到 `delta=0` 的 `window` 日志。一直是 `no runtime guard series` 时，回到 `user_config.json` 检查对应检测器的 `enabled`，并确认 Decode 已用这份配置重启。

## 调优建议

检测阈值在引擎侧按检测器调整。Coordinator 不能通过 `precision_detection_config` 改变检测规则。

| 关键参数 | 建议 | 说明 |
|----------|------|------|
| 打开哪些检测器 | 先只打开业务需要确认的一种 | 三种检测器都会进入同一个告警。同时打开时，告警附加信息不区分种类，看 Coordinator 的 `window` 日志里 `samples` 是哪种名字。 |
| token_repeat | 先用默认窗口 32、阈值 64、`min_tokens` 32 | 调高 `repeat_sum_threshold` 或 `min_tokens` 会减少短输出上的命中；调低会更快计数。`ignore_token_ids` 只排除明确不参与判断的 token。 |
| logits_finite | 先保持 `item_sync=false` | 需要结果对上语法处理前的 logits 时再改为 `true`。同步读取发生在采样前。 |
| spec_acceptance | 只在已经开启投机解码时打开 | 接受率区间先用默认 0.3 到 0.96。未开投机解码时没有有效样本。 |
| precision_clear_threshold | 先用默认 10 | 调小会更快清除活动告警。抓取失败或 endpoint 消失不会推进清除。 |
| probe_timeout_seconds | 先用默认 600 秒 | 这是拨测超时，不是用户请求超时。低于模型正常生成时间时，拨测会计入失败，告警仍会上报。 |

指标采集周期由 `prometheus_metrics_config.reuse_time` 决定，默认 3 秒。窗口固定为 10 次可读抓取，不在精度检测配置里。

## 常见问题

### 打开配置后一直没有精度告警

**问题描述**

Decode 已写入检测器配置，业务请求正常，但没有 `0xFC001009`。

**原因分析**

`user_config.json` 里对应检测器仍是 `enabled=false`，或引擎没有导出检测计数。没有计数时 Motor 不上报告警。计数没有增加时也不会上报。

**解决步骤**

看 Coordinator 日志。出现 `no runtime guard series` 时，检查 `user_config.json` 里该检测器的 `enabled`，并确认 Decode 已用这份配置重启。出现 `window` 且 `delta=0` 时，链路已经打开，只是本窗口没有新增。

### 日志一直停在 collecting

**问题描述**

Coordinator 日志反复出现 `[PrecisionDetection] collecting`，没有 `window`。

**原因分析**

该 endpoint 的可读抓取还没满 10 次。抓取为空或文本无法解析的次数不计入进度。

**解决步骤**

看同一条日志的 `progress`。若同时有 `scrape empty`，或 `schedule`、`ingest` 里的 `missing` 大于 0，说明 Coordinator 没抓到该 endpoint 的指标文本，先恢复指标采集，不要去 Decode 上查启动参数。

### 告警已上报但实例没有终止

**问题描述**

已看到精度告警，Decode 实例仍在运行。

**原因分析**

`precision_auto_recovery_enabled` 为 `false` 或未配置。关闭时 Controller 只保留告警。

**解决步骤**

在 Controller 配置中把它设为 `true` 并使配置生效。自动恢复只终止告警中的 Decode 实例。

### 自动恢复没有终止 Prefill

**问题描述**

开启自动恢复后，只看到 Decode 被终止。

**原因分析**

本告警的 `p_instance_id` 为空。自动恢复按告警内容只终止 Decode。

**解决步骤**

这是当前行为。Prefill 不因这条精度告警被终止。

### 投机解码检测没有计数

**问题描述**

`user_config.json` 里 `spec_acceptance.enabled` 已设为 `true`，Coordinator 的 `window` 日志里没有 `spec_acceptance` 计数增加。

**原因分析**

该检测器依赖投机解码产生的接受率样本。未开启投机解码时没有有效样本。日志是 `no runtime guard series` 时，则是检测器配置没有生效。

**解决步骤**

在 `user_config.json` 中确认 Decode 已开启投机解码，并已按该配置重启。未使用投机解码时不要打开此项。不要通过 Decode 启动参数核对。
