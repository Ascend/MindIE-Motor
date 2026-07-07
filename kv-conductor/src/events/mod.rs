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
//! 1. **Mooncake Master** (pool backend): `ZmqEventMap` with `seq_hashes`/`block_hashes`,
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
// Mooncake Master event types (pool backend ZMQ format)
// ---------------------------------------------------------------------------

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

/// A vLLM msgspec-tagged union event, sent as arrays:
/// ``["BlockStored", block_hashes, parent_hash?, token_ids, block_size, ...]``
/// because ``KVEventBatch`` uses ``array_like=True`` which propagates to
/// child structs.
///
/// Field order (same as vLLM's ``BlockStored`` struct definition):
///
/// ```text
/// [tag, block_hashes, parent_block_hash?, token_ids, block_size,
///  lora_id?, medium?, lora_name?, extra_keys?, group_idx?,
///  kv_cache_spec_kind?, kv_cache_spec_sliding_window?]
/// ```
///
/// Optional fields are OMITTED when null (msgspec ``omit_defaults=True``),
/// so array length varies. This deserializer collects remaining
/// elements as ``rmpv::Value`` and matches them by type + order.
#[derive(Debug)]
struct VllmEventMap {
    event_type: String,
    block_hashes: Option<Vec<FlexHash>>,
    parent_block_hash: Option<FlexHash>,
    token_ids: Option<Vec<i64>>,
    block_size: Option<u32>,
    medium: Option<String>,
    group_idx: Option<u32>,
    #[allow(dead_code)]
    lora_id: Option<i64>,
    #[allow(dead_code)]
    lora_name: Option<String>,
    kv_cache_spec_kind: Option<String>,
    #[allow(dead_code)]
    kv_cache_spec_sliding_window: Option<u32>,
}

impl VllmEventMap {
    fn empty(event_type: String) -> Self {
        VllmEventMap {
            event_type,
            block_hashes: None,
            parent_block_hash: None,
            token_ids: None,
            block_size: None,
            medium: None,
            group_idx: None,
            lora_id: None,
            lora_name: None,
            kv_cache_spec_kind: None,
            kv_cache_spec_sliding_window: None,
        }
    }
}

/// Deserializes the **array format** from vLLM's msgspec ``array_like`` encoding.
///
/// Uses tag-based dispatch: reads the event type string, collects remaining
/// elements as ``Vec<serde_json::Value>``, then parses each variant according
/// to its own field layout.  This is robust against ``omit_defaults=True``
/// (which skips null fields, changing array length).
impl<'de> Deserialize<'de> for VllmEventMap {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct VllmEventVisitor;
        impl<'de> serde::de::Visitor<'de> for VllmEventVisitor {
            type Value = VllmEventMap;

            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a sequence representing a vLLM KV cache event")
            }

            fn visit_seq<A>(self, mut seq: A) -> Result<VllmEventMap, A::Error>
            where
                A: serde::de::SeqAccess<'de>,
            {
                let event_type: String = seq
                    .next_element()?
                    .ok_or_else(|| serde::de::Error::invalid_length(0, &self))?;

                // Collect remaining elements as rmpv::Value so we can
                // inspect types before consuming – necessary because msgspec's
                // omit_defaults=True skips null fields, shifting positions.
                // rmpv::Value handles binary (default vLLM block hash format)
                // which serde_json::Value cannot represent.
                let mut values: Vec<rmpv::Value> = Vec::new();
                while let Some(v) = seq.next_element::<rmpv::Value>()? {
                    values.push(v);
                }

                match event_type.as_str() {
                    "BlockStored" => parse_block_stored_values(event_type, &values)
                        .map_err(serde::de::Error::custom),
                    "BlockRemoved" => parse_block_removed_values(event_type, &values)
                        .map_err(serde::de::Error::custom),
                    "AllBlocksCleared" => Ok(VllmEventMap::empty(event_type)),
                    _ => Ok(VllmEventMap::empty(event_type)),
                }
            }
        }
        deserializer.deserialize_seq(VllmEventVisitor)
    }
}

