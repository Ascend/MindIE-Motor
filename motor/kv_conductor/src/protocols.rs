// SPDX-FileCopyrightText: Copyright (c) 2024-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-FileCopyrightText: Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
//
// Portions of this file (hash newtypes, KV cache event / store payloads, and
// overlap-match result types) are a Derivative Work of NVIDIA Dynamo
// kv-router lib/kv-router/src/protocols.rs, licensed under Apache-2.0.
// Upstream project: https://github.com/ai-dynamo/dynamo
// SPDX-License-Identifier: Apache-2.0
//
// You may obtain a copy of the Apache License at:
//   http://www.apache.org/licenses/LICENSE-2.0
// Local copy: licenses/Apache-2.0.txt
// Attribution: THIRD_PARTY_NOTICES.md
//
// MindIE HTTP API types (register / query / health, WorkerKey, StorageMedium
// parsing, HbmIpIndex, etc.) and other Huawei modifications are also
// available under Mulan PSL v2 (http://license.coscl.org.cn/MulanPSL2).
// Redistribution of the Dynamo-derived portions must still comply with
// Apache License 2.0.

//! Protocol types for the KV conductor service.
//!
//! Hash / KV-event payload types are derived from NVIDIA Dynamo kv-router
//! `lib/kv-router/src/protocols.rs` (Apache-2.0); see `THIRD_PARTY_NOTICES.md`.
//! HTTP API types are the MindIE conductor contract used by Python
//! `ConductorApiClient` in `motor/coordinator/api_client/`.

use std::collections::HashMap;
use std::sync::Arc;

use parking_lot::RwLock as ParkingRwLock;
use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};
use tracing;

use crate::hashing::compute_block_hash_for_seq;

// ---------------------------------------------------------------------------
// Shared types
// ---------------------------------------------------------------------------

/// Shared registry index for Mooncake auto-attach.
/// Maps node IP → list of (instance_id, dp_rank) for HBM-registered endpoints.
/// When a Mooncake pool subscriber receives an event with `backend_id=<ip>`,
/// the event is applied to every DP whose HBM endpoint resolves to that IP.
pub type HbmIpIndex = Arc<ParkingRwLock<HashMap<String, Vec<(String, u32)>>>>;

// ---------------------------------------------------------------------------
// Hash types
// ---------------------------------------------------------------------------

/// XXH3-based hash of a block's token content. Used as the primary radix-tree key.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Ord, PartialOrd)]
pub struct LocalBlockHash(pub u64);

/// Engine-provided rolling sequence hash (includes parent hash context).
/// Used in per-worker reverse-lookup tables.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, Ord, PartialOrd)]
pub struct SequenceBlockHash(pub u64);

impl Serialize for LocalBlockHash {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: serde::Serializer,
    {
        serializer.serialize_u64(self.0)
    }
}

impl<'de> Deserialize<'de> for LocalBlockHash {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let value = u64::deserialize(deserializer)?;
        Ok(LocalBlockHash(value))
    }
}

// ---------------------------------------------------------------------------
// Storage tier / medium (RFC #1527)
// ---------------------------------------------------------------------------

/// Storage tier for KV cache blocks, following RFC #1527 `medium` values.
#[derive(
    Clone, Copy, Debug, Default, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize,
)]
#[serde(rename_all = "snake_case")]
pub enum StorageMedium {
    /// NPU HBM — from inference engine workers.
    /// Wire aliases `gpu` / `xpu` / `hbm` / `device` still parse to this variant.
    #[default]
    Npu,
    /// Host DDR / CPU pinned memory — from Mooncake master (MEMORY replica).
    Cpu,
    /// SSD / DFS / NVMe disk — from Mooncake master (DISK replica).
    Disk,
    /// Unknown or unspecified medium.
    Unknown,
}

impl StorageMedium {
    pub fn parse(s: &str) -> Self {
        match s {
            "npu" | "NPU" | "gpu" | "GPU" | "xpu" | "XPU" | "hbm" | "HBM" | "device" | "DEVICE" => {
                Self::Npu
            }
            "cpu" | "CPU" | "cpu_pinned" | "CPU_PINNED" | "host" | "HOST" | "memory" | "MEMORY" => {
                Self::Cpu
            }
            "disk" | "DISK" | "ssd" | "SSD" | "nvme" | "NVME" | "nof_ssd" | "dfs" | "DFS" => {
                Self::Disk
            }
            _ => Self::Unknown,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Npu => "NPU",
            Self::Cpu => "CPU",
            Self::Disk => "DISK",
            Self::Unknown => "UNKNOWN",
        }
    }

    /// Lowercase medium name for event logs, matching the wire values
    /// (`xpu` / `cpu` / `disk`) so `grep medium=cpu` works uniformly.
    pub fn log_str(&self) -> &'static str {
        match self {
            Self::Npu => "xpu",
            Self::Cpu => "cpu",
            Self::Disk => "disk",
            Self::Unknown => "unknown",
        }
    }

    /// Whether `s` names the device-HBM tier (npu / gpu / xpu / hbm / device).
    pub fn is_hbm_key(s: &str) -> bool {
        matches!(Self::parse(s), Self::Npu)
    }
}

