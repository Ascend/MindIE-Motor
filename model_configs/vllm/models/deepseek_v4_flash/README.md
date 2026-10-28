# deepseek_v4_flash

DeepSeek-V4-Flash **MTP** 典配，与 `deepseek_v4_flash_0731`（DSpark）分目录存放，不可混用权重或并行切分。

| 硬件 | PD 分离 | PD 混部 | 池化 |
|---|---|---|---|
| A2 | [A2/pd](./A2/pd) | [A2/hybrid_single](./A2/hybrid_single) | [A2/mooncake_pool](./A2/mooncake_pool) |
| A3 | [A3/pd](./A3/pd)（现场跑通） | [A3/hybrid_single](./A3/hybrid_single) | [A3/memcache_pool](./A3/memcache_pool) |
| A5 | [A5/pd](./A5/pd) | [A5/hybrid_single](./A5/hybrid_single) | [A5/memcache_pool](./A5/memcache_pool) |

A3 PD / 池化为实验室 1P1D 跑通。A2 按「一机 A3 = 两机 A2（16=8+8）」复用同一套引擎切分，每个实例 2 个 8 卡 Pod；未上 A2 机。A5 按仓库既有 MTP 根典配整理。池化内层 Connector 不写 `engine_id`。`A2/`、`A3/`、`A5/` 根目录 JSON 为既有自动生成，部署请用上表嵌套目录。