// ---------------------------------------------------------------------------
// rmpv::Value → field converters
// ---------------------------------------------------------------------------

/// Convert an `rmpv::Value` to `FlexHash`.
///
/// Handles three representations of vLLM's `ExternalBlockHash`:
/// - **Integer** — `VLLM_KV_EVENTS_USE_INT_BLOCK_HASHES=true` (truncated u64)
/// - **Binary**  — default: raw SHA256 bytes (≤8 bytes used, big-endian)
/// - **String**  — hex (``"0xABCD"``) or decimal string
fn flex_hash_from_rmpv(v: &rmpv::Value) -> Option<FlexHash> {
    match v {
        rmpv::Value::Integer(i) => i.as_u64().map(FlexHash),
        rmpv::Value::String(s) => {
            let s = s.as_str().unwrap_or("").trim();
            if let Some(hex) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
                u64::from_str_radix(hex, 16).ok().map(FlexHash)
            } else if let Ok(n) = s.parse::<u64>() {
                Some(FlexHash(n))
            } else {
                u64::from_str_radix(s, 16).ok().map(FlexHash)
            }
        }
        rmpv::Value::Binary(b) => {
            if b.len() > 8 {
                // vLLM: SHA256 truncated to u64 → last 8 bytes
                let start = b.len().saturating_sub(8);
                let mut buf = [0u8; 8];
                let usable = (b.len() - start).min(8);
                buf[8 - usable..].copy_from_slice(&b[start..start + usable]);
                Some(FlexHash(u64::from_be_bytes(buf)))
            } else {
                let mut buf = [0u8; 8];
                buf[8 - b.len()..].copy_from_slice(b);
                Some(FlexHash(u64::from_be_bytes(buf)))
            }
        }
        rmpv::Value::Nil => None,
        _ => None,
    }
}

fn flex_hashes_from_rmpv(v: &rmpv::Value) -> Option<Vec<FlexHash>> {
    v.as_array()
        .map(|arr| arr.iter().filter_map(flex_hash_from_rmpv).collect())
}

fn i64_vec_from_rmpv(v: &rmpv::Value) -> Option<Vec<i64>> {
    v.as_array()
        .map(|arr| arr.iter().filter_map(|v| v.as_i64()).collect())
}

fn u32_from_rmpv(v: &rmpv::Value) -> Option<u32> {
    v.as_u64().and_then(|n| u32::try_from(n).ok())
}

// ---------------------------------------------------------------------------
// Per-event-type parsers (operating on Vec<serde_json::Value>)
// ---------------------------------------------------------------------------

