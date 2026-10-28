# deepseek_v4_flash_0731 · A2 · pd

来源：vLLM Ascend `DeepSeek-V4-Flash.md` A2 DSpark 2P2D（4 机 32 卡）。Prefill ExternalDP2×TP1×PCP8（2 个 8 卡 Pod），Decode DP16×TP1（2 个 8 卡 Pod），`MooncakeHybridConnector`，权重 `DeepSeek-V4-Flash-0731-w8a8`。P/D 均 `VLLM_USE_V2_MODEL_RUNNER=1`，`dspark` tokens=5。Decode `max-num-seqs=42` 满足文档 `seqs×(1+draft)≤256`。未写 `api-server-count`。未在本实验室实测。
