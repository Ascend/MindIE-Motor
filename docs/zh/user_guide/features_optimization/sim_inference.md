# 虚拟推理健康探测

## 特性介绍

虚拟推理用于在业务低负载时主动向推理面发送轻量请求，结合NPU AI Cube利用率判断推理引擎是否可用（引擎进程存活、`/health`正常但实际无法推理的场景，如NPU卡死、驱动异常等静默故障）。

原生拉起（Native Launch）路径下，虚推由 Node Manager 直接执行（motor/node_manager/core/services/native_engine/virtual_inference/），不再依赖 Engine Server 的 mgmt 面：

- Node Manager 每个实例维护单个虚推 monitor，仅绑定有效的 DP0 target（仅 vLLM）；周期性探测原生引擎 GET /health，首次观察到该 target READY 后幂等启动虚推循环。
- 虚推请求直接发往 vLLM 引擎推理面 POST /v1/completions。
- 虚推达到失败阈值时，仅使 endpoint 呈现 UNHEALTHY/ABNORMAL，不终止进程、不重启引擎。
- HeartbeatManager 保持现有状态映射与连续异常恢复语义：连续 5 次上报 abnormal 后触发节点自杀重调度。

**依据原文上下文内容重组，请进行人工校验。**
使用虚拟推理功能的优势：

- 检测静默故障：覆盖引擎进程存活、`/health`正常但NPU卡死、驱动异常等场景，及时发现推理引擎不可用状态。
- 降低运维开销：无需人工介入即可自动感知引擎异常，联动心跳上报实现节点自愈。
- 减少定位时间： 缩短故障发现时间，避免无效流量调度至故障引擎。

### 工作原理

启用条件（须同时满足，在`pull`时判定；由`NativeEngineService`在创建vLLM monitor前判定。先走`should_enable_vllm_virtual_inference`，仅对原本eligible的vLLM target再读最终引擎启动环境`launch_spec.command.env`做ERROR日志门禁——不是NodeManager自身`os.environ`，也不是deploy侧改写user_config）：

- 引擎类型受支持：虚拟推理仅支持vLLM；虚拟推理请求使用vLLM `POST /v1/completions`轻量请求（保留`X-Request-Id: {timestamp}_virtual`标识，不执行vLLM指标过滤）。SGLang不参与Motor虚拟推理：SGLang由自身在`/health`中执行生成式健康检查，Motor仅做健康心跳
- `health_check_config.enable_virtual_inference`为`true`
- 仅DP rank 0（endpoint id 0）执行虚拟推理（非DP0节点自动关闭）
- 非headless endpoint（PCP从节点自动关闭）
- `0 < npu_usage_threshold <= 100`
- `ASCEND_GLOBAL_LOG_LEVEL`为ERROR（仅在上述条件已满足后检查）：取最终引擎环境中的该变量；未设置 / `None` / 空字符串 / 纯空白视为默认ERROR并允许；`str(...).strip()`后等于`"3"`允许；其它显式值不创建vLLM monitor，打印warning，不终止/不重启已成功拉起的引擎，并经正常`reconcile(None)`清理旧monitor。功能已关闭或不合格endpoint不会因非ERROR环境误打该warning。该限制仅约束Motor vLLM虚拟推理，不影响SGLang原生生成式`GET /health`

虚拟推理循环在原生`/health`首次READY后启动（`NativeEngineService.runtime_state()`合并二者）。启动时还会通过`npu-smi info watch -h`校验HDK是否支持AI Cube Usage；不支持则自动关闭虚拟推理。虚拟推理请求区分两个超时：

- warmup（首次）请求：固定180秒（保持旧行为，独立于下述配置）；
- 周期性请求：使用`health_check_config.virtual_inference_timeout`（默认5秒），仅对vLLM生效、用户可配置。

**虚拟推理请求（vLLM）**：向推理面`POST /v1/completions`，请求体为`prompt: "1"`、`max_tokens: 1`，`X-Request-Id`携带`{timestamp}_virtual`标识。vLLM layerwise decode（`dispatch_profile=trigger`）额外携带`kv_transfer_params.do_virtual: true`及PD分离相关字段；handoff decode与Prefill/Union角色发送普通completion请求。

**SGLang生成式健康开关（`SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION`）**：NodeManager拉起SGLang时将该环境变量显式写入启动命令env并恒为`true`（不受容器外部同名环境变量影响，也不受`enable_virtual_inference`影响），使SGLang的`/health`使用自身生成式健康检查；Motor对SGLang不发起任何虚拟推理请求。SGLang心跳超时使用`health_collector_timeout` / `health_collector_timeout_retry_attempts`，不使用`virtual_inference_timeout`。