// ---------------------------------------------------------------------------
// Identity types
// ---------------------------------------------------------------------------

/// Instance grouping identifier, e.g. "vllm-prefill-42".
pub type InstanceId = String;

/// Data-parallel rank within an instance.
pub type DpRank = u32;

/// Composite identity used in the radix tree worker sets.
#[derive(Clone, Debug, PartialEq, Eq, Hash, Ord, PartialOrd)]
pub struct WorkerKey {
    pub instance_id: InstanceId,
    /// RFC #1527: backend that owns the KV blocks (engine worker, Mooncake daemon, etc.).
    pub backend_id: String,
    pub dp_rank: DpRank,
    /// RFC #1527: cache medium (npu, cpu, disk).
    pub medium: StorageMedium,
}

// ---------------------------------------------------------------------------
// Registration types (matching Python ConductorApiClient)
// ---------------------------------------------------------------------------

/// POST /register request body.
#[derive(Debug, Clone, Deserialize)]
pub struct RegisterRequest {
    pub instance_id: InstanceId,
    /// Per-medium ZMQ PUB endpoints (new protocol).
    /// e.g. {"npu": "tcp://...:5557", "cpu": "tcp://...:5558"}.
    /// Multiple media may share the same endpoint URL; the conductor deduplicates.
    /// When empty, falls back to the legacy `endpoint` field.
    #[serde(default)]
    pub medium_endpoints: HashMap<String, String>,
    /// Legacy single endpoint for all media (Mooncake Master compat).
    /// When `medium_endpoints` is non-empty this is ignored.
    /// e.g. "tcp://10.0.0.1:5557"
    #[serde(default)]
    pub endpoint: Option<String>,
    #[serde(rename = "type")]
    pub engine_type: String,
    pub modelname: String,
    pub block_size: u32,
    pub dp_rank: DpRank,
    /// KV storage backend type: "Mooncake", "YuanRong", etc.
    /// Distinguishes the pooling/broadcast architecture.
    #[serde(default = "default_store_backend")]
    pub store_backend: String,
    #[serde(default)]
    pub replay_endpoint: Option<String>,
    #[serde(default = "default_tenant")]
    pub tenant_id: String,
}

fn default_store_backend() -> String {
    "Mooncake".to_string()
}

fn default_tenant() -> String {
    "default".to_string()
}

/// POST /unregister request body.
#[derive(Debug, Clone, Deserialize)]
pub struct UnregisterRequest {
    pub instance_id: InstanceId,
    #[serde(rename = "type")]
    pub engine_type: String,
    pub modelname: String,
    pub block_size: u32,
    pub dp_rank: DpRank,
    #[serde(default = "default_tenant")]
    pub tenant_id: String,
}

/// POST /query request body (matching Python client).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryRequest {
    pub model: String,
    pub block_size: u32,
    pub token_ids: Vec<i64>,
    #[serde(default = "default_tenant")]
    pub tenant_id: String,
}

/// POST /query_by_hash request body — query using pre-computed block hashes
/// instead of raw token IDs. This avoids redundant XXH3 computation when the
/// caller has already hashed the sequence.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct QueryByHashRequest {
    pub model: String,
    pub block_size: u32,
    /// Pre-computed `LocalBlockHash` values (as u64 on the wire).
    pub block_hashes: Vec<u64>,
    #[serde(default = "default_tenant")]
    pub tenant_id: String,
}

// ---------------------------------------------------------------------------
// Query response types (matching Python client expectations)
//
// Python reads:
//   rsp[tenant_id][instance_id]["longest_matched"]            (in tokens)
//   rsp[tenant_id][instance_id]["DP"][dp_rank_str]            (DpBlocks obj:
//     matched_tokens / npu_blocks / cpu_blocks / disk_blocks)
// ---------------------------------------------------------------------------

/// Per-DP matched block counts across storage media.
#[derive(Debug, Clone, Serialize, Default)]
pub struct DpBlocks {
    /// Unweighted coverage for this DP, in tokens:
    /// `(npu_blocks + cpu_blocks + disk_blocks) × block_size`.
    ///
    /// Per-medium `*_blocks` are exclusive contributions after partitioning
    /// absolute coverage ends with priority NPU > CPU > Disk (replicas of the
    /// same prefix are attributed to the highest tier only). Coordinator
    /// applies tier affinity weights when scoring.
    pub matched_tokens: u32,
    /// Exclusive HBM (NPU) matched block count.
    pub npu_blocks: u32,
    /// Exclusive CPU matched block count (beyond NPU coverage).
    pub cpu_blocks: u32,
    /// Exclusive Disk matched block count (beyond max(NPU, CPU) coverage).
    pub disk_blocks: u32,
}

/// Per-instance match data returned in query response.
#[derive(Debug, Clone, Serialize, Default)]
pub struct InstanceMatchData {
    /// Longest continuous prefix match across all DP ranks, in tokens.
    pub longest_matched: u32,
    /// Per-DP-rank matched block counts across media.
    #[serde(rename = "DP")]
    pub dp: HashMap<String, DpBlocks>,
}

