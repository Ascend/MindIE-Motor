# MemCache 后端

MemCache 为默认池化后端，基于 [memcache_hybrid](https://gitcode.com/Ascend/memcache) 提供 KV 池化能力，已预装在 Motor 镜像中，无需额外安装。

## 配置

`AscendStoreConnector` 中配置 `"backend": "memcache"`：

```json
"backend": "memcache"
```

`kv_cache_store_config` 中配置 `"backend": "memcache"`，可选配置 MetaService 端口、LocalService 部署模式及单进程 DRAM 池化内存：

```json
"kv_cache_store_config": {
  "backend": "memcache",
  "local_service_mode": "standalone",
  "dram_size": "100GB"
}
```

- `dram_size`（可选）：**每个节点**贡献给 KV 池化的 DRAM 总内存大小，`inprocess` 和 `standalone` 模式均生效。格式如 `"100GB"`。
  - `inprocess` 模式：daemon 会自动将该值除以本节点 DP 数，得到每个 vLLM 进程的 `dram.size`，确保节点总贡献等于 `dram_size`。
  - `standalone` 模式：独立 LocalService 直接使用该值作为 DRAM 池化内存。
  - 未配置时通过 `free -b` 自动扫描节点可用内存（保留 20% 余量）。

> deploy.py 会自动启动 MemCache MetaService（对标 Mooncake 的 `mooncake_master`），无需手动干预。

### LocalService 部署模式（`local_service_mode`）

MemCache 在每个 P/D 引擎节点上需要运行一个 LocalService 进程来管理 DRAM 池化内存。LocalService 支持两种部署模式：

| 模式 | 值 | DRAM 分配方式 | LocalService 进程 | 适用场景 |
|------|-----|--------------|-------------------|----------|
| **同进程** | `inprocess` | vLLM 进程内分配；每个进程的 `dram.size` = `dram_size` ÷ 本节点 DP 数 | 无独立进程，集成在 vLLM 内 | 部署简单，资源占用少 |
| **独立进程** | `standalone` | 独立 LocalService 直接使用 `dram_size`；vLLM 侧 `dram.size=0GB` | NodeManager 自动拉起并监控 | 内存隔离更好，LS 崩溃不影响 vLLM |

**默认值**：A2 硬件默认 `inprocess`（device_rdma），A3/A5 硬件默认 `standalone`（device_sdma）。
如需覆盖硬件默认值，在 `user_config.json` 中显式配置即可。

两种模式的差异和部署示例详见 [MemCache 分离部署方案](https://gitcode.com/Ascend/memcache/wiki/MemCache+vLLM+A3%E5%88%86%E7%A6%BB%E9%83%A8%E7%BD%B2%E6%A1%88%E4%BE%8B.md)。
