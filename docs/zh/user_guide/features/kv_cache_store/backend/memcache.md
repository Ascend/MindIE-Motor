# MemCache 后端

MemCache 为默认池化后端，基于 [memcache_hybrid](https://gitcode.com/Ascend/memcache) 提供 KV 池化能力，已预装在 Motor 镜像中，无需额外安装。

## 配置

`AscendStoreConnector` 中配置 `"backend": "memcache"`：

```json
"backend": "memcache"
```

`kv_cache_store_config` 中配置 `"backend": "memcache"`，可选配置 MetaService 端口及 LocalService 部署模式：

```json
"kv_cache_store_config": {
  "backend": "memcache",
  "config_store_port": 56001,
  "metrics_port": 58001,
  "local_service_mode": "standalone"
}
```

> deploy.py 会自动启动 MemCache MetaService（对标 Mooncake 的 `mooncake_master`），无需手动干预。

### LocalService 部署模式（`local_service_mode`）

MemCache 在每个 P/D 引擎节点上需要运行一个 LocalService 进程来管理 DRAM 池化内存。LocalService 支持两种部署模式：

| 模式 | 值 | DRAM 分配方式 | LocalService 进程 | 适用场景 |
|------|-----|--------------|-------------------|----------|
| **同进程** | `inprocess` | vLLM 进程内分配，`dram.size` 自动扫描 Pod 可用内存 | 无独立进程，集成在 vLLM 内 | 部署简单，资源占用少 |
| **独立进程** | `standalone` | 独立 `local_service` 进程分配，vLLM 侧 `dram.size=0GB` | NodeManager 自动拉起并监控 | 内存隔离更好，LS 崩溃不影响 vLLM |

**默认值**：A2 硬件默认 `inprocess`（device_rdma），A3/A5 硬件默认 `standalone`（device_sdma）。
如需覆盖硬件默认值，在 `user_config.json` 中显式配置即可。

两种模式的差异和部署示例详见 [MemCache 分离部署方案](https://gitcode.com/Ascend/memcache/wiki/MemCache+vLLM+A3%E5%88%86%E7%A6%BB%E9%83%A8%E7%BD%B2%E6%A1%88%E4%BE%8B.md)。
