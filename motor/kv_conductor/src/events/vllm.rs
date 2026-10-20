// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.
//
// Portions: the main-attention allow/deny kind set used by
// `is_main_attention_kind` is derived from NVIDIA Dynamo kv-router
// `lib/kv-router/src/zmq_wire/filter.rs` (Apache-2.0).
// Copyright (c) 2024-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// See ../THIRD_PARTY_NOTICES.md and ../licenses/Apache-2.0.txt.
// The msgspec/JSON visitor and event-application logic in this file are
// Huawei original work under Mulan PSL v2.

//! vLLM-native event types, parsing, and application logic.
//!
//! Handles msgspec map and legacy array events with tag-based dispatch,
//! attention-group filtering, and two-phase offload/pool insertion.
//! Attention-kind filtering policy: see `THIRD_PARTY_NOTICES.md`.

use std::collections::HashMap;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

use parking_lot::Mutex;
use serde::Deserialize;

use crate::backend::MatchMode;
use crate::error::KvConductorError;
use crate::hashing::compute_block_hash_for_seq;
use crate::indexer::Indexer;
use crate::protocols::*;

use super::flex_hash::FlexHash;
use super::helpers::{resolve_medium, resolve_workers};

/// Minimum interval between repeated `block_size_mismatch` WARN lines for the
/// same `(backend_id, event_block_size, registered_block_size)` key.
const BLOCK_SIZE_MISMATCH_WARN_INTERVAL: Duration = Duration::from_secs(30);

struct BlockSizeMismatchWarnState {
    last_warn: Instant,
    /// Events dropped since the last WARN (excluding the one that triggers
    /// the next WARN).
    suppressed: u64,
}

type BlockSizeMismatchKey = (String, u32, u32);
type BlockSizeMismatchWarnMap = Mutex<HashMap<BlockSizeMismatchKey, BlockSizeMismatchWarnState>>;

fn block_size_mismatch_warn_map() -> &'static BlockSizeMismatchWarnMap {
    static MAP: OnceLock<BlockSizeMismatchWarnMap> = OnceLock::new();
    MAP.get_or_init(|| Mutex::new(HashMap::new()))
}

// ---------------------------------------------------------------------------
// vLLM-native event types (msgspec KVEventBatch wire format)
// ---------------------------------------------------------------------------

/// A vLLM msgspec-tagged event, encoded as a map with a `type` field.
/// The batch's `array_like=True` does not propagate to nested event structs.
/// Legacy tagged arrays are also accepted, using type + order matching.
#[derive(Debug)]
pub(crate) struct VllmEventMap {
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

/// Deserializes a vLLM msgspec KV event, tolerating both the tagged-array
/// (``array_like``) and tagged-map wire shapes.
///
/// msgspec's ``tag=True`` emits an event as either ``["BlockStored", ...]``
/// or ``{"type": "BlockStored", ...}`` depending on whether ``array_like``
/// was also enabled on the concrete event struct. The outer ``KVEventBatch``
/// is always an array; the inner event shape varies across vLLM versions, so
/// both forms are decoded here.
impl<'de> Deserialize<'de> for VllmEventMap {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct VllmEventVisitor;
        impl<'de> serde::de::Visitor<'de> for VllmEventVisitor {
            type Value = VllmEventMap;

            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("a tagged sequence or map representing a vLLM KV cache event")
            }

            fn visit_seq<A>(self, mut seq: A) -> Result<VllmEventMap, A::Error>
            where
                A: serde::de::SeqAccess<'de>,
            {
                let event_type: String = seq
                    .next_element()?
                    .ok_or_else(|| serde::de::Error::invalid_length(0, &self))?;

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

            fn visit_map<A>(self, mut map: A) -> Result<VllmEventMap, A::Error>
            where
                A: serde::de::MapAccess<'de>,
            {
                let mut event_type: Option<String> = None;
                let mut fields: Vec<(String, rmpv::Value)> = Vec::new();
                while let Some(key) = map.next_key::<String>()? {
                    let value: rmpv::Value = map.next_value()?;
                    if key == "type" {
                        event_type = value.as_str().map(|s| s.to_string());
                    } else {
                        fields.push((key, value));
                    }
                }
                let event_type = event_type.unwrap_or_default();
                parse_vllm_event_map(event_type, &fields).map_err(serde::de::Error::custom)
            }
        }
        deserializer.deserialize_any(VllmEventVisitor)
    }
}

