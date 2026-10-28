# deepseek_v4_flash · A2 · mooncake_pool

并行与同级 `pd/` 一致（A3 实测 1P1D 映射到 2×8 卡）。池化叠 `MultiConnector` = `MooncakeHybridConnector` + `AscendStoreConnector backend=mooncake`，内层不写 `engine_id`。A2 不用 MemCache standalone。未在 A2 机器上实测。
