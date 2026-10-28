# deepseek_v4_flash_0731 · A5 · pd

来源：vLLM Ascend `DeepSeek-V4-Flash.md` 950DT 单机 PD（P NPUs 0–3 DP1×TP1×PCP4，D NPUs 4–7 DP4×TP1，`MooncakeConnectorV2`）。权重 `DeepSeek-V4-Flash-0731`。P/D 均 `VLLM_USE_V2_MODEL_RUNNER=1`。Motor 映射为 1P1D、各 4 卡、`hardware_type=Ascend950`。文档是同机分卡；K8s 需自行保证 P/D 落到同一 950DT 节点。未在本实验室实测。
