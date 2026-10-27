# glm_5.3_flash · A3 · pd

来源：vLLM Ascend `GLM5.3-Flash.md`（提交 `9db9336d492e9a2bc6320355e2c803a4a9449592`，§5.3 A3，Prefill 代码块起始行 734 / Decode 起始行 816）。Motor 适配后在 Atlas 800I A3 上完成 1P1D 启动与 Coordinator 推理。

场景：A3 1P1D PD（Prefill DP2×TP8，Decode DP4×TP4，`MooncakeConnectorV2`）。本目录与其他部署形态、硬件和上下文变体独立呈现，不可跨硬件套用参数。填写镜像与权重路径后，按仓库 deployer 指导部署。

相对源头文档的 Motor / 现场调整：

- Decode 使用 `max-num-seqs=8`、`max-model-len=133120`。官方 `32×1048576` 与 `8×1048576` 在验证环境触发 `aclrtMallocHostWithCfg 207001`（宿主机锁页不足，不是 HBM OOM）。Prefill 仍保持文档 1M。
- `VLLM_USE_V2_MODEL_RUNNER=0`。
