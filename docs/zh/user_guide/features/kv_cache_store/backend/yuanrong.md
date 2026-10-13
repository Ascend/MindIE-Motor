# Yuanrong 后端特性

## 特性介绍

Yuanrong 后端基于开源分布式缓存系统 [openYuanrong datasystem](https://gitcode.com/openeuler/yuanrong-datasystem) 实现 KV 池化：Prefill 实例将 KV Cache 写入本节点 Yuanrong Worker 管理的 DRAM 共享内存池，Decode 实例跨节点读取复用，减少重复 Prefill 计算，提升推理吞吐。

### 工作原理

Yuanrong 以内存为中心，提供近计算部署的分布式对象缓存，基于共享内存本地亲和访问、以及灵衢亲和的跨机跨设备访问，实现高性能 KV 读写。Yuanrong 采用**分布式 Master 架构**：每个 P/D Pod 内的 Worker 对等互联、各自作为元数据 Master 管理本地资源，并经外部 ETCD 完成节点注册与健康检测。Prefill 时将 KV 写入本 Pod Worker 的共享内存池，**多个推理引擎的节点共享同一池化后端**，获取数据时优先访问本地节点再访问其他节点实现**本地亲和访问**；每个 Worker 在数据存入/移除时通过事件广播 KV 元数据块事件，Motor 的 kv-conductor 基于 `backend_id` 计算 KV 亲和度，实现**请求级 KV Cache 亲和性调度**，将请求优先路由到已缓存前缀的节点。此外，Yuanrong 支持将**本地 NVMe SSD 作为第三级缓存**（HBM → DRAM → SSD），将冷 KV Cache 自动下沉到 SSD，仅保留热数据在内存中；同时还集成了 UCM，**通过高性能分布式存储提供第四级缓存**（HBM → DRAM → SSD → 存储），进一步提高 KV Cache 容量和推理性能。

### 核心功能

Yuanrong 提供了异构数据对象抽象，支持 key/value 读写，并构建了由 HBM、DRAM、SSD 组成的分级缓存体系，通过以下关键技术实现高性能、大容量、易用的分布式数据访问：

- **D2D 高效访问**：支持卡间 HBM 数据直接通信，自动管理动态链路建立、P2P 通信算子调度与负载均衡，降低跨设备传输延迟，简化多卡编程；
- **H2D/D2H 高性能访问**：采用内存大页聚合与批量 SDMA/RDMA 操作，显著提升 KB 级小数据的传输效率，实现单卡高达 20GB/s 的传输吞吐；
- **H2H 零冗余拷贝**：通过共享内存与通信内存的复用机制，实现进程间数据零冗余拷贝，一次 URMA Write 单边写操作，传输速率可达 48GB/s；
- **跨节点 H2D 直通访问**：基于 NPU NIC 支持主机内存到设备内存的直接访问，避免数据经由 HBM 中转，降低 KV Cache 等场景的跨机访问延迟；
- **SSD 容量扩展与分级管理**：将数据自动溢出至 SSD，并智能调度数据在 DRAM 与 SSD 间的迁移与淘汰，实现存储容量与访问性能的平衡。

### 约束与限制

**表 1** <a id="table001"></a>硬件约束

| 硬件 | 依赖 | 说明 |
|------|------|------|
| Atlas 800I A3超节点服务器 | <ul><li>HDK >= 25.2 </li><li>CANN >= 8.5.0</li></ul> | Worker 启动配置与 A2 使用同一格式；引擎通信环境需按实际链路（RoCE/HCCS）及所用 vllm-ascend 版本配置，参见 [KV Pool 部署指南](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/kv_pool.html)。 |
| Atlas 800I A2推理服务器 | <ul><li>HDK >= 25.2</li><li>CANN >= 8.5.0</li></ul> | 无需额外环境变量，通用必配项即可。 |

## 特性使用

### 环境准备

- 已使用 MindIE Motor 完成 PD 分离基础推理服务部署，服务运行正常。参考 [PD 分离服务部署](../../../deployment/k8s/pd_disaggregation_deployment.md)。
- 已部署外部 ETCD，并保证其客户端地址在 P/D Pod 容器网络内可达。ETCD 部署方式可参考 [Yuanrong 部署指南](https://pages.openeuler.openatom.cn/openyuanrong-datasystem/docs/zh-cn/latest/deployment/deploy.html#etcd)。

   ```bash
   # 确认连通性
   etcdctl --endpoints "http://<etcd_ip>:<etcd_port>" endpoint health
   ```

### 使用样例

以下步骤展示如何配置和使用 Yuanrong 后端。

**操作步骤**

1. 安装 Yuanrong。

   - 方式一：启动服务时安装。

   从 [Yuanrong releases](https://gitcode.com/openeuler/yuanrong-datasystem/releases) 获取 Yuanrong wheel，放置于容器内可访问的目录，例如 `/mnt/yuanrong/`。

   修改 `boot.sh`，添加 `pip install xxx.whl` 命令，在启动服务时安装：

   ```sh
   ...
   case "$ROLE" in
       "SINGLE_CONTAINER")
           source "$SCRIPT_DIR/all_combine_in_single_container.sh"
           ;;
       "encode"|"prefill"|"decode"|"union")
           # 安装 Yuanrong whl 包
           pip install /mnt/yuanrong/openyuanrong_datasystem-0.9.2-cp312-cp312-manylinux_2_35_aarch64.whl --force-reinstall -v

           source "$SCRIPT_DIR/engine.sh"
           ;;
       "controller")
           source "$SCRIPT_DIR/controller.sh"
           ;;
   ...
   ```

   - 方式二：构建 Motor 镜像时通过 `pip install` 预装 Yuanrong whl 包。

2. 在 `user_config.json` 中配置 P/D 实例的 `kv_transfer_config`。

   `connectors[0]` 为 P/D 实时传输 Connector，`connectors[1]` 为池化后端 Store Connector（`AscendStoreConnector`，`backend` 为 `"yuanrong"`）。

   - P 实例（motor_engine_prefill_config）：

     ```json
     "motor_engine_prefill_config": {
       "engine_type": "vllm",
       "engine_config": {
         "...": "...",
         "kv_transfer_config": {
           "kv_connector": "MultiConnector",
           "kv_role": "kv_producer",
           "kv_connector_extra_config": {
             "connectors": [
               {
                 "kv_connector": "MooncakeConnectorV1",
                 "kv_role": "kv_producer",
                 "kv_port": "30001"
               },
               {
                 "kv_connector": "AscendStoreConnector",
                 "kv_role": "kv_producer",
                 "kv_connector_extra_config": {
                   "backend": "yuanrong"
                 }
               }
             ]
           }
         }
       }
     }
     ```

   - D 实例（motor_engine_decode_config）：

     ```json
     "motor_engine_decode_config": {
       "engine_type": "vllm",
       "engine_config": {
         "...": "...",
         "kv_transfer_config": {
           "kv_connector": "MultiConnector",
           "kv_role": "kv_consumer",
           "kv_connector_extra_config": {
             "connectors": [
               {
                 "kv_connector": "MooncakeConnectorV1",
                 "kv_role": "kv_consumer",
                 "kv_port": "30002"
               },
               {
                 "kv_connector": "AscendStoreConnector",
                 "kv_role": "kv_consumer",
                 "kv_connector_extra_config": {
                   "backend": "yuanrong"
                 }
               }
             ]
           }
         }
       }
     }
     ```

   **表 2** kv_transfer_config 关键参数说明

   | 字段 | 说明 |
   |------|------|
   | kv_connector | 需配置为 `"MultiConnector"`，用于组合传输 Connector 与 Store Connector |
   | kv_role | P 实例配置为 `"kv_producer"`，D 实例配置为 `"kv_consumer"` |
   | connectors[0] | P/D 实时传输 Connector，负责 P/D 实例间的 KV Cache 传输 |
   | connectors[1] | 池化后端 Store Connector，负责读写共享 KV Pool |
   | connectors[1].backend | 需配置为 `"yuanrong"`，且与全局 `kv_cache_store_config.backend` 保持一致 |

3. 配置 kv_cache_store_config（全局配置）。

   `kv_cache_store_config` 为 KV 池化全局配置，由 P/D 实例共享。Worker 启动配置示例如下：

   ```json
   "kv_cache_store_config": {
     "backend": "yuanrong",
     "etcd_address": "xx.xx.xx.110:2379",
     "worker_port": 18481,
     "shared_memory_size_mb": 51200,
     "worker_launcher": {
       "start_args": [
         {"interleave": "0-7"},
         {"datasystem_home_dir": "/home/yuanrong"},
         {"timeout": 90}
       ],
       "worker_args": [
         {"node_timeout_s": 20},
         {"node_dead_timeout_s": 30},
         {"log_only_write_info_file": false},
         {"arena_per_tenant": 1},
         {"enable_huge_tlb": false}
       ]
     }
   }
   ```

   NodeManager 会将该配置组装为：

   ```bash
   dscli start \
     --interleave 0-7 \
     --datasystem_home_dir /home/yuanrong \
     --timeout 90 \
     -w \
     --worker_address ${POD_IP}:18481 \
     --etcd_address xx.xx.xx.110:2379 \
     --shared_memory_size_mb 51200 \
     --node_timeout_s 20 \
     --node_dead_timeout_s 30 \
     --log_only_write_info_file false \
     --arena_per_tenant 1 \
     --enable_huge_tlb false
   ```

   **表 3** 配置参数说明

   | 配置项 | 类型 | 取值范围 | 必填/选填 | 默认值 | 说明 |
   |--------|-----|----------|-----------|--------|------|
   | backend | string | `"yuanrong"` | 使用 Yuanrong 后端时必填 | — | 必须显式配置为 `"yuanrong"`，以激活 Yuanrong Worker 服务 |
   | etcd_address | string | `host:port` | 选填 | `""` | ETCD 地址；可留空或省略，运行时优先使用环境变量 `ETCD_ADDRESS`，两处都为空时 Worker 启动失败 |
   | worker_port | int | 1~65535 | 必填 | — | Worker 监听端口，需在 Pod 内未被占用 |
   | shared_memory_size_mb | int | 正整数 | 必填 | — | Worker 共享内存池（DRAM）大小上限，单位 MB |
   | worker_launcher.start_args | array[object] | — | 选填 | `[]` | 按序插入在 `dscli start` 与 `-w` 之间的 dscli 自身参数（NUMA 策略、home 目录、超时等，如 `interleave`、`d`、`t`） |
   | worker_launcher.worker_args | array[object] | — | 选填 | `[]` | 按序插入在 Motor 管理参数之后的 datasystem_worker 参数（可靠性、日志、性能等，如 `node_timeout_s`、`enable_huge_tlb`） |

   >[!NOTE] 说明
   >- 每个数组元素只能包含一个参数名和值；数组顺序即命令行顺序，同名参数可以重复。
   >- 参数名不带 `-`：单字符名自动生成短参数（如 `{"d": "/home/yuanrong"}` → `-d /home/yuanrong`），其他名称自动生成长参数。
   >- 字符串、数字和布尔值会转换成单个 argv 值；布尔值使用小写 `true`/`false`。
   >- `null` 表示无值开关；数组值表示一个参数后跟多个值。
   >- `w`、`worker_address`、`etcd_address`、`shared_memory_size_mb` 是 Motor 保留参数，配置后会在部署校验或 NodeManager 解析阶段报错。
   >- 启动参数必须与容器中安装的 Yuanrong 版本匹配，可通过 `dscli start --help` 核对；完整参数说明参见 [dscli 命令行参数](https://gitcode.com/openeuler/yuanrong-datasystem/blob/master/docs/source_zh_cn/deployment/dscli.md)。

   使用样例中出现的 Yuanrong 原生参数说明如下：

   **表 4** 使用样例中的 Yuanrong 原生参数

   | 参数 | 配置位置 | 类型 | 默认值 | 说明 |
   |------|----------|------|--------|------|
   | interleave | start_args | string | 未配置 | NUMA 内存交错策略，取值遵循 numactl 节点语法（`0`、`0,1`、`0-7`、`all`）；大共享内存场景建议跨全部 NUMA 节点交错分配 |
   | datasystem_home_dir（短参数 `d`） | start_args | string | dscli 当前目录 | 基础路径，配置中的相对路径以该目录转换为绝对路径；当前目录路径过长可能导致 Worker 启动失败，建议显式指定较短目录 |
   | timeout（短参数 `t`） | start_args | int | 90 | `dscli` 等待服务就绪的最大时间（秒） |
   | node_timeout_s | worker_args | int | 60 | 节点心跳超时时间（秒），须不小于 1 |
   | node_dead_timeout_s | worker_args | int | 300 | 节点死亡判定窗口（秒），超时未恢复心跳则标记为死亡节点；必须大于 `node_timeout_s`，否则 Worker 启动校验失败 |
   | log_only_write_info_file | worker_args | bool | true | 是否只生成 INFO 日志文件（INFO 文件始终包含全部级别日志）；设为 `false` 时额外生成 WARNING/ERROR 日志文件 |
   | arena_per_tenant | worker_args | int | 16 | 每个租户的共享内存分配器数量，取值范围 [1, 32]；多个分配器可提升首次分配性能，但每个分配器额外占用一个 fd |
   | enable_huge_tlb | worker_args | bool | false | 共享内存大页开关，开启后共享内存按 2 MiB 大页映射，减少页表开销；开启约束与前置条件见[调优建议](#调优建议) |

4. 部署服务。

   ```bash
   cd examples/deployer

   # 方式一：指定配置目录（推荐）
   python deploy.py --config_dir ../infer_engines/vllm

   # 方式二：单独指定配置文件
   python deploy.py --user_config_path ../infer_engines/vllm/user_config.json --env_config_path ../infer_engines/vllm/env.json
   ```

   >[!NOTE] 说明
   >上述命令以 `../infer_engines/vllm` 为配置目录示例。请先按上文修改该目录中的 `user_config.json`，并准备 `env.json`；使用其他目录时，将两条命令中的 `../infer_engines/vllm` 统一替换为实际配置目录。

   **部署完成后**：

   - 生成的部署资源中不包含 kv-store 角色/Pod（Yuanrong 无中央 kv-store），ETCD 由外部提供。
   - 配置了 `etcd_address` 时，引擎 Pod 被注入 `ETCD_ADDRESS` 环境变量。
   - 各 P/D Pod 内 NodeManager 在引擎启动前拉起 Worker，日志中依次出现 `Starting Yuanrong Worker: dscli start ...` 与 `Yuanrong Worker is ready at <POD_IP>:18481`。

### 验证特性

通过以下方式验证 Yuanrong 后端是否配置成功：

1. 检查 NodeManager 启动日志。

   ```bash
   kubectl logs <prefill/decode-pod-name> -n <namespace> | grep -i "Yuanrong Worker"
   ```

   预期输出：

   ```text
   Starting Yuanrong Worker: dscli start ...
   Yuanrong Worker is ready at <POD_IP>:18481
   ```

   出现就绪日志且无 `is not ready yet ... after prepare` 告警，说明 Worker 在引擎启动前就绪。

2. 确认 Pod 内 Worker 进程与端口。

   ```bash
   kubectl exec -it <prefill/decode-pod-name> -n <namespace> -- bash -c "ps -ef | grep datasystem | grep -v grep; ss -tlnp | grep <worker_port>"
   ```

   预期输出：存在 `datasystem_worker` 进程，且 `<POD_IP>:<worker_port>` 处于监听状态。

3. 查询 ETCD 中的 Worker 注册与健康状态。

   ```bash
   kubectl exec -it <prefill/decode-pod-name> -n <namespace> -- dscli query cluster --etcd_address <etcd_address>
   ```

   预期输出 JSON：`status` 为 `OK`，`nodes` 数组包含每个 P/D Pod 的 `worker_address`，`health` 为 `HEALTHY`、`state` 为 `ACTIVE`。

4. 发送推理请求验证 KV 写入与复用。

   ```bash
   curl -X POST http://<服务IP>:<服务端口>/v1/chat/completions \
        -H "Content-Type: application/json" \
        -d '{"model":"<model_name>","messages":[{"role":"user","content":"<相同的长前缀和问题>"}]}'
   ```

   连续发送两次包含相同长前缀的请求，均应返回 HTTP 200 及正常推理响应；同时检查 Yuanrong 日志（客户端日志目录 `<Motor 日志根目录>/yuanrong/<ROLE>/client` 及 Worker 日志）中出现 KV 写入与读取记录。仅请求成功不能证明 KV 池化已经生效。

## 调优建议

| 场景 | 关键参数 | 建议取值 | 说明 |
|------|----------|----------|------|
| 通用 | `shared_memory_size_mb` | 模型 KV Cache 预估大小的 1.5~2 倍 | Worker DRAM 池上限；过小导致缓存频繁淘汰，过大挤占容器内存甚至被 OOM 终止 |
| 多 NUMA 节点 + 大共享内存 | `start_args` 中的 `interleave` | 跨全部 NUMA 节点（如 `"0-7"` 或 `"all"`） | 内存交错分配，避免大块共享内存集中占用单个 NUMA 节点导致内存不均；取值遵循 numactl 节点语法 |
| 大共享内存池 | `worker_args` 中的 `enable_huge_tlb` | `true` | 开启共享内存大页：共享内存按 2 MiB 大页（hugetlbfs）映射，减少页表开销、提升内存访问性能。前提是节点已预留足够大页（`/proc/sys/vm/nr_hugepages`；配合 `interleave` 交错分配时，参与交错的各 NUMA 节点均需预留），预留不足时 Worker 启动失败。 |
| Worker 启动慢 | `start_args` 中的 `timeout`（短参数 `t`） | 按实际启动耗时增大，例如 120 | `dscli` 等待服务就绪的最大时间（秒），默认 90；该值不改变 NodeManager 的 30 秒就绪等待时间 |
| 快速故障感知 | `worker_args` 中的 `node_timeout_s` / `node_dead_timeout_s` | 按需调小 | 节点超时与死亡判定窗口；`node_dead_timeout_s` 必须大于 `node_timeout_s` |
| 排障 | `worker_args` 中的 `log_only_write_info_file` | `false` | 额外生成 WARNING/ERROR 级别日志文件，便于定位问题 |
| 长序列/大并发 | 容器内存与共享内存 | 覆盖 `shared_memory_size_mb` 与引擎、Worker 开销 | K8s 通过 `motor_deploy_config.dshm_size` 调整，Docker 部署通过 `--shm-size` 调整 |

**参数联动说明**：

- `shared_memory_size_mb` 与容器内存限制联动：取值接近容器可用内存时，Worker 可能申请共享内存失败或被 OOM 终止，需同步调整容器内存。
- `node_dead_timeout_s` 必须大于 `node_timeout_s`，否则 Worker 拒绝启动。
- 开启 `enable_huge_tlb` 后，Yuanrong 会强制 `arena_per_tenant=1`、`enable_fallocate=false`（显式配置会被覆盖），共享内存映射大小按 2 MiB 向上取整；内核不支持 `MFD_HUGETLB` 时该开关不生效，Worker 记录 WARNING 后按普通页运行。
- Motor 在引擎启动前最多等待 Worker 就绪 30 秒：超时后 NodeManager 记录 `is not ready yet ... after prepare` 告警，引擎首次连接可能失败。增大 `start_args` 中的 `timeout`（短参数 `t`）仅延长 dscli 的等待时间，不能延长 Motor 的就绪等待时间；应结合 Worker 启动日志检查 ETCD 连通性、启动参数和容器资源。

>[!NOTE] 说明
>以上配置仅作参考。实际最优配置取决于模型规模、并发量与节点资源，建议根据实际场景测试调优。Yuanrong 完整参数说明参考 [Worker 配置项](https://gitcode.com/openeuler/yuanrong-datasystem/blob/master/docs/source_zh_cn/deployment/dscli.md)。

## 常见问题

### Pod 中没有 Yuanrong Worker 进程

**问题描述**

部署后 Pod 内找不到 `datasystem_worker` 进程，引擎侧 Yuanrong 客户端初始化失败。

**原因分析**

镜像未安装 Yuanrong 或 `dscli` 不在 `PATH` 中；或 `start_args` / `worker_args` 中的参数与镜像内安装的 Yuanrong 版本不匹配，`dscli` 报错退出。

**解决步骤**

1. 查看 NodeManager 日志中 `Starting Yuanrong Worker` 及紧随其后的 `dscli` 错误输出：

   ```bash
   kubectl logs <prefill/decode-pod-name> -n <namespace> | grep -A 5 "Starting Yuanrong Worker"
   ```

2. 确认镜像内可执行 `dscli`：

   ```bash
   kubectl exec -it <prefill/decode-pod-name> -n <namespace> -- dscli --version
   ```

3. 通过 `dscli start --help` 核对 `start_args` / `worker_args` 中各参数是否受当前版本支持，删除不支持的参数后重新部署。

### 提示 ETCD 地址缺失

**问题描述**

NodeManager 日志或部署告警提示 ETCD 地址缺失，Worker 启动失败。

**原因分析**

容器环境变量 `ETCD_ADDRESS` 与 `kv_cache_store_config.etcd_address` 均为空，Worker 无法向 ETCD 注册。

**解决步骤**

1. 在 `kv_cache_store_config.etcd_address` 中配置地址后重新部署，deployer 会将其注入引擎 Pod 的 `ETCD_ADDRESS`。
2. 或在容器启动环境中注入 `ETCD_ADDRESS`；两者同时存在时优先使用环境变量。
3. 确认 ETCD 在容器网络内可达：

   ```bash
   kubectl exec -it <prefill/decode-pod-name> -n <namespace> -- dscli query cluster --etcd_address <etcd_address>
   ```

### 引擎连接 Worker 地址失败

**问题描述**

引擎日志报连接 `${POD_IP}:18481` 失败或超时。

**原因分析**

Worker 未成功监听端口（启动失败或仍在初始化），或 `worker_port` 被 Pod 内其他进程占用。

**解决步骤**

1. 确认 NodeManager 日志已出现 `Yuanrong Worker is ready at <POD_IP>:<worker_port>`。
2. 在 Pod 内确认端口监听状态：

   ```bash
   kubectl exec -it <prefill/decode-pod-name> -n <namespace> -- ss -tlnp | grep <worker_port>
   ```

3. 确认 Worker 命令包含 Motor 自动生成的 `--worker_address`（查看 NodeManager 日志中 `Starting Yuanrong Worker` 打印的完整命令），且该地址在 Pod 内监听。
4. 若日志出现 `is not ready yet ... after prepare`，参见[调优建议](#调优建议)中两层就绪等待时间的说明。

### 部署校验提示保留参数错误

**问题描述**

执行 `deploy.py` 或 NodeManager 解析配置时报错，提示 `contains Motor-managed argument` 或 `contains unsupported fields`。

**原因分析**

`start_args` / `worker_args` 中配置了 Motor 保留参数（`w`、`worker_address`、`etcd_address`、`shared_memory_size_mb`），或 `worker_launcher` / `kv_cache_store_config` 中包含不支持的字段（如 `executable`、顶层 `worker_args`）。

**解决步骤**

1. 从 `start_args` / `worker_args` 中删除保留参数；这些参数由 Motor 放在正确位置，地址类信息通过 `kv_cache_store_config` 顶层字段（`etcd_address`、`worker_port`、`shared_memory_size_mb`）配置。
2. 确认 `worker_launcher` 仅包含 `start_args` 与 `worker_args` 两个字段。
3. 修改后重新部署。

### Worker 反复重启或被 OOM 终止

**问题描述**

NodeManager 日志反复出现 `Yuanrong Worker died` / `Failed to restart Yuanrong Worker`，或 Worker 进程被 OOMKilled。

**原因分析**

`shared_memory_size_mb` 接近或超过容器可用内存，Worker 申请共享内存失败或被系统终止；NodeManager 按指数退避（10 秒起步、最长 5 分钟）持续重试拉起。

**解决步骤**

1. 调低 `shared_memory_size_mb`，或增大容器内存（K8s 调整 `motor_deploy_config.dshm_size`，Docker 部署增大 `--shm-size`）后重新部署。
2. 查看被 OOMKilled 的 Worker 日志确认根因：`kubectl describe pod <pod-name> -n <namespace>` 中 `Last State` 显示 `OOMKilled` 即为内存不足。
