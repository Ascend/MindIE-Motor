# deepseek_v4_1_flash · A3 · pd

来源：vLLM Ascend `DeepSeek-V4.1-Flash.md`（`releases/v0.30.0`，§5.2 A3 1P1D）。Motor 适配后在 Atlas 800I A3 上完成 1P1D 启动与 Coordinator 推理。

场景：A3 1P1D PD（Prefill DP4×TP4，Decode DP8×TP2，`MooncakeHybridConnector`，投机 `dspark` tokens=5）。本目录与 `deepseek_v4_flash` 的 MTP / 0731 DSpark 典配独立，不可混用权重或并行切分。填写镜像与权重路径后，按仓库 deployer 指导部署。

相对源头文档的 Motor / 现场调整：

- 权重目录使用 **`DeepSeek-V4.1-Flash-W4A8`（大写）**。小写 `DeepSeek-V4.1-Flash-w4a8` 含官方没有的 `mtp.1.embed.weight`，加载时报 `KeyError: model.layers.41.embed.weight`。
- Docker / Kubernetes 部署需要 `--ipc=host`：本目录 `env.json` 已配置 `MOTOR_ENABLE_IPC_HOST=1`。
- `VLLM_USE_V2_MODEL_RUNNER=0`。Prefill `HCCL_BUFFSIZE=1024`，Decode `HCCL_BUFFSIZE=1800`。
- 对外模型名 `dsv41`。
