# deepseek_v4_flash_0731 · A5 · memcache_pool

来源：vLLM Ascend `DeepSeek-V4-Flash.md` 950DT MemCache KV Cache Pool。并行与同级 `pd/` 一致；`MultiConnector` = `MooncakeConnectorV2` + `AscendStoreConnector backend=memcache`。Prefill `use_layerwise=true`。Motor A5 强制 `inprocess`。内层不写 `engine_id`。未在本实验室实测。
