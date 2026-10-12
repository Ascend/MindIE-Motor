# [2026-10-09] Decode 高负载拉取已超时释放的 Prefill KV 导致精度问题

- **现象 (Symptom)**：PD 分离流式请求在 Decode 高负载时长时间等待拉取 KV；等待期间 Prefill 侧 Mooncake 请求先达到 `VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT` 并释放 KV，Decode 随后继续处理可能出现精度问题。
- **根因 (Root cause)**：`motor/coordinator/router/strategies/unified_pd.py` 的 Decode 流式转发复用 `infer_timeout`（默认 3600 秒），与 vLLM Mooncake abort timeout（常用 480 秒）没有配置关联；Coordinator 允许 Decode 等待超过 KV 生命周期。
- **为什么会写出 (Why)**：原有超时模型区分了 Prefill 首 token 和整体推理超时，但遗漏了 Decode 首 token 等待窗口必须受底层 KV 生命周期约束；部署配置也将引擎环境变量和 Coordinator 配置视为彼此独立。
- **修复 (Fix)**：新增 `exception_config.decode_first_token_timeout`（默认 480 秒），作为 Unified PD 从 Prefill 完成到 Decode 首个完整响应帧的绝对期限；首帧后仍使用原有 `infer_timeout`。BOOTSTRAP/TRIGGER 在 Prefill 完成后起算，HANDOFF 按请求扣除已经消耗的 KV 生命周期。`deploy.py` 按 Prefill 最终环境的合并优先级读取 `VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT`（`motor_engine_prefill_env` 优先，否则回退 `motor_common_env`），并在普通部署、单容器、更新配置和扩缩容路径中将同值写入 Coordinator 配置。
- **测试拦截 (Test interception)**：`tests/coordinator/router/test_unified_pd_router.py` 覆盖绝对首帧期限、Prefill 完成后起算、首帧后恢复原超时及多 prompt 独立剩余期限；`tests/coordinator/core/test_config.py` 覆盖默认值和非法值；`tests/examples/deployer/test_additional_labels_annotations.py`、`test_pd_hybrid.py` 覆盖部署同步、单容器 ConfigMap、扩缩容、缺省保持和非法环境值拒绝。
- **场景 (Scenario)**：PD 分离流式推理、Decode 高负载或排队较久、使用 Mooncake 拉取 Prefill KV，且 Coordinator Decode 等待时间大于 Mooncake abort timeout。
- **关键词 (Keywords)**：Coordinator, Decode high load, Mooncake abort timeout, KV release, accuracy
