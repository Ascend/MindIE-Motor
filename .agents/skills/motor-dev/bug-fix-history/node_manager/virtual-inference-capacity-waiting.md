# [2026-09-29] Prefill KV容量耗尽导致虚推误判异常

- **现象 (Symptom)**：Prefill实例的GPU KV Cache利用率达到99.7%至99.8%，业务请求持续排队；虚推请求超时且AI Cube采样短时为0%，连续失败累计到6/6后将健康实例标为异常。
- **根因 (Root cause)**：`motor/node_manager/core/services/native_engine/virtual_inference/worker.py`仅以“虚推失败 + 低AI Cube”计数，无法区分引擎失活与请求因调度/KV容量不足而排队。
- **为什么会写出 (Why)**：原判据把低计算利用率等同于空闲，遗漏了KV Cache已满时调度器无法安排请求、AI Cube同样可能为低的容量受限状态。
- **修复 (Fix)**：低AI Cube虚推失败后读取同一引擎`/metrics`；仅当`vllm:num_requests_waiting_by_reason{reason="capacity"}`可用且总和为0时计数。容量等待大于0或指标不可用时跳过计数并保留已有计数。
- **测试拦截 (Test interception)**：`test_requesters.py`覆盖指标解析与HTTP请求；`test_worker.py`验证capacity为0时达到失败阈值，capacity大于0或指标不可用时不计数、不标异常。
- **场景 (Scenario)**：PD分离Prefill实例KV Cache接近或达到上限，虚推排队并超过周期请求超时，同时AI Cube采样低于阈值。
- **关键词 (Keywords)**：NodeManager, virtual inference, KV Cache, capacity waiting, false positive
