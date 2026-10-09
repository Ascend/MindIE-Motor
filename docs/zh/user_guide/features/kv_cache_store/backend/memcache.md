# MemCache 后端特性

## 特性介绍

MemCache为MindIE Motor默认池化后端，基于 [memcache_hybrid](https://gitcode.com/Ascend/memcache) 提供高效KV池化能力，已预装在MindIE Motor镜像中，无需额外安装。通过池化机制实现KV缓存跨请求复用，提升缓存命中率，降低推理TTFT，减少重复计算，提升推理吞吐。

### 工作原理

MemCache 在每个 P/D 引擎节点上通过 LocalService 进程管理 DRAM 池化内存，支持同进程（inprocess）和独立进程（standalone）两种部署模式。LocalService 将 KV Cache 数据在 DRAM 中统一管理，多个推理引擎节点可共享同一池化后端。通过 MetaService 广播 KV 块元数据事件（STORED / REMOVED / CLEARED），Motor 的 kv-conductor 基于 `backend_id` 计算 KV 亲和度，实现缓存感知 prefill 调度，将请求优先路由到已缓存前缀的节点。
此外，MemCache 支持通过 UBSIO 引擎接入本地 NVMe SSD 作为第三级缓存（HBM → DRAM → SSD），将冷 KV Cache 自动下沉到 SSD，仅保留热数据在内存中。

### 核心功能

- LocalService 部署模式：支持inprocess（同进程，vLLM 内集成）和standalone（独立进程，NodeManager 自动拉起）两种模式，可根据硬件场景和隔离需求灵活选择。
- KV events 广播（缓存感知调度）：MetaService 在 KV 块元数据写入/删除后通过 ZMQ PUB 广播事件，kv-conductor 订阅后计算 KV 亲和度，实现基于缓存的请求路由，提升 prefill 效率。
- UBSIO/SSD 三级缓存：支持通过 UBSIO 引擎接入本地 NVMe SSD 作为第三级缓存，实现 HBM → DRAM → SSD 三级缓存架构，扩展缓存容量。Kubernetes 块设备准备见 [DRAM + SSD 多级池化 Kubernetes 配置示例](https://gitcode.com/Ascend/memcache/wiki/DRAM+%20SSD%20多级池化%20Kubernetes%20配置示例.md)。生产环境仍需独占块设备，暂不直接按默认模板上线。
- 多服务 kv_store 共享：支持跨推理服务复用 kv_store，通过target_job_id配置指向目标服务的 kv_store，减少重复缓存开销。

### 约束与限制

| 约束维度 | 要求 |
|----------|------|
| 硬件 | 支持 Atlas 800I A2推理服务器、Ascend 950PR系列产品、Atlas 800I A3超节点服务器。 |
| 部署场景 | 仅支持 PD 分离部署场景。 |
| 引擎 | 仅支持 vLLM 推理引擎。 |
| 部署模式 | Atlas 800I A3 保持 `standalone`。Atlas 800I A2 与 Ascend 950PR 的运行时会强制 `inprocess`，在 `user_config.json` 里写成 `standalone` 也不会按独立进程拉起。 |
| 特性互斥 | <ul><li>请勿将 `"backend"` 配置为 `"ucm"`，UCM 不通过 `AscendStoreConnector` 的 backend 机制接入。</li><li>`AscendStoreConnector` 与 `kv_cache_store_config` 必须配置一致，且均为 `"memcache"`。</li><li>`MooncakeConnectorV1` / `MooncakeHybridConnector` 等属于 P/D 实时传输层，与 MemCache 池化后端不是同一层配置，切勿混淆。</li></ul> |
| 软件依赖 | 基础能力开箱即用，无需额外安装（memcache_hybrid 已预装）。开启 KV events 时，memcache_hybrid 版本需包含 KvEvent 功能（**MemCache Hybrid v1.2.0 及以后版本**，对应 memcache PR #334）；若使用 `MultiConnector`，则需应用 `vllm_ascend_multi_connector_kv_events.patch` 补丁。 |
| 其他限制 | <ul><li>SSD 需要独占块设备。已经带文件系统的整盘不要分区、不要格式化。</li><li>`ubsio.disk.path` 填写容器内路径 `/dev/memcache-ubsio0`，不要填写宿主机 `/dev/nvme*` 或 `/dev/loop*`。</li><li>`ubsio.standalone.device_count` 的合法范围是 0～16，等于本节点上 `dram.size` 大于 0 的 LocalService 进程数。standalone 填 `1`。它不等于 vLLM worker 数，填 32 会在启动时被 UBSIO 拒绝。</li><li>入池粒度是 128 token。短于 128 token 的 prompt 不会 put，也不会在 SSD 上留下对应写入。</li><li>新 put 必须先在 DRAM 分配连续 blob。SSD 盘剩余空间很大时，DRAM 碎片仍会使这次 put 返回 -1。</li><li>`MooncakeConnectorV1` 的 P2P 失败不能当成 SSD 失败，二者不是同一层。</li></ul> |

## 特性使用

### 环境准备

已安装并部署MindIE Motor环境，MemCache已预装在MindIE Motor镜像中。

### 使用样例

以下步骤展示如何配置和使用MemCache后端。

>[!NOTE] 说明
>混合attention模型的 `connectors[0]` 需使用 `MooncakeHybridConnector`，详情请参见[P/D传输Connector选型](../README.md#table_Connector)。

**操作步骤**

1. 配置 backend 为 memcache。

    在 `AscendStoreConnector` 中配置 `"backend": "memcache"`：

    ```json
    "backend": "memcache"
    ```

2. 配置 kv_cache_store_config。
   kv_cache_store_config 中配置 "backend": "memcache"，可选配置 LocalService 部署模式及跨服务复用 kv_store。

    A3 standalone 的 `local_config_path` 仍然指向 inprocess 文件。NodeManager 拉起独立 LocalService 时，会把子进程的配置切到同目录的 `mmc-local-standalone.conf`。

    ```json
    "kv_cache_store_config": {
      "backend": "memcache",
      "local_service_mode": "standalone",
      "local_config_path": "/usr/local/Ascend/pyMotor/conf/mmc-local-inprocess.conf",
      "target_job_id": "service-a"
    }
    ```

    **表 1** 配置参数说明

    | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
    |--------|------|----------|-----------|--------|------|
    | backend | 字符串 | "memcache" | 必填 | — | 指定后端类型为 memcache |
    | local_service_mode | 字符串 | "inprocess" / "standalone" | 选填 | <ul><li>Atlas 800I A2推理服务器/Ascend 950PR系列产品：inprocess；</li><li>Atlas 800I A3超节点服务器：standalone</li></ul> | LocalService 部署模式。A2/A5 运行时强制 inprocess |
    | local_config_path | 字符串 | 容器内 conf 路径 | 选填 | `/usr/local/Ascend/pyMotor/conf/mmc-local-inprocess.conf` | worker 与 NodeManager 读取的客户端配置。standalone 也填这份 inprocess 文件，不要改成 `mmc-local-standalone.conf` |
    | target_job_id| 字符串 | 目标服务的 `motor_deploy_config.job_id` | 选填 | — | 复用其他推理服务的 kv_store。未配置、与自身 `job_id` 相同、或目标 kv_store 不可用时，在本 namespace 新建 kv_store。详见 [KV 池化 README — 多套服务共享 kv_store](../README.md#step3) |

    >[!NOTE] 说明
    >DRAM、协议和 SSD 参数写在两份模板里，位于 `examples/deployer/startup/roles/kv_store_backends/memcache/`。部署时 `common.sh` 把它们同步到 `$CONFIG_PATH/`，引擎容器内对应 `/usr/local/Ascend/pyMotor/conf/`。
    >- `mmc-local-inprocess.conf`：vLLM worker 的客户端配置。A3 standalone 下保持 `dram.size = 0GB`、`storage.enabled = false`、`ubsio.disk.path` 为空。
    >- `mmc-local-standalone.conf`：独立 LocalService 的配置。只有这份文件打开 DRAM 和 SSD。
    >`deploy.py --update_config` 的白名单不包含 `kv_cache_store_config`。修改 `local_service_mode` 或 `local_config_path` 后，需要重新部署引擎 Pod，只刷 ConfigMap 不会改到已经运行的进程。

3. （可选）配置 LocalService 部署模式。

    根据硬件和隔离需求，选择 `inprocess` 或 `standalone` 模式：

    - `inprocess`：vLLM 进程内分配 DRAM，部署简单，资源占用少。
    - `standalone`：独立 LocalService 进程，NodeManager 自动拉起并监控，内存隔离更好，LocalService 崩溃不影响 vLLM。

    Atlas 800I A3 使用 standalone。如需覆盖 A2/A5 以外的默认值，在 `user_config.json` 中显式配置 `local_service_mode`。两种模式的差异和部署示例详见 [MemCache 分离部署方案](https://gitcode.com/Ascend/memcache/wiki/MemCache+vLLM+A3分离部署方案.md)。

4. （可选）开启 KV events 广播。<a id="kv-events-affinity"></a>

    启用缓存感知 prefill 调度：
    Motor 侧配置及验收见 [池化亲和配置](../../kvcache_affinity.md#pool-affinity)。

    1. 开启 MetaService 广播。
      解除examples/deployer/startup/roles/kv_store_backends/memcache/memcache_meta_service.py 中「KV events 广播」配置块的注释，并按需填写 kv_events_model_name / kv_events_block_size。（需与 kv_conductor_config 中注册的 model_path / block_size 一致，否则事件无法命中索引）
    2. 重启 kv_store。
    3. 配置订阅地址。
      在 kv_conductor_config.pool_endpoint 中配置 MetaService 的 kv_events 广播地址，如 "tcp://mindie-motor-kvs-master:5557"（端口须与脚本中 kv_events_endpoint 一致）。也可写作 "tcp://*:5557"，* 会自动替换为 K8s 注入的 KVS_MASTER_SERVICE 域名。Coordinator 启动注册时会把该地址告知 kv-conductor 并订阅。K8s Service mindie-motor-kvs-master 已默认暴露 kv-events: 5557 端口。
    4. backend_id 自动注入。
      每个引擎节点 LocalService 的 ock.mmc.local_service.backend_id 由 deployer 在部署时自动替换为本节点 Pod IP，无需用户配置。kv-conductor 据此区分 KV 块所属节点。

        >[!NOTE] 说明
        >MultiConnector 补丁（必装）：vLLM 上游未实现 MultiConnector.get_kv_connector_kv_cache_events()（TODO），kv_transfer_config.kv_connector 使用 MultiConnector 时 worker 侧 AscendStoreConnector 收集的 KV 事件会被静默丢弃，导致引擎 offload 事件到不了 kv-conductor、两阶段匹配永远缺引擎侧。vllm-ascend 已将 MultiConnector 注册替换为 AscendMultiConnector，部署时应用 examples/deployer/patch/vllm_ascend_multi_connector_kv_events.patch（为 AscendMultiConnector 补充子 connector 事件代理，一个补丁适配 v0.20.2 ~ v0.26.0）。已同步建议上游 vLLM 合入。
        **前提**：memcache_hybrid 需为包含 KvEvent 功能的版本（**MemCache Hybrid v1.2.0 及以后版本**，对应 memcache PR #334），否则 MetaConfig 不识别 kv_events_* 字段。

5. （可选）启用 SSD 三级缓存。

    块设备准备以 [DRAM + SSD 多级池化 Kubernetes 配置示例](https://gitcode.com/Ascend/memcache/wiki/DRAM+%20SSD%20多级池化%20Kubernetes%20配置示例.md) 为准。下面是按该示例，用 `examples/deployer/deploy.py` 启用 MemCache SSD 的步骤。`kv_cache_store_config.backend` 为 `memcache`。Atlas 800I A2 与 Ascend 950PR 使用 `inprocess`，Atlas 800I A3 使用 `standalone`。

    >[!NOTE] 说明
    >- 标准 attention 的 `connectors[0]` 使用 `MooncakeConnectorV1` 或 `MooncakeConnectorV2`，`connectors[1]` 使用 `AscendStoreConnector` 且 `"backend": "memcache"`。混合 attention 的 `connectors[0]` 使用 `MooncakeHybridConnector`。选型见 [P/D传输Connector选型](../README.md#table_Connector)。
    >- 协议按硬件填写：A2 为 `device_rdma`，A3 为 `device_sdma`，A5 为 `device_urma`。conf 模板默认值不能直接套到其他硬件。
    >- `storage.enabled` 只在 `ubsio.disk.path` 非空且本进程 `dram.size > 0` 时由该进程打开 SSD。A3 standalone 的 worker 读 inprocess 文件，其中 `dram.size = 0GB`，因此 worker 不打开 SSD。SSD 只由独立 LocalService 按 standalone 文件打开。
    >- `ubsio.disk.path` 填容器内路径，单盘为 `/dev/memcache-ubsio0`，多盘用冒号拼接，例如 `/dev/memcache-ubsio0:/dev/memcache-ubsio1`。不要把宿主机 `/dev/nvme*` 或 `/dev/loop*` 写进 conf。
    >- `examples/deployer` 的 storage 配置是 hostPath 目录挂载，不能传入块设备。块设备用 `volumeMode: Block` 的 PV/PVC，再以 `volumeDevices` 挂到引擎容器。`deploy_mode` 使用 `multi_deployment`。InferServiceSet 展开 Deployment 时会丢掉 `volumeDevices`。
    >- 节点内存需要大于 `ubsio.mem.size_in_gb × device_count` 再加上 vLLM 占用。只使用没有文件系统的独占盘。已经带文件系统的整盘不要分区、不要格式化。没有空闲物理盘时，按 wiki 在现有文件系统上建 loop 镜像再 `losetup`。`partition_disks.sh` 不在本仓库和 Motor 镜像中，Kubernetes 场景不要用它生成 conf 里的路径。
    >- `ubsio.standalone.force_new_disk` 只在这块盘第一次初始化时设为 `true`。`Start boostio success` 之后改回 `false` 再部署。之后重启 LocalService 会继续用已有盘上的缓存。

    1. 准备块设备。集群已有 local-static-provisioner 时，给节点打标签 `memcache.io/ubsio-node`，在 `/mnt/local-pv/memcache-ubsio` 下放置指向独占盘或 loop 的符号链接，由 provisioner 生成 PV。没有 provisioner 时，手工创建 StorageClass（`kubernetes.io/no-provisioner`，`volumeBindingMode: Immediate`）和 `volumeMode: Block` 的 local PV，并用 `nodeAffinity` 绑到引擎所在节点。每个需要写 SSD 的引擎角色各用一块盘。没有空闲裸盘时，在已有文件系统上创建 loop 镜像并 `losetup`，再把 loop 设备做成 local PV。
    2. 为每块盘创建 `volumeMode: Block` 的 PVC，并用 `volumeName` 绑定对应 PV。
    3. 在 prefill、decode 引擎 Pod 上挂入块设备。Prefill 示例：

        ```yaml
        volumes:
          - name: ubsio0
            persistentVolumeClaim:
              claimName: ubsio-prefill
        volumeDevices:
          - name: ubsio0
            devicePath: /dev/memcache-ubsio0
        ```

        Decode 把 `claimName` 换成自己的 PVC。容器内路径仍然是 `/dev/memcache-ubsio0`，两边可以同名，因为对应的是各自节点上的不同宿主机设备。当前 deployer 不会自动生成这段 `volumeDevices`，需要在生成引擎 YAML 时补上，或在部署前改 Deployment。
    4. 按模式编辑 conf。A2、inprocess、单盘改 `mmc-local-inprocess.conf`，协议 `device_rdma`，`dram.size` 大于 0，`device_count` 等于 `endpoints × local_world_size`，且不超过 16。示例里尖括号中的值按现场替换：

        ```text
        ock.mmc.local_service.protocol = device_rdma
        ock.mmc.local_service.dram.size = <大于0的DRAM容量>
        ock.mmc.local_service.storage.enabled = true
        ubsio.disk.path = /dev/memcache-ubsio0
        ubsio.mem.size_in_gb = <UBSIO内存GB>
        ubsio.wcache.evict_water_level = 0
        ubsio.wcache.disk_evict_water_level = 90
        ubsio.standalone.device_count = <endpoints × local_world_size，且为 0～16>
        ```

        A3 standalone 同时保留两份文件。`user_config.json` 的 `local_config_path` 指向 inprocess 文件，worker 使用下面这份客户端配置：

        ```text
        ock.mmc.local_service.protocol = device_sdma
        ock.mmc.local_service.dram.size = 0GB
        ock.mmc.local_service.storage.enabled = false
        ubsio.disk.path =
        ubsio.standalone.device_count = 1
        ```

        独立 LocalService 使用同目录的 `mmc-local-standalone.conf`。`device_count` 填 1。仓库模板里 `ubsio.wcache.evict_water_level` 为 0，按模板填写即可完成初始化。`force_new_disk` 在首次清盘初始化成功后保持 `false`：

        ```text
        ock.mmc.local_service.protocol = device_sdma
        ock.mmc.local_service.dram.size = 2GB
        ock.mmc.local_service.storage.enabled = true
        ubsio.disk.path = /dev/memcache-ubsio0
        ubsio.mem.size_in_gb = 8
        ubsio.wcache.evict_water_level = 0
        ubsio.wcache.disk_evict_water_level = 90
        ubsio.standalone.device_count = 1
        ubsio.standalone.force_new_disk = false
        ```

        `dram.size = 2GB` 是模板量级。96K 前缀、多个 worker 同时 put 时，2GB DRAM 很容易占到 90% 以上并出现连续空间不足。要减少 put 失败，按并发写入量加大这份 standalone 文件里的 `dram.size`。加大 DRAM 不会绕过“新数据先写入 DRAM”这条路径。

        `common.sh` 会把两份文件同步到 ConfigMap。引擎启动后，worker 读 `/usr/local/Ascend/pyMotor/conf/mmc-local-inprocess.conf`。standalone 子进程再改读同目录的 `mmc-local-standalone.conf`。模板和字段注释在 `examples/deployer/startup/roles/kv_store_backends/memcache/`。
    5. `user_config.json` 中 `kv_cache_store_config.backend` 设为 `memcache`。A3 的 `local_service_mode` 设为 `standalone`，`local_config_path` 仍指向 `mmc-local-inprocess.conf`。P/D connector 与 [P/D传输Connector选型](../README.md#table_Connector) 保持一致。
    6. 执行 `examples/deployer/deploy.py` 做全量部署。修改模式、`local_config_path` 或上述两份 conf 后，不要只执行 `--update_config`。prompt 不少于 128 token 才会按 chunk 入池。部署前确认推理 NodePort 不与集群里已有服务冲突。

### 验证特性

通过以下方式验证 MemCache 后端是否配置成功：

1. **检查配置加载日志**：在 Motor 启动日志中搜索 `"backend"` 相关输出，确认 backend 为 `"memcache"` 且配置项已正确加载。
2. **检查 LocalService 状态**：
   - `inprocess` 模式：vLLM 启动日志中查看 DRAM 池分配信息。
   - `standalone` 模式：在引擎 Pod 内执行 `ps -eo pid,etime,comm`。`MindIE-Motor::LocalService` 是独立进程，父进程是 NodeManager。NodeManager 日志中有 `Starting standalone LocalService`，随后有一次 `Start boostio success` 和 `bm register succeed`。每个引擎 Pod 只应出现一次成功的 boostio 初始化。
3. **验证 KV events 功能**（如已开启）：在 kv-conductor 日志中搜索 `"subscribe kv_events"` 或 `"kv_events"` 关键字，确认广播订阅成功。
4. **验证 SSD 缓存**（如已启用）：
   1. 启动日志中搜索 `storage.enabled` 和 `InitUbsIo`。预期出现 `storage.enabled=true` 与 `InitUbsIo success`，且 `ubsio.disk.path` 没有 IO error。再确认没有 `No free standalone disk slot` 和 `Invalid value for ubsio.standalone.device_count`。Pod `Ready` 只说明容器探针通过，请求到来后 worker 仍可能因为 slot 失败退出。
   2. 在引擎容器内对 `/dev/memcache-ubsio0` 执行 `ls -l`，记下主次设备号 `MAJ:MIN`。计数文件是 `/sys/dev/block/MAJ:MIN/stat`。第 3 列是读扇区，第 7 列是写扇区，每扇区 512 字节。Prefill 与 Decode 各自统计自己的块设备。
   3. 策略为 `WRITE_BACK`。不少于 128 token 的新前缀会写 SSD，同时留在 DRAM。DRAM 尚未淘汰时立刻复读，读写扇区增量都是 0。继续发送互不相同、且不少于 128 token 的前缀，直到超过 `dram.size`，再复读第一条：读扇区增加、写扇区增量为 0，说明回读来自 SSD。关闭本地 prefix cache 时，`Prefix cache hit rate` 为 0，`External prefix cache hit rate` 来自 memcache。`Mooncake transfer failed` 只说明 `connectors[0]` 的 P2P 失败。

**预期输出**：

- 日志中无 `"backend"` 配置加载错误或 `"memcache"` 初始化失败相关错误信息。
- `standalone` 模式下，每个引擎 Pod 有且仅有一个 LocalService，并且各有一次 `Start boostio success`。
- KV events 开启后，kv-conductor 正常接收并处理事件，无订阅失败或连接断开日志。
- SSD 启用后，新前缀使对应 loop 或 NVMe 的写扇区增加；DRAM 淘汰后复读使读扇区增加，且这次写扇区增量为 0。

## 常见问题

### 启用 KV events 后，kv-conductor 日志中未出现订阅成功信息

**问题描述**

按照步骤开启 KV events 广播后，kv-conductor 日志中未出现 `"subscribe kv_events success"` 或类似关键字。

**原因分析**

`pool_endpoint` 配置的广播地址与 `memcache_meta_service.py` 中的 `kv_events_endpoint` 端口不一致，或 `kv_events_model_name` / `kv_events_block_size` 与 `kv_conductor_config` 中注册的值不匹配。

**解决步骤**

1. 确认 `kv_conductor_config.pool_endpoint` 中的端口与脚本中 `kv_events_endpoint` 一致。
2. 确认 `kv_events_model_name` 和 `kv_events_block_size` 与 `kv_conductor_config` 中注册的 `model_path` / `block_size` 一致。
3. 重启 kv_store 使配置生效。

### 使用 MultiConnector 时，KV events 无法订阅

**问题描述**

`kv_transfer_config.kv_connector` 使用 `MultiConnector` 时，引擎 offload 事件无法到达 kv-conductor，两阶段匹配永远缺少引擎侧。

**原因分析**

vLLM 上游未实现 `MultiConnector.get_kv_connector_kv_cache_events()`，导致 AscendStoreConnector 收集的 KV 事件被静默丢弃。

**解决步骤**

1. 应用 `examples/deployer/patch/vllm_ascend_multi_connector_kv_events.patch` 补丁。
2. 重启服务使补丁生效。

### 把宿主机盘路径写进 ubsio.disk.path 后 SSD 不生效

**问题描述**

conf 中填写了 `/dev/nvme0n1` 或 `/dev/loop0`，容器内 LocalService 找不到设备，或者请求期间块设备写扇区不增加。

**原因分析**

`ubsio.disk.path` 是容器内路径。宿主机设备必须通过 `volumeDevices` 映射为 `/dev/memcache-ubsio0`。另外，本进程 `dram.size = 0GB` 时不会打开 SSD。A3 standalone 下这是 worker 客户端的预期状态，SSD 是否打开要看 LocalService 加载的 standalone 文件。A2 若仍使用模板中的 `device_sdma`，协议与硬件不匹配。

**解决步骤**

1. PV 使用 `volumeMode: Block`，`volumeDevices.devicePath` 与 standalone 文件中的 `ubsio.disk.path` 都设为 `/dev/memcache-ubsio0`。
2. A2 的 `ock.mmc.local_service.protocol` 设为 `device_rdma`，并由 `dram.size > 0` 的进程打开 SSD。A3 只在 `mmc-local-standalone.conf` 中把 `dram.size` 设为大于 0、`storage.enabled` 设为 `true`。
3. `deploy_mode` 使用 `multi_deployment`，避免 InferServiceSet 丢掉 `volumeDevices`。
4. 块设备准备方式见 [DRAM + SSD 多级池化 Kubernetes 配置示例](https://gitcode.com/Ascend/memcache/wiki/DRAM+%20SSD%20多级池化%20Kubernetes%20配置示例.md)。

### 开启 MemCache 池化后，长序列请求把 Decode 实例打挂，报错 shm_crash

**问题描述**

开启 MemCache 池化后，短请求可正常推理；长序列请求会把 Decode 实例打挂，日志出现 `shm_crash`。

**原因分析**

引擎 Pod 的 `/dev/shm` 默认仅为 `4Gi`。MemCache 池化在长序列场景下需要更大的共享内存，容量不足时 Decode 侧会因 shm 不足崩溃。

**解决步骤**

1. 在 `user_config.json` 的 `motor_deploy_config` 中配置 `"dshm_size": "32Gi"`（或按现场负载继续增大）。
2. 重新部署使 `dshm` emptyDir `sizeLimit`（Docker 部署时为 `--shm-size`）生效。
3. 压测验证：128 条请求（80k-1k）在 `32Gi` 下服务仍可正常运行。
