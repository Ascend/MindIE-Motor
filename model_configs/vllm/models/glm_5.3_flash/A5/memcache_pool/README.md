# glm_5.3_flash · A5 · memcache_pool

来源：原文 `GLM5.3-Flash.md` §5.4 只有 A3 MemCache 与 A2 Mooncake，没有 950DT 池化脚本。本目录在 `glm_5.3_flash/A5/pd` 上按 #1268 A5 MemCache 处理逻辑叠加，并保留 Flash 的 `use_layerwise=false`（该模型 hybrid KV 含不可 prefix-cache 的 group）。

场景：950DT 2 机 1P1D + MemCache 池化。`kv_cache_store_config` 已选择 A5 所需的 inprocess LocalService。填写镜像与权重路径后，按仓库 deployer 指导部署。

Motor 适配要点：

- 外层 `MultiConnector`：内层保留 `MooncakeConnectorV2`，并增加 `AscendStoreConnector`（`backend=memcache`，`use_layerwise=false`）。
- Decode 保持 `enable-prefix-caching`。
- 该配置完成静态转换与字段校验，未执行 NPU 启动验证。
