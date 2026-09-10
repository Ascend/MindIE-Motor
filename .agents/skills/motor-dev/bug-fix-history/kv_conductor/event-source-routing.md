# [2026-09-09] ZMQ 订阅器将池事件误按 vLLM 事件解析

- **现象 (Symptom)**：CPU/Disk 事件端口收到的池事件被统一尝试按 vLLM 批次解析。
- **根因 (Root cause)**：订阅器没有保存发布端的事件来源，只按 payload 形状优先尝试 vLLM 解析。
- **为什么会写出 (Why)**：把事件载荷格式判断放在订阅端媒体注册信息之前，忽略了 CPU/Disk 端口的发布者协议。
- **修复 (Fix)**：增加 `EventSource::{Engine, Pool}`；根据注册媒体将 CPU/Disk 订阅标记为 Pool，并限制对应解析分支。
- **测试拦截 (Test interception)**：`cpu_and_disk_subscribers_use_pool_wire_format` 验证媒体到来源的映射。
- **场景 (Scenario)**：注册包含 CPU 或 Disk 媒体的 ZMQ 端口并接收池后端事件。
- **关键词 (Keywords)**：kv_conductor, ZmqSubscriber, EventSource, PoolEvent, dp_rank。
