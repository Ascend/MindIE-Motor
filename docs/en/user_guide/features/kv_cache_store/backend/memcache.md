# MemCache Backend

MemCache is the default pooled backend. It provides KV pooling capabilities based on [memcache_hybrid](https://gitcode.com/Ascend/memcache) and is preinstalled in the Motor image, requiring no additional installation.

## Pre-deployment Preparation (for 950 Series Servers Only)

When using the MemCache pooled backend on 950 series servers, first modify the service startup script `examples/deployer/startup/boot.sh` and add the following content near the top of the file (around line 12) before performing deployment:

```bash
# Add the following two lines of code near the top of the file, around line 12
python3 -c "import memfabric_hybrid"
export PYTHONHASHSEED=0

# The original code in the boot.sh file does not need to be modified and is provided only for reference on where to make the change
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/common.sh"
```

## Configuration

Configure `"backend": "memcache"` in `AscendStoreConnector`:

```json
"backend": "memcache"
```

Configure `"backend": "memcache"` in `kv_cache_store_config`, and optionally configure the LocalService deployment mode:

```json
"kv_cache_store_config": {
  "backend": "memcache",
  "local_service_mode": "standalone"
}
```

- (Optional) `local_service_mode`: LocalService deployment mode. See the description below. The default values are as follows:

  - Atlas 800I A2 inference server/Atlas 850 SuperPoD Server: `inprocess`;

  - Atlas 800I A3 SuperPoD Server: `standalone`.

> **All internal memcache configuration items** (DRAM pool size, communication protocol, SSD cache, UBSIO parameters, etc.) are managed directly by the user in `mmc-local-inprocess.conf`. The template file is located in `examples/deployer/startup/roles/kv_store_backends/memcache/`, and `common.sh` automatically synchronizes it to `$CONFIG_PATH/` during deployment.

### LocalService Deployment Mode (`local_service_mode`)

MemCache requires a LocalService process to run on each P/D engine node to manage DRAM pooled memory. LocalService supports two deployment modes:

| Mode | Value | DRAM Allocation Method | LocalService Process | Applicable Scenario |
|------|-----|--------------|-------------------|----------|
| **In-process** | `inprocess` | Allocated within the vLLM process; the `dram.size` of each process is configured in `mmc-local-inprocess.conf` | No independent process; integrated within vLLM | Simple deployment, low resource usage |
| **Standalone process** | `standalone` | The independent LocalService uses `mmc-local-standalone.conf`; on the vLLM side, `dram.size=0GB` | Automatically started and monitored by NodeManager | Better memory isolation; an LS crash does not affect vLLM |

**Default value**: For the Atlas 800I A2 inference server and Atlas 850 SuperPoD Server hardware, default to `inprocess`, while for the Atlas 800I A3 SuperPoD Server hardware, default to `standalone`. To override the hardware default, explicitly configure it in `user_config.json`.

For the differences between the two modes and deployment examples, see [MemCache Disaggregated Deployment Solution](https://gitcode.com/Ascend/memcache/wiki/MemCache+vLLM+A3%E5%88%86%E7%A6%BB%E9%83%A8%E7%BD%B2%E6%A1%88%E4%BE%8B.md).

## UBSIO/SSD Three-Level Cache (HBM → DRAM → SSD)

> **⚠️ This feature is not yet mature and is not recommended for use in production environments.** Enabling the SSD three-level cache requires extensive targeted configuration based on the actual hardware, workload, and performance requirements of the production environment. Parameter tuning is complex and lacks universal default values. It is recommended to wait for subsequent versions to provide more complete automated configuration capabilities before enabling it.

MemCache supports connecting local NVMe SSDs as the third-level cache through the UBSIO engine, automatically offloading cold KV Cache to the SSD while keeping only hot data in memory.

> For more information about the multi-level pooling principles, UBSIO configuration details, and more, see [MemCache Official Wiki — Multi-Level Pooling UBSIO Configuration Guide](https://gitcode.com/Ascend/memcache/wiki/%E3%80%90WIP%E3%80%91%E5%A4%9A%E7%BA%A7%E6%B1%A0%E5%8C%96%20UBSIO%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md).

### Partition Operation (⚠️ Required on Every Node)

> **⚠️ High-risk operation**: Selecting the wrong disk will cause irreversible data loss. Make sure that the target disk is a raw disk. Partitioning must be performed on **all** nodes where SSD caching is enabled, not only on the master node.

Use the official `partition_disks.sh` script to partition the raw NVMe disk:

1. Download the script and confirm the disk status (use `lsblk` to confirm that the target disk has no partitions and is not mounted)

2. Plan the number of partitions based on `device_count` (`standalone` is 1, and `inprocess` is `endpoints × local_world_size`)

3. Perform partitioning. The script outputs the `ubsio.disk.path` configuration line.

For detailed steps, script parameter descriptions, and the loop device simulation solution, see:

👉 [MemCache Wiki — Partition Operation](https://gitcode.com/Ascend/memcache/wiki/%E3%80%90WIP%E3%80%91%E5%A4%9A%E7%BA%A7%E6%B1%A0%E5%8C%96%20UBSIO%E9%85%8D%E7%BD%AE%E6%8C%87%E5%8D%97.md#%E5%88%86%E5%8C%BA%E6%93%8D%E4%BD%9C)

After running the script, fill the output path (for example, `/dev/nvme0n1p1:/dev/nvme0n1p2:...`) into `ubsio.disk.path` in the conf file.

### Enabling SSD Cache

The SSD cache is configured directly through the conf file and does not need to be set in `user_config.json`. **The target file depends on the deployment mode**:

- `local_service_mode = "inprocess"` → Edit `mmc-local-inprocess.conf`

- `local_service_mode = "standalone"` → Edit `mmc-local-standalone.conf`

Specific steps:

1. Edit the corresponding conf file and set `ock.mmc.local_service.storage.enabled = true`.

2. Set `ubsio.disk.path` to the path output by the partitioning script.

3. Adjust `ubsio.wcache.evict_water_level` (`standalone` = `85`, `inprocess` = `0`) and `ubsio.standalone.device_count` (`standalone` = `1`, `inprocess` = `endpoints × local_world_size`) according to the deployment mode.

4. Adjust other UBSIO parameters as needed.

> For configuration templates and detailed comments, see `mmc-local-inprocess.conf` and `mmc-local-standalone.conf`, located in `examples/deployer/startup/roles/kv_store_backends/memcache/`.
