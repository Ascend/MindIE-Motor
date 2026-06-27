// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Per-(model, tenant) radix tree indexer.
//!
//! Each `IndexerEntry` manages a `ConcurrentRadixTree`, per-worker reverse
//! lookup tables, and a non-HBM event cache for a single (model_name, tenant_id)
//! pair.
//!
//! ## Two-phase non-HBM event matching
//!
//! When the engine offloads blocks to CPU/DISK, the conductor uses a two-phase
//! approach:
//!
//! 1. Engine offloading event → cache the ``block_hash → tokens_hash`` mapping
//!    (do NOT insert into radix tree — the pool backend may place the block on
//!    a different node).
//! 2. Pool backend confirm event → look up the cache, insert ``tokens_hash``
//!    into the radix tree under the pool backend's worker key.
//! 3. Pool backend eviction event → look up the cache, remove from tree,
//!    evict cache entry.

use std::collections::HashMap;
use std::sync::Arc;

use dashmap::DashMap;
use parking_lot::RwLock;
use rustc_hash::FxHashMap;

use crate::concurrent_tree::{ConcurrentRadixTree, WorkerLookup};
use crate::error::KvConductorError;
use crate::hashing::compute_block_hash_for_seq;
use crate::protocols::*;

/// Key identifying a unique indexer instance: (model_name, tenant_id).
/// Hashes at different block_sizes coexist in the same tree — they are
/// distinct u64 values with no collision risk.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct IndexerKey {
    pub model_name: String,
    pub tenant_id: String,
}

/// An indexer entry for one (model, tenant) pair.
pub struct IndexerEntry {
    /// The shared radix tree.
    pub tree: Arc<ConcurrentRadixTree>,
    /// Per-worker reverse lookup tables: WorkerKey -> WorkerLookup.
    /// Protected by a single RwLock for simplicity in the standalone case.
    pub lookups: Arc<RwLock<FxHashMap<WorkerKey, WorkerLookup>>>,
    /// Cache for non-HBM engine events: maps the engine's `block_hash` (SHA256
    /// u64) → the kv-conductor's `tokens_hash` (XXH3 content hash).
    ///
    /// When a vLLM engine publishes offloading events (medium=``"cpu"`` or
    /// ``"disk"``), we cache the computed XXH3 hash here instead of inserting
    /// into the radix tree.  Later, when the pool backend (Mooncake Master)
    /// broadcasts its own store/remove events carrying the same `seq_hash`,
    /// we look up the cache to find the correct `tokens_hash` and insert it
    /// into the tree under the appropriate worker key.
    pub non_hbm_cache: Arc<RwLock<FxHashMap<u64, u64>>>,
}

impl Default for IndexerEntry {
    fn default() -> Self {
        Self {
            tree: Arc::new(ConcurrentRadixTree::new()),
            lookups: Arc::new(RwLock::new(FxHashMap::default())),
            non_hbm_cache: Arc::new(RwLock::new(FxHashMap::default())),
        }
    }
}

