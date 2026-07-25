# Mooncake 后端

Mooncake 池化后端，由 vllm-ascend 天然集成，**无需额外安装任何组件**。

## 配置

`AscendStoreConnector` 中配置 `"backend": "mooncake"`：

```json
"backend": "mooncake"
```

`kv_cache_store_config` 中配置 `"backend": "mooncake"`：

```json
"kv_cache_store_config": {
  "backend": "mooncake",
  "metadata_server": "P2PHANDSHAKE",
  "protocol": "ascend",
  "device_name": "",
  "global_segment_size": "1GB",
  "eviction_high_watermark_ratio": 0.9,
  "eviction_ratio": 0.1
}
```

`eviction_high_watermark_ratio` 和 `eviction_ratio` 为 Mooncake 专属参数，会传递给 `mooncake_master` 进程。

## 调优建议

- **`global_segment_size`**：根据模型大小和并发量调整，过小会导致频繁驱逐；过大则浪费显存。建议设为模型 KV Cache 预估大小的 1.5~2 倍。
- **`eviction_high_watermark_ratio`** 与 **`eviction_ratio`**：当池化空间使用率达到 `eviction_high_watermark_ratio` 时触发驱逐，每次驱逐 `eviction_ratio` 比例的空间。高并发场景可适度降低驱逐比例以减少抖动。
- **`default_kv_lease_ttl`**：控制 KV 对象的租约有效期，需确保大于传输超时时间（`ASCEND_CONNECT_TIMEOUT` / `ASCEND_TRANSFER_TIMEOUT`），避免租约在传输完成前过期。
