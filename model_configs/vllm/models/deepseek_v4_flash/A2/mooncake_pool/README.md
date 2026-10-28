# deepseek_v4_flash · A2 · mooncake_pool

A2 MTP PD + Mooncake KV Pool。并行与同级 `pd/` 一致。`MultiConnector` = `MooncakeHybridConnector` + `AscendStoreConnector backend=mooncake`，内层不写 `engine_id`。未在本实验室实测。
