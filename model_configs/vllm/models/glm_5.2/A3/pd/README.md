# glm_5.2 · A3 · pd

来源：vLLM Ascend `GLM5.2.md` A3 四机 PD（Prefill DP1×TP16×PP2，Decode DP16×TP2，`VLLM_PP_LAYER_PARTITION=41,37`，`MooncakeConnectorV1`）。Motor 适配后在 Atlas 800I A3 上分别完成 Prefill 半边与 Decode 半边启动及引擎口推理。

场景：A3 4-pod PD。本目录与其他部署形态、硬件和上下文变体独立呈现，不可跨硬件套用参数。填写镜像与权重路径后，按仓库 deployer 指导部署。完整 1P1D 需要 4 台 A3（P 跨 2 机、D 跨 2 机）。

Motor 适配要点：

- Prefill 写 `nnodes=2` 与 `master-port`；Decode **不要**写 `nnodes`（Motor 会按 `dp_size × nnodes` 等待 NodeManager）。
- `attention_config` 写成嵌套对象，不要用点号键。
- 不要写 `api-server-count`：Motor 给 PP rank1 自动加 `--headless`，共享配置里带该字段会冲突。
- 源头文档没有 `fuse_muls_add`，本典配不写该字段。
