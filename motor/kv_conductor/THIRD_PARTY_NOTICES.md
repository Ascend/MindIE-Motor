# Third-Party Notices — KV Conductor

This component is primarily licensed under Mulan PSL v2. The following
third-party material is included as Derivative Work (or derived policy) and
remains subject to its original license.

## NVIDIA Dynamo (kv-router)

| Item | Detail |
|------|--------|
| Project | [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo) |
| Copyright | Copyright (c) 2024-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. |
| License | Apache License, Version 2.0 |
| License text | [`licenses/Apache-2.0.txt`](licenses/Apache-2.0.txt) |

### Derivative source files

| Local file | Upstream path | Summary of Huawei modifications |
|------------|---------------|----------------------------------|
| `src/lower_tier.rs` | `lib/kv-router/src/indexer/lower_tier.rs` | `RwLock` + `FxHashMap` instead of `DashMap`; embedded `worker_blocks` reverse index; `ContiguousHit` API; `WorkerKey` / medium integration |
| `src/concurrent_tree.rs` | `lib/kv-router/src/indexer/concurrent_radix_tree.rs` | `WorkerKey` + `Arc<FxHashSet>` COW workers; lookup owned by `Indexer`; `find_matches_detailed` / `PrefixMatch`; `sweep_stale_nodes`; no `CleanupState` / metrics / `early_exit` |
| `src/hashing.rs` | `lib/kv-router/src/protocols.rs` (XXH3 / `compute_block_hash*`) | `i64` token input; per-chunk conversion; rayon parallel batching; include partial trailing block (`div_ceil`) |
| `src/protocols.rs` (portions) | `lib/kv-router/src/protocols.rs` | Retained hash newtypes / KV event store payloads / overlap-match types; added MindIE HTTP API, `WorkerKey`, `StorageMedium` parsing, thinner `u64` fields |

### Derived policy (not a full file port)

| Local location | Upstream path | Notes |
|----------------|---------------|--------|
| `src/events/vllm.rs` — `is_main_attention_kind` | `lib/kv-router/src/zmq_wire/filter.rs` | Allow/deny attention-kind names aligned with Dynamo; local string-normalize implementation. Unknown kinds are kept (forward-compat); msgspec/JSON visitor is Huawei original. |

### Compliance notes

Huawei modifications are also offered under Mulan PSL v2. Redistribution of
Dynamo-derived source must still comply with Apache License 2.0: retain
copyright and license notices, and provide the Apache-2.0 license text to
recipients (`licenses/Apache-2.0.txt`).

### Files reviewed as original / protocol-compat only (no Plan A derivative header)

The following were checked against Dynamo kv-router and are **not** treated as
substantial code derivatives for this notice (wire-field names or MindIE-only
logic may still overlap conceptually):

- `src/indexer.rs`, `src/backend.rs`, `src/registry.rs`, `src/server.rs`
- `src/zmq_subscriber.rs`, `src/error.rs`, `src/main.rs`, `src/lib.rs`
- `src/events/pool.rs`, `src/events/helpers.rs`, `src/events/flex_hash.rs`
- `src/events/mod.rs` (docs only), `src/events/tests.rs`
- `tests/integration_test.rs`

### Disclaimer

NVIDIA, Dynamo, and related marks are trademarks of their respective owners.
This notice is for attribution only and does not grant trademark rights.
