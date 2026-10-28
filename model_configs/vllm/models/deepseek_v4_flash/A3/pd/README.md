# deepseek_v4_flash · A3 · pd（MTP）

来源：vLLM Ascend `DeepSeek-V4-Flash.md`（`releases/v0.30.0`，§5.2.1 A3 series MTP）。Motor 适配后在 Atlas 800I A3 上完成 1P1D 启动与 Coordinator 推理。权重为 `DeepSeek-V4-Flash-w8a8-mtp`。

场景：A3 1P1D PD（Prefill DP4×TP4，Decode DP16×TP1，`MooncakeHybridConnector`，投机 `mtp` tokens=1）。与 `deepseek_v4_flash_0731` 独立，不可混用权重或并行切分。填写镜像与权重路径后，按仓库 deployer 指导部署。

相对源头文档的 Motor / 现场调整：

- Prefill 为 DP4×TP4 + `enable_dsa_cp`，不要套用 main 文档的 DP4×TP1×PCP4。本形态不开启 `VLLM_USE_V2_MODEL_RUNNER`。
- Prefill `HCCL_BUFFSIZE=2560`，Decode `HCCL_BUFFSIZE=1024`。`block-size=32`。
- 对外模型名 `dsv4`。
