# deepseek_v4_flash · A3 · memcache_pool

A3 MTP 1P1D + MemCache。并行与同级 `pd/` 一致（现场跑通）；Prefill 打开 prefix caching。`MultiConnector` = `MooncakeHybridConnector` + `AscendStoreConnector backend=memcache`，内层不写 `engine_id`。A3 `standalone`。