impl IndexerEntry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Find matches for a token sequence against all registered workers,
    /// using the given `block_size` for hash computation.
    pub fn find_matches(&self, token_ids: &[i64], block_size: u32) -> OverlapScores {
        let t_hash = std::time::Instant::now();
        let block_hashes = compute_block_hash_for_seq(token_ids, block_size);
        let hash_us = t_hash.elapsed().as_micros();
        let scores = self.tree.find_matches(&block_hashes);
        tracing::debug!(
            num_tokens = token_ids.len(),
            block_size,
            num_hashes = block_hashes.len(),
            hash_us,
            "hash_computed"
        );
        scores
    }

    /// Cache a block_hash → tokens_hash mapping for a non-HBM engine event.
    ///
    /// Called when a vLLM engine publishes offloading events (medium != HBM).
    /// The mapping is later used to insert the correct XXH3 hash into the
    /// radix tree when the pool backend confirms the block has been stored.
    #[inline]
    pub fn cache_non_hbm_block(&self, block_hash: u64, tokens_hash: u64) {
        self.non_hbm_cache.write().insert(block_hash, tokens_hash);
    }

    /// Look up a cached tokens_hash by the engine's block_hash.
    /// Returns `None` if this hash was never cached (e.g. the pool backend
    /// stored a block the engine never offloaded, or the cache was evicted).
    #[inline]
    pub fn lookup_cached_tokens_hash(&self, block_hash: u64) -> Option<u64> {
        self.non_hbm_cache.read().get(&block_hash).copied()
    }

    /// Remove a cached entry (called when a pool backend remove event arrives).
    #[inline]
    pub fn evict_cached_block(&self, block_hash: u64) {
        self.non_hbm_cache.write().remove(&block_hash);
    }

    /// Apply a KV cache event for a specific worker.
    pub fn apply_event(
        &self,
        worker: &WorkerKey,
        event: &KvCacheEventData,
    ) -> Result<(), KvConductorError> {
        let mut lookups = self.lookups.write();
        let lookup = lookups.entry(worker.clone()).or_default();
        let lookup_size_before = lookup.len();

        let result = match event {
            KvCacheEventData::Stored(store_data) => {
                self.tree.apply_store(worker, lookup, store_data)
            }
            KvCacheEventData::Removed { block_hashes } => {
                self.tree.apply_remove(worker, lookup, block_hashes)
            }
            KvCacheEventData::Cleared => {
                self.tree.clear_worker(worker, lookup);
                Ok(())
            }
        };

        let lookup_size_after = lookup.len();
        tracing::trace!(
            instance_id = %worker.instance_id,
            dp_rank = worker.dp_rank,
            lookup_before = lookup_size_before,
            lookup_after = lookup_size_after,
            result = ?result.as_ref().map_err(|e| e.to_string()),
            "apply_event completed"
        );

        result
    }

    /// Remove a worker entirely from this indexer entry.
    pub fn remove_worker(&self, worker: &WorkerKey) {
        let mut lookups = self.lookups.write();
        if let Some(lookup) = lookups.get_mut(worker) {
            self.tree.remove_worker_from_tree(worker, lookup);
        }
        lookups.remove(worker);
    }

    /// Remove all cache entries for a given instance and DP rank across
    /// **all** storage media (XPU, CPU, DISK). This is the correct cleanup
    /// path for worker unregistration, as a single instance may have blocks
    /// spread across multiple tiers.
    pub fn remove_worker_all_media(&self, instance_id: &str, dp_rank: u32) {
        let mut lookups = self.lookups.write();
        // Collect all WorkerKeys matching this (instance_id, dp_rank)
        let matching: Vec<WorkerKey> = lookups
            .keys()
            .filter(|k| k.instance_id == instance_id && k.dp_rank == dp_rank)
            .cloned()
            .collect();
        for wk in &matching {
            if let Some(lookup) = lookups.get_mut(wk) {
                self.tree.remove_worker_from_tree(wk, lookup);
            }
            lookups.remove(wk);
        }
    }

    /// Get the total number of cached blocks across all workers.
    pub fn total_blocks(&self) -> usize {
        let lookups = self.lookups.read();
        lookups.values().map(|l| l.len()).sum()
    }

    /// Get all registered worker keys.
    pub fn worker_keys(&self) -> Vec<WorkerKey> {
        let lookups = self.lookups.read();
        lookups.keys().cloned().collect()
    }
}

/// Top-level indexer managing multiple (model, tenant) trees.
pub struct Indexer {
    entries: DashMap<IndexerKey, Arc<IndexerEntry>>,
}

impl Indexer {
    pub fn new() -> Self {
        Self {
            entries: DashMap::new(),
        }
    }

    /// Get or create an indexer entry for the given model and tenant.
    pub fn get_or_create(&self, model_name: &str, tenant_id: &str) -> Arc<IndexerEntry> {
        let key = IndexerKey {
            model_name: model_name.to_string(),
            tenant_id: tenant_id.to_string(),
        };
        self.entries
            .entry(key)
            .or_insert_with(|| Arc::new(IndexerEntry::new()))
            .value()
            .clone()
    }

    /// Get an existing indexer entry.
    pub fn get(&self, model_name: &str, tenant_id: &str) -> Option<Arc<IndexerEntry>> {
        let key = IndexerKey {
            model_name: model_name.to_string(),
            tenant_id: tenant_id.to_string(),
        };
        self.entries.get(&key).map(|e| e.value().clone())
    }

    /// Remove an indexer entry if it has no more workers.
    pub fn remove_if_empty(&self, model_name: &str, tenant_id: &str) {
        let key = IndexerKey {
            model_name: model_name.to_string(),
            tenant_id: tenant_id.to_string(),
        };
        let should_remove = self
            .entries
            .get(&key)
            .is_some_and(|e| e.value().lookups.read().is_empty());
        if should_remove {
            self.entries.remove(&key);
        }
    }

