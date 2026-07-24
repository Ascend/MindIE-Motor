// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! KV event types, deserialization, filtering, and application logic.
//!
//! Supports two event source formats:
//!
//! 1. **Pool backend** (Mooncake / Memcache / YuanRong): `PoolEvent` with
//!    `seq_hashes`/`block_hashes`, processed by `apply_pool_event`.  No
//!    `token_ids` — `tokens_hash` is set equal to `block_hash`.
//!
//! 2. **vLLM engine** (native): `VllmEventMap` with `token_ids` + `block_size`,
//!    processed by `apply_vllm_event`.  `tokens_hash` is re-computed from
//!    `token_ids` via `compute_block_hash_for_seq` (XXH3, seed 1337), while
//!    the engine's chained `block_hashes` are kept as
//!    `ExternalSequenceBlockHash` for reverse-lookup on `BlockRemoved`.
//!
//! ## Event filtering
//!
//! Following Dynamo kv-router's approach, events from non-main attention
//! groups (SWA, Mamba, ChunkedLocal, etc.) are filtered out.  Only
//! `FullAttention`, `MlaAttention`, and `SinkFullAttention` events are
//! processed.  This ensures all ingested events share the same `block_size`,
//! avoiding the multi-group hash granularity mismatch problem.

pub(crate) mod flex_hash;
mod helpers;
pub(crate) mod pool;
pub(crate) mod vllm;

// Re-export key types so callers don't need to reach into sub-modules.
#[allow(unused_imports)]
pub(crate) use flex_hash::FlexHash; // used by tests via `use super::*`
pub(crate) use pool::{apply_pool_event, PoolEvent};
#[allow(unused_imports)]
pub(crate) use vllm::{
    apply_vllm_event, is_main_attention_kind, parse_vllm_batch, VllmEvent, VllmEventMap,
};

// Re-export types and functions needed by tests via `use super::*`.
// Some are only consumed in test code; the `allow` prevents lib-build warnings.
#[allow(unused_imports)]
pub(crate) use crate::backend::MatchMode;
#[allow(unused_imports)]
pub(crate) use crate::error::KvConductorError;
#[allow(unused_imports)]
pub(crate) use crate::hashing::compute_block_hash_for_seq;
#[allow(unused_imports)]
pub(crate) use crate::protocols::{
    HbmIpIndex, KvCacheEventData, KvCacheStoreData, KvCacheStoredBlockData, ScoringConfig,
    SequenceBlockHash, StorageMedium, WorkerKey,
};

#[cfg(test)]
mod tests;
