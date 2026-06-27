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
//! 1. **Mooncake Master** (legacy): `ZmqEventMap` with `seq_hashes`/`block_hashes`,
//!    processed by `apply_zmq_event`. No `token_ids` — `tokens_hash` is set
//!    equal to `block_hash`.
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

use serde::Deserialize;

use crate::backend::MatchMode;
use crate::error::KvConductorError;
use crate::hashing::compute_block_hash_for_seq;
use crate::indexer::Indexer;
use crate::protocols::*;

// ---------------------------------------------------------------------------
// FlexHash — polymorphic u64 deserialization
// ---------------------------------------------------------------------------

/// A u64 that can be deserialized from multiple msgpack representations:
///   - integer (u64, i64, u32, …)
///   - decimal string   "12345678901234567890"
///   - hex string       "0xABCD1234…" or "ABCD1234…"
///   - binary bytes     up to 8 bytes, big-endian (vLLM BlockHash compat)
#[derive(Debug, Clone, Copy)]
pub(crate) struct FlexHash(pub(crate) u64);

impl<'de> Deserialize<'de> for FlexHash {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct FlexHashVisitor;
        impl<'de> serde::de::Visitor<'de> for FlexHashVisitor {
            type Value = FlexHash;

            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a u64, decimal/hex string, or up to 8 bytes")
            }

            fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<FlexHash, E> {
                Ok(FlexHash(v))
            }

            fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<FlexHash, E> {
                if v < 0 {
                    return Err(E::custom(format!("negative hash: {v}")));
                }
                Ok(FlexHash(v as u64))
            }

            fn visit_str<E: serde::de::Error>(self, v: &str) -> Result<FlexHash, E> {
                let s = v.trim();
                if let Some(hex) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
                    return u64::from_str_radix(hex, 16)
                        .map(FlexHash)
                        .map_err(|e| E::custom(format!("invalid hex hash '{v}': {e}")));
                }
                if let Ok(n) = s.parse::<u64>() {
                    return Ok(FlexHash(n));
                }
                u64::from_str_radix(s, 16).map(FlexHash).map_err(|_| {
                    E::custom(format!(
                        "invalid hash '{v}': expected u64, hex, or 0x-prefixed"
                    ))
                })
            }

            fn visit_bytes<E: serde::de::Error>(self, v: &[u8]) -> Result<FlexHash, E> {
                bytes_to_flex(v)
            }

            fn visit_byte_buf<E: serde::de::Error>(self, v: Vec<u8>) -> Result<FlexHash, E> {
                bytes_to_flex(&v)
            }
        }

        fn bytes_to_flex<E: serde::de::Error>(v: &[u8]) -> Result<FlexHash, E> {
            if v.len() > 8 {
                return Err(E::custom(format!("hash bytes too long: {} > 8", v.len())));
            }
            let mut buf = [0u8; 8];
            buf[8 - v.len()..].copy_from_slice(v);
            Ok(FlexHash(u64::from_be_bytes(buf)))
        }

        deserializer.deserialize_any(FlexHashVisitor)
    }
}

// ---------------------------------------------------------------------------
// Mooncake Master event types (legacy ZMQ format)
// ---------------------------------------------------------------------------

/// Msgpack batch envelope in map format (newer publishers).
#[derive(Debug, Deserialize)]
pub(crate) struct ZmqBatchMap {
    #[serde(default)]
    #[allow(dead_code)]
    pub(crate) timestamp_ms: Option<i64>,
    #[serde(default)]
    pub(crate) dp_rank: Option<u32>,
    #[serde(default)]
    pub(crate) events: Vec<ZmqEventMap>,
}