/// Full query response: { tenant_id: { instance_id: InstanceMatchData } }
#[derive(Debug, Clone, Serialize, Default)]
pub struct QueryResponse {
    #[serde(flatten)]
    pub tenants: HashMap<String, HashMap<InstanceId, InstanceMatchData>>,
}

// ---------------------------------------------------------------------------
// MessagePack query codec
//
// `/query` and `/query_by_hash` accept both JSON and MessagePack bodies
// (selected by `Content-Type: application/msgpack`). The request side is
// decoded with `rmp_serde` straight into `QueryRequest` / `QueryByHashRequest`
// (neither uses serde `flatten`, so the generic path works). The response
// side is hand-encoded below because `QueryResponse` relies on
// `#[serde(flatten)]`, which MessagePack serializers do not support.
//
// The MessagePack response mirrors the JSON wire shape exactly:
//
// ```text
// { tenant_id: { instance_id: { longest_matched, DP:
//   { rank: { matched_tokens, npu_blocks, cpu_blocks, disk_blocks } } } } }
// ```

/// Content-Type values accepted as MessagePack on the query endpoints.
pub const MSGPACK_CONTENT_TYPES: [&str; 2] = ["application/msgpack", "application/x-msgpack"];

/// True when the request `Content-Type` header selects MessagePack.
pub fn is_msgpack_content_type(headers: &axum::http::HeaderMap) -> bool {
    headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(|ct| {
            // Strip parameters such as "; charset=utf-8".
            let ct = ct
                .split(';')
                .next()
                .unwrap_or("")
                .trim()
                .to_ascii_lowercase();
            MSGPACK_CONTENT_TYPES.contains(&ct.as_str())
        })
        .unwrap_or(false)
}

/// Encode a full `/query` response into MessagePack.
///
/// Written with `rmp::encode` instead of `rmp_serde` to avoid the
/// `#[serde(flatten)]` map-merge pitfall on `QueryResponse`.
pub fn encode_query_response_msgpack(response: &QueryResponse, out: &mut Vec<u8>) {
    use rmp::encode::*;
    write_map_len(
        out,
        u32::try_from(response.tenants.len()).expect("tenants len fits u32"),
    )
    .expect("write map len");
    for (tenant, instances) in &response.tenants {
        write_str(out, tenant).expect("write tenant");
        write_map_len(
            out,
            u32::try_from(instances.len()).expect("instances len fits u32"),
        )
        .expect("write map len");
        for (instance, data) in instances {
            write_str(out, instance).expect("write instance");
            write_map_len(out, 2).expect("instance map len");
            write_str(out, "longest_matched").expect("write key");
            write_u32(out, data.longest_matched).expect("write longest_matched");
            write_str(out, "DP").expect("write key");
            write_map_len(out, u32::try_from(data.dp.len()).expect("dp len fits u32"))
                .expect("write map len");
            for (rank, blocks) in &data.dp {
                write_str(out, rank).expect("write rank");
                write_map_len(out, 4).expect("blocks map len");
                write_str(out, "matched_tokens").expect("write key");
                write_u32(out, blocks.matched_tokens).expect("write matched_tokens");
                write_str(out, "npu_blocks").expect("write key");
                write_u32(out, blocks.npu_blocks).expect("write npu_blocks");
                write_str(out, "cpu_blocks").expect("write key");
                write_u32(out, blocks.cpu_blocks).expect("write cpu_blocks");
                write_str(out, "disk_blocks").expect("write key");
                write_u32(out, blocks.disk_blocks).expect("write disk_blocks");
            }
        }
    }
}

/// Encode a single-key error map `{ "error": "..." }` into MessagePack.
pub fn encode_error_msgpack(message: &str, out: &mut Vec<u8>) {
    use rmp::encode::*;
    write_map_len(out, 1).expect("map len");
    write_str(out, "error").expect("write key");
    write_str(out, message).expect("write error");
}

/// Encode the empty query result `{ "<tenant_id>": {} }` into MessagePack.
pub fn encode_empty_tenant_msgpack(tenant_id: &str, out: &mut Vec<u8>) {
    use rmp::encode::*;
    write_map_len(out, 1).expect("map len");
    write_str(out, tenant_id).expect("write tenant");
    write_map_len(out, 0).expect("empty map");
}

// ---------------------------------------------------------------------------
// KV event types (for POST /events, push-based KV cache event ingestion)
// ---------------------------------------------------------------------------

/// Batch of KV cache events from workers.
///
/// Routing context (`instance_id`, `model_name`, `tenant_id`) identifies the
/// originating worker and the model/tenant scope for indexer lookup. When
/// `model_name` / `tenant_id` are omitted and the instance is already
/// registered, the registered values are used as a fallback. `block_size`
/// is carried for wire compatibility; the HTTP events path does not consume
/// it (query uses the registered value).
#[derive(Debug, Clone, Deserialize)]
pub struct KvEventBatch {
    /// The worker instance these events originate from.
    pub instance_id: String,
    /// Model name for indexer routing (falls back to registered value if omitted).
    #[serde(default)]
    pub model_name: Option<String>,
    /// Tenant id for indexer routing (falls back to registered value if omitted).
    #[serde(default)]
    pub tenant_id: Option<String>,
    /// KV block size in tokens. The HTTP events path currently does not
    /// consume this field (query uses the registered `block_size`); the
    /// serde default of 128 keeps the wire type stable.
    #[serde(default = "default_block_size")]
    pub block_size: u32,
    #[serde(default)]
    pub events: Vec<KvCacheEvent>,
    #[serde(default)]
    pub shutdown: bool,
}

