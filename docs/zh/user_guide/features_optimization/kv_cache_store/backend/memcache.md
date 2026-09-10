# MemCache 后端特性

## 特性介绍

**依据原文上下文内容重组，请进行人工校验。**

MemCache为MindIE Motor默认池化后端，基于 [memcache_hybrid](https://gitcode.com/Ascend/memcache) 提供高效KV池化能力，已预装在MindIE Motor镜像中，无需额外安装。通过池化机制实现KV缓存跨请求复用，提升缓存命中率，降低推理TTFT，减少重复计算，提升推理吞吐。

### 工作原理

**依据原文上下文内容重组，请进行人工校验。**

MemCache 在每个 P/D 引擎节点上通过 LocalService 进程管理 DRAM 池化内存，支持同进程（inprocess）和独立进程（standalone）两种部署模式。LocalService 将 KV Cache 数据在 DRAM 中统一管理，多个推理引擎节点可共享同一池化后端。通过 MetaService 广播 KV 块元数据事件（STORED / REMOVED / CLEARED），Motor 的 kv-conductor 基于 `backend_id` 计算 KV 亲和度，实现缓存感知 prefill 调度，将请求优先路由到已缓存前缀的节点。
此外，MemCache 支持通过 UBSIO 引擎接入本地 NVMe SSD 作为第三级缓存（HBM → DRAM → SSD），将冷 KV Cache 自动下沉到 SSD，仅保留热数据在内存中。

### 核心功能

**依据原文上下文内容重组，请进行人工校验。**

- LocalService 部署模式：支持inprocess（同进程，vLLM 内集成）和standalone（独立进程，NodeManager 自动拉起）两种模式，可根据硬件场景和隔离需求灵活选择。
- KV events 广播（缓存感知调度）：MetaService 在 KV 块元数据写入/删除后通过 ZMQ PUB 广播事件，kv-conductor 订阅后计算 KV 亲和度，实现基于缓存的请求路由，提升 prefill 效率。
- UBSIO/SSD 三级缓存：支持通过 UBSIO 引擎接入本地 NVMe SSD 作为第三级缓存，实现 HBM → DRAM → SSD 三级缓存架构，扩展缓存容量。**（当前该特性尚不成熟，暂不推荐生产环境使用。）**
- 多服务 kv_store 共享：支持跨推理服务复用 kv_store，通过target_job_id配置指向目标服务的 kv_store，减少重复缓存开销。

### 约束与限制

**依据原文上下文内容重组，请进行人工校验。**

| 约束维度 | 要求 |
|----------|------|
| 硬件 | 支持 Atlas 800I A2 推理服务器、Atlas 850 超节点服务器、Atlas 800I A3 超节点服务器。 |
| 部署场景 | 仅支持 PD 分离部署场景。 |
| 引擎 | 仅支持 vLLM 推理引擎。 |
| 特性互斥 | **内容缺失，需要人工补齐。** |
| 软件依赖 | memcache_hybrid 需为包含 KvEvent 功能（**memcache PR #334 起**？**给出具体版本？**）的版本；使用 `MultiConnector` 时需应用 `vllm_ascend_multi_connector_kv_events.patch` 补丁。 |
| 其他限制 | SSD 三级缓存尚不成熟，暂不推荐在生产环境中使用；分区操作为高危操作，选错磁盘将造成不可逆的数据丢失，需在所有启用SSD缓存的节点上执行。 |

## 特性使用

### 环境准备

已安装并部署MindIE Motor环境，MemCache已预装在MindIE Motor镜像中。

### 使用样例

**依据原文上下文内容重组，请进行人工校验。**

以下步骤展示如何配置和使用MemCache后端。

**操作步骤**

1. 配置 backend 为 memcache。

    在 `AscendStoreConnector` 中配置 `"backend": "memcache"`：

    ```json
    "backend": "memcache"
    ```

2. 配置 kv_cache_store_config。
   kv_cache_store_config 中配置 "backend": "memcache"，可选配置 LocalService 部署模式及跨服务复用 kv_store。

    ```json
    "kv_cache_store_config": {
      "backend": "memcache",
      "local_service_mode": "standalone",
      "target_job_id": "service-a"
    }
    ```

    **表 1** 配置参数说明

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    |--------|------|----------|-----------|--------|------|
    | backend | 字符串 | "memcache" | 必填 | — | 指定后端类型为 memcache |
    | local_service_mode | 字符串 | "inprocess" / "standalone"`| 选填 | <ul><li>Atlas 800I A2推理服务器/Atlas 850超节点服务器：inprocess；</li><li>Atlas 800I A3超节点服务器：standalone</li></ul> | LocalService 部署模式 |
    | target_job_id| 字符串 | 目标服务的 `motor_deploy_config.job_id` | 选填 | — | 复用其他推理服务的 kv_store。未配置、与自身 `job_id` 相同、或目标 kv_store 不可用时，在本 namespace 新建 kv_store。详见 [KV 池化 README — 多套服务共享 kv_store](../README.md#step3) |

    >[!NOTE] 说明
    >所有 memcache 内部配置项（DRAM 池大小、通信协议、SSD 缓存、UBSIO 参数等）均由用户在 `mmc-local-inprocess.conf` 中管理。模板文件位于 `examples/deployer/startup/roles/kv_store_backends/memcache/`，部署时 `common.sh` 自动同步到 `$CONFIG_PATH/`。

3. （可选）配置 LocalService 部署模式。

    根据硬件和隔离需求，选择 `inprocess` 或 `standalone` 模式：

    - `inprocess`：vLLM 进程内分配 DRAM，部署简单，资源占用少。
    - `standalone`：独立 LocalService 进程，NodeManager 自动拉起并监控，内存隔离更好，LocalService 崩溃不影响 vLLM。

    如需覆盖硬件默认值，在 `user_config.json` 中显式配置 `local_service_mode` 即可。两种模式的差异和部署示例详见 [MemCache 分离部署方案](https://gitcode.com/Ascend/memcache/wiki/MemCache+vLLM+A3%E5%88%86%E7%A6%BB%E9%83%A8%E7%BD%B2%E6%A1%88%E4%BE%8B.md)。

4. （可选）开启 KV events 广播。

    启用缓存感知 prefill 调度：

    1. 开启 MetaService 广播。
      解除examples/deployer/startup/roles/kv_store_backends/memcache/memcache_meta_service.py 中「KV events 广播」配置块的注释，并按需填写 kv_events_model_name / kv_events_block_size。（需与 kv_conductor_config 中注册的 model_path / block_size 一致，否则事件无法命中索引）
    2. 重启 kv_store。
    3. 配置订阅地址。
      在 kv_conductor_config.pool_endpoint 中配置 MetaService 的 kv_events 广播地址，如 "tcp://mindie-motor-kvs-master:5557"（端口须与脚本中 kv_events_endpoint 一致）。也可写作 "tcp://*:5557"，* 会自动替换为 K8s 注入的 KVS_MASTER_SERVICE 域名。Coordinator 启动注册时会把该地址告知 kv-conductor 并订阅。K8s Service mindie-motor-kvs-master 已默认暴露 kv-events: 5557 端口。
    4. backend_id 自动注入。
      每个引擎节点 LocalService 的 ock.mmc.local_service.backend_id 由 deployer 在部署时自动替换为本节点 Pod IP，无需用户配置。kv-conductor 据此区分 KV 块所属节点。

        >[!NOTE] 说明
        >MultiConnector 补丁（必装）：vLLM 上游未实现 MultiConnector.get_kv_connector_kv_cache_events()（TODO），kv_transfer_config.kv_connector 使用 MultiConnector 时 worker 侧 AscendStoreConnector 收集的 KV 事件会被静默丢弃，导致引擎 offload 事件到不了 kv-conductor、两阶段匹配永远缺引擎侧。vllm-ascend 已将 MultiConnector 注册替换为 AscendMultiConnector，部署时应用 examples/deployer/patch/vllm_ascend_multi_connector_kv_events.patch（为 AscendMultiConnector 补充子 connector 事件代理，一个补丁适配 v0.20.2 ~ v0.26.0）。已同步建议上游 vLLM 合入。
        **前提**：memcache_hybrid 需为包含 KvEvent 功能（memcache PR #334 起）的版本，否则 MetaConfig 不识别 kv_events_* 字段。

5. （可选，不推荐生产）启用 SSD 三级缓存。

    >[!NOTE] 注意
    >该特性尚不成熟，暂不推荐在生产环境中使用。

    1. 在所有启用 SSD 缓存的节点上执行分区操作：使用 `partition_disks.sh` 脚本对 NVMe 裸盘分区，将输出的路径（如 `/dev/nvme0n1p1:/dev/nvme0n1p2:...`）填入 conf 文件中的 `ubsio.disk.path`。
        >[!NOTE] 说明
        >- 需确认磁盘状态（lsblk 确认目标盘无分区、无挂载）
        >- 按 device_count 规划分区数（standalone 为 1，inprocess 为 endpoints × local_world_size）。
    2. 编辑对应 conf 文件。
         - local_service_mode = "inprocess" 时：编辑 mmc-local-inprocess.conf
         - local_service_mode = "standalone" 时：编辑 mmc-local-standalone.conf
    3. 设置 `ock.mmc.local_service.storage.enabled = true`。
    4. 设置 `ubsio.disk.path` 为分区脚本输出的路径。
    5. 根据部署模式调整 `ubsio.wcache.evict_water_level`（`standalone` = `85`，`inprocess` = `0`）和 `ubsio.standalone.device_count`（`standalone` = `1`，`inprocess` = `endpoints × local_world_size`）。
    6. 其余 UBSIO 参数按需调整。
      配置模板及详细注释见 mmc-local-inprocess.conf 和 mmc-local-standalone.conf，位于 examples/deployer/startup/roles/kv_store_backends/memcache/目录。

### 验证特性

**依据原文上下文内容重组，请进行人工校验。**

通过以下方式验证 MemCache 后端是否配置成功：

1. **检查配置加载日志**：在 Motor 启动日志中搜索 `"backend"` 相关输出，确认 backend 为 `"memcache"` 且配置项已正确加载。
2. **检查 LocalService 状态**：
   - `inprocess` 模式：vLLM 启动日志中查看 DRAM 池分配信息。
   - `standalone` 模式：使用 `kubectl` 或 `ps` 命令确认 LocalService 进程是否存在，NodeManager 日志中是否有进程拉起记录。
3. **验证 KV events 功能**（如已开启）：在 kv-conductor 日志中搜索 `"subscribe kv_events"` 或 `"kv_events"` 关键字，确认广播订阅成功。
4. **验证 SSD 缓存**（如已启用）：在对应 conf 文件所在节点的日志中搜索 `"ubsio"` 和 `"storage.enabled"` 关键字，确认 SSD 缓存配置已生效。

**预期输出**：

- 日志中无 `"backend"` 配置加载错误或 `"memcache"` 初始化失败相关错误信息。
- `standalone` 模式下，LocalService 进程持续运行，状态正常。
- KV events 开启后，kv-conductor 正常接收并处理事件，无订阅失败或连接断开日志。
- SSD 缓存启用后，`ubsio.disk.path` 配置的磁盘分区可正常读写，无 IO 错误。

## 常见问题

**依据原文上下文内容重组，请进行人工校验。**

### 启用 KV events 后，kv-conductor 日志中未出现订阅成功信息

**问题描述**：按照步骤开启 KV events 广播后，kv-conductor 日志中未出现 `"subscribe kv_events success"` 或类似关键字。

**原因分析**：`pool_endpoint` 配置的广播地址与 `memcache_meta_service.py` 中的 `kv_events_endpoint` 端口不一致，或 `kv_events_model_name` / `kv_events_block_size` 与 `kv_conductor_config` 中注册的值不匹配。

**解决步骤**：

1. 确认 `kv_conductor_config.pool_endpoint` 中的端口与脚本中 `kv_events_endpoint` 一致。
2. 确认 `kv_events_model_name` 和 `kv_events_block_size` 与 `kv_conductor_config` 中注册的 `model_path` / `block_size` 一致。
3. 重启 kv_store 使配置生效。

### 使用 MultiConnector 时，KV events 无法订阅

**问题描述**：`kv_transfer_config.kv_connector` 使用 `MultiConnector` 时，引擎 offload 事件无法到达 kv-conductor，两阶段匹配永远缺少引擎侧。

**原因分析**：vLLM 上游未实现 `MultiConnector.get_kv_connector_kv_cache_events()`，导致 AscendStoreConnector 收集的 KV 事件被静默丢弃。

**解决步骤**：

1. 应用 `examples/deployer/patch/vllm_ascend_multi_connector_kv_events.patch` 补丁。
2. 重启服务使补丁生效。

### SSD 分区操作后磁盘无法正常使用

**问题描述**：执行 `partition_disks.sh` 脚本进行分区后，磁盘无法正常读写或系统识别异常。

**原因分析**：选错了目标磁盘，或分区规划数量与部署模式不匹配。

**解决步骤**：

1. 使用 `lsblk` 确认目标磁盘为裸盘（无分区、无挂载）。
2. 按部署模式规划分区数：`standalone` 为 1，`inprocess` 为 `endpoints × local_world_size`。
3. 重新执行分区脚本，确保使用正确的磁盘设备。
