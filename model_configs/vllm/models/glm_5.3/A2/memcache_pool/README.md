# glm_5.3 · A2 · memcache_pool

来源：在同硬件 `pd/` 典配上叠加 Motor **KV Conductor 亲和调度**（现场标准池化），不是 vLLM Ascend 文档里的 MemCache/Mooncake Store `MultiConnector`。

对照字段：

- Coordinator：`prefill_scheduler_type=kv_cache_affinity`，`decode_scheduler_type=load_balance`
- Prefill：`kv-events-config`（zmq，`tcp://*:5557` / replay `tcp://*:6667`）
- 顶层 `kv_conductor_config`：`http_server_port=13333`，`block_size=128`
- P/D 传输仍用 `MooncakeConnectorV2`，不改拓扑
- 非 Flash 未开 Render；`block_size` 先按 Motor 默认主组粒度 `128`。若 conductor 报 `block_size_mismatch`，按主注意力组实际事件粒度改。

场景：A2 PD 分离 + KV Conductor 池化。镜像名、权重路径、`prefill_node_selector` 按实际环境填写。不要把现场 `NO_PROXY` / 业务 IP 写进典配。

该配置完成静态转换与字段校验，未在本仓库侧重复跑 NPU。