fn default_block_size() -> u32 {
    128
}

/// A single KV cache event on the wire. Supports both engine-style JSON
/// and RFC #1527 msgpack formats via serde aliases.
///
/// Accepts two JSON shapes:
///   - Nested:  `{"event_id": 1, "data": {"type": "stored", ...}, "dp_rank": 0}`
///   - Flat:    `{"event_id": 1, "type": "stored", ..., "dp_rank": 0}`
#[derive(Debug, Clone, Deserialize)]
pub struct KvCacheEvent {
    pub event_id: u64,
    /// The wire payload — deserialized flexibly from either format.
    /// The `#[serde(flatten)]` + custom deserialize accepts both nested
    /// `"data": {...}` and flat `"type": "stored"` top-level shapes.
    #[serde(flatten)]
    pub data: KvEventWirePayload,
    #[serde(default)]
    pub dp_rank: DpRank,
}

/// Flexible wire format payload that accepts both engine and RFC #1527 shapes.
///
/// Engine-style:
///   `{"type": "stored", "blocks": [...], "parent_hash": ...}`
///
/// RFC 1527-style:
///   `{"event_type": "stored", "seq_hashes": [...], "medium": "cpu", "backend_id": "daemon-1"}`
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct KvEventWirePayload {
    /// RFC #1527: "stored" | "removed" | "cleared"
    #[serde(alias = "type")]
    pub event_type: String,
    /// Engine-style: blocks with block_hash + tokens_hash.
    pub blocks: Vec<KvCacheStoredBlockData>,
    /// Engine-style: parent sequence hash.
    /// Accepts both the RFC #1527 field name `parent_hash` and the vLLM
    /// engine field name `parent_block_hash`.
    #[serde(alias = "parent_block_hash")]
    pub parent_hash: Option<i64>,
    /// Engine-style: raw token ids of the stored chain, used to recompute
    /// `tokens_hash` (XXH3) when the event carries no pre-computed blocks.
    #[serde(default)]
    pub token_ids: Vec<i64>,
    /// Engine-style: block size in tokens, must pair with `token_ids`.
    #[serde(default)]
    pub block_size: Option<u32>,
    /// RFC #1527: rolling sequence hashes.
    pub seq_hashes: Vec<u64>,
    /// RFC #1527: cache medium (npu, cpu, disk).
    pub medium: Option<String>,
    /// RFC #1527: backend that owns the blocks.
    pub backend_id: Option<String>,
    /// Legacy compat: block_hashes (vLLM / engine alias for seq_hashes).
    #[serde(default)]
    pub block_hashes: Vec<u64>,
    /// Legacy compat: old event type string (e.g. "BlockStored").
    #[serde(default)]
    pub legacy_type: Option<String>,
}

impl KvEventWirePayload {
    /// Normalize into canonical event data + metadata.
    pub fn normalize(&self) -> (KvCacheEventData, Option<String>, Option<String>) {
        let event_type = self.resolve_event_type();
        tracing::debug!(
            raw_event_type = %self.event_type,
            resolved = %event_type,
            blocks_len = self.blocks.len(),
            seq_hashes_len = self.seq_hashes.len(),
            "KvEventWirePayload::normalize"
        );
        let seq_hashes = self.collect_seq_hashes();

        let data = match event_type {
            "stored" => {
                let blocks: Vec<KvCacheStoredBlockData> = if !self.blocks.is_empty() {
                    self.blocks.clone()
                } else if !self.token_ids.is_empty() && self.block_size.is_some_and(|bs| bs > 0) {
                    // Engine-style event (vLLM map/JSON): recompute the XXH3
                    // content hash from token_ids, same as the ZMQ path.
                    let bs = self.block_size.unwrap_or(0);
                    let computed = compute_block_hash_for_seq(&self.token_ids, bs);
                    let num = computed.len().min(self.block_hashes.len());
                    (0..num)
                        .map(|i| KvCacheStoredBlockData {
                            block_hash: self.block_hashes[i],
                            tokens_hash: computed[i].0,
                        })
                        .collect()
                } else {
                    // Build engine-style blocks from seq_hashes (Mooncake path: no tokens_hash)
                    seq_hashes
                        .iter()
                        .map(|&h| KvCacheStoredBlockData {
                            block_hash: h,
                            tokens_hash: h, // use seq_hash as fallback
                        })
                        .collect()
                };
                KvCacheEventData::Stored(KvCacheStoreData {
                    // Engine events carry `parent_block_hash` (aliased into
                    // `parent_hash`); RFC #1527 pool events have no parent.
                    parent_hash: self.parent_hash.map(|h| h as u64),
                    start_position: None,
                    blocks,
                })
            }
            "removed" => KvCacheEventData::Removed {
                block_hashes: seq_hashes.to_vec(),
            },
            "cleared" => KvCacheEventData::Cleared,
            // Infer event type from available data when the type field is
            // missing (e.g. due to serde flatten + alias interaction with
            // nested "data": {...} JSON shapes).
            _ => {
                if !self.blocks.is_empty() {
                    KvCacheEventData::Stored(KvCacheStoreData {
                        parent_hash: self.parent_hash.map(|h| h as u64),
                        start_position: None,
                        blocks: self.blocks.clone(),
                    })
                } else if !self.block_hashes.is_empty() || !self.seq_hashes.is_empty() {
                    let mut hashes: Vec<u64> = self.seq_hashes.clone();
                    hashes.extend(self.block_hashes.iter().copied());
                    if hashes.is_empty() {
                        KvCacheEventData::Cleared
                    } else {
                        KvCacheEventData::Removed {
                            block_hashes: hashes,
                        }
                    }
                } else {
                    tracing::warn!("unrecognized event type, treating as cleared");
                    KvCacheEventData::Cleared
                }
            }
        };

        (data, self.medium.clone(), self.backend_id.clone())
    }