// ---------------------------------------------------------------------------
// rmpv::Value → field converters
// ---------------------------------------------------------------------------

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

fn rmpv_type_name(v: &rmpv::Value) -> &'static str {
    match v {
        rmpv::Value::Nil => "nil",
        rmpv::Value::Boolean(_) => "bool",
        rmpv::Value::Integer(_) => "integer",
        rmpv::Value::F32(_) => "f32",
        rmpv::Value::F64(_) => "f64",
        rmpv::Value::String(_) => "string",
        rmpv::Value::Binary(_) => "binary",
        rmpv::Value::Array(_) => "array",
        rmpv::Value::Map(_) => "map",
        rmpv::Value::Ext(..) => "ext",
    }
}

/// Like `get(name).and_then(convert)`, but logs when the key is present and
/// the value cannot be converted — distinguishing omit-defaults from a wire
/// shape mismatch that would otherwise silently drop the event.
fn map_field_or_warn<T>(
    fields: &[(String, rmpv::Value)],
    name: &str,
    convert: impl Fn(&rmpv::Value) -> Option<T>,
) -> Option<T> {
    let (_, value) = fields.iter().find(|(k, _)| k == name)?;
    match convert(value) {
        Some(ok) => Some(ok),
        None => {
            tracing::warn!(
                field = name,
                value_type = rmpv_type_name(value),
                "vllm map field present but not convertible; treating as omitted"
            );
            None
        }
    }
}

// ---------------------------------------------------------------------------
// Per-event-type parsers
// ---------------------------------------------------------------------------

