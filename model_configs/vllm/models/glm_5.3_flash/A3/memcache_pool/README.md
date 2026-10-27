# glm_5.3_flash · A3 · memcache_pool

来源：在本仓库 `glm_5.3_flash/A3/pd` 上按 #1268 A3 MemCache 处理逻辑叠加；对照 vLLM Ascend `GLM5.3-Flash.md` §5.4 A3（提交 `9db9336d492e9a2bc6320355e2c803a4a9449592`，代码块起始行 1448/1476）。

场景：A3 1P1D（Prefill DP2×TP8，Decode DP4×TP4）+ MemCache 池化。`kv_cache_store_config` 已选择 A3 所需的 standalone LocalService。填写镜像与权重路径后，按仓库 deployer 指导部署。

Motor 适配要点：

- 外层 `MultiConnector`：内层保留 `MooncakeConnectorV2`，并增加 `AscendStoreConnector`（`backend=memcache`，`use_layerwise=false`，`memcache_dp_init_barrier=true`）。
- Decode 打开 `enable-prefix-caching`。Decode 的 `max-num-seqs` / `max-model-len` 沿用已验证无池 PD（`8` / `133120`），避免官方 1M 在验证环境触发锁页 `207001`。
- 该配置完成静态转换与字段校验；无池 1P1D 已在 A3 上启动验证，本池化变体未单独执行 NPU 启动。
