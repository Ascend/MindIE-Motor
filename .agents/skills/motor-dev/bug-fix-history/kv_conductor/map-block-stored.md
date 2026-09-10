# [2026-09-09] Conductor 拒绝 map 格式的 BlockStored

- **现象 (Symptom)**：包含 map 事件的 vLLM MessagePack 批次无法解析。
- **根因 (Root cause)**：`src/events/vllm.rs` 的 `VllmEventMap` 仅实现序列反序列化。
- **为什么会写出 (Why)**：误认为外层批次的 array_like 设置会传递给嵌套事件；事件实际独立继承 KVCacheEvent。
- **修复 (Fix)**：增加按字段名读取的 map visitor，复用 FlexHash，保留旧数组格式；未知字段忽略，type 必填。
- **测试拦截 (Test interception)**：`test_vllm_map_*` 覆盖完整批次、删除与清空、可选字段、注意力过滤和非法字段类型。
- **场景 (Scenario)**：vLLM 发布 [timestamp, [带 type 的 map 事件], dp_rank]。
- **关键词 (Keywords)**：kv_conductor, BlockStored, msgspec, map, deserialize_any。