    /// Query overlap scores for a token sequence against a specific model/tenant.
    ///
    /// `block_size` determines the token-to-hash granularity — it must match
    /// the size used by the engine when publishing events.
    pub fn query(
        &self,
        model_name: &str,
        tenant_id: &str,
        token_ids: &[i64],
        block_size: u32,
        hit_detail: bool,
    ) -> Result<QueryResponse, KvConductorError> {
        let t0 = std::time::Instant::now();

        let entry = self
            .get(model_name, tenant_id)
            .ok_or_else(|| KvConductorError::NoIndexer {
                model_name: model_name.to_string(),
                tenant_id: tenant_id.to_string(),
            })?;

        let overlap = entry.find_matches(token_ids, block_size);
        let t_tree = t0.elapsed();

        let resp = self.build_response(overlap, tenant_id, block_size, hit_detail);
        let total = t0.elapsed();

        tracing::debug!(
            num_tokens = token_ids.len(),
            block_size,
            hash_us = t_tree.as_micros(),
            total_us = total.as_micros(),
            "query profile"
        );
        resp
    }

    /// Query overlap scores using pre-computed `LocalBlockHash` values.
    pub fn query_by_hash(
        &self,
        model_name: &str,
        tenant_id: &str,
        block_hashes: &[LocalBlockHash],
    ) -> Result<QueryResponse, KvConductorError> {
        let entry = self
            .get(model_name, tenant_id)
            .ok_or_else(|| KvConductorError::NoIndexer {
                model_name: model_name.to_string(),
                tenant_id: tenant_id.to_string(),
            })?;

        let overlap = entry.tree.find_matches(block_hashes);
        if overlap.is_empty() {
            return Err(KvConductorError::NoWorkers {
                model_name: model_name.to_string(),
                tenant_id: tenant_id.to_string(),
            });
        }
        // query_by_hash uses pre-computed hashes — block_size is irrelevant
        // for the hashes themselves, but we still need it for token scaling.
        // Default to 1 token per hash (no scaling) since we don't know the
        // original block_size from the hash alone.
        self.build_response(overlap, tenant_id, 1, false)
    }

    /// Build a `QueryResponse` from overlap scores.
    fn build_response(
        &self,
        overlap: OverlapScores,
        tenant_id: &str,
        block_size: u32,
        hit_detail: bool,
    ) -> Result<QueryResponse, KvConductorError> {
        if overlap.is_empty() {
            return Err(KvConductorError::NoWorkers {
                model_name: String::new(),
                tenant_id: tenant_id.to_string(),
            });
        }

        let mut instance_data: HashMap<String, InstanceMatchData> = HashMap::new();

        for (worker, matched_blocks) in &overlap.scores {
            let matched_tokens = matched_blocks * block_size;
            let dp_rank_str = worker.dp_rank.to_string();

            let imd = instance_data.entry(worker.instance_id.clone()).or_default();

            imd.longest_matched = imd.longest_matched.max(matched_tokens);

            match worker.medium {
                StorageMedium::Xpu => imd.xpu = imd.xpu.max(matched_tokens),
                StorageMedium::Cpu => imd.cpu = imd.cpu.max(matched_tokens),
                StorageMedium::Disk => imd.disk = imd.disk.max(matched_tokens),
                StorageMedium::Unknown => {
                    imd.xpu = imd.xpu.max(matched_tokens);
                }
            }

            imd.dp.insert(dp_rank_str.clone(), matched_tokens);

            // Populate per-DP per-medium detail when requested.
            if hit_detail {
                let detail = imd
                    .media_detail
                    .get_or_insert_with(HashMap::new)
                    .entry(dp_rank_str.clone())
                    .or_default();
                match worker.medium {
                    StorageMedium::Xpu => detail.xpu = detail.xpu.max(*matched_blocks),
                    StorageMedium::Cpu => detail.cpu = detail.cpu.max(*matched_blocks),
                    StorageMedium::Disk => detail.disk = detail.disk.max(*matched_blocks),
                    StorageMedium::Unknown => detail.xpu = detail.xpu.max(*matched_blocks),
                }

                tracing::trace!(
                    instance_id = %worker.instance_id,
                    dp_rank = worker.dp_rank,
                    medium = %worker.medium.as_str(),
                    matched_blocks,
                    "query hit detail"
                );
            }
        }

        let mut response = QueryResponse::default();
        response
            .tenants
            .insert(tenant_id.to_string(), instance_data);

        Ok(response)
    }

    /// Get a summary of all tracked entries.
    pub fn summary(&self) -> Vec<IndexerSummary> {
        self.entries
            .iter()
            .map(|entry| {
                let key = entry.key();
                let value = entry.value();
                IndexerSummary {
                    model_name: key.model_name.clone(),
                    tenant_id: key.tenant_id.clone(),
                    worker_count: value.worker_keys().len(),
                    total_blocks: value.total_blocks(),
                }
            })
            .collect()
    }
}