/// Parse BlockStored fields from collected rmpv values.
///
/// Expected sequence (``?`` = optional, may be omitted):
///
/// ```text
/// block_hashes (arr)  parent_hash? (int|bin|str|nil)  token_ids (arr)
/// block_size (int)  lora_id? (int|nil)  medium? (str|nil)
/// lora_name? (str|nil)  extra_keys? (arr|nil)  group_idx? (int|nil)
/// kv_cache_spec_kind? (str|nil)  kv_cache_spec_sliding_window? (int|nil)
/// ```
fn parse_block_stored_values(
    event_type: String,
    values: &[rmpv::Value],
) -> Result<VllmEventMap, String> {
    // Filter out explicit nils.  With ``omit_defaults=True`` null fields
    // are omitted from the array entirely.  Test data may include nil
    // placeholders for the old fixed-position format — strip them.
    let values: Vec<&rmpv::Value> = values.iter().filter(|v| !v.is_nil()).collect();
    let len = values.len();
    let mut p: usize = 0;

    // --- block_hashes: first array (required) ---
    let block_hashes = if p < len && matches!(values[p], rmpv::Value::Array(_)) {
        let v = flex_hashes_from_rmpv(values[p]);
        p += 1;
        v
    } else {
        None
    };

    // --- parent_block_hash: optional scalar before the token_ids array ---
    // If the next value is NOT an array it may be parent_block_hash.
    // If it IS an array, parent_hash was omitted and it's already token_ids.
    let parent_block_hash = if p < len && !matches!(values[p], rmpv::Value::Array(_)) {
        let v = flex_hash_from_rmpv(values[p]);
        p += 1;
        v
    } else {
        None
    };

    // --- token_ids: array of ints (required) ---
    let token_ids = if p < len && matches!(values[p], rmpv::Value::Array(_)) {
        let v = i64_vec_from_rmpv(values[p]);
        p += 1;
        v
    } else {
        None
    };

    // --- block_size: integer (required) ---
    let block_size = if p < len && matches!(values[p], rmpv::Value::Integer(_)) {
        let v = u32_from_rmpv(values[p]);
        p += 1;
        v
    } else {
        None
    };

    // --- optional tail: types follow a predictable alternation ---
    //   int(lora_id?)  str(medium?)  str(lora_name?)
    //   arr(extra_keys?)  int(group_idx?)
    //   str(spec_kind?)  int(sliding_window?)
    let lora_id = if p < len && matches!(values[p], rmpv::Value::Integer(_)) {
        let v = if let rmpv::Value::Integer(i) = values[p] {
            i.as_i64()
        } else {
            None
        };
        p += 1;
        v
    } else {
        None
    };

    let medium = if p < len && matches!(values[p], rmpv::Value::String(_)) {
        let v = if let rmpv::Value::String(s) = values[p] {
            s.as_str().map(|x| x.to_string())
        } else {
            None
        };
        p += 1;
        v
    } else {
        None
    };

    let lora_name = if p < len && matches!(values[p], rmpv::Value::String(_)) {
        let v = if let rmpv::Value::String(s) = values[p] {
            s.as_str().map(|x| x.to_string())
        } else {
            None
        };
        p += 1;
        v
    } else {
        None
    };

    // extra_keys – skip
    if p < len && matches!(values[p], rmpv::Value::Array(_)) {
        p += 1;
    }

    let group_idx = if p < len && matches!(values[p], rmpv::Value::Integer(_)) {
        let v = u32_from_rmpv(values[p]);
        p += 1;
        v
    } else {
        None
    };

    let kv_cache_spec_kind = if p < len && matches!(values[p], rmpv::Value::String(_)) {
        let v = if let rmpv::Value::String(s) = values[p] {
            s.as_str().map(|x| x.to_string())
        } else {
            None
        };
        p += 1;
        v
    } else {
        None
    };

    let kv_cache_spec_sliding_window = if p < len && matches!(values[p], rmpv::Value::Integer(_)) {
        u32_from_rmpv(values[p])
    } else {
        None
    };

    // Drain any unexpected trailing values (forward compat).

    Ok(VllmEventMap {
        event_type,
        block_hashes,
        parent_block_hash,
        token_ids,
        block_size,
        medium,
        group_idx,
        lora_id,
        lora_name,
        kv_cache_spec_kind,
        kv_cache_spec_sliding_window,
    })
}

/// Parse BlockRemoved fields from collected rmpv values.
///
/// Expected sequence (``?`` = optional):
///
/// ```text
/// block_hashes (arr)  medium? (str|nil)  group_idx? (int|nil)
/// ```
fn parse_block_removed_values(
    event_type: String,
    values: &[rmpv::Value],
) -> Result<VllmEventMap, String> {
    // Filter out explicit nils (same rationale as parse_block_stored_values).
    let values: Vec<&rmpv::Value> = values.iter().filter(|v| !v.is_nil()).collect();
    let len = values.len();
    let mut p: usize = 0;

    // --- block_hashes: array (required) ---
    let block_hashes = if p < len && matches!(values[p], rmpv::Value::Array(_)) {
        let v = flex_hashes_from_rmpv(values[p]);
        p += 1;
        v
    } else {
        None
    };

    // --- medium: optional string ---
    let medium = if p < len && matches!(values[p], rmpv::Value::String(_)) {
        let v = if let rmpv::Value::String(s) = values[p] {
            s.as_str().map(|x| x.to_string())
        } else {
            None
        };
        p += 1;
        v
    } else {
        None
    };

    // --- group_idx: optional integer (last field) ---
    let group_idx = if p < len && matches!(values[p], rmpv::Value::Integer(_)) {
        u32_from_rmpv(values[p])
    } else {
        None
    };

    Ok(VllmEventMap::with_removed(
        event_type,
        block_hashes,
        medium,
        group_idx,
    ))
}