/// Deserialized msgpack event map from a Mooncake Master ZMQ PUB frame.
#[derive(Debug, Deserialize)]
pub(crate) struct ZmqEventMap {
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

// ---------------------------------------------------------------------------
// vLLM-native event types (msgspec KVEventBatch wire format)
// ---------------------------------------------------------------------------

/// A single event from vLLM's msgspec-tagged union.
///
/// vLLM encodes `KVCacheEvent` subclasses with `tag=True`, producing a map
/// with a `"type"` discriminator field.  `omit_defaults=True` means most
/// optional fields are absent when null.
#[derive(Debug, Deserialize)]
struct VllmEventMap {
    #[serde(rename = "type")]
    event_type: String,
    #[serde(default)]
    block_hashes: Option<Vec<FlexHash>>,
    #[serde(default)]
    parent_block_hash: Option<FlexHash>,
    /// Raw token IDs for all blocks in this event, concatenated.
    #[serde(default)]
    token_ids: Option<Vec<i64>>,
    #[serde(default)]
    block_size: Option<u32>,
    #[serde(default)]
    medium: Option<String>,
    #[serde(default)]
    group_idx: Option<u32>,
    #[serde(default)]
    #[allow(dead_code)]
    lora_id: Option<i64>,
    #[serde(default)]
    #[allow(dead_code)]
    lora_name: Option<String>,
    /// Attention type tag from vLLM (e.g. "FullAttention", "MlaAttention",
    /// "SlidingWindow").  Used to filter out non-main attention groups.
    #[serde(default)]
    kv_cache_spec_kind: Option<String>,
    #[serde(default)]
    #[allow(dead_code)]
    kv_cache_spec_sliding_window: Option<u32>,
}

/// Returns `true` if `kind` is a main attention type whose events should be
/// ingested.  Following Dynamo kv-router, only `FullAttention`,
/// `MlaAttention`, and `SinkFullAttention` qualify.  Events with no
/// `kv_cache_spec_kind` (older vLLM versions) are kept for backward compat.
fn is_main_attention_kind(kind: Option<&str>) -> bool {
    match kind {
        None => true, // older vLLM — no filtering info; accept
        Some("FullAttention") | Some("MlaAttention") | Some("SinkFullAttention") => true,
        Some("SlidingWindow")
        | Some("Mamba")
        | Some("ChunkedLocalAttention")
        | Some("EncoderOnlyAttention")
        | Some("CrossAttention") => false,
        _ => true, // unknown future kind — accept (forward compat)
    }
}

/// Parsed vLLM-native event with normalized fields.
#[derive(Debug)]
#[allow(dead_code)] // parent_block_hash & group_idx reserved for future use
pub(crate) enum VllmEvent {
    BlockStored {
        block_hashes: Vec<u64>,
        parent_block_hash: Option<u64>,
        token_ids: Vec<i64>,
        block_size: u32,
        medium: Option<String>,
        group_idx: Option<u32>,
    },
    BlockRemoved {
        block_hashes: Vec<u64>,
        medium: Option<String>,
        group_idx: Option<u32>,
    },
    AllBlocksCleared,
    /// Events we don't handle (e.g. from non-main-attention groups).
    Ignored,
}

impl VllmEventMap {
    fn normalize(&self) -> VllmEvent {
        // AllBlocksCleared always passes through — it clears the entire
        // cache regardless of which attention group emitted it.
        let is_cleared = self.event_type.as_str() == "AllBlocksCleared";

        // Filter out non-main attention groups (SWA, Mamba, etc.)
        // before building the event — same strategy as Dynamo kv-router.
        if !is_cleared && !is_main_attention_kind(self.kv_cache_spec_kind.as_deref()) {
            return VllmEvent::Ignored;
        }

        match self.event_type.as_str() {
            "BlockStored" => {
                let block_hashes: Vec<u64> = self
                    .block_hashes
                    .as_ref()
                    .map(|v| v.iter().map(|h| h.0).collect())
                    .unwrap_or_default();
                let token_ids: Vec<i64> = self.token_ids.clone().unwrap_or_default();
                let block_size = self.block_size.unwrap_or(0);

                VllmEvent::BlockStored {
                    block_hashes,
                    parent_block_hash: self.parent_block_hash.map(|h| h.0),
                    token_ids,
                    block_size,
                    medium: self.medium.clone(),
                    group_idx: self.group_idx,
                }
            }
            "BlockRemoved" => {
                let block_hashes: Vec<u64> = self
                    .block_hashes
                    .as_ref()
                    .map(|v| v.iter().map(|h| h.0).collect())
                    .unwrap_or_default();
                VllmEvent::BlockRemoved {
                    block_hashes,
                    medium: self.medium.clone(),
                    group_idx: self.group_idx,
                }
            }
            "AllBlocksCleared" => VllmEvent::AllBlocksCleared,
            _ => VllmEvent::Ignored,
        }
    }
}

// ---------------------------------------------------------------------------
// Event application
// ---------------------------------------------------------------------------

/// Apply a single parsed Mooncake ZMQ event to the indexer.
#[allow(clippy::too_many_arguments)]
pub(crate) fn apply_zmq_event(
    indexer: &Indexer,
    zmq_event: &ZmqEventMap,
    model_name: &str,
    tenant_id: &str,
    backend_id: &str,
    _batch_dp_rank: u32,
    _subscriber_dp_rank: u32,
    default_media: &[StorageMedium],
    match_mode: MatchMode,
    hbm_ip_index: &Option<HbmIpIndex>,
) -> Result<(), KvConductorError> {
    let event_type = zmq_event
        .event_type
        .as_deref()
        .or(zmq_event.legacy_type.as_deref())
        .unwrap_or("unknown");

    let is_stored = event_type.contains("stored") || event_type.contains("Stored");
    let is_removed = event_type.contains("removed") || event_type.contains("Removed");
    let is_cleared = event_type.contains("cleared")
        || event_type.contains("Cleared")
        || event_type.contains("AllBlocksCleared");

    let mut seq_hashes: Vec<SequenceBlockHash> = Vec::new();
    if let Some(ref hashes) = zmq_event.seq_hashes {
        seq_hashes.extend(hashes.iter().map(|h| SequenceBlockHash(h.0)));
    }
    if let Some(ref hashes) = zmq_event.block_hashes {
        seq_hashes.extend(hashes.iter().map(|h| SequenceBlockHash(h.0)));
    }

    if seq_hashes.is_empty() {
        return Ok(());
    }

    let target_media: Vec<StorageMedium> = if let Some(ref m) = zmq_event.medium {
        vec![StorageMedium::parse(m)]
    } else {
        default_media.to_vec()
    };

    let be_id = zmq_event.backend_id.as_deref().unwrap_or(backend_id);
    let dp_rank = zmq_event.dp_rank.unwrap_or(_subscriber_dp_rank);
    let mn = zmq_event.model_name.as_deref().unwrap_or(model_name);
    let tid = zmq_event.tenant_id.as_deref().unwrap_or(tenant_id);

    let entry = indexer.get_or_create(mn, tid);

    let target_workers: Vec<WorkerKey> = if match_mode == MatchMode::None {
        target_media
            .iter()
            .map(|&medium| WorkerKey {
                instance_id: be_id.to_string(),
                backend_id: be_id.to_string(),
                dp_rank,
                medium,
            })
            .collect()
    } else {
        match_mode.resolve_workers(hbm_ip_index.as_ref(), be_id, dp_rank, &target_media)
    };

    for worker in &target_workers {
        if is_stored {
            // Pool backend store: look up each seq_hash in the non-HBM cache.
            // If found, insert the cached XXH3 tokens_hash into the radix tree
            // under this worker.  If not found, the block was never cached
            // (may have been offloaded by a different engine) — skip silently.
            let blocks: Vec<KvCacheStoredBlockData> = seq_hashes
                .iter()
                .filter_map(|h| {
                    entry
                        .lookup_cached_tokens_hash(h.0)
                        .map(|tokens_hash| KvCacheStoredBlockData {
                            block_hash: h.0,
                            tokens_hash,
                        })
                })
                .collect();

            if !blocks.is_empty() {
                tracing::trace!(
                    model = %mn, tenant = %tid,
                    matched = blocks.len(),
                    total = seq_hashes.len(),
                    "pool confirm: inserted cached blocks into radix tree"
                );
                let store_data = KvCacheStoreData {
                    parent_hash: None,
                    start_position: None,
                    blocks,
                };
                entry.apply_event(worker, &KvCacheEventData::Stored(store_data))?;
            }
        } else if is_removed {
            // Pool backend remove: look up cached tokens_hash, remove from tree,
            // then evict the cache entry.
            let cached_hashes: Vec<u64> = seq_hashes
                .iter()
                .filter_map(|h| {
                    entry.lookup_cached_tokens_hash(h.0).inspect(|_| {
                        entry.evict_cached_block(h.0);
                    })
                })
                .collect();

            if !cached_hashes.is_empty() {
                // Also pass through the seq_hashes for the legacy lookup path.
                // The radix tree's apply_remove checks the WorkerLookup for
                // ExternalSequenceBlockHash → node mapping.
                let block_hashes: Vec<u64> = seq_hashes.iter().map(|h| h.0).collect();
                entry.apply_event(worker, &KvCacheEventData::Removed { block_hashes })?;
            }
        } else if is_cleared {
            entry.apply_event(worker, &KvCacheEventData::Cleared)?;
        }
    }

    Ok(())
}

/// Apply a parsed vLLM-native event to the indexer.
///
/// vLLM `BlockStored` events carry `token_ids` and `block_size`, allowing
/// us to re-compute `tokens_hash` (XXH3 content hash).  The behaviour
/// depends on the storage medium:
///
/// - **HBM** (XPU/GPU): insert directly into the radix tree.
/// - **Non-HBM** (CPU/DISK): cache the ``block_hash → tokens_hash`` mapping
///   in the `IndexerEntry.non_hbm_cache`.  The radix tree is updated later
///   when the pool backend (Mooncake Master) confirms the block placement
///   and broadcasts its own store event with the same ``block_hash``.
///
/// This two-phase approach is required because the pool backend may place
/// the block on a different node than the engine that offloaded it — the
/// engine's offloading event tells us *what* was offloaded, and the pool
/// backend's event tells us *where* it was placed.
#[allow(clippy::too_many_arguments)]
pub(crate) fn apply_vllm_event(
    indexer: &Indexer,
    event: &VllmEvent,
    model_name: &str,
    tenant_id: &str,
    backend_id: &str,
    _batch_dp_rank: u32,
    subscriber_dp_rank: u32,
    default_media: &[StorageMedium],
    match_mode: MatchMode,
    hbm_ip_index: &Option<HbmIpIndex>,
) -> Result<(), KvConductorError> {
    match event {
        VllmEvent::BlockStored {
            block_hashes,
            parent_block_hash: _,
            token_ids,
            block_size,
            medium,
            group_idx: _,
        } => {
            if block_hashes.is_empty() {
                return Ok(());
            }

            // Determine whether this is an HBM or offloading event.
            let event_medium = medium.as_deref().unwrap_or("xpu");
            let is_non_hbm = !event_medium.eq_ignore_ascii_case("xpu")
                && !event_medium.eq_ignore_ascii_case("gpu");

            // Compute tokens_hash from token_ids via XXH3.
            let computed_hashes: Vec<u64> = if token_ids.is_empty() || *block_size == 0 {
                // Legacy fallback: no token_ids, use block_hashes directly.
                block_hashes.to_vec()
            } else {
                let hashes = compute_block_hash_for_seq(token_ids, *block_size);
                let num = hashes.len().min(block_hashes.len());
                hashes[..num].iter().map(|h| h.0).collect()
            };

            if computed_hashes.is_empty() {
                return Ok(());
            }

            let num = computed_hashes.len().min(block_hashes.len());
            let entry = indexer.get_or_create(model_name, tenant_id);

            if is_non_hbm {
                // Phase 1 — cache the engine's block_hash → tokens_hash mapping.
                for i in 0..num {
                    entry.cache_non_hbm_block(block_hashes[i], computed_hashes[i]);
                }
                tracing::trace!(
                    model = %model_name, tenant = %tenant_id,
                    num_blocks = num, medium = %event_medium,
                    "vLLM non-HBM: cached blocks"
                );
            } else {
                // HBM: insert directly into the radix tree.
                tracing::trace!(
                    model = %model_name, tenant = %tenant_id,
                    num_blocks = num, medium = %event_medium,
                    "vLLM HBM: inserting blocks into radix tree"
                );
                let blocks: Vec<KvCacheStoredBlockData> = (0..num)
                    .map(|i| KvCacheStoredBlockData {
                        block_hash: block_hashes[i],
                        tokens_hash: computed_hashes[i],
                    })
                    .collect();

                let store_data = KvCacheStoreData {
                    parent_hash: None,
                    start_position: None,
                    blocks,
                };

                let target_media = resolve_medium(medium.as_deref(), default_media);
                let target_workers = resolve_workers(
                    match_mode,
                    hbm_ip_index,
                    backend_id,
                    subscriber_dp_rank,
                    &target_media,
                );
                for worker in &target_workers {
                    entry.apply_event(worker, &KvCacheEventData::Stored(store_data.clone()))?;
                }
            }
        }
        VllmEvent::BlockRemoved {
            block_hashes,
            medium,
            group_idx: _,
        } => {
            if block_hashes.is_empty() {
                return Ok(());
            }
            let target_media = resolve_medium(medium.as_deref(), default_media);
            let target_workers = resolve_workers(
                match_mode,
                hbm_ip_index,
                backend_id,
                subscriber_dp_rank,
                &target_media,
            );
            let entry = indexer.get_or_create(model_name, tenant_id);
            for worker in &target_workers {
                entry.apply_event(
                    worker,
                    &KvCacheEventData::Removed {
                        block_hashes: block_hashes.clone(),
                    },
                )?;
            }
        }
        VllmEvent::AllBlocksCleared => {
            let target_media = resolve_medium(None, default_media);
            let target_workers = resolve_workers(
                match_mode,
                hbm_ip_index,
                backend_id,
                subscriber_dp_rank,
                &target_media,
            );
            let entry = indexer.get_or_create(model_name, tenant_id);
            for worker in &target_workers {
                entry.apply_event(worker, &KvCacheEventData::Cleared)?;
            }
        }
        VllmEvent::Ignored => { /* skip */ }
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// vLLM batch parsing
// ---------------------------------------------------------------------------

/// Parse a vLLM `KVEventBatch` from msgpack payload bytes.
///
/// vLLM's `ZmqEventPublisher` serialises `KVEventBatch` with msgspec
/// (`array_like=True` on the batch, `tag=True` on each event). The wire
/// format is a 3-element array:
///
/// ```text
/// [ts: f64, events: [...], dp_rank: int|null]
/// ```
///
/// Both `[ts, events, dp_rank]` and `[ts, dp_rank, events]` orderings are
/// tried to be robust against msgspec / version variations.
pub(crate) fn parse_vllm_batch(payload: &[u8]) -> Option<(Vec<VllmEvent>, u32)> {
    // Format A: [ts: f64, events: [...], dp_rank: int|null]
    if let Ok((_ts, events, dp_rank)) =
        rmp_serde::from_slice::<(f64, Vec<VllmEventMap>, Option<i32>)>(payload)
    {
        return Some((
            events.iter().map(|e| e.normalize()).collect(),
            dp_rank.unwrap_or(0) as u32,
        ));
    }
    // Format B: [ts: f64, dp_rank: int|null, events: [...]]
    if let Ok((_ts, dp_rank, events)) =
        rmp_serde::from_slice::<(f64, Option<i32>, Vec<VllmEventMap>)>(payload)
    {
        return Some((
            events.iter().map(|e| e.normalize()).collect(),
            dp_rank.unwrap_or(0) as u32,
        ));
    }
    None
}

/// Try to parse a single vLLM event (bare, no batch wrapper).
pub(crate) fn parse_vllm_bare(payload: &[u8]) -> Option<VllmEvent> {
    rmp_serde::from_slice::<VllmEventMap>(payload)
        .ok()
        .map(|e| e.normalize())
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

fn resolve_medium(
    event_medium: Option<&str>,
    default_media: &[StorageMedium],
) -> Vec<StorageMedium> {
    if let Some(m) = event_medium {
        vec![StorageMedium::parse(m)]
    } else {
        default_media.to_vec()
    }
}

fn resolve_workers(
    match_mode: MatchMode,
    hbm_ip_index: &Option<HbmIpIndex>,
    backend_id: &str,
    dp_rank: u32,
    target_media: &[StorageMedium],
) -> Vec<WorkerKey> {
    if match_mode == MatchMode::None {
        target_media
            .iter()
            .map(|&medium| WorkerKey {
                instance_id: backend_id.to_string(),
                backend_id: backend_id.to_string(),
                dp_rank,
                medium,
            })
            .collect()
    } else {
        match_mode.resolve_workers(hbm_ip_index.as_ref(), backend_id, dp_rank, target_media)
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use rmp_serde::from_slice;

    fn msgpack_bin(data: &[u8]) -> Vec<u8> {
        let mut buf = vec![0x91, 0xC4, data.len() as u8];
        buf.extend_from_slice(data);
        buf
    }

    #[test]
    fn test_flex_hash_u64() {
        let data = rmp_serde::to_vec(&vec![42u64, 18446744073709551615u64]).unwrap();
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, 42);
        assert_eq!(hashes[1].0, u64::MAX);
    }

    #[test]
    fn test_flex_hash_decimal_string() {
        let data = rmp_serde::to_vec(&vec!["42"]).unwrap();
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, 42);
    }

    #[test]
    fn test_flex_hash_hex_string() {
        let data = rmp_serde::to_vec(&vec!["0x2A"]).unwrap();
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, 0x2A);
    }

    #[test]
    fn test_flex_hash_hex_string_no_prefix() {
        let data = rmp_serde::to_vec(&vec!["FF"]).unwrap();
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, 0xFF);
    }

    #[test]
    fn test_flex_hash_bytes() {
        let data = msgpack_bin(&[0x00, 0x00, 0x00, 0x2A]);
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, 42);
    }

