# deepseek_v4_flash · DSpark

DeepSeek-V4-Flash **0731 DSpark** 典配，与上级 MTP（`A3/pd`、`A3/user_config.json`）独立。按硬件与形态分子目录：

| 硬件 | PD 分离 | PD 混部 | 池化 |
|---|---|---|---|
| A2 | `A2/dspark/pd` | `A2/dspark/hybrid_single` | `A2/dspark/mooncake_pool` |
| A3 | `A3/dspark/pd`（现场跑通） | `A3/dspark/hybrid_single` | `A3/dspark/memcache_pool` |
| A5 | `A5/dspark/pd` | `A5/dspark/hybrid_single` | `A5/dspark/memcache_pool` |

来源：vLLM Ascend `DeepSeek-V4-Flash.md`。A3 PD / 池化 Decode `max-num-seqs=8` 为实验室锁页规避；其余形态按社区文档转写，未在本实验室实测。池化内层 Connector 不写 `engine_id`。
