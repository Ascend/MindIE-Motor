# deepseek_v4_flash_0731 · A3 · memcache_pool

来源：vLLM Ascend `DeepSeek-V4-Flash.md` A3 1P1D + MemCache KV Cache Pool。并行与投机与同级 `pd/` 一致；`MultiConnector` = `MooncakeHybridConnector` + `AscendStoreConnector backend=memcache`。内层 Connector **不写** `engine_id`。A3 `local_service_mode=standalone`。Decode `max-num-seqs=8` 沿用现场锁页规避。填写镜像与权重后按 deployer 与 memcache.md 部署。