    /// Resolve the event type to one of `stored` / `removed` / `cleared`.
    ///
    /// Both the canonical wire names ("stored", "removed", "cleared") and the
    /// engine class names ("BlockStored", "BlockRemoved", "AllBlocksCleared")
    /// are recognized via keyword matching, so a vLLM-style event pushed over
    /// HTTP /events is never misclassified (a `BlockStored` must not fall into
    /// the `Removed` fallback branch).
    fn resolve_event_type(&self) -> &str {
        let raw = if !self.event_type.is_empty() {
            self.event_type.as_str()
        } else {
            match &self.legacy_type {
                Some(t) => t.as_str(),
                None => return "unknown",
            }
        };
        let lower = raw.to_ascii_lowercase();
        if lower.contains("removed") {
            "removed"
        } else if lower.contains("stored") {
            "stored"
        } else if lower.contains("cleared") {
            "cleared"
        } else {
            "unknown"
        }
    }

    fn collect_seq_hashes(&self) -> Vec<u64> {
        let mut hashes: Vec<u64> = Vec::new();
        hashes.extend_from_slice(&self.seq_hashes);
        for &h in &self.block_hashes {
            hashes.push(h);
        }
        for b in &self.blocks {
            hashes.push(b.block_hash);
        }
        hashes
    }
}

// ---------------------------------------------------------------------------
// Canonical internal event types (used by radix tree apply_event)
// ---------------------------------------------------------------------------

/// The internal canonical event payload for radix tree operations.
#[derive(Debug, Clone, PartialEq)]
pub enum KvCacheEventData {
    Stored(KvCacheStoreData),
    Removed { block_hashes: Vec<u64> },
    Cleared,
}

/// Data for a block-store event.
#[derive(Debug, Clone, PartialEq)]
pub struct KvCacheStoreData {
    /// Parent sequence hash (None for root-level blocks).
    pub parent_hash: Option<u64>,
    /// Absolute position of the first block (for positional replay, optional).
    pub start_position: Option<u32>,
    /// Stored block data.
    pub blocks: Vec<KvCacheStoredBlockData>,
}

/// A single stored block within an event.
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct KvCacheStoredBlockData {
    /// Engine-computed sequence hash (u64 to match u64 XXH3 output).
    pub block_hash: u64,
    /// Content-based XXH3 tokens_hash.
    pub tokens_hash: u64,
}

// ---------------------------------------------------------------------------
// Internal types
// ---------------------------------------------------------------------------

/// Overlap match result from a radix-tree lookup.
#[derive(Debug, Clone, Default)]
pub struct OverlapBlocks {
    /// worker -> matched block count
    pub blocks: FxHashMap<WorkerKey, u32>,
}

impl OverlapBlocks {
    pub fn is_empty(&self) -> bool {
        self.blocks.is_empty()
    }

    /// Add matched block count for a worker.
    #[inline]
    pub fn add_blocks(&mut self, worker: WorkerKey, n: u32) {
        self.blocks
            .entry(worker)
            .and_modify(|s| *s += n)
            .or_insert(n);
    }

