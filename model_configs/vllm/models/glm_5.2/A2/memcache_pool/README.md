# glm_5.2 · A2 · memcache_pool

来源：在本仓库 `glm_5.2/A2/pd` 上按 #1268 的 MemCache 处理逻辑叠加；对照 vLLM Ascend `GLM5.2.md` §5.1.4 A2（提交 `9db9336d492e9a2bc6320355e2c803a4a9449592`，代码块起始行 3536/3664）。PP 分区按 GLM-5.2 使用 `41,37`。

场景：A2 8-pod PD + MemCache 池化。`kv_cache_store_config` 已选择 A2 所需的 inprocess LocalService（Motor 对 A2 会强制 inprocess）。填写镜像与权重路径后，按仓库 deployer 指导部署。

Motor 适配要点：

- 外层 `MultiConnector`：内层保留 `MooncakeConnectorV2`，并增加 `AscendStoreConnector`（`backend=memcache`，`use_layerwise=false`）。
- Decode 打开 `enable-prefix-caching`。Prefill 按原文使用 `no-async-scheduling`。
- Prefill 仍写 `nnodes=4`；Decode 不写 `nnodes`。不写 `api-server-count`。

该配置完成静态转换与字段校验，未执行 NPU 启动、精度或性能验证。
