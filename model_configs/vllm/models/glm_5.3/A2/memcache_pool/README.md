# glm_5.3 · A2 · memcache_pool

来源：vLLM Ascend `releases/v0.30.0` `GLM5.2.md` / `GLM5.3-Flash.md` §5.1.4 A2（八机 PD + MemCache）。在 `glm_5.3/A2/pd` 上按 #1268 池化逻辑叠加：`MultiConnector` = `MooncakeConnectorV2` + `AscendStoreConnector`（`use_layerwise=false`）。PP 分区 `42,36`。官方池化 `max-model-len=128000`，Prefill MTP=1 且 `no-async-scheduling`，Decode MTP=5。

场景：A2 8-pod PD + MemCache 池化。`kv_cache_store_config` 使用 A2 所需的 inprocess LocalService。填写镜像与权重路径后，按仓库 deployer 指导部署。

该配置完成静态转换与字段校验，未执行 NPU 启动、精度或性能验证。