**NPU负载采样**：使用`npu-smi info watch -s u`采集AI Cube利用率（5秒采样窗口内取峰值）。

动态探测间隔：

| AI Cube利用率峰值（5秒采样窗口） | 下一轮间隔 |
|-----------------------------------|------------|
| ≥ 80% | 20秒 |
| < `npu_usage_threshold` | 5秒（默认） |
| `[npu_usage_threshold, 80%)` | 保持当前间隔不变 |

**异常判定（vLLM）**：当AI Cube利用率峰值低于`npu_usage_threshold`且虚拟推理请求失败时，累计连续失败次数；达到`max_failure_count`后，该endpoint的运行时状态由`READY`降级为`UNHEALTHY`，心跳上报`ABNORMAL`，虚拟推理循环停止。主动探测失败但AI Cube ≥ threshold视为引擎繁忙，不累计失败；AI Cube采样不可用时不累计失败次数。虚拟推理仅改变状态上报，不会触发进程重启或Pod级恢复。监控循环自身出现未预期异常时，仅记录日志并退避重试，不据此判定引擎异常（不累计失败次数、不降级`UNHEALTHY`）。

**指标说明**：vLLM虚拟推理请求保留`_virtual`请求ID标识；不执行vLLM per-request指标过滤（`patch_vllm_metrics`未迁移，Engine Server侧旧逻辑将在其整体删除时移除）。

### 核心功能

**依据原文上下文内容重组，请进行人工校验。**
虚拟推理健康探测提供以下核心功能：

- **主动健康探测**：在业务低负载时向推理面发送轻量`POST /v1/completions`请求，结合NPU AI Cube利用率判断推理引擎是否可用。
- **静默故障检测**：覆盖引擎进程存活、`/health`正常但NPU卡死、驱动异常等无法通过常规健康检查发现的故障场景。
- **动态探测间隔**：根据AI Cube利用率峰值自动调整探测频率，在高负载时降低探测频率，低负载时提高探测频率。
- **异常状态上报**：达到失败阈值后将endpoint运行时状态降级为`UNHEALTHY`，心跳上报`ABNORMAL`，联动`HeartbeatManager`实现节点自愈。
- **SGLang生成式健康检查**：对SGLang引擎通过环境变量`SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION`启用自身生成式健康检查，Motor不发起虚拟推理请求。

### 约束与限制

**依据原文上下文内容重组，请进行人工校验。**

| 维度 | 约束内容 |
|------|----------|
| 硬件 | Atlas 800I A2推理服务器 |
| 部署场景 | PD分离服务部署 |
| 引擎 | 虚拟推理仅支持vLLM引擎，使用vLLM `POST /v1/completions`轻量请求。|
| 特性互斥 | **内容缺失，需要人工补齐。** |
| 软件依赖 | HDK 26.0.RC1及以后版本，依赖`npu-smi info watch -s u`提供AI Cube Usage指标。 |
| 其他限制 | **内容缺失，需要人工补齐。** |

## 特性使用

### 使用场景

**依据原文上下文内容重组，请进行人工校验。**
虚拟推理健康探测适用于以下场景：

- PD分离部署场景：在Prefill与Decode引擎配置中启用虚拟推理，检测NPU静默故障。
- 业务低负载时段：在业务低负载时主动发送轻量请求，避免影响正常推理请求。
- 高可用部署场景：需要快速感知引擎异常并联动心跳上报实现节点自愈。

### 使用样例

**依据原文上下文内容重组，请进行人工校验。**

