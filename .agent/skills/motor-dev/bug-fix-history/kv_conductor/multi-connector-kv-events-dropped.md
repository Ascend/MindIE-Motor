# [2026-08-21] MultiConnector 顶层配置下引擎 offload 事件被静默丢弃

- **现象 (Symptom)**：memcache KV 事件链路中，memcache 事件能到 kv-conductor 并排队（`pool stored: no matching offload blocks yet — queued for later`），但引擎 offload 事件永远不到——conductor 的 `vllm-prefill-X` subscriber 只收到 `medium=GPU` 的 HBM 事件（直插树），`vLLM non-HBM: ingesting offload blocks` 从不出现，两阶段匹配永远缺引擎侧。EngineCore 日志：`update_connector_output: kv_cache_events=None`、`connector events: []`。
- **根因 (Root cause)**：vLLM v0.23.0 `MultiConnector.get_kv_connector_kv_cache_events()` 未实现（上游 TODO，`vllm/distributed/kv_transfer/kv_connector/v1/multi_connector.py:382`）。`kv_transfer_config.kv_connector="MultiConnector"` 时 worker 侧 `_KV_CONNECTOR_AGENT` 是 MultiConnector，`post_forward` 调其 `get_kv_connector_kv_cache_events` → 继承 base 默认返回 None → AscendStoreConnector 子 connector 队列里的事件永不取出（kv_transfer.py 的 `update_kv_event` 有写入，`pool_worker.get_kv_events` 有读取，但读取入口没被 MultiConnector 代理）。scheduler 侧 `take_events()` 是代理了的（有值），但 worker 侧没传上来 → `update_connector_output` 收到 None。
- **为什么会写出 (Why)**：排查时把「scheduler 侧 take_events 有代理」误当成「worker 侧 get_kv_connector_kv_cache_events 也有代理」——两个方法是两条独立链路（worker 收集 vs scheduler 消费），MultiConnector 只实现了后者。教训：MultiConnector 的代理方法要逐一核对，不能凭一个方法的实现推断另一个。另一个教训：worker 侧 `has_kv_transfer_group()` 决定 `NO_OP_KV_CONNECTOR`，但 MultiConnector 场景不是 NO_OP 而是"有对象但方法缺失"——`ensure_kv_transfer_initialized` 的条件（`is_kv_transfer_instance`）要单独验证。
- **修复 (Fix)**：
  - motor 仓库：`examples/deployer/patch/0.23.0/vllm_multi_connector_kv_events.patch`——为 MultiConnector 补充 `get_kv_connector_kv_cache_events` 代理（遍历 `self._connectors`，`add_events`/`increment_workers` 合并，保持第一个非 None 的容器类型以通过 `AscendStoreConnector.update_connector_output` 的 `isinstance(AscendStoreKVEvents)` 检查）。
  - vllm-ascend 侧（用户部署环境）可用同逻辑的 AscendMultiConnector 实现。
  - 已建议给 vLLM 上游提 PR 补齐。
- **测试拦截 (Test interception)**：无自动化测试（patch 是第三方仓库代码，motor 仓库内无法单测）。验证方式：部署后 EngineCore 日志 `update_connector_output: kv_cache_events=<AscendStoreKVEvents>` + `connector events: [N]` + conductor `vLLM non-HBM: ingesting offload blocks`。
- **场景 (Scenario)**：`kv_transfer_config.kv_connector="MultiConnector"`（含 AscendStoreConnector 子 connector）+ `enable_kv_cache_events=True` + vLLM ≤ v0.23.0（上游未合入前所有版本）。
- **关键词 (Keywords)**：kv_conductor、MultiConnector、get_kv_connector_kv_cache_events、offload 事件丢失、两阶段匹配、kv_transfer_config
