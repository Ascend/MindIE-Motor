# 虚推健康探测

## 特性介绍

虚推（虚拟推理）用于在业务低负载时主动向推理面发送轻量请求，结合 NPU **AI Cube 利用率**判断推理引擎是否可用（引擎进程存活、`/health` 正常但实际无法推理的场景，如 NPU 卡死、驱动异常等「静默故障」）。配置位于 `user_config` 中 `motor_engine_prefill_config` / `motor_engine_decode_config` 的 **`health_check_config`** 子块，**默认关闭**。

**原生拉起（Native Launch）路径**下，虚推由 **Node Manager** 直接执行（`motor/node_manager/core/services/native_engine/virtual_inference/`），不再依赖 Engine Server 的 mgmt 面：

- Node Manager 每个实例维护**单个**虚推 monitor，仅绑定有效的 DP0 target（仅 vLLM）；周期性探测原生引擎 `GET /health`，首次观察到该 target `READY` 后**幂等启动**虚推循环
- 虚推请求直接发往 vLLM 引擎推理面 `POST /v1/completions`
- 虚推达到失败阈值时，仅使 endpoint 呈现 `UNHEALTHY`/`ABNORMAL`，**不杀进程、不重启引擎**
- `HeartbeatManager` 保持现有状态映射与连续异常恢复语义：连续 5 次上报 abnormal 后触发节点自杀重调度

**版本要求**：虚推仅支持 **HDK 26.0.RC1** 及以后版本（`npu-smi info watch -s u` 提供 AI Cube Usage 指标）。

## 工作机制

**启用条件**（须同时满足，在 `pull` 时判定；由 `NativeEngineService` 在创建 vLLM monitor 前判定。先走 `should_enable_vllm_virtual_inference`，**仅对原本 eligible 的 vLLM target** 再读最终引擎启动环境 `launch_spec.command.env` 做 ERROR 日志门禁——不是 NodeManager 自身 `os.environ`，也不是 deploy 侧改写 `user_config`）：

1. 引擎类型受支持：虚推**仅支持 vLLM**；虚推请求使用 vLLM `POST /v1/completions` 轻量请求（保留 `X-Request-Id: {timestamp}_virtual` 标识，不执行 vLLM 指标过滤）。**SGLang 不参与 Motor 虚推**：SGLang 由自身在 `/health` 中执行生成式健康检查，Motor 仅做健康心跳
2. `health_check_config.enable_virtual_inference` 为 `true`
3. 仅 **DP rank 0**（endpoint id 0）执行虚推（非 DP0 节点自动关闭）
4. 非 headless endpoint（PCP 从节点自动关闭）
5. `0 < npu_usage_threshold <= 100`
6. **`ASCEND_GLOBAL_LOG_LEVEL` 为 ERROR**（仅在上述条件已满足后检查）：取最终引擎环境中的该变量；未设置 / `None` / 空字符串 / 纯空白视为默认 ERROR 并允许；`str(...).strip()` 后等于 `"3"` 允许；其它显式值不创建 vLLM monitor，打印 warning，**不杀/不重启已成功拉起的引擎**，并经正常 `reconcile(None)` 清理旧 monitor。功能已关闭或不合格 endpoint **不会**因非 ERROR 环境误打该 warning。该限制**仅约束 Motor vLLM 虚推**，不影响 SGLang 原生生成式 `GET /health`

虚推循环在**原生 `/health` 首次 READY 后**启动（`NativeEngineService.runtime_state()` 合并二者）。启动时还会通过 `npu-smi info watch -h` 校验 HDK 是否支持 AI Cube Usage；不支持则自动关闭虚推。虚推请求区分两个超时：

- **warmup（首次）请求**：固定 **180 秒**（保持旧行为，独立于下述配置）；
- **周期性请求**：使用 `health_check_config.virtual_inference_timeout`（默认 5 秒），仅对 vLLM 生效、用户可配置。

**虚推请求（vLLM）**：向推理面 `POST /v1/completions`，请求体为 `prompt: "1"`、`max_tokens: 1`，`X-Request-Id` 携带 `{timestamp}_virtual` 标识。vLLM **layerwise decode**（`dispatch_profile=trigger`）额外携带 `kv_transfer_params.do_virtual: true` 及 PD 分离相关字段；**handoff decode** 与 Prefill/Union 角色发送普通 completion 请求。

