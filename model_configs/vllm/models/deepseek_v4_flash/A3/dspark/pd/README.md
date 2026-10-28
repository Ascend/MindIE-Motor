# deepseek_v4_flash · A3 · dspark · pd

来源：vLLM Ascend `DeepSeek-V4-Flash.md`（`releases/v0.30.0`，A3 series with DSpark PD）。Motor 适配后在 Atlas 800I A3 上完成 1P1D 启动与 Coordinator 推理。权重 `DeepSeek-V4-Flash-0731-w8a8`。

场景：A3 1P1D PD（Prefill DP2×TP1×PCP8，Decode DP16×TP1，`MooncakeHybridConnector`，`dspark` tokens=5）。填写镜像与权重路径后，按仓库 deployer 指导部署。

相对源头文档的 Motor / 现场调整：

- Prefill / Decode 均需 `VLLM_USE_V2_MODEL_RUNNER=1`（PCP>1 硬条件）。不要开 `enable_dsa_cp`。
- Decode 使用 `max-num-seqs=8`、`max-model-len=1000000`。官方 `60×1048576` 在验证环境触发 `aclrtMallocHostWithCfg 207001`（宿主机锁页不足）。
- 对外模型名 `dsv4`。
