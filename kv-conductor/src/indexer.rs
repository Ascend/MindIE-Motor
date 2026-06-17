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
//! Each `IndexerEntry` manages a `ConcurrentRadixTree` and per-worker reverse
//! lookup tables for a single (model_name, tenant_id) pair.

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
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct IndexerKey {
    pub model_name: String,
    pub tenant_id: String,
}

/// An indexer entry for one (model, tenant) pair.
pub struct IndexerEntry {
    /// The shared radix tree.
    pub tree: Arc<ConcurrentRadixTree>,
    /// KV block size (tokens per block).
    pub block_size: u32,
    /// Per-worker reverse lookup tables: WorkerKey -> WorkerLookup.
    /// Protected by a single RwLock for simplicity in the standalone case.
    pub lookups: Arc<RwLock<FxHashMap<WorkerKey, WorkerLookup>>>,
}

impl IndexerEntry {
    pub fn new(block_size: u32) -> Self {
        Self {
            tree: Arc::new(ConcurrentRadixTree::new()),
            block_size,
            lookups: Arc::new(RwLock::new(FxHashMap::default())),
        }
    }

    /// Find matches for a token sequence against all registered workers.
    pub fn find_matches(&self, token_ids: &[i64]) -> OverlapScores {
        let block_hashes = compute_block_hash_for_seq(token_ids, self.block_size);
        self.tree.find_matches(&block_hashes)
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
        tracing::debug!(
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
    pub fn get_or_create(
        &self,
        model_name: &str,
        tenant_id: &str,
        block_size: u32,
    ) -> Arc<IndexerEntry> {
        let key = IndexerKey {
            model_name: model_name.to_string(),
            tenant_id: tenant_id.to_string(),
        };

        let entry = self
            .entries
            .entry(key)
            .or_insert_with(|| Arc::new(IndexerEntry::new(block_size)))
            .value()
            .clone();

        // Warn if an existing entry has a different block_size — this
        // indicates a configuration mismatch between workers for the same
        // model/tenant.
        if entry.block_size != block_size {
            tracing::warn!(
                model_name,
                tenant_id,
                existing_block_size = entry.block_size,
                requested_block_size = block_size,
                "block_size mismatch for existing indexer; using existing value"
            );
        }
        entry
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
    ///
    /// Holds the write lock on the entry's lookups table while checking
    /// emptiness so that a concurrent `apply_event` cannot add a worker
    /// between the check and the removal.
    pub fn remove_if_empty(&self, model_name: &str, tenant_id: &str) {
        let key = IndexerKey {
            model_name: model_name.to_string(),
            tenant_id: tenant_id.to_string(),
        };

        let should_remove = if let Some(entry) = self.entries.get(&key) {
            let lookups = entry.lookups.write();
            lookups.is_empty()
        } else {
            false
        };

        if should_remove {
            self.entries.remove(&key);
        }
    }

    /// Query overlap scores for a token sequence against a specific model/tenant.
    ///
    /// Returns a `QueryResponse` with per-instance and per-DP-rank match depths
    /// in tokens (scaled by block_size).
    pub fn query(
        &self,
        model_name: &str,
        tenant_id: &str,
        token_ids: &[i64],
    ) -> Result<QueryResponse, KvConductorError> {
        let entry = self
            .get(model_name, tenant_id)
            .ok_or_else(|| KvConductorError::NoIndexer {
                model_name: model_name.to_string(),
                tenant_id: tenant_id.to_string(),
            })?;

        let overlap = entry.find_matches(token_ids);

        self.build_response(overlap, tenant_id, entry.block_size)
    }

    /// Query overlap scores using pre-computed `LocalBlockHash` values,
    /// skipping the XXH3 hashing step. This is useful when the caller has
    /// already computed block hashes (e.g. from a previous query or from
    /// a shared hash cache).
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

        self.build_response(overlap, tenant_id, entry.block_size)
    }

    /// Build a `QueryResponse` from overlap scores.
    fn build_response(
        &self,
        overlap: OverlapScores,
        tenant_id: &str,
        block_size: u32,
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

            imd.dp.insert(dp_rank_str, matched_tokens);
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
                    block_size: value.block_size,
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
    pub block_size: u32,
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
        let entry = indexer.get_or_create("model-a", "tenant-1", 4);
        assert_eq!(entry.block_size, 4);

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
        let resp = indexer.query("model-a", "tenant-1", &tokens).unwrap();
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
        let entry = indexer.get_or_create("model-b", "t1", 4);

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
        let resp = indexer.query("model-b", "t1", &tokens_a).unwrap();
        let tenant = &resp.tenants["t1"];

        let imd1 = &tenant["inst-1"];
        assert!(imd1.xpu > 0, "inst-1 should have XPU match for tokens_a");
        assert_eq!(imd1.cpu, 0, "inst-1 should have no CPU match");

        // Query with tokens_b — should match inst-2 (CPU) only
        let resp = indexer.query("model-b", "t1", &tokens_b).unwrap();
        let tenant = &resp.tenants["t1"];

        let imd2 = &tenant["inst-2"];
        assert_eq!(imd2.xpu, 0, "inst-2 should have no XPU match");
        assert!(imd2.cpu > 0, "inst-2 should have CPU match for tokens_b");
    }

    #[test]
    fn test_no_indexer_error() {
        let indexer = Indexer::new();
        let err = indexer.query("no-such-model", "default", &[1, 2, 3, 4]);
        assert!(err.is_err());
        assert!(matches!(
            err.unwrap_err(),
            KvConductorError::NoIndexer { .. }
        ));
    }
}