    #[test]
    fn test_flex_hash_bytes_max() {
        let data = msgpack_bin(&[0xFFu8; 8]);
        let hashes: Vec<FlexHash> = from_slice(&data).unwrap();
        assert_eq!(hashes[0].0, u64::MAX);
    }

    #[test]
    fn test_flex_hash_i64_negative_rejected() {
        let data = rmp_serde::to_vec(&vec![-1i64]).unwrap();
        let result: Result<Vec<FlexHash>, _> = from_slice(&data);
        assert!(result.is_err());
    }

    #[test]
    fn test_flex_hash_bytes_too_long_rejected() {
        let packed = rmp_serde::to_vec(&vec![vec![0u8; 9]]).unwrap();
        let result: Result<Vec<FlexHash>, _> = from_slice(&packed);
        assert!(result.is_err());
    }

    #[test]
    fn test_flex_hash_integrated_in_zmq_event_map() {
        let event = serde_json::json!({
            "event_id": 1,
            "event_type": "stored",
            "medium": "cpu",
            "seq_hashes": ["0xABCD", "12345"],
            "block_hashes": [100, 200]
        });
        let packed = rmp_serde::to_vec(&event).unwrap();
        let map: ZmqEventMap = from_slice(&packed).unwrap();
        let seq: Vec<u64> = map.seq_hashes.unwrap().iter().map(|h| h.0).collect();
        assert_eq!(seq, vec![0xABCD, 12345]);
        let blk: Vec<u64> = map.block_hashes.unwrap().iter().map(|h| h.0).collect();
        assert_eq!(blk, vec![100, 200]);
    }

