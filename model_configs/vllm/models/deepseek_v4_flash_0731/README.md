# deepseek_v4_flash_0731

DeepSeek-V4-Flash **0731 DSpark** 典配，与 `deepseek_v4_flash`（MTP）分目录存放，不可混用权重或并行切分。

| 硬件 | PD 分离 | PD 混部 | 池化 |
|---|---|---|---|
| A2 | [A2/pd](./A2/pd) | [A2/hybrid_single](./A2/hybrid_single) | [A2/mooncake_pool](./A2/mooncake_pool) |
| A3 | [A3/pd](./A3/pd)（现场跑通） | [A3/hybrid_single](./A3/hybrid_single) | [A3/memcache_pool](./A3/memcache_pool) |
| A5 | [A5/pd](./A5/pd) | [A5/hybrid_single](./A5/hybrid_single) | [A5/memcache_pool](./A5/memcache_pool) |

来源：vLLM Ascend `DeepSeek-V4-Flash.md`。A3 PD / 池化 Decode `max-num-seqs=8` 为实验室锁页规避；其余形态按社区文档转写，未在本实验室实测。池化内层 Connector 不写 `engine_id`。