// Extend VllmEventMap with a BlockRemoved-specific constructor.
impl VllmEventMap {
    fn with_removed(
        event_type: String,
        block_hashes: Option<Vec<FlexHash>>,
        medium: Option<String>,
        group_idx: Option<u32>,
    ) -> Self {
        VllmEventMap {
            event_type,
            block_hashes,
            parent_block_hash: None,
            token_ids: None,
            block_size: None,
            medium,
            group_idx,
            lora_id: None,
            lora_name: None,
            kv_cache_spec_kind: None,
            kv_cache_spec_sliding_window: None,
        }
    }
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
pub(crate) enum VllmEvent {
    BlockStored {
        block_hashes: Vec<u64>,
        parent_block_hash: Option<u64>,
        token_ids: Vec<i64>,
        block_size: u32,
        medium: Option<String>,
        #[allow(dead_code)]
        group_idx: Option<u32>,
    },
    BlockRemoved {
        block_hashes: Vec<u64>,
        medium: Option<String>,
        #[allow(dead_code)]
        group_idx: Option<u32>,
    },
    AllBlocksCleared,
    /// Events we don't handle (e.g. from non-main-attention groups).
    Ignored,
}

impl VllmEventMap {
    fn normalize(&self) -> VllmEvent {
        tracing::trace!(
            event_type = %self.event_type,
            num_block_hashes = self.block_hashes.as_ref().map(|v| v.len()).unwrap_or(0),
            num_token_ids = self.token_ids.as_ref().map(|v| v.len()).unwrap_or(0),
            block_size = self.block_size,
            medium = %self.medium.as_deref().unwrap_or("-"),
            spec_kind = %self.kv_cache_spec_kind.as_deref().unwrap_or("-"),
            group_idx = self.group_idx,
            "vLLM event"
        );

        // AllBlocksCleared always passes through — it clears the entire
        // cache regardless of which attention group emitted it.
        let is_cleared = self.event_type.as_str() == "AllBlocksCleared";

        // Filter out non-main attention groups (SWA, Mamba, etc.)
        // before building the event — same strategy as Dynamo kv-router.
        if !is_cleared && !is_main_attention_kind(self.kv_cache_spec_kind.as_deref()) {
            tracing::trace!(spec_kind = %self.kv_cache_spec_kind.as_deref().unwrap_or("-"), "vLLM event filtered (non-main attention)");
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
    subscriber_dp_rank: u32,
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
        tracing::debug!(
            event_type,
            backend_id = zmq_event.backend_id.as_deref().unwrap_or(backend_id),
            dp_rank = zmq_event.dp_rank.unwrap_or(subscriber_dp_rank),
            "ZMQ event has no seq_hashes or block_hashes — skipping"
        );
        return Ok(());
    }

    let target_media: Vec<StorageMedium> = if let Some(ref m) = zmq_event.medium {
        vec![StorageMedium::parse(m)]
    } else {
        default_media.to_vec()
    };

    let be_id = zmq_event.backend_id.as_deref().unwrap_or(backend_id);
    let dp_rank = zmq_event.dp_rank.unwrap_or(subscriber_dp_rank);
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
                    entry.lookup_cached_tokens_hash(h.0).map(|tokens_hash| {
                        // Evict cache entry after lookup to prevent unbounded
                        // growth of non_hbm_cache over the service lifetime.
                        entry.evict_cached_block(h.0);
                        KvCacheStoredBlockData {
                            block_hash: h.0,
                            tokens_hash,
                        }
                    })
                })
                .collect();