    /// Max-based update, used internally by HBM tree traversal.
    #[inline]
    pub fn update_blocks(&mut self, worker: WorkerKey, depth: u32) {
        self.blocks
            .entry(worker)
            .and_modify(|s| *s = (*s).max(depth))
            .or_insert(depth);
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;

    // ── StorageMedium ─────────────────────────────────────────────────

    #[test]
    fn test_storage_medium_from_str_npu() {
        assert_eq!(StorageMedium::parse("npu"), StorageMedium::Npu);
        assert_eq!(StorageMedium::parse("NPU"), StorageMedium::Npu);
        assert_eq!(StorageMedium::parse("gpu"), StorageMedium::Npu);
        assert_eq!(StorageMedium::parse("GPU"), StorageMedium::Npu);
        assert_eq!(StorageMedium::parse("xpu"), StorageMedium::Npu);
        assert_eq!(StorageMedium::parse("XPU"), StorageMedium::Npu);
        assert_eq!(StorageMedium::parse("hbm"), StorageMedium::Npu);
        assert_eq!(StorageMedium::parse("device"), StorageMedium::Npu);
        assert!(StorageMedium::is_hbm_key("npu"));
        assert!(StorageMedium::is_hbm_key("gpu"));
        assert!(!StorageMedium::is_hbm_key("cpu"));
    }

    #[test]
    fn test_storage_medium_from_str_cpu_disk() {
        assert_eq!(StorageMedium::parse("cpu"), StorageMedium::Cpu);
        assert_eq!(StorageMedium::parse("CPU"), StorageMedium::Cpu);
        assert_eq!(StorageMedium::parse("memory"), StorageMedium::Cpu);
        assert_eq!(StorageMedium::parse("host"), StorageMedium::Cpu);
        assert_eq!(StorageMedium::parse("disk"), StorageMedium::Disk);
        assert_eq!(StorageMedium::parse("DISK"), StorageMedium::Disk);
        assert_eq!(StorageMedium::parse("ssd"), StorageMedium::Disk);
        assert_eq!(StorageMedium::parse("nvme"), StorageMedium::Disk);
    }

    #[test]
    fn test_storage_medium_default_is_npu() {
        assert_eq!(StorageMedium::default(), StorageMedium::Npu);
    }

    #[test]
    fn test_storage_medium_as_str() {
        assert_eq!(StorageMedium::Npu.as_str(), "NPU");
        assert_eq!(StorageMedium::Cpu.as_str(), "CPU");
        assert_eq!(StorageMedium::Disk.as_str(), "DISK");
        assert_eq!(StorageMedium::Unknown.as_str(), "UNKNOWN");
    }

    // ── WorkerKey ──────────────────────────────────────────────────────

    #[test]
    fn test_worker_key_fields() {
        let wk = WorkerKey {
            instance_id: "inst-1".into(),
            backend_id: "backend-a".into(),
            dp_rank: 2,
            medium: StorageMedium::Npu,
        };
        assert_eq!(wk.instance_id, "inst-1");
        assert_eq!(wk.backend_id, "backend-a");
        assert_eq!(wk.dp_rank, 2);
        assert_eq!(wk.medium, StorageMedium::Npu);
    }

    #[test]
    fn test_worker_key_equality() {
        let a = WorkerKey {
            instance_id: "i1".into(),
            backend_id: "b1".into(),
            dp_rank: 0,
            medium: StorageMedium::Cpu,
        };
        let b = WorkerKey {
            instance_id: "i1".into(),
            backend_id: "b1".into(),
            dp_rank: 0,
            medium: StorageMedium::Cpu,
        };
        assert_eq!(a, b);
    }

    #[test]
    fn test_worker_key_different_medium_not_equal() {
        let a = WorkerKey {
            instance_id: "i1".into(),
            backend_id: "b1".into(),
            dp_rank: 0,
            medium: StorageMedium::Npu,
        };
        let b = WorkerKey {
            instance_id: "i1".into(),
            backend_id: "b1".into(),
            dp_rank: 0,
            medium: StorageMedium::Cpu,
        };
        assert_ne!(a, b);
    }

    // ── InstanceMatchData serialization ─────────────────────────────────

    #[test]
    fn test_instance_match_data_serialization() {
        let mut imd = InstanceMatchData {
            longest_matched: 256,
            ..Default::default()
        };
        imd.dp.insert(
            "0".into(),
            DpBlocks {
                matched_tokens: 768,
                npu_blocks: 6,
                cpu_blocks: 0,
                disk_blocks: 0,
            },
        );
        imd.dp.insert(
            "1".into(),
            DpBlocks {
                matched_tokens: 1024,
                npu_blocks: 0,
                cpu_blocks: 4,
                disk_blocks: 0,
            },
        );

        let json = serde_json::to_string(&imd).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed["longest_matched"], 256);
        assert!(parsed.get("total_score").is_none());
        assert!(parsed["DP"]["0"].get("XPU").is_none());
        assert!(parsed["DP"]["0"].get("total").is_none());
        assert_eq!(parsed["DP"]["0"]["matched_tokens"], 768);
        assert_eq!(parsed["DP"]["0"]["npu_blocks"], 6);
        assert_eq!(parsed["DP"]["0"]["cpu_blocks"], 0);
        assert_eq!(parsed["DP"]["1"]["matched_tokens"], 1024);
        assert_eq!(parsed["DP"]["1"]["cpu_blocks"], 4);
    }

    // ── KvEventWirePayload normalization ────────────────────────────────

    #[test]
    fn test_normalize_engine_stored_event() {
        let payload = KvEventWirePayload {
            event_type: "stored".into(),
            blocks: vec![KvCacheStoredBlockData {
                block_hash: 100,
                tokens_hash: 0xABCD,
            }],
            parent_hash: Some(50),
            ..Default::default()
        };

        let (data, medium, backend_id) = payload.normalize();
        assert!(matches!(data, KvCacheEventData::Stored(_)));
        assert!(medium.is_none());
        assert!(backend_id.is_none());
    }