impl Default for Indexer {
    fn default() -> Self {
        Self::new()
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct IndexerSummary {
    pub model_name: String,
    pub tenant_id: String,
    pub worker_count: usize,
    pub total_blocks: usize,
}

use serde::Serialize;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_indexer_get_or_create_and_query() {
        let indexer = Indexer::new();
        let entry = indexer.get_or_create("model-a", "tenant-1");

        // Compute the actual hash for the test token sequence
        let tokens: Vec<i64> = vec![10, 20, 30, 40];
        let hashes = compute_block_hash_for_seq(&tokens, 4);
        assert!(!hashes.is_empty());
        let tokens_hash = hashes[0];

        // Insert a worker with XPU blocks using the real hash
        let wk_xpu = WorkerKey {
            instance_id: "inst-1".into(),
            backend_id: "inst-1".into(),
            dp_rank: 0,
            medium: StorageMedium::Xpu,
        };

        let store = KvCacheEventData::Stored(KvCacheStoreData {
            parent_hash: None,
            start_position: None,
            blocks: vec![KvCacheStoredBlockData {
                block_hash: 100,
                tokens_hash: tokens_hash.0,
            }],
        });
        entry.apply_event(&wk_xpu, &store).unwrap();

        // Query with the same tokens
        let resp = indexer
            .query("model-a", "tenant-1", &tokens, 4, false)
            .unwrap();
        let tenant = &resp.tenants["tenant-1"];
        let imd = &tenant["inst-1"];
        assert!(imd.xpu > 0, "should have XPU match");
        assert_eq!(imd.cpu, 0);
        assert_eq!(imd.disk, 0);
        assert_eq!(imd.longest_matched, imd.xpu);
    }

    #[test]
    fn test_per_tier_aggregation() {
        let indexer = Indexer::new();
        let entry = indexer.get_or_create("model-b", "t1");

        // Two different token sequences → different block hashes
        let tokens_a: Vec<i64> = vec![10, 20, 30, 40];
        let tokens_b: Vec<i64> = vec![50, 60, 70, 80];
        let hash_a = compute_block_hash_for_seq(&tokens_a, 4)[0];
        let hash_b = compute_block_hash_for_seq(&tokens_b, 4)[0];

        // Worker 1: XPU blocks
        let wk1 = WorkerKey {
            instance_id: "inst-1".into(),
            backend_id: "inst-1".into(),
            dp_rank: 0,
            medium: StorageMedium::Xpu,
        };
        entry
            .apply_event(
                &wk1,
                &KvCacheEventData::Stored(KvCacheStoreData {
                    parent_hash: None,
                    start_position: None,
                    blocks: vec![KvCacheStoredBlockData {
                        block_hash: 100,
                        tokens_hash: hash_a.0,
                    }],
                }),
            )
            .unwrap();

        // Worker 2: CPU blocks (different instance, different tokens)
        let wk2 = WorkerKey {
            instance_id: "inst-2".into(),
            backend_id: "mooncake-1".into(),
            dp_rank: 0,
            medium: StorageMedium::Cpu,
        };
        entry
            .apply_event(
                &wk2,
                &KvCacheEventData::Stored(KvCacheStoreData {
                    parent_hash: None,
                    start_position: None,
                    blocks: vec![KvCacheStoredBlockData {
                        block_hash: 200,
                        tokens_hash: hash_b.0,
                    }],
                }),
            )
            .unwrap();

        // Query with tokens_a — should match inst-1 (XPU) only
        let resp = indexer.query("model-b", "t1", &tokens_a, 4, false).unwrap();
        let tenant = &resp.tenants["t1"];

        let imd1 = &tenant["inst-1"];
        assert!(imd1.xpu > 0, "inst-1 should have XPU match for tokens_a");
        assert_eq!(imd1.cpu, 0, "inst-1 should have no CPU match");

        // Query with tokens_b — should match inst-2 (CPU) only
        let resp = indexer.query("model-b", "t1", &tokens_b, 4, false).unwrap();
        let tenant = &resp.tenants["t1"];

        let imd2 = &tenant["inst-2"];
        assert_eq!(imd2.xpu, 0, "inst-2 should have no XPU match");
        assert!(imd2.cpu > 0, "inst-2 should have CPU match for tokens_b");
    }

    #[test]
    fn test_no_indexer_error() {
        let indexer = Indexer::new();
        let err = indexer.query("no-such-model", "default", &[1, 2, 3, 4], 4, false);
        assert!(err.is_err());
        assert!(matches!(
            err.unwrap_err(),
            KvConductorError::NoIndexer { .. }
        ));
    }
}
