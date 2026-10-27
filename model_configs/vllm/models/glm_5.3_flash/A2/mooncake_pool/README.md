# glm_5.3_flash · A2 · mooncake_pool

来源：在本仓库 `glm_5.3_flash/A2/pd` 上按 #1268 A2 Mooncake pool 处理逻辑叠加；对照 vLLM Ascend `GLM5.3-Flash.md` §5.4 A2（提交 `9db9336d492e9a2bc6320355e2c803a4a9449592`，代码块起始行 1646/1682）。A2 无 HCCS 织物，池化走 Mooncake / `P2PHANDSHAKE`，不是 MemCache。

场景：A2 4 机 1P1D + Mooncake KV 池。`kv_cache_store_config` 已选择 Mooncake master（端口 50088，`global_segment_size=8GB`）。填写镜像与权重路径后，按仓库 deployer 指导部署。

Motor 适配要点：

- 外层 `MultiConnector`：内层保留 `MooncakeConnectorV2`，并增加 `AscendStoreConnector`（`backend=mooncake`，`use_layerwise=false`）。
- Decode 保持 `enable-prefix-caching`。`lookup_rpc_port` 由 Motor 分配，配置里写 `"0"`。
- 该配置完成静态转换与字段校验，未执行 NPU 启动验证。
