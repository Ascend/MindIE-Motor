// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Protocol types for the KV conductor service.
//!
//! These types define the HTTP API contract, compatible with the Python
//! `ConductorApiClient` in `motor/coordinator/api_client/`.

use std::collections::HashMap;
use std::sync::Arc;

use parking_lot::RwLock as ParkingRwLock;
use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};
use tracing;

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
    /// GPU/NPU HBM — from inference engine workers.
    #[default]
    Xpu,
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
            "xpu" | "XPU" | "hbm" | "HBM" | "device" | "DEVICE" => Self::Xpu,
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
            Self::Xpu => "XPU",
            Self::Cpu => "CPU",
            Self::Disk => "DISK",
            Self::Unknown => "UNKNOWN",
        }
    }
}

// ---------------------------------------------------------------------------
// Scoring configuration
// ---------------------------------------------------------------------------

/// Per-medium block match weights, configurable at startup.
#[derive(Debug, Clone)]
pub struct ScoringConfig {
    /// Weight for each HBM (XPU) block matched.
    pub hbm_weight: u32,
    /// Weight for each CPU block matched.
    pub cpu_weight: u32,
    /// Weight for each disk block matched.
    pub disk_weight: u32,
}

impl Default for ScoringConfig {
    fn default() -> Self {
        Self {
            hbm_weight: 3,
            cpu_weight: 2,
            disk_weight: 1,
        }
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
    /// RFC #1527: cache medium (xpu, cpu, disk).
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
    /// e.g. {"xpu": "tcp://...:5557", "cpu": "tcp://...:5558"}.
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
#[derive(Debug, Clone, Deserialize)]
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
#[derive(Debug, Clone, Deserialize)]
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
//   rsp[tenant_id][instance_id]["longest_matched"]  (in tokens)
//   rsp[tenant_id][instance_id]["DP"][dp_rank_str]   (in tokens)
// ---------------------------------------------------------------------------

/// Per-DP weighted score breakdown across storage media.
#[derive(Debug, Clone, Serialize, Default)]
pub struct DpScoring {
    /// HBM matched blocks × 3
    #[serde(rename = "XPU")]
    pub xpu_score: u32,
    /// CPU matched blocks × 2
    #[serde(rename = "CPU")]
    pub cpu_score: u32,
    /// Disk matched blocks × 1
    #[serde(rename = "DISK")]
    pub disk_score: u32,
    /// Total weighted score (xpu_score + cpu_score + disk_score)
    pub total: u32,
    /// Cached prefix length for this DP, in tokens (blocks × block_size).
    pub matched_tokens: u32,
    /// HBM raw block count.
    #[serde(rename = "XPU_blk")]
    pub xpu_blocks: u32,
    /// CPU raw block count.
    #[serde(rename = "CPU_blk")]
    pub cpu_blocks: u32,
    /// Disk raw block count.
    #[serde(rename = "DISK_blk")]
    pub disk_blocks: u32,
}

/// Per-instance match data returned in query response.
#[derive(Debug, Clone, Serialize, Default)]
pub struct InstanceMatchData {
    /// Longest continuous prefix match across all DP ranks, in tokens.
    pub longest_matched: u32,
    /// Per-DP-rank scoring breakdown across media.
    #[serde(rename = "DP")]
    pub dp: HashMap<String, DpScoring>,
    /// Sum of all DP total scores for this instance.
    pub total_score: u32,
}

/// Full query response: { tenant_id: { instance_id: InstanceMatchData } }
#[derive(Debug, Clone, Serialize, Default)]
pub struct QueryResponse {
    #[serde(flatten)]
    pub tenants: HashMap<String, HashMap<InstanceId, InstanceMatchData>>,
}

// ---------------------------------------------------------------------------
// KV event types (for POST /events, push-based KV cache event ingestion)
// ---------------------------------------------------------------------------

/// Batch of KV cache events from workers.
///
/// Routing context (`instance_id`, `model_name`, `tenant_id`, `block_size`)
/// identifies the originating worker and the model/tenant scope for indexer
/// lookup. When `model_name` / `tenant_id` are omitted and the instance is
/// already registered, the registered values are used as a fallback.
/// `block_size` defaults to 128 when neither the batch nor a prior
/// registration provides it.
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
    /// KV block size in tokens (falls back to registered value, then 128).
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
    pub parent_hash: Option<i64>,
    /// RFC #1527: rolling sequence hashes.
    pub seq_hashes: Vec<u64>,
    /// RFC #1527: cache medium (xpu, cpu, disk).
    pub medium: Option<String>,
    /// RFC #1527: backend that owns the blocks.
    pub backend_id: Option<String>,
    /// Legacy compat: block_hashes (vLLM/Dynamo alias for seq_hashes).
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
                let parent_hash = if !self.blocks.is_empty() {
                    self.parent_hash
                } else {
                    None // Mooncake events have no parent
                };
                KvCacheEventData::Stored(KvCacheStoreData {
                    parent_hash: parent_hash.map(|h| h as u64),
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

    fn resolve_event_type(&self) -> &str {
        if !self.event_type.is_empty() {
            return &self.event_type;
        }
        match &self.legacy_type {
            Some(t) if t.contains("Removed") || t.contains("removed") => "removed",
            Some(t) if t.contains("Stored") || t.contains("stored") => "stored",
            Some(t) if t.contains("Cleared") || t.contains("cleared") => "cleared",
            _ => "unknown",
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

/// Overlap scores result from a radix-tree lookup.
#[derive(Debug, Clone, Default)]
pub struct OverlapScores {
    /// worker -> matched block count
    pub scores: FxHashMap<WorkerKey, u32>,
}

impl OverlapScores {
    pub fn is_empty(&self) -> bool {
        self.scores.is_empty()
    }

    /// Add points for a worker (HBM depth × 3, CPU block × 2, disk block × 1).
    #[inline]
    pub fn add_score(&mut self, worker: WorkerKey, points: u32) {
        self.scores
            .entry(worker)
            .and_modify(|s| *s += points)
            .or_insert(points);
    }

    /// Legacy max-based update, used internally by HBM tree traversal.
    #[inline]
    pub fn update_score(&mut self, worker: WorkerKey, depth: u32) {
        self.scores
            .entry(worker)
            .and_modify(|s| *s = (*s).max(depth))
            .or_insert(depth);
    }

    /// Merge scores from another `OverlapScores` into this one.
    /// Used by parallel flat lookup to combine per-thread results.
    #[inline]
    pub fn merge(&mut self, other: OverlapScores) {
        for (worker, score) in other.scores {
            self.add_score(worker, score);
        }
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
    fn test_storage_medium_from_str_xpu() {
        assert_eq!(StorageMedium::parse("xpu"), StorageMedium::Xpu);
        assert_eq!(StorageMedium::parse("XPU"), StorageMedium::Xpu);
        assert_eq!(StorageMedium::parse("hbm"), StorageMedium::Xpu);
        assert_eq!(StorageMedium::parse("device"), StorageMedium::Xpu);
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
    fn test_storage_medium_default_is_xpu() {
        assert_eq!(StorageMedium::default(), StorageMedium::Xpu);
    }

    #[test]
    fn test_storage_medium_as_str() {
        assert_eq!(StorageMedium::Xpu.as_str(), "XPU");
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
            medium: StorageMedium::Xpu,
        };
        assert_eq!(wk.instance_id, "inst-1");
        assert_eq!(wk.backend_id, "backend-a");
        assert_eq!(wk.dp_rank, 2);
        assert_eq!(wk.medium, StorageMedium::Xpu);
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
            medium: StorageMedium::Xpu,
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
        let mut imd = InstanceMatchData::default();
        imd.longest_matched = 256;
        imd.dp.insert(
            "0".into(),
            DpScoring {
                xpu_score: 6,
                cpu_score: 0,
                disk_score: 0,
                total: 6,
                matched_tokens: 768,
                xpu_blocks: 6,
                cpu_blocks: 0,
                disk_blocks: 0,
            },
        );
        imd.dp.insert(
            "1".into(),
            DpScoring {
                xpu_score: 0,
                cpu_score: 8,
                disk_score: 0,
                total: 8,
                matched_tokens: 1024,
                xpu_blocks: 0,
                cpu_blocks: 4,
                disk_blocks: 0,
            },
        );
        imd.total_score = 14;

        let json = serde_json::to_string(&imd).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();

        assert_eq!(parsed["longest_matched"], 256);
        assert_eq!(parsed["total_score"], 14);
        assert_eq!(parsed["DP"]["0"]["XPU"], 6);
        assert_eq!(parsed["DP"]["0"]["total"], 6);
        assert_eq!(parsed["DP"]["0"]["matched_tokens"], 768);
        assert_eq!(parsed["DP"]["0"]["XPU_blk"], 6);
        assert!(parsed["DP"]["0"]["CPU_blk"].as_u64().unwrap() == 0);
        assert_eq!(parsed["DP"]["1"]["CPU"], 8);
        assert_eq!(parsed["DP"]["1"]["total"], 8);
        assert_eq!(parsed["DP"]["1"]["matched_tokens"], 1024);
        assert_eq!(parsed["DP"]["1"]["CPU_blk"], 4);
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
}