**SGLang 生成式健康开关（`SGLANG_ENABLE_HEALTH_ENDPOINT_GENERATION`）**：NodeManager 拉起 SGLang 时将该环境变量**显式写入启动命令 env 并恒为 `true`**（不受容器外部同名环境变量影响，也不受 `enable_virtual_inference` 影响），使 SGLang 的 `/health` 使用自身生成式健康检查；Motor 对 SGLang 不发起任何虚推请求。SGLang 心跳超时使用 `health_collector_timeout` / `health_collector_timeout_retry_attempts`，**不使用** `virtual_inference_timeout`。

**NPU 负载采样**：使用 `npu-smi info watch -s u` 采集 **AI Cube 利用率**（5 秒采样窗口内取峰值）。

**动态探测间隔**：

| AI Cube 利用率峰值（5 秒采样窗口） | 下一轮间隔 |
|-----------------------------------|------------|
| ≥ 80% | 20 秒 |
| < `npu_usage_threshold` | 5 秒（默认） |
| `[npu_usage_threshold, 80%)` | 保持当前间隔不变 |

**异常判定**（vLLM）：当 AI Cube 利用率峰值低于 `npu_usage_threshold` 且虚推请求失败时，累计连续失败次数；达到 `max_failure_count` 后，该 endpoint 的运行时状态由 `READY` 降级为 `UNHEALTHY`，心跳上报 `ABNORMAL`，虚推循环停止。主动探测失败但 AI Cube ≥ threshold 视为引擎繁忙，不累计失败；AI Cube 采样不可用时**不累计**失败次数。虚推仅改变状态上报，**不会**触发进程重启或 Pod 级恢复。监控循环自身出现未预期异常时，仅记录日志并退避重试，**不据此判定引擎异常**（不累计失败次数、不降级 `UNHEALTHY`）。

**指标说明**：vLLM 虚推请求保留 `_virtual` 请求 ID 标识；不执行 vLLM per-request 指标过滤（`patch_vllm_metrics` 未迁移，Engine Server 侧旧逻辑将在其整体删除时移除）。

## 配置说明

**配置示例**（未配置项使用下列默认值）：

```json
"health_check_config": {
  "enable_virtual_inference": false,
  "npu_usage_threshold": 3,
  "max_failure_count": 6,
  "virtual_inference_timeout": 5.0,
  "health_collector_timeout": 5,
  "health_collector_timeout_retry_attempts": 3
}
```

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable_virtual_inference | bool | `false` | Motor 主动虚推开关。**仅关闭/开启 vLLM 的 Motor 虚推**；`false` **不会**关闭 SGLang 原生生成式 `/health`（拉起时 env 恒为 `true`）。仅 DP rank 0、非 headless 的 vLLM endpoint 生效。另需最终引擎环境 `ASCEND_GLOBAL_LOG_LEVEL` 为 ERROR（未设置默认 ERROR）；显式非 ERROR 时 NodeManager 不创建 monitor 并 warning |
| npu_usage_threshold | int | `3` | AI Cube 利用率阈值（%）；仅 vLLM Motor 虚推使用 |
| max_failure_count | int | `6` | 连续虚推失败次数上限；仅 vLLM Motor 虚推使用 |
| virtual_inference_timeout | float | `5.0` | **周期性**主动虚推请求的客户端超时（秒），必须为正数，**仅对 vLLM Motor 虚推生效**；首次 warmup 请求固定 180 秒，不受此配置影响。配置保留兼容，SGLang 忽略该字段 |
| health_collector_timeout | int | `5` | 推理面 `GET /health` 探测超时（秒）；vLLM 与 SGLang 心跳均使用 |
| health_collector_timeout_retry_attempts | int | `3` | 推理面 `GET /health` 超时重试次数（含首次，仅超时触发）；vLLM 与 SGLang 心跳均使用 |

完整字段说明见 [配置参考 health_check_config](../configuration/config_reference.md#health_check_config)。

## 启用方式

在 PD 分离部署的 `user_config.json` 中，将 Prefill 与 Decode 引擎配置的 `health_check_config.enable_virtual_inference` 设为 `true`，并按业务调整 `npu_usage_threshold`、`max_failure_count`。原生拉起路径下虚推由 Node Manager 执行，无需（也不依赖）Engine Server 参与。
