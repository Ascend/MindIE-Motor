# deepseek_v4_flash_0731 · A2 · mooncake_pool

来源：vLLM Ascend `DeepSeek-V4-Flash.md` A2 KV Pooling（Mooncake backend）。并行与同级 `pd/` 一致；`MultiConnector` = `MooncakeHybridConnector` + `AscendStoreConnector backend=mooncake`。内层不写 `engine_id`。Motor A2 池化按 1288 使用 Mooncake。未在本实验室实测。