    #[test]
    fn test_normalize_rfc_removed_event() {
        let payload = KvEventWirePayload {
            event_type: "removed".into(),
            seq_hashes: vec![111, 222],
            medium: Some("cpu".into()),
            backend_id: Some("master-1".into()),
            ..Default::default()
        };

        let (data, medium, backend_id) = payload.normalize();
        assert!(matches!(data, KvCacheEventData::Removed { .. }));
        assert_eq!(medium.as_deref(), Some("cpu"));
        assert_eq!(backend_id.as_deref(), Some("master-1"));
    }

    #[test]
    fn test_normalize_cleared_event() {
        let payload = KvEventWirePayload {
            event_type: "cleared".into(),
            ..Default::default()
        };

        let (data, _, _) = payload.normalize();
        assert!(matches!(data, KvCacheEventData::Cleared));
    }

    #[test]
    fn test_normalize_legacy_type_block_stored() {
        let payload = KvEventWirePayload {
            legacy_type: Some("BlockStored".into()),
            blocks: vec![KvCacheStoredBlockData {
                block_hash: 1,
                tokens_hash: 2,
            }],
            ..Default::default()
        };

        let (data, _, _) = payload.normalize();
        assert!(matches!(data, KvCacheEventData::Stored(_)));
    }

    #[test]
    fn test_normalize_legacy_type_block_removed() {
        let payload = KvEventWirePayload {
            legacy_type: Some("BlockRemoved".into()),
            block_hashes: vec![42],
            ..Default::default()
        };

        let (data, _, _) = payload.normalize();
        assert!(matches!(data, KvCacheEventData::Removed { .. }));
    }

    // ── Engine class-name type resolution (vLLM map/JSON events) ────────

    #[test]
    fn test_normalize_engine_block_stored_is_stored_not_removed() {
        // vLLM engine events pushed via HTTP /events carry `type: "BlockStored"`.
        // They must normalize to Stored — the historical fallback classified
        // them as Removed, which would delete index entries.
        let payload = KvEventWirePayload {
            event_type: "BlockStored".into(),
            block_hashes: vec![100, 200],
            parent_hash: Some(50),
            token_ids: vec![1, 2, 3, 4, 5, 6, 7, 8],
            block_size: Some(4),
            ..Default::default()
        };
        let (data, _, _) = payload.normalize();
        let KvCacheEventData::Stored(store) = &data else {
            panic!("BlockStored must normalize to Stored, got {data:?}");
        };
        assert_eq!(store.parent_hash, Some(50));
        assert_eq!(store.blocks.len(), 2);
        // tokens_hash is the XXH3 content hash recomputed from token_ids,
        // never the sequence hash.
        let computed = compute_block_hash_for_seq(&[1, 2, 3, 4, 5, 6, 7, 8], 4);
        assert_eq!(store.blocks[0].tokens_hash, computed[0].0);
        assert_eq!(store.blocks[1].tokens_hash, computed[1].0);
        assert_ne!(store.blocks[0].tokens_hash, 100);
    }

    #[test]
    fn test_normalize_engine_block_removed() {
        let payload = KvEventWirePayload {
            event_type: "BlockRemoved".into(),
            block_hashes: vec![111],
            ..Default::default()
        };
        let (data, _, _) = payload.normalize();
        assert!(matches!(data, KvCacheEventData::Removed { .. }));
    }

    #[test]
    fn test_normalize_engine_all_blocks_cleared() {
        let payload = KvEventWirePayload {
            event_type: "AllBlocksCleared".into(),
            ..Default::default()
        };
        let (data, _, _) = payload.normalize();
        assert!(matches!(data, KvCacheEventData::Cleared));
    }

    #[test]
    fn test_normalize_engine_parent_block_hash_alias() {
        // The vLLM field name `parent_block_hash` maps onto `parent_hash`.
        let payload = KvEventWirePayload {
            event_type: "BlockStored".into(),
            block_hashes: vec![100],
            parent_hash: Some(999),
            token_ids: vec![1, 2, 3, 4],
            block_size: Some(4),
            ..Default::default()
        };
        let (data, _, _) = payload.normalize();
        let KvCacheEventData::Stored(store) = &data else {
            panic!("expected Stored, got {data:?}");
        };
        assert_eq!(store.parent_hash, Some(999));
    }

    // -----------------------------------------------------------------------
    // MessagePack query codec
    // -----------------------------------------------------------------------

    #[test]
    fn test_query_request_msgpack_roundtrip() {
        let req = QueryRequest {
            model: "llama-7b".into(),
            block_size: 128,
            token_ids: (0..100_000).map(|i| i % 32000).collect(),
            tenant_id: "default".into(),
        };
        let encoded = rmp_serde::to_vec(&req).unwrap();
        let decoded: QueryRequest = rmp_serde::from_slice(&encoded).unwrap();
        assert_eq!(decoded.model, req.model);
        assert_eq!(decoded.block_size, req.block_size);
        assert_eq!(decoded.tenant_id, req.tenant_id);
        assert_eq!(decoded.token_ids, req.token_ids);
    }