    // -----------------------------------------------------------------------
    // is_main_attention_kind
    // -----------------------------------------------------------------------

    #[test]
    fn test_main_attention_kinds_accepted() {
        assert!(is_main_attention_kind(Some("FullAttention")));
        assert!(is_main_attention_kind(Some("MlaAttention")));
        assert!(is_main_attention_kind(Some("SinkFullAttention")));
    }

    #[test]
    fn test_non_main_attention_kinds_filtered() {
        assert!(!is_main_attention_kind(Some("SlidingWindow")));
        assert!(!is_main_attention_kind(Some("Mamba")));
        assert!(!is_main_attention_kind(Some("ChunkedLocalAttention")));
        assert!(!is_main_attention_kind(Some("EncoderOnlyAttention")));
        assert!(!is_main_attention_kind(Some("CrossAttention")));
    }

    #[test]
    fn test_unknown_and_none_kinds_accepted() {
        // None: older vLLM without spec_kind — backward compat
        assert!(is_main_attention_kind(None));
        // Unknown future kind — forward compat
        assert!(is_main_attention_kind(Some("FutureAttentionType")));
    }

    // -----------------------------------------------------------------------
    // VllmEventMap normalize — filtering by spec_kind
    // -----------------------------------------------------------------------

    /// Helper: build a BlockStored msgpack map and deserialize + normalize.
    fn normalize_block_stored(
        kind: Option<&str>,
        token_ids: Vec<i64>,
        block_size: u32,
        block_hashes: Vec<u64>,
    ) -> VllmEvent {
        let mut map = serde_json::Map::new();
        map.insert("type".into(), "BlockStored".into());
        map.insert(
            "block_hashes".into(),
            serde_json::to_value(&block_hashes).unwrap(),
        );
        map.insert(
            "token_ids".into(),
            serde_json::to_value(&token_ids).unwrap(),
        );
        map.insert("block_size".into(), serde_json::json!(block_size));
        if let Some(k) = kind {
            map.insert("kv_cache_spec_kind".into(), k.into());
        }
        let packed = rmp_serde::to_vec(&map).unwrap();
        let parsed: VllmEventMap = from_slice(&packed).unwrap();
        parsed.normalize()
    }

