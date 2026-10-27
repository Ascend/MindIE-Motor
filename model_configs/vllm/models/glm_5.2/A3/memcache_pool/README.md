# glm_5.2 · A3 · memcache_pool

来源：在本仓库 `glm_5.2/A3/pd` 上按 #1268 的 A3 MemCache 处理逻辑叠加；对照 vLLM Ascend `GLM5.2.md` §5.1.4 A3（提交 `9db9336d492e9a2bc6320355e2c803a4a9449592`，代码块起始行 2914/3143）。

场景：A3 4-pod PD + MemCache 池化。本目录与其他部署形态、硬件和上下文变体独立呈现，不可跨硬件套用参数。`kv_cache_store_config` 已选择 A3 所需的 standalone LocalService；填写镜像与权重路径后，按仓库 deployer 指导部署。

Motor 适配要点（相对无池 PD）：

- `kv_transfer_config` 外层改为 `MultiConnector`：内层保留 PD 的 `MooncakeConnectorV1`（PP 分区 `41,37`），并增加 `AscendStoreConnector`（`backend=memcache`，`use_layerwise=false`）。
- Decode 打开 `enable-prefix-caching`（池化场景 Decode 不能关 prefix caching）。
- Prefill 仍写 `nnodes=2`；Decode 不写 `nnodes`。不写 `api-server-count`，不写源头文档没有的 `fuse_muls_add`。

该配置完成静态转换与字段校验。无池 PD 半边已在 A3 上启动验证；本池化变体未单独执行 NPU 启动、精度或性能验证。
