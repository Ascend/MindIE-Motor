# KV Cache 亲和性调度

本页内容已合并到 [KV Cache 亲和性调度](../features/kvcache_affinity.md)，后续在该入口统一维护。

- [典型配置](../features/kvcache_affinity.md#典型配置)
- [池化亲和配置（CPU/Disk）](../features/kvcache_affinity.md#pool-affinity)
- [参数说明](../features/kvcache_affinity.md#参数说明)
- [DeepSeek V4 / 混合 KV Cache](../features/kvcache_affinity.md#deepseek-v4)
- [调度评分与回退](../../design/kv_conductor.md#coordinator-调度集成)
- [重注册与回放](../../design/kv_conductor.md#registration-lifecycle)
- [验证结果](../features/kvcache_affinity.md#验证结果)

字段默认值与兼容迁移见[配置参考](../configuration/config_reference.md#kv_conductor_config-自动推导与兼容)，
索引和协议实现见 [KV Conductor 设计](../../design/kv_conductor.md)。