1. 配置user_config.json文件。
   配置`motor_engine_prefill_config` / `motor_engine_decode_config`字段中的`health_check_config`子模块。将`enable_virtual_inference`设为`true`，其余参数按业务需求进行调整，示例如下。

    ```json
    "health_check_config": {
      "enable_virtual_inference": true,
      "npu_usage_threshold": 3,
      "max_failure_count": 6,
      "virtual_inference_timeout": 5.0,
      "health_collector_timeout": 5,
      "health_collector_timeout_retry_attempts": 3
    }
    ```

    **配置参数说明**

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    |--------|------|----------|-----------|--------|------|
    | enable_virtual_inference | bool | true / false | 选填 | false | Motor主动虚拟推理开关。仅关闭/开启vLLM的Motor虚拟推理；false不会关闭SGLang原生生成式`/health`（拉起时env恒为true）。仅DP rank 0、非headless的vLLM endpoint生效。另需最终引擎环境`ASCEND_GLOBAL_LOG_LEVEL`为ERROR（未设置默认ERROR）；显式非ERROR时NodeManager不创建monitor并warning |
    | npu_usage_threshold | int | 1–100 | 选填 | 3 | AI Cube利用率阈值（%）；仅vLLM Motor虚拟推理使用 |
    | max_failure_count | int | ≥ 1 | 选填 | 6 | 连续虚拟推理失败次数上限；仅vLLM Motor虚拟推理使用 |
    | virtual_inference_timeout | float | > 0 | 选填 | 5.0 | 周期性主动虚拟推理请求的客户端超时（秒），必须为正数，仅对vLLM Motor虚拟推理生效；首次warmup请求固定180秒，不受此配置影响。配置保留兼容，SGLang忽略该字段 |
    | health_collector_timeout | int | > 0 | 选填 | 5 | 推理面`GET /health`探测超时（秒）；vLLM与SGLang心跳均使用 |
    | health_collector_timeout_retry_attempts | int | ≥ 1 | 选填 | 3 | 推理面`GET /health`超时重试次数（含首次，仅超时触发）；vLLM与SGLang心跳均使用 |

    完整字段说明请参见[配置参考health_check_config](../configuration/config_reference.md#health_check_config)。

2. 原生拉起路径下虚拟推理由Node Manager执行并启动。（**具体如何启动，是否需要给出操作命令**）
    **依据原文上下文内容重组，请进行人工校验。**
    配置启用后，NodeManager在检测到vLLM引擎`/health`首次READY后幂等启动虚拟推理循环。虚拟推理请求的`X-Request-Id`携带`{timestamp}_virtual`标识，可在引擎日志中观察到虚拟推理请求记录。

    **内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**
    当日志中出现以下关键字时，表示虚拟推理已正常启动：

    ```bash
    Virtual inference monitor started for endpoint {endpoint_id}
    ```

    当引擎异常时，日志中可观察到：

    ```text
    Runtime state of endpoint {endpoint_id} degraded to UNHEALTHY
    Heartbeat status: ABNORMAL
    ```

### 验证特性

**依据原文上下文内容重组，请进行人工校验。**

#### 验证方法一：检查 AI Cube利用率

使用`npu-smi`命令查看AI Cube利用率，确认虚拟推理在低负载时发起探测：

```bash
npu-smi info watch -s u
```

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

预期输出中应包含`ai_cube_usage`字段，显示当前AI Cube利用率百分比。当虚拟推理启动后，AI Cube利用率应出现周期性波动（虚拟推理请求触发计算）。

#### 验证方法二：检查虚拟推理请求日志

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

检查NodeManager日志，确认虚拟推理请求正常发送：

```bash
grep -i "virtual_inference\|_virtual\|virtual inference" /var/log/node_manager.log
```

输出示例：

```text
INFO: Virtual inference request sent to endpoint 0, X-Request-Id: 1693123456_virtual
INFO: AI Cube usage: 2%, below threshold, continuing virtual inference
```

#### 验证方法三：检查 endpoint 健康状态

**内容缺失，需要补充。以下内容基于原文上下文推断，请人工确认：**

通过查询endpoint运行时状态，确认虚拟推理正常执行：

```bash
curl -X GET http://localhost:8080/health
```

或在引擎停止/异常时，检查心跳上报状态，预期为`ABNORMAL`。

## 常见问题

### 虚拟推理启用后未生效

**依据原文上下文内容重组，请进行人工校验。**

**问题描述**

配置`enable_virtual_inference: true`后，虚拟推理未正常启动。

**原因分析**

1. 引擎类型非vLLM（虚拟推理仅支持vLLM，SGLang使用自身生成式健康检查）。
2. 非DP rank 0或headless endpoint。
3. `ASCEND_GLOBAL_LOG_LEVEL`显式设置为非ERROR值。
4. `npu-smi`不支持AI Cube Usage指标（HDK版本低于26.0.RC1）。
5. 引擎`/health`尚未返回READY。

**解决步骤**

1. 确认引擎类型为vLLM。
2. 确认endpoint为DP rank 0且非headless。
3. 检查`ASCEND_GLOBAL_LOG_LEVEL`环境变量，确保为ERROR或未设置。
4. 使用`npu-smi info watch -h`检查HDK是否支持AI Cube Usage。
5. 检查引擎日志，确认`/health`已返回READY。

### 虚拟推理导致引擎日志过多

**问题描述**

启用虚拟推理后，引擎日志中出现大量虚拟推理请求记录。

**原因分析**

虚拟推理请求携带`X-Request-Id: {timestamp}_virtual`标识，vLLM引擎会记录这些请求日志。

**解决步骤**

可通过调整引擎日志级别减少日志输出，或通过日志过滤规则忽略`_virtual`标识的请求。
