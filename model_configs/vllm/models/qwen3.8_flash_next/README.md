# Qwen3.8-Flash-Next 典配

配置参考 [Qwen3.8-Flash-Next 模型教程](https://docs.vllm.ai/projects/ascend/zh-cn/v0.32.0rc/tutorials/models/Qwen3.8-Flash-Next.html)，按 Motor 部署约定适配服务地址、端口和 LocalService 模式。

本目录提供 A2/A3/A5 的 Qwen3.8-Flash-Next 模型典配，每个机型包含 PD 分离部署使用的 `user_config.json` 和 `env.json`。

| 目录 | 权重（ModelScope ID） | Prefill / Decode（TP×PP×DP） | P / D 卡数 | 最大上下文 / 序列数 / 批 token 数 |
| --- | --- | --- | --- | --- |
| A2 | `Eco-Tech/Qwen3.8-Flash-Next-w4a8` | 4×2×1 / 2×1×4 | 8 / 8 | 141312 / 32 / 8192 |
| A3 | `Eco-Tech/Qwen3.8-Flash-Next-w4a8` | 2×2×2 / 2×1×4 | 8 / 8 | 262144 / 32 / 8192 |
| A5（Ascend 950DT） | `Eco-Tech/Qwen3.8-Flash-Next-w4a8-mxfp` | 2×1×2 / 2×1×2 | 4 / 4 | 262144 / 64 / 4096 |

A3 使用教程中的 128K/1K 多前缀部署拓扑。A5 的工具调用解析器为 `qwen3_coder`，P/D 的 `HCCL_BUFFSIZE` 均为 512。三个机型均保留教程的 Engram CPU offload、MTP 和图模式设置。

## 运行时要求

- MemCache 与 MemFabric 均要求兼容的 `>=1.3.0` 版本，资源池内的软件版本需一致。
- 在镜像中初始化 CANN/ATB、自定义算子以及 Mooncake、MemCache、MemFabric 和 Python 的动态库路径，确认 `import vllm, vllm_ascend, mooncake, memcache_hybrid, memfabric_hybrid` 成功。`env.json` 不固化依赖包的安装位置。
- 教程的 A2 池化环境使用 HDK 25.5.1 或以上版本；A3 HCCS 需要灵衢计算网络 1.5 或以上版本。A5 按教程准备 URMA 通信环境，在服务 shell 设置 `ulimit -l unlimited`、`ulimit -s 65536`。
- 启动前清理教程列出的冲突环境变量：A2/A3 不设置 `VLLM_PP_LAYER_PARTITION`；A2 清理 Fabric 相关变量，A3 清理 `HCCL_INTRA_ROCE_ENABLE`、`ASCEND_GLOBAL_RESOURCE_CONFIG`、`ASCEND_LOCAL_COMM_RES`；A5 清理 `HCCL_INTRA_ROCE_ENABLE`、`ASCEND_ENABLE_USE_FABRIC_MEM`、`ASCEND_GLOBAL_RESOURCE_CONFIG`，并保留本典配的 `ASCEND_LOCAL_COMM_RES={"version":"1.3"}`。代理绕过规则按实际服务网络设置。

## Motor 部署适配

1. 下载表中权重，填写 `motor_deploy_config.image_name`、`weight_mount_path` 和 P/D 引擎的 `model`。P/D 必须加载相同权重。
2. P/D 使用 `MultiConnector`，组合 `MooncakeConnectorV2` 传输和 `AscendStoreConnector` 缓存，`use_layerwise=false`。`kv_cache_store_config` 显式启用 `memcache`，按 Motor 的硬件部署约定：A2/A5 设置 `local_service_mode=inprocess`，A3 设置 `local_service_mode=standalone`，由 NodeManager 拉起独立 LocalService。A3 的 `local_config_path` 仍指向 `mmc-local-inprocess.conf`，独立 LocalService 子进程会切换到同目录的 `mmc-local-standalone.conf`。
3. MetaService 由 Motor 拉起，使用 Motor 的 RPC/ConfigStore 端口 50088/50089，LocalService 配置中的 `mindie-motor-kvs-master` 由部署启动脚本替换为实际服务地址。无需再启动教程中的手工 MetaService 或 Proxy。MetaService 的淘汰策略由 Motor 的 MetaService 启动配置管理，教程的 `mmc-meta.conf` 不会自动生效。
4. LocalService 使用 deployer 的 [inprocess 模板](../../../../examples/deployer/startup/roles/kv_store_backends/memcache/mmc-local-inprocess.conf) 和 [standalone 模板](../../../../examples/deployer/startup/roles/kv_store_backends/memcache/mmc-local-standalone.conf)。A2/A5 在 inprocess 模板中配置协议及实际 DRAM 容量；A3 的 inprocess 模板作为 worker 客户端，保持 `dram.size=0GB`，在 standalone 模板中设置 `protocol=device_sdma` 和实际 DRAM 容量。Motor 会将两份模板同步到容器内 `/usr/local/Ascend/Motor/conf/`。
5. 本典配关闭 SSD，无需准备缓存块设备、块设备 PVC 或 `volumeDevices`。MemCache 部署方式参见 [Motor MemCache 指导](../../../../docs/zh/user_guide/features/inference_scheduling/kv_cache_store/memcache.md)。

服务端口、设备可见性、主机 IP、通信网卡和 DP RPC 由 Motor 管理。保留了 Mooncake 子 connector 的 `prefill/decode` 并行声明；调整切分时需同步修改引擎、connector 和部署卡数。

## DRAM 参数

| 机型 | 模式 / DRAM 配置文件 | protocol | DRAM 配置 |
| --- | --- | --- | --- |
| A2 | inprocess / mmc-local-inprocess.conf | device_rdma | 按每个 worker 预算，10GB 为示例起点 |
| A3 | standalone / mmc-local-standalone.conf | device_sdma | 按独立 LocalService 的容量需求及主机预算设置 |
| A5 | inprocess / mmc-local-inprocess.conf | device_urma | 按每个 worker 预算，10GB 为示例起点 |

上述 DRAM 容量需在 deployer 模板中设置，未写入 `user_config.json` 或 `env.json`。A2 教程未给出固定容量；A3 在此采用 Motor 的 standalone 模式，不再使用教程的每个 worker 10GB 内存分配方式。A3 的 worker 客户端配置保持 `dram.size=0GB`。

模板中的 `ock.mmc.local_service.storage.enabled` 保持 `false`，`ubsio.disk.path` 保持为空，以关闭 SSD。

A2/A5 每个 worker 的 DRAM 缓存、A3 独立 LocalService 的 DRAM 缓存，均需与 Engram、vLLM、操作系统和 `/dev/shm` 一并纳入主机内存预算，不能将 `dshm_size` 当作总主机内存。A2/A3 的 `dshm_size=128Gi`，A5 为 `500Gi`。

## 验证

本典配完成了静态检查，尚未执行 A2/A3/A5 硬件推理。部署后需确认 P/D 拓扑、MemCache DRAM 初始化成功，再执行重复前缀和多前缀请求，验证 DRAM 缓存复用；API 就绪不能证明缓存命中生效。修改 LocalService 模式或 conf 后需重新部署引擎，不能仅使用 `--update_config`。
