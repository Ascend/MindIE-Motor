// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Pool backend event format and application logic.
//!
//! Used by all pool backends (Mooncake, Memcache, YuanRong) — the pool
//! daemon broadcasts KV cache events that this module deserializes and
//! resolves against the offload cache, retained content, or an
//! already-indexed lower tier (two-phase match, pool-first arrivals wait
//! in `pending_pool`).

use serde::Deserialize;

use crate::backend::MatchMode;
use crate::error::KvConductorError;
use crate::indexer::Indexer;
use crate::protocols::*;

use super::flex_hash::FlexHash;
use super::helpers::{resolve_medium, resolve_workers};

/// Memcache KvEvent batch — wire format `{"events": [PoolEvent, ...]}`.
///
/// Published by the memcache MetaService (memcache PR #334). Each event
/// map carries its own `backend_id` (the originating LocalService's Pod
/// IP), `event_type` ("stored"/"removed"/"cleared"), `medium`, and
/// `seq_hashes` (uint64 array when `hash_as_int=true`, else hex strings).
#[derive(Debug, Deserialize)]
pub(crate) struct MemcacheEventBatch {
    #[serde(default)]
    pub(crate) events: Vec<PoolEvent>,
}

/// Deserialized msgpack event from a pool backend ZMQ PUB frame.
#[derive(Debug, Deserialize)]
pub(crate) struct PoolEvent {
    #[serde(default)]
    pub(crate) event_id: u64,
    #[serde(default)]
    #[allow(dead_code)]
    pub(crate) timestamp: Option<i64>,
    #[serde(default, alias = "event_type")]
    pub(crate) event_type: Option<String>,
    #[serde(default)]
    #[serde(rename = "type")]
    pub(crate) legacy_type: Option<String>,
    #[serde(default)]
    pub(crate) model_name: Option<String>,
    #[serde(default)]
    pub(crate) tenant_id: Option<String>,
    #[serde(default)]
    pub(crate) backend_id: Option<String>,
    #[serde(default)]
    pub(crate) medium: Option<String>,
    #[serde(default)]
    pub(crate) dp_rank: Option<u32>,
    #[serde(default)]
    pub(crate) seq_hashes: Option<Vec<FlexHash>>,
    #[serde(default)]
    pub(crate) block_hashes: Option<Vec<FlexHash>>,
}