    #[test]
    fn test_vllm_block_stored_with_full_attention_is_accepted() {
        let ev = normalize_block_stored(Some("FullAttention"), vec![1, 2, 3, 4], 4, vec![100]);
        assert!(matches!(ev, VllmEvent::BlockStored { .. }));
    }

    #[test]
    fn test_vllm_block_stored_with_mla_attention_is_accepted() {
        let ev = normalize_block_stored(Some("MlaAttention"), vec![1, 2, 3, 4], 4, vec![200]);
        assert!(matches!(ev, VllmEvent::BlockStored { .. }));
    }

    #[test]
    fn test_vllm_block_stored_with_sliding_window_is_filtered() {
        let ev = normalize_block_stored(Some("SlidingWindow"), vec![1, 2, 3, 4], 4, vec![300]);
        assert!(matches!(ev, VllmEvent::Ignored));
    }

    #[test]
    fn test_vllm_block_stored_with_mamba_is_filtered() {
        let ev = normalize_block_stored(Some("Mamba"), vec![1, 2], 2, vec![400]);
        assert!(matches!(ev, VllmEvent::Ignored));
    }

    #[test]
    fn test_vllm_block_stored_without_spec_kind_is_accepted() {
        // Backward compat: older vLLM without the field
        let ev = normalize_block_stored(
            None,
            vec![10, 20, 30, 40, 50, 60, 70, 80],
            4,
            vec![500, 600],
        );
        assert!(matches!(ev, VllmEvent::BlockStored { .. }));
    }

