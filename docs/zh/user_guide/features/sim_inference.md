# 虚推健康探测

## 特性介绍

虚推（虚拟推理，实现见 `motor/engine_server/core/sim_inference.py`）用于在业务低负载时主动向推理面发送轻量请求，结合 NPU **AI Cube 利用率**判断 Engine Server 推理引擎是否可用。配置位于 `user_config` 中 `motor_engine_prefill_config` / `motor_engine_decode_config` 的 **`health_check_config`** 子块，**默认关闭**。

Node Manager 周期性请求 Engine Server mgmt 面 **`GET /status`**；返回值综合推理面 `/health` 与虚推结果。连续 abnormal 时 `HeartbeatManager` 可触发节点自杀重调度。

**版本要求**：虚推仅支持 **HDK 26.0.RC1** 及以后版本。

## 工作机制

**启用条件**（须同时满足）：

1. `engine_type` 为 **`vllm`**（SGLang 引擎即使配置 `enable_virtual_inference: true` 也会在运行时被自动关闭）
2. `health_check_config.enable_virtual_inference` 为 `true`
3. `0 < health_check_config.npu_usage_threshold <= 100`
4. 推理面 `GET /health` 返回正常（由 `HealthCollector` 探测，`health_collector_timeout` 控制超时, `health_collector_timeout_retry_attempts` 控制超时重试次数）
5. 仅 **DP rank 0** 执行虚推（非 DP0 节点运行时自动关闭）

满足条件后，`mgmt_endpoint.py` 在首次 `/status` 请求时调用 `run_virtual_inference()` 启动虚推循环。

**虚推请求**：向推理面 `POST /v1/completions`，请求体为 `prompt: "1"`、`max_tokens: 1`。vLLM **layerwise decode**（`dispatch_profile=trigger`）额外携带 `kv_transfer_params.do_virtual: true` 及 PD 分离相关字段；**handoff decode** 与 Prefill/Union 角色发送普通 completion 请求。

**NPU 负载采样**：使用 `npu-smi info watch -s u` 采集 **AI Cube 利用率**（AI Cube Usage）。启动虚推前会通过 `npu-smi info watch -h` 检查 help 是否包含 `u - AI Cube Usage`；若当前 HDK 不支持该指标，Engine Server 会自动关闭虚推。

**动态探测间隔**：

| AI Cube 利用率峰值（5 秒采样窗口） | 下一轮间隔 |
|-----------------------------------|------------|
| ≥ 80% | 20 秒 |
| < `npu_usage_threshold` | 5 秒（默认） |
| `[npu_usage_threshold, 80%)` | 保持当前间隔不变 |

**异常判定**：当 AI Cube 利用率峰值低于 `npu_usage_threshold` 且虚推请求失败时，累计连续失败次数；达到 `max_failure_count` 后，`GET /status` 返回 `abnormal` 且虚推循环停止。Node Manager 的 `HeartbeatManager` 连续 5 次收到 abnormal 后触发自杀重调度。

**vLLM 指标过滤（v0.18+）**：启用虚推时，Engine Server 会 patch vLLM `OutputProcessor._update_stats_from_finished`，在写入 per-request 指标前跳过 `external_req_id` 含 `_virtual` 后缀的虚推请求（对应虚推 `X-Request-Id: {timestamp}_virtual`）。仅过滤 `request_success_total` 等 per-request 指标；`prompt_tokens` / `generation_tokens` 等 iteration 级 counter 仍会累计。

## 配置说明

**配置示例**（未配置项使用下列默认值）：

```json
"health_check_config": {
  "enable_virtual_inference": false,
  "npu_usage_threshold": 3,
  "max_failure_count": 6,
  "health_collector_timeout": 5,
  "health_collector_timeout_retry_attempts": 3
}
```

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| enable_virtual_inference | bool | `false` | 虚推总开关。**仅支持 vLLM**；SGLang 配置为 `true` 时运行时会自动关闭 |
| npu_usage_threshold | int | `3` | AI Cube 利用率阈值（%） |
| max_failure_count | int | `6` | 连续虚推失败次数上限 |
| health_collector_timeout | int | `5` | 推理面 `/health` 探测超时（秒） |
| health_collector_timeout_retry_attempts | int | `3` | 推理面 `/health` 超时重试次数（含首次，仅超时触发） |

完整字段说明见 [配置参考 health_check_config](../configuration/config_reference.md#health_check_config)。

## 启用方式

在 PD 分离部署的 `user_config.json` 中，将 Prefill 与 Decode 引擎配置的 `health_check_config.enable_virtual_inference` 设为 `true`，并按业务调整 `npu_usage_threshold`、`max_failure_count`。配置示例与字段说明见 [PD 分离服务部署](../deployment/k8s/pd_disaggregation_deployment.md#virtual-inference-health-check)。
