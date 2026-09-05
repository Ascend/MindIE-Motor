# Slurm Deployer

- [环境准备](../../docs/zh/user_guide/slurm/environment_preparation.md)
- [服务部署](../../docs/zh/user_guide/slurm/service_deployment.md)

## 部署前必改

1. 将现场准备好的 `user_config.json`、`env.json` **拷贝到** `conf/` 下（仓库不内置这两个文件；可参考 `examples/features/config_sample.json` 或 `examples/infer_engines/` 下通用配置示例）。也可以在 `start` 命令后传入其他配置目录。
2. 在 `deploy.sh` 中设置以下变量，勿提交真实集群地址；`PARTITION` 须与 `slurm.conf` 中的 `PartitionName` 一致。临时测试时，也可以使用命令前缀传入变量，例如 `PARTITION=my-partition bash deploy.sh start`：

| 变量 | 说明 |
|------|------|
| `COORDINATOR_SERVICE` | Coordinator 节点地址（主机名、IPv4 或 IPv6） |
| `COORDINATOR_INFER_SERVICE` | 可选，Coordinator 推理服务地址；未设置时默认使用 `COORDINATOR_SERVICE` |
| `COORDINATOR_OBS_SERVICE` | 可选，Coordinator 观测服务地址；未设置时默认使用 `COORDINATOR_SERVICE` |
| `CONTROLLER_SERVICE` | Controller 节点地址（主机名、IPv4 或 IPv6） |
| `KVS_MASTER_SERVICE` | 可选，kv_store 节点地址（主机名、IPv4 或 IPv6）；启用 KV Store 或 KV Conductor 时必须设置 |
| `KV_CONDUCTOR_SERVICE` | 可选，KV Conductor 独立地址；启用 KV Conductor 时必须设置 |
| `KV_CONDUCTOR_NODE` | 可选，KV Conductor 对应的 Slurm `NodeName`；服务地址无法解析时必须设置 |
| `MF_STORE_SERVICE` | mf_store 节点地址（主机名、IPv4 或 IPv6；`engine_type=sglang` 时使用） |
| `PARTITION` | 提交作业使用的 Slurm 分区名 |

如果服务地址无法通过 DNS 或 `/etc/hosts` 解析为 Slurm 节点名，可额外设置对应的 `*_NODE` 变量，直接指定 `slurm.conf` 中的 `NodeName`。这些变量只用于 `sbatch -w` 节点绑定；KV Conductor 对应 `KV_CONDUCTOR_NODE`。

Slurm 使用统一的多角色启动流程，不读取 K8s 的 `deploy_mode` 字段。

```bash
bash deploy.sh start [配置目录]
# 未指定时使用 conf/
bash deploy.sh start
# 使用自定义目录
bash deploy.sh start /path/to/conf
```

如果启用 Memcache KV 池化，直接将需要覆盖的 LocalService conf 放入 `conf/`，执行 `start` 时会在每个容器内自动生成运行配置。文件名应保持为：

```text
conf/kv_store_backends.memcache.mmc-local-inprocess.conf
conf/kv_store_backends.memcache.mmc-local-standalone.conf
```

`user_config.json` 中的 `kv_cache_store_config.backend` 和引擎侧
`AscendStoreConnector` 对象内部的 `kv_connector_extra_config.backend` 都应设置为 `memcache`。其中 `backend` 只选择后端，
DRAM 池大小、通信协议和 SSD/UBSIO 等参数仍需在对应的 `mmc-local-*.conf` 中配置。
容器内的 `/configmap` 使用临时文件系统，不会在宿主机生成 `conf/configmap` 目录。

启动后，脚本会将本次提交的 Job ID 保存到 `logs/.slurm_job_ids`。执行 `bash deploy.sh stop` 时只取消这些 Job ID，不需要再指定 `PARTITION`，也不会影响同一分区中的其他作业。