fn parse_block_stored_values(
    event_type: String,
    values: &[rmpv::Value],
) -> Result<VllmEventMap, String> {
    let values: Vec<&rmpv::Value> = values.iter().filter(|v| !v.is_nil()).collect();
    let len = values.len();
    let mut p: usize = 0;

    let block_hashes = if p < len && matches!(values[p], rmpv::Value::Array(_)) {
        let v = flex_hashes_from_rmpv(values[p]);
        p += 1;
        v
    } else {
        None
    };

    let parent_block_hash = if p < len && !matches!(values[p], rmpv::Value::Array(_)) {
        let v = flex_hash_from_rmpv(values[p]);
        p += 1;
        v
    } else {
        None
    };

    let token_ids = if p < len && matches!(values[p], rmpv::Value::Array(_)) {
        let v = i64_vec_from_rmpv(values[p]);
        p += 1;
        v
    } else {
        None
    };

    let block_size = if p < len && matches!(values[p], rmpv::Value::Integer(_)) {
        let v = u32_from_rmpv(values[p]);
        p += 1;
        v
    } else {
        None
    };

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

    if p < len && matches!(values[p], rmpv::Value::Array(_)) {
        p += 1; // extra_keys — skip
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

fn parse_block_removed_values(
    event_type: String,
    values: &[rmpv::Value],
) -> Result<VllmEventMap, String> {
    let values: Vec<&rmpv::Value> = values.iter().filter(|v| !v.is_nil()).collect();
    let len = values.len();
    let mut p: usize = 0;

    let block_hashes = if p < len && matches!(values[p], rmpv::Value::Array(_)) {
        let v = flex_hashes_from_rmpv(values[p]);
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

/// Build a [`VllmEventMap`] from a tagged-map event's ``(key, value)`` pairs.
///
/// The map form carries the same logical fields as the array form but names
/// them explicitly (``block_hashes``, ``parent_block_hash``, ``token_ids``,
/// ``block_size``, ``medium``, ``group_idx``, ``kv_cache_spec_kind``). Unknown
/// keys are ignored so future vLLM fields (e.g. ``locality``, ``extra_keys``)
/// do not break decoding.
fn parse_vllm_event_map(
    event_type: String,
    fields: &[(String, rmpv::Value)],
) -> Result<VllmEventMap, String> {
    let get = |name: &str| -> Option<&rmpv::Value> {
        fields.iter().find(|(k, _)| k == name).map(|(_, v)| v)
    };

    match event_type.as_str() {
        "BlockStored" => {
            // Known fields that gate ingestion: warn on present-but-bad types
            // so wire-format drift is visible (missing keys stay silent /
            // omit_defaults).
            let block_hashes = map_field_or_warn(fields, "block_hashes", flex_hashes_from_rmpv);
            let parent_block_hash = get("parent_block_hash").and_then(flex_hash_from_rmpv);
            let token_ids = map_field_or_warn(fields, "token_ids", i64_vec_from_rmpv);
            let block_size = map_field_or_warn(fields, "block_size", u32_from_rmpv);
            let medium = get("medium").and_then(|v| v.as_str().map(|s| s.to_string()));
            let group_idx = get("group_idx").and_then(u32_from_rmpv);
            let kv_cache_spec_kind =
                get("kv_cache_spec_kind").and_then(|v| v.as_str().map(|s| s.to_string()));

            Ok(VllmEventMap {
                event_type,
                block_hashes,
                parent_block_hash,
                token_ids,
                block_size,
                medium,
                group_idx,
                lora_id: None,
                lora_name: None,
                kv_cache_spec_kind,
                kv_cache_spec_sliding_window: None,
            })
        }
        "BlockRemoved" => {
            let block_hashes = map_field_or_warn(fields, "block_hashes", flex_hashes_from_rmpv);
            let medium = get("medium").and_then(|v| v.as_str().map(|s| s.to_string()));
            let group_idx = get("group_idx").and_then(u32_from_rmpv);

            Ok(VllmEventMap::with_removed(
                event_type,
                block_hashes,
                medium,
                group_idx,
            ))
        }
        "AllBlocksCleared" => Ok(VllmEventMap::empty(event_type)),
        _ => Ok(VllmEventMap::empty(event_type)),
    }
}

// ---------------------------------------------------------------------------
// Attention-group filter
// ---------------------------------------------------------------------------

/// Returns `true` if `kind` is a main attention type whose events should be
/// ingested.  Allow/deny kind names follow NVIDIA Dynamo kv-router
/// `zmq_wire/filter.rs` (Apache-2.0; see `THIRD_PARTY_NOTICES.md`): only
/// `FullAttention`, `MlaAttention`, and `SinkFullAttention` qualify.
/// Events with no `kv_cache_spec_kind` (older vLLM versions) are kept for
/// backward compat.
///
/// Matching is case-insensitive and ignores underscores so both PascalCase
/// (`MlaAttention`) and vLLM wire snake_case (`mla_attention`) are matched;
/// denied kinds (e.g. `sliding_window_mla`) are filtered out.
pub(crate) fn is_main_attention_kind(kind: Option<&str>) -> bool {
    let Some(kind) = kind else {
        return true;
    };
    // Normalize: lowercase + strip underscores → "MlaAttention"/"mla_attention"
    // both become "mlaattention".
    let normalized: String = kind
        .chars()
        .filter(|c| *c != '_')
        .flat_map(|c| c.to_lowercase())
        .collect();
    match normalized.as_str() {
        "fullattention" | "mlaattention" | "sinkfullattention" => true,
        // Non-main groups (SWA / Mamba / local / encoder / cross).
        // `slidingwindowmla` covers wire form `sliding_window_mla`.
        "slidingwindow"
        | "slidingwindowmla"
        | "mamba"
        | "chunkedlocalattention"
        | "encoderonlyattention"
        | "crossattention" => false,
        // Unknown future kinds — forward compat (same as before).
        _ => true,
    }
}

// ---------------------------------------------------------------------------
// VllmEvent — normalized parsed event
// ---------------------------------------------------------------------------

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
    pub(super) fn normalize(&self) -> VllmEvent {
        tracing::trace!(
            event_type = %self.event_type,
            num_block_hashes = self.block_hashes.as_ref().map(|v| v.len()).unwrap_or(0),
            num_token_ids = self.token_ids.as_ref().map(|v| v.len()).unwrap_or(0),
            block_size = self.block_size,
            medium = %StorageMedium::parse(self.medium.as_deref().unwrap_or("npu")).log_str(),
            spec_kind = %self.kv_cache_spec_kind.as_deref().unwrap_or("-"),
            group_idx = self.group_idx,
            "kv_event event_parsed backend=vllm"
        );

        let is_cleared = self.event_type.as_str() == "AllBlocksCleared";

        if !is_cleared && !is_main_attention_kind(self.kv_cache_spec_kind.as_deref()) {
            tracing::trace!(
                spec_kind = %self.kv_cache_spec_kind.as_deref().unwrap_or("-"),
                reason = "non_main_attention",
                "kv_event dropped"
            );
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
    match rmp_serde::from_slice::<(serde::de::IgnoredAny, Vec<VllmEventMap>, Option<i32>)>(payload)
    {
        Ok((_ts, events, dp_rank)) => {
            let parsed: Vec<VllmEvent> = events.iter().map(|e| e.normalize()).collect();
            tracing::debug!(
                num_events = parsed.len(),
                dp_rank = dp_rank.unwrap_or(0),
                layout = "events,dp_rank",
                "kv_event parsed backend=vllm"
            );
            return Some((parsed, dp_rank.unwrap_or(0) as u32));
        }
        Err(e) => {
            tracing::trace!(error = %e, layout = "events,dp_rank", "kv_event parse_failed backend=vllm")
        }
    }
    // Format B: [ts, dp_rank: int|null, events: [...]]
    match rmp_serde::from_slice::<(serde::de::IgnoredAny, Option<i32>, Vec<VllmEventMap>)>(payload)
    {
        Ok((_ts, dp_rank, events)) => {
            let parsed: Vec<VllmEvent> = events.iter().map(|e| e.normalize()).collect();
            tracing::debug!(
                num_events = parsed.len(),
                dp_rank = dp_rank.unwrap_or(0),
                layout = "dp_rank,events",
                "kv_event parsed backend=vllm"
            );
            return Some((parsed, dp_rank.unwrap_or(0) as u32));
        }
        Err(e) => {
            tracing::trace!(error = %e, layout = "dp_rank,events", "kv_event parse_failed backend=vllm")
        }
    }
    None
}

// ---------------------------------------------------------------------------
// vLLM event application
// ---------------------------------------------------------------------------

/// Emit a rate-limited WARN for `block_size_mismatch` drops.
///
/// Misconfigured deployments can drop every BlockStored event; without
/// rate limiting that floods logs. Warn on first occurrence of each
/// `(backend_id, block_size, registered)` key, then at most once per
/// [`BLOCK_SIZE_MISMATCH_WARN_INTERVAL`], including how many events were
/// suppressed in between.
fn warn_block_size_mismatch(backend_id: &str, dp: u32, block_size: u32, registered: u32) {
    let key = (backend_id.to_string(), block_size, registered);
    let now = Instant::now();
    let suppressed_to_report = {
        let mut map = block_size_mismatch_warn_map().lock();
        match map.get_mut(&key) {
            Some(state) => {
                if now.duration_since(state.last_warn) >= BLOCK_SIZE_MISMATCH_WARN_INTERVAL {
                    let suppressed = state.suppressed;
                    state.last_warn = now;
                    state.suppressed = 0;
                    Some(suppressed)
                } else {
                    state.suppressed = state.suppressed.saturating_add(1);
                    None
                }
            }
            None => {
                map.insert(
                    key,
                    BlockSizeMismatchWarnState {
                        last_warn: now,
                        suppressed: 0,
                    },
                );
                Some(0)
            }
        }
    };

    if let Some(suppressed) = suppressed_to_report {
        tracing::warn!(
            %backend_id,
            dp,
            block_size,
            registered,
            suppressed,
            reason = "block_size_mismatch",
            "kv_event dropped backend=vllm"
        );
    }
}

/// Apply a parsed vLLM-native event to the indexer.
///
/// vLLM `BlockStored` events carry `token_ids` and `block_size`, allowing
/// us to re-compute `tokens_hash` (XXH3 content hash).  The behaviour
/// depends on the storage medium:
///
/// - **HBM** (NPU): insert directly into the radix tree.
/// - **Non-HBM** (CPU/DISK): bidirectional matching — cache the
///   `block_hash → tokens_hash` mapping and check for pending pool events
///   that arrived earlier.  If a match is found the block enters the tree
///   immediately; otherwise it waits for the pool confirmation.
///
/// This two-phase approach is required because the pool backend may place
/// the block on a different node than the engine that offloaded it — the
/// engine's offloading event tells us *what* was offloaded, and the pool
/// backend's event tells us *where* it was placed.
///
/// After the first pool confirmation, `(tokens_hash, parent_hash)` is
/// retained so a later pool medium (e.g. Disk SSD offload) can reuse
/// content without another engine event.
#[allow(clippy::too_many_arguments)]
pub(crate) fn apply_vllm_event(
    indexer: &Indexer,
    event: &VllmEvent,
    model_name: &str,
    tenant_id: &str,
    backend_id: &str,
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
            // Hybrid KV groups publish their *native* block_size (DeepSeek-V4
            // MLA = 128), which can differ from the engine `--block-size` /
            // LCM page size (32). Conductor `kv_conductor_config.block_size`
            // must match the main-attention event grain, not the scheduler LCM.
            if *block_size != 0 && *block_size != registered_block_size {
                warn_block_size_mismatch(
                    backend_id,
                    subscriber_dp_rank,
                    *block_size,
                    registered_block_size,
                );
                return Ok(());
            }
            if block_hashes.is_empty() {
                return Ok(());
            }

            let event_medium = medium.as_deref().unwrap_or("npu");
            let is_non_hbm = !StorageMedium::is_hbm_key(event_medium);

            let computed_hashes: Vec<u64> = if token_ids.is_empty() || *block_size == 0 {
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
                // Walk the offload chain so each block carries its own
                // `parent_hash`: the first block's parent is the event's
                // `parent_block_hash`, and each subsequent block's parent is
                // the immediately preceding block in this same chain. This
                // preserves continuation-edge semantics across the two-phase
                // offload/pool confirmation protocol.
                let mut parent = *parent_block_hash;
                let mut triples: Vec<(u64, u64, Option<u64>)> = Vec::with_capacity(num);
                for i in 0..num {
                    triples.push((block_hashes[i], computed_hashes[i], parent));
                    parent = Some(block_hashes[i]);
                }

                let preview_hashes: Vec<u64> = triples.iter().take(4).map(|p| p.0).collect();
                tracing::trace!(
                    model = %model_name, tenant = %tenant_id,
                    num = triples.len(),
                    ?preview_hashes,
                    medium = %StorageMedium::parse(event_medium).log_str(),
                    "kv_event offload_ingesting backend=vllm"
                );

                let matched = entry.ingest_offload_blocks(&triples);
                let total_matched: usize = matched.values().map(|v| v.len()).sum();

                if !matched.is_empty() {
                    tracing::info!(
                        model = %model_name, tenant = %tenant_id,
                        num_blocks = total_matched,
                        num_workers = matched.len(),
                        medium = %StorageMedium::parse(event_medium).log_str(),
                        "kv_event matched backend=vllm"
                    );
                    // Apply one `Stored` event per block, each with its own
                    // `parent_hash`, instead of batching them under
                    // `parent_hash: None` — batching would silently drop
                    // continuation-edge chaining between blocks.
                    for (worker, blocks) in matched {
                        for (parent_hash, block) in blocks {
                            let store_data = KvCacheStoreData {
                                parent_hash,
                                start_position: None,
                                blocks: vec![block],
                            };
                            entry.apply_event(&worker, &KvCacheEventData::Stored(store_data))?;
                        }
                    }
                }

                let cached = num.saturating_sub(total_matched);
                if cached > 0 {
                    tracing::debug!(
                        model = %model_name, tenant = %tenant_id,
                        num_blocks = cached,
                        medium = %StorageMedium::parse(event_medium).log_str(),
                        "kv_event offload_cached backend=vllm"
                    );
                }
            } else {
                tracing::trace!(
                    model = %model_name, tenant = %tenant_id,
                    num_blocks = num,
                    medium = %StorageMedium::parse(event_medium).log_str(),
                    "kv_event applied backend=vllm"
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
                let tree_hashes = entry.evict_pending_blocks(block_hashes, worker);
                if !tree_hashes.is_empty() {
                    entry.apply_event(
                        worker,
                        &KvCacheEventData::Removed {
                            block_hashes: tree_hashes,
                        },
                    )?;
                }
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
                entry.remove_pending_worker(worker);
            }
        }
        VllmEvent::Ignored => { /* skip */ }
    }
    Ok(())
}