    #[test]
    fn test_query_by_hash_request_msgpack_roundtrip() {
        let req = QueryByHashRequest {
            model: "llama-7b".into(),
            block_size: 128,
            block_hashes: (0..10_000).map(|i| (i as u64) * 2654435761).collect(),
            tenant_id: "default".into(),
        };
        let encoded = rmp_serde::to_vec(&req).unwrap();
        let decoded: QueryByHashRequest = rmp_serde::from_slice(&encoded).unwrap();
        assert_eq!(decoded.block_hashes, req.block_hashes);
    }

    /// Convert a MessagePack value into its JSON equivalent so the two wire
    /// shapes can be compared structurally.
    fn rmpv_to_json(v: &rmpv::Value) -> serde_json::Value {
        match v {
            rmpv::Value::Nil => serde_json::Value::Null,
            rmpv::Value::Boolean(b) => serde_json::Value::Bool(*b),
            rmpv::Value::Integer(i) => {
                if let Some(u) = i.as_u64() {
                    serde_json::Value::from(u)
                } else {
                    serde_json::Value::from(i.as_i64().unwrap_or_default())
                }
            }
            rmpv::Value::F64(f) => serde_json::Value::from(*f),
            rmpv::Value::F32(f) => serde_json::Value::from(*f),
            rmpv::Value::String(s) => {
                serde_json::Value::String(s.as_str().unwrap_or_default().to_string())
            }
            rmpv::Value::Binary(b) => serde_json::Value::String(format!("{b:?}")),
            rmpv::Value::Array(a) => serde_json::Value::Array(a.iter().map(rmpv_to_json).collect()),
            rmpv::Value::Map(m) => {
                let mut map = serde_json::Map::new();
                for (k, v) in m {
                    let key = match k {
                        rmpv::Value::String(s) => s.as_str().unwrap_or_default().to_string(),
                        other => format!("{other:?}"),
                    };
                    map.insert(key, rmpv_to_json(v));
                }
                serde_json::Value::Object(map)
            }
            rmpv::Value::Ext(..) => serde_json::Value::Null,
        }
    }

    fn sample_query_response() -> QueryResponse {
        let mut tenants = HashMap::new();
        let mut instances = HashMap::new();
        let mut dp = HashMap::new();
        dp.insert(
            "0".to_string(),
            DpBlocks {
                matched_tokens: 384,
                npu_blocks: 3,
                cpu_blocks: 0,
                disk_blocks: 0,
            },
        );
        dp.insert(
            "1".to_string(),
            DpBlocks {
                matched_tokens: 512,
                npu_blocks: 1,
                cpu_blocks: 3,
                disk_blocks: 0,
            },
        );
        instances.insert(
            "prefill-0".to_string(),
            InstanceMatchData {
                longest_matched: 512,
                dp,
            },
        );
        tenants.insert("default".to_string(), instances);
        QueryResponse { tenants }
    }

    #[test]
    fn test_query_response_msgpack_matches_json_shape() {
        let response = sample_query_response();
        let mut buf = Vec::new();
        encode_query_response_msgpack(&response, &mut buf);

        // Decode the msgpack payload and compare with the JSON wire shape
        // field-by-field. This guards the hand-written encoder against
        // drifting from the serde_json shape (which the Python client parses).
        let msgpack_value = rmpv::decode::read_value(&mut buf.as_slice()).unwrap();
        let msgpack_json = rmpv_to_json(&msgpack_value);
        let json_value = serde_json::to_value(&response).unwrap();
        assert_eq!(
            msgpack_json, json_value,
            "msgpack response diverges from JSON wire shape"
        );
    }

    #[test]
    fn test_query_response_msgpack_empty_tenant() {
        let response = QueryResponse::default();
        let mut buf = Vec::new();
        encode_query_response_msgpack(&response, &mut buf);
        let msgpack_value = rmpv::decode::read_value(&mut buf.as_slice()).unwrap();
        assert_eq!(rmpv_to_json(&msgpack_value), serde_json::json!({}));
    }

    #[test]
    fn test_error_msgpack_encoding() {
        let mut err = Vec::new();
        encode_error_msgpack("boom", &mut err);
        assert_eq!(
            rmpv_to_json(&rmpv::decode::read_value(&mut err.as_slice()).unwrap()),
            serde_json::json!({"error": "boom"})
        );
    }

    #[test]
    fn test_is_msgpack_content_type() {
        let cases: Vec<(&str, bool)> = vec![
            ("application/msgpack", true),
            ("application/x-msgpack", true),
            ("application/msgpack; charset=utf-8", true),
            ("Application/MSGPACK", true),
            ("application/json", false),
            ("", false),
            ("text/plain", false),
        ];
        for (ct, expected) in cases {
            let mut headers = axum::http::HeaderMap::new();
            if !ct.is_empty() {
                headers.insert(axum::http::header::CONTENT_TYPE, ct.parse().unwrap());
            }
            assert_eq!(is_msgpack_content_type(&headers), expected, "ct={ct}");
        }
    }
}