    #[test]
    fn test_vllm_block_removed_with_swa_is_filtered() {
        let mut map = serde_json::Map::new();
        map.insert("type".into(), "BlockRemoved".into());
        map.insert("block_hashes".into(), serde_json::json!([100, 200]));
        map.insert("kv_cache_spec_kind".into(), "SlidingWindow".into());
        let packed = rmp_serde::to_vec(&map).unwrap();
        let parsed: VllmEventMap = from_slice(&packed).unwrap();
        assert!(matches!(parsed.normalize(), VllmEvent::Ignored));
    }

    #[test]
    fn test_vllm_all_blocks_cleared_always_accepted() {
        // AllBlocksCleared should pass even with non-main spec_kind
        // because it clears everything, not just one group.
        let mut map = serde_json::Map::new();
        map.insert("type".into(), "AllBlocksCleared".into());
        map.insert("kv_cache_spec_kind".into(), "SlidingWindow".into());
        let packed = rmp_serde::to_vec(&map).unwrap();
        let parsed: VllmEventMap = from_slice(&packed).unwrap();
        assert!(matches!(parsed.normalize(), VllmEvent::AllBlocksCleared));
    }

    // -----------------------------------------------------------------------
    // VllmEventMap normalize — correct field extraction
    // -----------------------------------------------------------------------

    #[test]
    fn test_vllm_block_stored_extracts_fields_correctly() {
        let ev = normalize_block_stored(
            Some("FullAttention"),
            vec![1, 2, 3, 4, 5, 6, 7, 8],
            4,
            vec![0xAA, 0xBB],
        );
        match ev {
            VllmEvent::BlockStored {
                block_hashes,
                token_ids,
                block_size,
                ..
            } => {
                assert_eq!(block_hashes, vec![0xAA, 0xBB]);
                assert_eq!(token_ids, vec![1, 2, 3, 4, 5, 6, 7, 8]);
                assert_eq!(block_size, 4);
            }
            _ => panic!("expected BlockStored"),
        }
    }

    #[test]
    fn test_vllm_block_removed_extracts_hashes() {
        let mut map = serde_json::Map::new();
        map.insert("type".into(), "BlockRemoved".into());
        map.insert("block_hashes".into(), serde_json::json!([0xDEAD, 0xBEEF]));
        map.insert("medium".into(), "cpu".into());
        let packed = rmp_serde::to_vec(&map).unwrap();
        let parsed: VllmEventMap = from_slice(&packed).unwrap();
        match parsed.normalize() {
            VllmEvent::BlockRemoved {
                block_hashes,
                medium,
                ..
            } => {
                assert_eq!(block_hashes, vec![0xDEAD, 0xBEEF]);
                assert_eq!(medium.unwrap(), "cpu");
            }
            _ => panic!("expected BlockRemoved"),
        }
    }

    // -----------------------------------------------------------------------
    // vLLM batch parsing
    // -----------------------------------------------------------------------

    fn make_vllm_block_stored_payload(
        kind: Option<&str>,
        block_hashes: Vec<u64>,
        token_ids: Vec<i64>,
        block_size: u32,
    ) -> Vec<u8> {
        let mut event = serde_json::Map::new();
        event.insert("type".into(), "BlockStored".into());
        event.insert(
            "block_hashes".into(),
            serde_json::to_value(&block_hashes).unwrap(),
        );
        event.insert(
            "token_ids".into(),
            serde_json::to_value(&token_ids).unwrap(),
        );
        event.insert("block_size".into(), serde_json::json!(block_size));
        if let Some(k) = kind {
            event.insert("kv_cache_spec_kind".into(), k.into());
        }
        // KVEventBatch: [ts: f64, events: [...], dp_rank: int|null]
        let batch = serde_json::json!([1.0, [event], null]);
        rmp_serde::to_vec(&batch).unwrap()
    }

    #[test]
    fn test_parse_vllm_batch_format_a() {
        let payload =
            make_vllm_block_stored_payload(Some("FullAttention"), vec![100], vec![1, 2, 3, 4], 4);
        let (events, dp_rank) = parse_vllm_batch(&payload).unwrap();
        assert_eq!(dp_rank, 0);
        assert_eq!(events.len(), 1);
        assert!(matches!(events[0], VllmEvent::BlockStored { .. }));
    }

    #[test]
    fn test_parse_vllm_batch_filters_swa_events() {
        let payload =
            make_vllm_block_stored_payload(Some("SlidingWindow"), vec![200], vec![5, 6, 7, 8], 4);
        let (events, _) = parse_vllm_batch(&payload).unwrap();
        assert_eq!(events.len(), 1);
        assert!(matches!(events[0], VllmEvent::Ignored));
    }

    #[test]
    fn test_parse_vllm_bare_event() {
        let mut event = serde_json::Map::new();
        event.insert("type".into(), "BlockRemoved".into());
        event.insert("block_hashes".into(), serde_json::json!([42]));
        let packed = rmp_serde::to_vec(&event).unwrap();

        let ev = parse_vllm_bare(&packed).unwrap();
        assert!(matches!(ev, VllmEvent::BlockRemoved { .. }));
    }

