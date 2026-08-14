# MemCache 后端

MemCache 为默认池化后端，基于 [memcache_hybrid](https://gitcode.com/Ascend/memcache) 提供 KV 池化能力，已预装在 Motor 镜像中，无需额外安装。

## 部署前准备（仅 950系列服务器；其他系列可跳过）

在 950系列服务器上使用 MemCache 池化后端时，请先修改服务启动脚本 `examples/deployer/startup/boot.sh`，在文件头部（约第 12 行附近）添加如下内容后再执行部署：

```bash
# 请在文件头部添加以下两行代码，在文件的12行附近
python3 -c "import memfabric_hybrid"
export PYTHONHASHSEED=0

# boot.sh文件原有代码，无需修改，仅用于参考修改位置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"
```

## 配置

`AscendStoreConnector` 中配置 `"backend": "memcache"`：

```json
"backend": "memcache"
```

`kv_cache_store_config` 中配置 `"backend": "memcache"`，可选配置 LocalService 部署模式：

```json
"kv_cache_store_config": {
  "backend": "memcache",
  "local_service_mode": "standalone"
}
```

- `local_service_mode`（可选）：LocalService 部署模式，见下方说明。默认值如下：
  - Atlas 800I A2 推理服务器/Atlas 850 超节点服务器： `inprocess`；
  - Atlas 800I A3 超节点服务器： `standalone`。

> **所有 memcache 内部配置项**（DRAM 池大小、通信协议、SSD 缓存、UBSIO 参数等）均由用户直接在 `mmc-local-inprocess.conf` 中管理。模板文件位于 `examples/deployer/startup/roles/kv_store_backends/memcache/`，部署时 `common.sh` 自动同步到 `$CONFIG_PATH/`。

### LocalService 部署模式（`local_service_mode`）

MemCache 在每个 P/D 引擎节点上需要运行一个 LocalService 进程来管理 DRAM 池化内存。LocalService 支持两种部署模式：

| 模式 | 值 | DRAM 分配方式 | LocalService 进程 | 适用场景 |
|------|-----|--------------|-------------------|----------|
| **同进程** | `inprocess` | vLLM 进程内分配；每个进程的 `dram.size` 在 `mmc-local-inprocess.conf` 中配置 | 无独立进程，集成在 vLLM 内 | 部署简单，资源占用少 |
| **独立进程** | `standalone` | 独立 LocalService 使用 `mmc-local-standalone.conf`；vLLM 侧 `dram.size=0GB` | NodeManager 自动拉起并监控 | 内存隔离更好，LS 崩溃不影响 vLLM |

**默认值**：Atlas 800I A2 推理服务器/Atlas 850 超节点服务器 硬件默认 `inprocess`，Atlas 800I A3 超节点服务器 硬件默认 `standalone`。如需覆盖硬件默认值，在 `user_config.json` 中显式配置即可。

两种模式的差异和部署示例详见 [MemCache 分离部署方案](https://gitcode.com/Ascend/memcache/wiki/MemCache+vLLM+A3%E5%88%86%E7%A6%BB%E9%83%A8%E7%BD%B2%E6%A1%88%E4%BE%8B.md)。

---

## UBSIO / SSD 三级缓存（HBM → DRAM → SSD）

> **⚠️ 该特性尚不成熟，暂不推荐在生产环境中使用。** 启用 SSD 三级缓存需要根据生产环境的实际硬件、负载和性能需求进行大量针对性配置，参数调优复杂且缺乏通用默认值。建议等待后续版本提供更完善的自动化配置能力后再启用。

MemCache 支持通过 UBSIO 引擎接入本地 NVMe SSD 作为第三级缓存，将冷 KV Cache 自动下沉到 SSD，内存仅保留热数据。

> 如需了解多级池化原理、UBSIO 配置项详情等更多内容，请参考 [MemCache 官方 Wiki — 多级池化 UBSIO 配置指南](https://gitcode.com/Ascend/memcache/wiki/%E3%80%90WIP%E3%80%91%E5%A4%9A%E7%BA%A7%E6%B1%A0%E5%8C%96%20UBSIO%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md)。

### 分区操作（⚠️ 每节点必做）

> **⚠️ 高危操作**：选错磁盘将造成不可逆的数据丢失。请务必确认目标磁盘为裸盘。分区需要在**所有**启用 SSD 缓存的节点上执行，不可仅在 master 节点操作。

使用官方 `partition_disks.sh` 脚本对 NVMe 裸盘分区：

1. 下载脚本并确认磁盘状态（`lsblk` 确认目标盘无分区、无挂载）
2. 按 `device_count` 规划分区数（`standalone` 为 1，`inprocess` 为 `endpoints × local_world_size`）
3. 执行分区，脚本输出 `ubsio.disk.path` 配置行

详细步骤、脚本参数说明及 loop 设备模拟方案详见：

👉 [MemCache Wiki — 分区操作](https://gitcode.com/Ascend/memcache/wiki/%E3%80%90WIP%E3%80%91%E5%A4%9A%E7%BA%A7%E6%B1%A0%E5%8C%96%20UBSIO%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md#%E5%88%86%E5%8C%BA%E6%93%8D%E4%BD%9C)

执行脚本后将输出的路径（如 `/dev/nvme0n1p1:/dev/nvme0n1p2:...`）填入 conf 文件中的 `ubsio.disk.path` 即可。

### 启用 SSD 缓存

SSD 缓存通过 conf 文件直接配置，无需在 `user_config.json` 中设置。**目标文件取决于部署模式**：

- `local_service_mode = "inprocess"` → 编辑 `mmc-local-inprocess.conf`
- `local_service_mode = "standalone"` → 编辑 `mmc-local-standalone.conf`

具体步骤：

1. 编辑对应 conf 文件，设置 `ock.mmc.local_service.storage.enabled = true`
2. 设置 `ubsio.disk.path` 为分区脚本输出的路径
3. 根据部署模式调整 `ubsio.wcache.evict_water_level`（`standalone` = `85`，`inprocess` = `0`）和 `ubsio.standalone.device_count`（`standalone` = `1`，`inprocess` = `endpoints × local_world_size`）
4. 其余 UBSIO 参数按需调整

> 配置模板及详细注释见 `mmc-local-inprocess.conf` 和 `mmc-local-standalone.conf`，位于 `examples/deployer/startup/roles/kv_store_backends/memcache/`。