/// Apply a single pool backend event to the indexer.
#[allow(clippy::too_many_arguments)]
pub(crate) fn apply_pool_event(
    indexer: &Indexer,
    pool_event: &PoolEvent,
    model_name: &str,
    tenant_id: &str,
    backend_id: &str,
    subscriber_dp_rank: u32,
    default_media: &[StorageMedium],
    match_mode: MatchMode,
    hbm_ip_index: &Option<HbmIpIndex>,
) -> Result<(), KvConductorError> {
    let event_type = pool_event
        .event_type
        .as_deref()
        .or(pool_event.legacy_type.as_deref())
        .unwrap_or("unknown");

    let is_stored = event_type.contains("stored") || event_type.contains("Stored");
    let is_removed = event_type.contains("removed") || event_type.contains("Removed");
    let is_cleared = event_type.contains("cleared")
        || event_type.contains("Cleared")
        || event_type.contains("AllBlocksCleared");

    // Collect and deduplicate seq_hashes from both fields.
    let mut seq_hashes: Vec<SequenceBlockHash> = Vec::new();
    {
        let mut seen: rustc_hash::FxHashSet<u64> = rustc_hash::FxHashSet::default();
        if let Some(ref hashes) = pool_event.seq_hashes {
            for h in hashes {
                if seen.insert(h.0) {
                    seq_hashes.push(SequenceBlockHash(h.0));
                }
            }
        }
        if let Some(ref hashes) = pool_event.block_hashes {
            for h in hashes {
                if seen.insert(h.0) {
                    seq_hashes.push(SequenceBlockHash(h.0));
                }
            }
        }
    }

    // ── Resolve target workers (shared by all event types) ────────────
    let target_media = resolve_medium(pool_event.medium.as_deref(), default_media);

    // For MatchMode::None (YuanRong), use the subscriber's backend_id which IS
    // the engine instance_id from registration.  The event's backend_id may be
    // the pool daemon's IP:port — using it directly would create a different
    // instance_id than the HBM blocks, breaking cross-media block aggregation.
    // For pool backends (Mooncake/Memcache), the event's backend_id is the node
    // IP, needed by resolve_workers for hbm_ip_index lookup.
    let event_be_id = pool_event.backend_id.as_deref().unwrap_or(backend_id);
    let be_id: &str = if match_mode == MatchMode::None {
        backend_id
    } else {
        event_be_id
    };
    let dp_rank = pool_event.dp_rank.unwrap_or(subscriber_dp_rank);
    let mn = pool_event.model_name.as_deref().unwrap_or(model_name);
    let tid = pool_event.tenant_id.as_deref().unwrap_or(tenant_id);

    let entry = indexer.get_or_create(mn, tid);

    let target_workers = resolve_workers(match_mode, hbm_ip_index, be_id, dp_rank, &target_media);

    // ── Cleared events: no seq_hashes needed ──────────────────────────
    if is_cleared {
        for worker in &target_workers {
            entry.apply_event(worker, &KvCacheEventData::Cleared)?;
            entry.remove_pending_worker(worker);
        }
        return Ok(());
    }

    if seq_hashes.is_empty() {
        tracing::debug!(
            event_type,
            backend_id = pool_event.backend_id.as_deref().unwrap_or(backend_id),
            dp_rank = pool_event.dp_rank.unwrap_or(subscriber_dp_rank),
            reason = "no_hashes",
            "kv_event dropped"
        );
        return Ok(());
    }

    // ── Stored / Removed ──────────────────────────────────────────────
    for worker in &target_workers {
        if is_stored {
            let block_hashes: Vec<u64> = seq_hashes.iter().map(|h| h.0).collect();
            let preview: Vec<u64> = block_hashes.iter().take(4).copied().collect();
            let blocks = entry.ingest_pool_blocks(&block_hashes, worker);

            if blocks.is_empty() {
                tracing::debug!(
                    model = %mn, tenant = %tid,
                    event_type,
                    total = seq_hashes.len(),
                    medium = %worker.medium.log_str(),
                    ?preview,
                    worker = %worker.instance_id,
                    "kv_event queued"
                );
            } else {
                tracing::info!(
                    model = %mn, tenant = %tid,
                    matched = blocks.len(),
                    total = seq_hashes.len(),
                    medium = %worker.medium.log_str(),
                    ?preview,
                    worker = %worker.instance_id,
                    "kv_event confirmed"
                );
                // Apply one `Stored` event per block, each with its own
                // `parent_hash` (resolved from the offload cache, retained
                // content, or a lower-tier lookup), instead of batching them
                // into a single event with `parent_hash: None` — batching
                // would silently drop continuation-edge chaining.
                for (parent_hash, block) in blocks {
                    let store_data = KvCacheStoreData {
                        parent_hash,
                        start_position: None,
                        blocks: vec![block],
                    };
                    entry.apply_event(worker, &KvCacheEventData::Stored(store_data))?;
                }
            }
        } else if is_removed {
            let block_hashes: Vec<u64> = seq_hashes.iter().map(|h| h.0).collect();
            let tree_hashes = entry.evict_pending_blocks(&block_hashes, worker);

            if tree_hashes.is_empty() {
                tracing::debug!(
                    model = %mn, tenant = %tid,
                    event_type,
                    total = seq_hashes.len(),
                    medium = %worker.medium.log_str(),
                    reason = "still_pending",
                    "kv_event dropped"
                );
            } else {
                tracing::debug!(
                    model = %mn, tenant = %tid,
                    event_type,
                    tree_blocks = tree_hashes.len(),
                    total = seq_hashes.len(),
                    medium = %worker.medium.log_str(),
                    "kv_event applied"
                );
                entry.apply_event(
                    worker,
                    &KvCacheEventData::Removed {
                        block_hashes: tree_hashes,
                    },
                )?;
            }
        } else {
            tracing::debug!(
                event_type,
                model = %mn,
                backend_id = %be_id,
                dp_rank,
                reason = "unknown_type",
                "kv_event dropped"
            );
        }
    }

    Ok(())
}