    #[test]
    fn test_parse_vllm_bare_filters_non_main() {
        let mut event = serde_json::Map::new();
        event.insert("type".into(), "BlockStored".into());
        event.insert("block_hashes".into(), serde_json::json!([99]));
        event.insert("token_ids".into(), serde_json::json!([1, 2]));
        event.insert("block_size".into(), serde_json::json!(2));
        event.insert("kv_cache_spec_kind".into(), "Mamba".into());
        let packed = rmp_serde::to_vec(&event).unwrap();

        let ev = parse_vllm_bare(&packed).unwrap();
        assert!(matches!(ev, VllmEvent::Ignored));
    }

    #[test]
    fn test_parse_vllm_bare_unknown_type_is_ignored() {
        let mut event = serde_json::Map::new();
        event.insert("type".into(), "SomeFutureEvent".into());
        let packed = rmp_serde::to_vec(&event).unwrap();

        let ev = parse_vllm_bare(&packed).unwrap();
        assert!(matches!(ev, VllmEvent::Ignored));
    }

    // -----------------------------------------------------------------------
    // apply_vllm_event — tokens_hash computation
    // -----------------------------------------------------------------------

    #[test]
    fn test_apply_vllm_block_stored_computes_tokens_hash() {
        use crate::hashing::compute_block_hash_for_seq;
        use crate::indexer::Indexer;

        let indexer = Indexer::new();
        let token_ids = vec![1i64, 2, 3, 4, 5, 6, 7, 8];
        let block_size = 4u32;

        // Pre-compute expected XXH3 hashes
        let expected = compute_block_hash_for_seq(&token_ids, block_size);

        let event = VllmEvent::BlockStored {
            block_hashes: vec![0xAAAA, 0xBBBB],
            parent_block_hash: None,
            token_ids: token_ids.clone(),
            block_size,
            medium: Some("xpu".into()),
            group_idx: None,
        };

        let result = apply_vllm_event(
            &indexer,
            &event,
            "test-model",
            "test-tenant",
            "test-backend",
            0,
            0,
            &[StorageMedium::Xpu],
            MatchMode::None,
            &None,
        );

        assert!(result.is_ok());

        // Verify the tree has correct tokens_hash values
        let entry = indexer.get_or_create("test-model", "test-tenant");
        let lookups = entry.lookups.read();
        // Should have one worker entry with lookup entries
        let wk = WorkerKey {
            instance_id: "test-backend".into(),
            backend_id: "test-backend".into(),
            dp_rank: 0,
            medium: StorageMedium::Xpu,
        };
        let lookup = lookups.get(&wk).expect("worker should exist");
        // 2 SHA256 hashes → 2 lookup entries
        assert_eq!(lookup.len(), 2);

        // Verify tokens_hash values match pre-computed XXH3 hashes
        for block in lookup.values() {
            let guard = block.read();
            // Each stored block should have its block_hash (SHA256) set
            assert!(guard.block_hash.is_some());
        }

        // Query via find_matches should match (tokens_hash == query hash)
        let scores = entry.find_matches(&token_ids, block_size);
        assert!(
            !scores.scores.is_empty(),
            "query should match stored blocks"
        );
    }

    // -----------------------------------------------------------------------
    // Non-HBM event caching → pool backend matching (two-phase)
    // -----------------------------------------------------------------------

    #[test]
    fn test_non_hbm_event_cached_not_in_tree() {
        use crate::indexer::Indexer;

        let indexer = Indexer::new();
        let token_ids = vec![1i64, 2, 3, 4];
        let block_size = 4u32;

        // Phase 1: engine offloads to CPU — should be cached, not in tree.
        let event = VllmEvent::BlockStored {
            block_hashes: vec![0xABCD],
            parent_block_hash: None,
            token_ids: token_ids.clone(),
            block_size,
            medium: Some("cpu".into()),
            group_idx: None,
        };
        apply_vllm_event(
            &indexer,
            &event,
            "test-model",
            "test-tenant",
            "test-backend",
            0,
            0,
            &[StorageMedium::Cpu],
            MatchMode::None,
            &None,
        )
        .unwrap();

        // Cache should have the entry.
        let entry = indexer.get_or_create("test-model", "test-tenant");
        assert!(entry.lookup_cached_tokens_hash(0xABCD).is_some());

        // Tree should NOT have the block (not inserted for non-HBM).
        let scores = entry.find_matches(&token_ids, block_size);
        assert!(
            scores.scores.is_empty(),
            "non-HBM events should not be inserted into tree"
        );
    }

