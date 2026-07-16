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