            if blocks.is_empty() {
                tracing::debug!(
                    model = %mn, tenant = %tid,
                    event_type,
                    total = seq_hashes.len(),
                    "stored event has no matching cached blocks — skipping"
                );
            } else {
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

            if cached_hashes.is_empty() {
                tracing::debug!(
                    model = %mn, tenant = %tid,
                    event_type,
                    total = seq_hashes.len(),
                    "removed event has no matching cached blocks — skipping"
                );
            } else {
                // Also pass through the seq_hashes for the legacy lookup path.
                // The radix tree's apply_remove checks the WorkerLookup for
                // ExternalSequenceBlockHash → node mapping.
                let block_hashes: Vec<u64> = seq_hashes.iter().map(|h| h.0).collect();
                entry.apply_event(worker, &KvCacheEventData::Removed { block_hashes })?;
            }
        } else if is_cleared {
            entry.apply_event(worker, &KvCacheEventData::Cleared)?;
        } else {
            tracing::debug!(
                event_type,
                model = %mn,
                backend_id = %be_id,
                dp_rank,
                "ZMQ event type not recognized (neither stored/removed/cleared) — skipping"
            );
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
    registered_block_size: u32,
) -> Result<(), KvConductorError> {
    match event {
        VllmEvent::BlockStored {
            block_hashes,
            parent_block_hash,
            token_ids,
            block_size,
            medium,
            group_idx: _,
        } => {
            // Filter out non-main attention groups when spec_kind is absent.
            // DeepSeek V4 Flash sends multiple groups (MLA, SWA) with different
            // block_sizes but no kv_cache_spec_kind field — use the registered
            // block_size as the discriminator.
            if *block_size != 0 && *block_size != registered_block_size {
                tracing::trace!(
                    %backend_id, dp = subscriber_dp_rank,
                    block_size,
                    registered = registered_block_size,
                    "vLLM BlockStored filtered (non-matching block_size)"
                );
                return Ok(());
            }
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
                    parent_hash: *parent_block_hash,
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
/// [ts: f64|int, events: [...], dp_rank: int|null]
/// ```
///
/// The timestamp field is ignored (we only need the events and dp_rank).
/// Using `IgnoredAny` accepts both `f64` and `u64`/`i64` timestamps, which
/// varies across Python msgpack implementations (msgspec uses f64 for
/// `float`, but some backends emit integer timestamps).
///
/// Both `[ts, events, dp_rank]` and `[ts, dp_rank, events]` orderings are
/// tried to be robust against msgspec / version variations.
pub(crate) fn parse_vllm_batch(payload: &[u8]) -> Option<(Vec<VllmEvent>, u32)> {
    // Format A: [ts, events: [...], dp_rank: int|null]
    if let Ok((_ts, events, dp_rank)) =
        rmp_serde::from_slice::<(serde::de::IgnoredAny, Vec<VllmEventMap>, Option<i32>)>(payload)
    {
        let parsed: Vec<VllmEvent> = events.iter().map(|e| e.normalize()).collect();
        tracing::trace!(
            num_events = parsed.len(),
            dp_rank = dp_rank.unwrap_or(0),
            "vLLM batch parsed (format A: [ts, events, dp_rank])"
        );
        return Some((parsed, dp_rank.unwrap_or(0) as u32));
    }
    // Format B: [ts, dp_rank: int|null, events: [...]]
    if let Ok((_ts, dp_rank, events)) =
        rmp_serde::from_slice::<(serde::de::IgnoredAny, Option<i32>, Vec<VllmEventMap>)>(payload)
    {
        let parsed: Vec<VllmEvent> = events.iter().map(|e| e.normalize()).collect();
        tracing::trace!(
            num_events = parsed.len(),
            dp_rank = dp_rank.unwrap_or(0),
            "vLLM batch parsed (format B: [ts, dp_rank, events])"
        );
        return Some((parsed, dp_rank.unwrap_or(0) as u32));
    }
    None
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

#[cfg(test)]
mod tests;