    #[test]
    fn test_pool_backend_store_matches_cached_block() {
        use crate::indexer::Indexer;

        let indexer = Indexer::new();
        let token_ids = vec![1i64, 2, 3, 4];
        let block_size = 4u32;

        // Phase 1: engine offloads CPU block.
        let engine_event = VllmEvent::BlockStored {
            block_hashes: vec![0xBEEF],
            parent_block_hash: None,
            token_ids: token_ids.clone(),
            block_size,
            medium: Some("cpu".into()),
            group_idx: None,
        };
        apply_vllm_event(
            &indexer,
            &engine_event,
            "test-model",
            "test-tenant",
            "test-backend",
            0,
            0,
            &[StorageMedium::Cpu],
            MatchMode::None,
            &None,
        )
        .unwrap();

        // Pre-compute expected XXH3 hash.
        let expected_hashes = compute_block_hash_for_seq(&token_ids, block_size);

        // Phase 2: pool backend confirms placement — insert into tree.
        let zmq_event = ZmqEventMap {
            event_id: 0,
            timestamp: None,
            event_type: Some("stored".into()),
            legacy_type: None,
            model_name: Some("test-model".into()),
            tenant_id: Some("test-tenant".into()),
            backend_id: Some("test-pool".into()),
            medium: Some("cpu".into()),
            dp_rank: Some(0),
            seq_hashes: Some(vec![FlexHash(0xBEEF)]),
            block_hashes: None,
        };
        apply_zmq_event(
            &indexer,
            &zmq_event,
            "test-model",
            "test-tenant",
            "test-pool",
            0,
            0,
            &[StorageMedium::Cpu],
            MatchMode::None,
            &None,
        )
        .unwrap();

        // Tree should now have the block at the pool backend's worker key.
        let entry = indexer.get_or_create("test-model", "test-tenant");
        let scores = entry.find_matches(&token_ids, block_size);
        // The pool backend worker ("test-pool") should have a match.
        let pool_worker = WorkerKey {
            instance_id: "test-pool".into(),
            backend_id: "test-pool".into(),
            dp_rank: 0,
            medium: StorageMedium::Cpu,
        };
        assert!(
            scores.scores.contains_key(&pool_worker),
            "pool backend store should insert cached block into tree at pool worker"
        );
    }

    #[test]
    fn test_pool_backend_store_ignores_unknown_hash() {
        use crate::indexer::Indexer;

        let indexer = Indexer::new();
        let entry = indexer.get_or_create("test-model", "test-tenant");

        // Pool backend stores a block we never cached — should be silent no-op.
        let zmq_event = ZmqEventMap {
            event_id: 0,
            timestamp: None,
            event_type: Some("stored".into()),
            legacy_type: None,
            model_name: Some("test-model".into()),
            tenant_id: Some("test-tenant".into()),
            backend_id: Some("test-pool".into()),
            medium: Some("cpu".into()),
            dp_rank: Some(0),
            seq_hashes: Some(vec![FlexHash(0xDEAD)]),
            block_hashes: None,
        };
        let result = apply_zmq_event(
            &indexer,
            &zmq_event,
            "test-model",
            "test-tenant",
            "test-pool",
            0,
            0,
            &[StorageMedium::Cpu],
            MatchMode::None,
            &None,
        );
        assert!(result.is_ok());

        // Worker lookup should be empty (nothing was inserted).
        let lookups = entry.lookups.read();
        let pool_worker = WorkerKey {
            instance_id: "test-pool".into(),
            backend_id: "test-pool".into(),
            dp_rank: 0,
            medium: StorageMedium::Cpu,
        };
        assert!(lookups.get(&pool_worker).is_none());
    }

    #[test]
    fn test_pool_backend_remove_evicts_cache() {
        use crate::indexer::Indexer;

        let indexer = Indexer::new();
        let token_ids = vec![1i64, 2, 3, 4, 5, 6, 7, 8];
        let block_size = 4u32;

        // Phase 1: engine offloads CPU blocks.
        let engine_event = VllmEvent::BlockStored {
            block_hashes: vec![0xAAA, 0xBBB],
            parent_block_hash: None,
            token_ids: token_ids.clone(),
            block_size,
            medium: Some("cpu".into()),
            group_idx: None,
        };
        apply_vllm_event(
            &indexer,
            &engine_event,
            "test-model",
            "test-tenant",
            "test-backend",
            0,
            0,
            &[StorageMedium::Cpu],
            MatchMode::None,
            &None,
        )
        .unwrap();

        let entry = indexer.get_or_create("test-model", "test-tenant");

        // Cache should have both entries.
        assert!(entry.lookup_cached_tokens_hash(0xAAA).is_some());
        assert!(entry.lookup_cached_tokens_hash(0xBBB).is_some());

        // Phase 2: pool backend confirm placement.
        let store_event = ZmqEventMap {
            event_id: 1,
            timestamp: None,
            event_type: Some("stored".into()),
            legacy_type: None,
            model_name: Some("test-model".into()),
            tenant_id: Some("test-tenant".into()),
            backend_id: Some("test-pool".into()),
            medium: Some("cpu".into()),
            dp_rank: Some(0),
            seq_hashes: Some(vec![FlexHash(0xAAA), FlexHash(0xBBB)]),
            block_hashes: None,
        };
        apply_zmq_event(
            &indexer,
            &store_event,
            "test-model",
            "test-tenant",
            "test-pool",
            0,
            0,
            &[StorageMedium::Cpu],
            MatchMode::None,
            &None,
        )
        .unwrap();

        // Phase 3: pool backend removes one block.
        let remove_event = ZmqEventMap {
            event_id: 2,
            timestamp: None,
            event_type: Some("removed".into()),
            legacy_type: None,
            model_name: Some("test-model".into()),
            tenant_id: Some("test-tenant".into()),
            backend_id: Some("test-pool".into()),
            medium: Some("cpu".into()),
            dp_rank: Some(0),
            seq_hashes: Some(vec![FlexHash(0xAAA)]),
            block_hashes: None,
        };
        apply_zmq_event(
            &indexer,
            &remove_event,
            "test-model",
            "test-tenant",
            "test-pool",
            0,
            0,
            &[StorageMedium::Cpu],
            MatchMode::None,
            &None,
        )
        .unwrap();

        // Cache entry 0xAAA should be evicted.
        assert!(entry.lookup_cached_tokens_hash(0xAAA).is_none());
        // 0xBBB should still be cached.
        assert!(entry.lookup_cached_tokens_hash(0xBBB).is_some());
    }
}
