// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Per-(model, tenant) radix tree indexer with per-medium scoring.
//!
//! Each `IndexerEntry` manages:
//!
//! - **HBM tree** (`hbm_tree`) — prefix-chain radix tree for XPU/GPU blocks
//!   (weight ×3). Only HBM blocks need prefix-chain matching.
//! - **CPU flat map** (`cpu_blocks`) — ``tokens_hash → {workers}`` (weight ×2).
//! - **Disk flat map** (`disk_blocks`) — ``tokens_hash → {workers}`` (weight ×1).
//! - **non_hbm_cache** — engine offload ``block_hash → tokens_hash`` for
//!   two-phase pool confirmation.
//!
//! Scoring: each matched HBM block = 3 pts, CPU block = 2 pts, disk block = 1 pt.
//! A DP's total score is the sum across all its media.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

use dashmap::DashMap;
use parking_lot::RwLock;
use rustc_hash::FxHashMap;
use rustc_hash::FxHashSet;

use crate::concurrent_tree::{ConcurrentRadixTree, WorkerLookup};
use crate::error::KvConductorError;
use crate::hashing::compute_block_hash_for_seq;
use crate::protocols::*;

/// Number of HBM removals after which `sweep_stale_nodes` is triggered
/// to reclaim orphan tree nodes.
const HBM_SWEEP_THRESHOLD: u64 = 1000;

/// Minimum number of block hashes to trigger parallel CPU/Disk flat lookup.
const FLAT_PAR_THRESHOLD: usize = 4096;

/// Per-worker reverse-lookup for non-HBM flat stores.
/// Maps ``SequenceBlockHash → LocalBlockHash(XXH3 tokens_hash)``.
type FlatLookup = FxHashMap<u64, u64>;

/// Key identifying a unique indexer instance: (model_name, tenant_id).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct IndexerKey {
    pub model_name: String,
    pub tenant_id: String,
}

/// An indexer entry for one (model, tenant) pair.
pub struct IndexerEntry {
    /// HBM prefix-chain radix tree (XPU workers, weight ×3).
    pub hbm_tree: Arc<ConcurrentRadixTree>,
    /// HBM per-worker reverse lookups: WorkerKey → WorkerLookup.
    pub lookups: Arc<RwLock<FxHashMap<WorkerKey, WorkerLookup>>>,

    /// CPU flat blocks: tokens_hash → set of workers with this block cached.
    pub cpu_blocks: Arc<RwLock<FxHashMap<LocalBlockHash, FxHashSet<WorkerKey>>>>,
    /// CPU per-worker reverse lookups: WorkerKey → (seq_hash → tokens_hash).
    cpu_lookups: Arc<RwLock<FxHashMap<WorkerKey, FlatLookup>>>,

    /// Disk flat blocks: tokens_hash → set of workers with this block cached.
    pub disk_blocks: Arc<RwLock<FxHashMap<LocalBlockHash, FxHashSet<WorkerKey>>>>,
    /// Disk per-worker reverse lookups.
    disk_lookups: Arc<RwLock<FxHashMap<WorkerKey, FlatLookup>>>,

    /// Cache for non-HBM engine offloading events.
    pub non_hbm_cache: Arc<RwLock<FxHashMap<u64, u64>>>,

    /// Count of HBM block removals since last sweep. When this exceeds
    /// `HBM_SWEEP_THRESHOLD`, `sweep_stale_nodes` is called to reclaim
    /// orphan tree nodes that accumulate after worker drops.
    hbm_removal_count: AtomicU64,
}

impl Default for IndexerEntry {
    fn default() -> Self {
        Self {
            hbm_tree: Arc::new(ConcurrentRadixTree::new()),
            lookups: Arc::new(RwLock::new(FxHashMap::default())),
            cpu_blocks: Arc::new(RwLock::new(FxHashMap::default())),
            cpu_lookups: Arc::new(RwLock::new(FxHashMap::default())),
            disk_blocks: Arc::new(RwLock::new(FxHashMap::default())),
            disk_lookups: Arc::new(RwLock::new(FxHashMap::default())),
            non_hbm_cache: Arc::new(RwLock::new(FxHashMap::default())),
            hbm_removal_count: AtomicU64::new(0),
        }
    }
}

impl IndexerEntry {
    pub fn new() -> Self {
        Self::default()
    }

    // -----------------------------------------------------------------------
    // Query
    // -----------------------------------------------------------------------

    pub fn find_matches(
        &self,
        token_ids: &[i64],
        block_size: u32,
        cfg: &ScoringConfig,
    ) -> OverlapScores {
        let t_hash = std::time::Instant::now();
        let block_hashes = compute_block_hash_for_seq(token_ids, block_size);
        let hash_us = t_hash.elapsed().as_micros();

        let scores = self.find_matches_by_hash(&block_hashes, cfg);

        tracing::debug!(
            num_tokens = token_ids.len(),
            block_size,
            num_hashes = block_hashes.len(),
            hash_us,
            scores = scores.scores.len(),
            "hash_computed"
        );
        scores
    }

    pub fn find_matches_by_hash(
        &self,
        block_hashes: &[LocalBlockHash],
        cfg: &ScoringConfig,
    ) -> OverlapScores {
        let mut scores = OverlapScores::default();

        let hbm_scores = self.hbm_tree.find_matches(block_hashes);
        for (worker, depth) in hbm_scores.scores {
            scores.add_score(worker, depth * cfg.hbm_weight);
        }

        if block_hashes.len() > FLAT_PAR_THRESHOLD {
            self.flat_lookup_parallel(block_hashes, cfg, &mut scores);
        } else {
            self.flat_lookup_sequential(block_hashes, cfg, &mut scores);
        }

        scores
    }

    /// Sequential CPU + Disk flat lookup (for small hash sets).
    fn flat_lookup_sequential(
        &self,
        block_hashes: &[LocalBlockHash],
        cfg: &ScoringConfig,
        scores: &mut OverlapScores,
    ) {
        let cpu = self.cpu_blocks.read();
        for hash in block_hashes {
            if let Some(workers) = cpu.get(hash) {
                for w in workers {
                    scores.add_score(w.clone(), cfg.cpu_weight);
                }
            }
        }
        drop(cpu);

        let disk = self.disk_blocks.read();
        for hash in block_hashes {
            if let Some(workers) = disk.get(hash) {
                for w in workers {
                    scores.add_score(w.clone(), cfg.disk_weight);
                }
            }
        }
    }

    /// Parallel CPU + Disk flat lookup using rayon (for large hash sets,
    /// e.g. DeepSeek V4 with 32K+ block hashes).
    fn flat_lookup_parallel(
        &self,
        block_hashes: &[LocalBlockHash],
        cfg: &ScoringConfig,
        scores: &mut OverlapScores,
    ) {
        use rayon::prelude::*;

        let cpu = self.cpu_blocks.read();
        let disk = self.disk_blocks.read();

        let (cpu_scores, disk_scores): (OverlapScores, OverlapScores) = rayon::join(
            || {
                block_hashes
                    .par_iter()
                    .fold(OverlapScores::default, |mut acc, hash| {
                        if let Some(workers) = cpu.get(hash) {
                            for w in workers {
                                acc.add_score(w.clone(), cfg.cpu_weight);
                            }
                        }
                        acc
                    })
                    .reduce(OverlapScores::default, |mut a, b| {
                        a.merge(b);
                        a
                    })
            },
            || {
                block_hashes
                    .par_iter()
                    .fold(OverlapScores::default, |mut acc, hash| {
                        if let Some(workers) = disk.get(hash) {
                            for w in workers {
                                acc.add_score(w.clone(), cfg.disk_weight);
                            }
                        }
                        acc
                    })
                    .reduce(OverlapScores::default, |mut a, b| {
                        a.merge(b);
                        a
                    })
            },
        );

        scores.merge(cpu_scores);
        scores.merge(disk_scores);
    }

    // -----------------------------------------------------------------------
    // Non-HBM cache (unchanged two-phase protocol)
    // -----------------------------------------------------------------------

    #[inline]
    pub fn cache_non_hbm_block(&self, block_hash: u64, tokens_hash: u64) {
        self.non_hbm_cache.write().insert(block_hash, tokens_hash);
    }

    #[inline]
    pub fn lookup_cached_tokens_hash(&self, block_hash: u64) -> Option<u64> {
        self.non_hbm_cache.read().get(&block_hash).copied()
    }

    #[inline]
    pub fn evict_cached_block(&self, block_hash: u64) {
        self.non_hbm_cache.write().remove(&block_hash);
    }

    // -----------------------------------------------------------------------
    // Flat (CPU/Disk) store / remove helpers
    // -----------------------------------------------------------------------

    /// Insert a block into the CPU or Disk flat store.
    fn flat_store(
        blocks: &RwLock<FxHashMap<LocalBlockHash, FxHashSet<WorkerKey>>>,
        lookups: &RwLock<FxHashMap<WorkerKey, FlatLookup>>,
        worker: &WorkerKey,
        tokens_hash: u64,
        seq_hash: u64,
    ) {
        // Update flat block set: tokens_hash → {workers}
        {
            let mut map = blocks.write();
            map.entry(LocalBlockHash(tokens_hash))
                .or_default()
                .insert(worker.clone());
        }
        // Update per-worker reverse lookup for removal
        {
            let mut lu = lookups.write();
            lu.entry(worker.clone())
                .or_default()
                .insert(seq_hash, tokens_hash);
        }
        tracing::trace!(
            instance_id = %worker.instance_id,
            dp_rank = worker.dp_rank,
            medium = %worker.medium.as_str(),
            ?seq_hash,
            tokens_hash,
            "flat store"
        );
    }

    /// Remove a block from the CPU or Disk flat store.
    fn flat_remove(
        blocks: &RwLock<FxHashMap<LocalBlockHash, FxHashSet<WorkerKey>>>,
        lookups: &RwLock<FxHashMap<WorkerKey, FlatLookup>>,
        worker: &WorkerKey,
        seq_hash: u64,
    ) {
        // Find tokens_hash from per-worker reverse lookup
        let tokens_hash = {
            let lu = lookups.read();
            lu.get(worker).and_then(|m| m.get(&seq_hash).copied())
        };

        if let Some(th) = tokens_hash {
            // Remove worker from the flat set
            let mut map = blocks.write();
            if let Some(set) = map.get_mut(&LocalBlockHash(th)) {
                set.remove(worker);
                if set.is_empty() {
                    map.remove(&LocalBlockHash(th));
                }
            }
            // Clean up reverse lookup
            let mut lu = lookups.write();
            if let Some(m) = lu.get_mut(worker) {
                m.remove(&seq_hash);
            }
        }
    }

    /// Clear all CPU/Disk flat blocks for a worker.
    fn flat_clear(
        blocks: &RwLock<FxHashMap<LocalBlockHash, FxHashSet<WorkerKey>>>,
        lookups: &RwLock<FxHashMap<WorkerKey, FlatLookup>>,
        worker: &WorkerKey,
    ) {
        // Collect tokens_hashes from reverse lookup
        let tokens_hashes: Vec<LocalBlockHash> = {
            let lu = lookups.read();
            lu.get(worker)
                .map(|m| m.values().map(|&th| LocalBlockHash(th)).collect())
                .unwrap_or_default()
        };
        // Remove worker from each block set
        if !tokens_hashes.is_empty() {
            let mut map = blocks.write();
            for th in &tokens_hashes {
                if let Some(set) = map.get_mut(th) {
                    set.remove(worker);
                    if set.is_empty() {
                        map.remove(th);
                    }
                }
            }
        }
        // Clear reverse lookup
        lookups.write().remove(worker);
    }

    /// Trigger a sweep of stale HBM tree nodes if the removal counter
    /// exceeds the threshold. Called after each HBM remove/clear.
    fn maybe_sweep_hbm(&self) {
        let count = self.hbm_removal_count.fetch_add(1, Ordering::Relaxed) + 1;
        if count.is_multiple_of(HBM_SWEEP_THRESHOLD) {
            let pruned = self.hbm_tree.sweep_stale_nodes();
            if pruned > 0 {
                tracing::debug!(pruned, total_removals = count, "swept stale HBM tree nodes");
            }
        }
    }

    /// Apply a KV cache event for a specific worker, dispatching to the
    /// correct data structure based on storage medium.
    pub fn apply_event(
        &self,
        worker: &WorkerKey,
        event: &KvCacheEventData,
    ) -> Result<(), KvConductorError> {
        match event {
            KvCacheEventData::Stored(store_data) => {
                match worker.medium {
                    StorageMedium::Xpu | StorageMedium::Unknown => {
                        // HBM: prefix-chain tree insert
                        let mut lookups = self.lookups.write();
                        let lookup = lookups.entry(worker.clone()).or_default();
                        self.hbm_tree.apply_store(worker, lookup, store_data)
                    }
                    StorageMedium::Cpu => {
                        // CPU: flat insert
                        for block in &store_data.blocks {
                            Self::flat_store(
                                &self.cpu_blocks,
                                &self.cpu_lookups,
                                worker,
                                block.tokens_hash,
                                block.block_hash,
                            );
                        }
                        Ok(())
                    }
                    StorageMedium::Disk => {
                        // Disk: flat insert
                        for block in &store_data.blocks {
                            Self::flat_store(
                                &self.disk_blocks,
                                &self.disk_lookups,
                                worker,
                                block.tokens_hash,
                                block.block_hash,
                            );
                        }
                        Ok(())
                    }
                }
            }
            KvCacheEventData::Removed { block_hashes } => match worker.medium {
                StorageMedium::Xpu | StorageMedium::Unknown => {
                    let mut lookups = self.lookups.write();
                    let lookup = lookups.entry(worker.clone()).or_default();
                    let result = self.hbm_tree.apply_remove(worker, lookup, block_hashes);
                    self.maybe_sweep_hbm();
                    result
                }
                StorageMedium::Cpu => {
                    for &h in block_hashes {
                        Self::flat_remove(&self.cpu_blocks, &self.cpu_lookups, worker, h);
                    }
                    Ok(())
                }
                StorageMedium::Disk => {
                    for &h in block_hashes {
                        Self::flat_remove(&self.disk_blocks, &self.disk_lookups, worker, h);
                    }
                    Ok(())
                }
            },
            KvCacheEventData::Cleared => {
                match worker.medium {
                    StorageMedium::Xpu | StorageMedium::Unknown => {
                        let mut lookups = self.lookups.write();
                        let lookup = lookups.entry(worker.clone()).or_default();
                        self.hbm_tree.remove_worker(worker, lookup);
                        self.maybe_sweep_hbm();
                    }
                    StorageMedium::Cpu => {
                        Self::flat_clear(&self.cpu_blocks, &self.cpu_lookups, worker);
                    }
                    StorageMedium::Disk => {
                        Self::flat_clear(&self.disk_blocks, &self.disk_lookups, worker);
                    }
                }
                Ok(())
            }
        }
    }

    /// Remove all cache entries for a given instance and DP rank across
    /// **all** storage media.
    pub fn remove_worker_all_media(&self, instance_id: &str, dp_rank: u32) {
        // HBM tree
        {
            let mut lookups = self.lookups.write();
            let matching: Vec<WorkerKey> = lookups
                .keys()
                .filter(|k| k.instance_id == instance_id && k.dp_rank == dp_rank)
                .cloned()
                .collect();
            for wk in &matching {
                if let Some(lookup) = lookups.get_mut(wk) {
                    self.hbm_tree.remove_worker(wk, lookup);
                }
                lookups.remove(wk);
            }
        }
        // CPU flat — collect keys first to avoid holding read lock
        // across the write in flat_clear.
        {
            let cpu_matches: Vec<WorkerKey> = {
                let cpu_lu = self.cpu_lookups.read();
                cpu_lu
                    .keys()
                    .filter(|wk| wk.instance_id == instance_id && wk.dp_rank == dp_rank)
                    .cloned()
                    .collect()
            };
            for wk in &cpu_matches {
                Self::flat_clear(&self.cpu_blocks, &self.cpu_lookups, wk);
            }
        }
        // Disk flat — same pattern
        {
            let disk_matches: Vec<WorkerKey> = {
                let disk_lu = self.disk_lookups.read();
                disk_lu
                    .keys()
                    .filter(|wk| wk.instance_id == instance_id && wk.dp_rank == dp_rank)
                    .cloned()
                    .collect()
            };
            for wk in &disk_matches {
                Self::flat_clear(&self.disk_blocks, &self.disk_lookups, wk);
            }
        }
    }

    /// Get the total number of cached blocks across all workers and media.
    pub fn total_blocks(&self) -> usize {
        let hbm = self.lookups.read().values().map(|l| l.len()).sum::<usize>();
        let cpu = self
            .cpu_lookups
            .read()
            .values()
            .map(|l| l.len())
            .sum::<usize>();
        let disk = self
            .disk_lookups
            .read()
            .values()
            .map(|l| l.len())
            .sum::<usize>();
        hbm + cpu + disk
    }

    /// Get all registered worker keys.
    pub fn worker_keys(&self) -> Vec<WorkerKey> {
        let mut keys: Vec<WorkerKey> = self.lookups.read().keys().cloned().collect();
        keys.extend(self.cpu_lookups.read().keys().cloned());
        keys.extend(self.disk_lookups.read().keys().cloned());
        keys
    }
}

/// Top-level indexer managing multiple (model, tenant) trees.
pub struct Indexer {
    entries: DashMap<IndexerKey, Arc<IndexerEntry>>,
    scoring: ScoringConfig,
}

impl Indexer {
    pub fn new(scoring: ScoringConfig) -> Self {
        Self {
            entries: DashMap::new(),
            scoring,
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

    /// Remove an indexer entry if it has no more workers across any medium.
    pub fn remove_if_empty(&self, model_name: &str, tenant_id: &str) {
        let key = IndexerKey {
            model_name: model_name.to_string(),
            tenant_id: tenant_id.to_string(),
        };
        let should_remove = self.entries.get(&key).is_some_and(|e| {
            let entry = e.value();
            entry.lookups.read().is_empty()
                && entry.cpu_lookups.read().is_empty()
                && entry.disk_lookups.read().is_empty()
                && entry.non_hbm_cache.read().is_empty()
        });
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
    ) -> Result<QueryResponse, KvConductorError> {
        let t0 = std::time::Instant::now();

        let entry = self
            .get(model_name, tenant_id)
            .ok_or_else(|| KvConductorError::NoIndexer {
                model_name: model_name.to_string(),
                tenant_id: tenant_id.to_string(),
            })?;

        let overlap = entry.find_matches(token_ids, block_size, &self.scoring);
        let t_tree = t0.elapsed();

        let resp = self.build_response(overlap, model_name, tenant_id, block_size);
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

        let overlap = entry.find_matches_by_hash(block_hashes, &self.scoring);
        // Default to 1 token per hash (no scaling) since we don't know the
        // original block_size from the hash alone.
        self.build_response(overlap, model_name, tenant_id, 1)
    }

    /// Build a `QueryResponse` from weighted overlap scores.
    fn build_response(
        &self,
        overlap: OverlapScores,
        model_name: &str,
        tenant_id: &str,
        block_size: u32,
    ) -> Result<QueryResponse, KvConductorError> {
        if overlap.is_empty() {
            return Err(KvConductorError::NoWorkers {
                model_name: model_name.to_string(),
                tenant_id: tenant_id.to_string(),
            });
        }

        let mut instance_data: HashMap<String, InstanceMatchData> = HashMap::new();

        for (worker, &score) in &overlap.scores {
            let dp_rank_str = worker.dp_rank.to_string();

            // Derive per-medium block count from the score and weight.
            let matched_blocks = match worker.medium {
                StorageMedium::Xpu | StorageMedium::Unknown => score / self.scoring.hbm_weight,
                StorageMedium::Cpu => score / self.scoring.cpu_weight,
                StorageMedium::Disk => score / self.scoring.disk_weight,
            };
            let matched_tokens = matched_blocks * block_size;

            let imd = instance_data.entry(worker.instance_id.clone()).or_default();

            imd.longest_matched = imd.longest_matched.max(matched_tokens);

            let dp_score = imd.dp.entry(dp_rank_str).or_default();
            match worker.medium {
                StorageMedium::Xpu | StorageMedium::Unknown => {
                    dp_score.xpu_score = dp_score.xpu_score.max(score);
                    dp_score.xpu_blocks = dp_score.xpu_blocks.max(matched_blocks);
                }
                StorageMedium::Cpu => {
                    dp_score.cpu_score = dp_score.cpu_score.max(score);
                    dp_score.cpu_blocks = dp_score.cpu_blocks.max(matched_blocks);
                }
                StorageMedium::Disk => {
                    dp_score.disk_score = dp_score.disk_score.max(score);
                    dp_score.disk_blocks = dp_score.disk_blocks.max(matched_blocks);
                }
            }
            dp_score.matched_tokens = dp_score.matched_tokens.max(matched_tokens);
            dp_score.total = dp_score.xpu_score + dp_score.cpu_score + dp_score.disk_score;
        }

        // Compute total_score once per instance after all DPs have been populated.
        for imd in instance_data.values_mut() {
            imd.total_score = imd.dp.values().map(|s| s.total).sum();
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
        Self::new(ScoringConfig::default())
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
        let indexer = Indexer::new(ScoringConfig::default());
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
        let resp = indexer.query("model-a", "tenant-1", &tokens, 4).unwrap();
        let tenant = &resp.tenants["tenant-1"];
        let imd = &tenant["inst-1"];
        let dp0 = &imd.dp["0"];
        assert!(dp0.xpu_blocks > 0, "should have XPU match");
        assert_eq!(dp0.cpu_blocks, 0);
        assert_eq!(dp0.disk_blocks, 0);
        assert!(dp0.matched_tokens > 0);
        assert_eq!(imd.longest_matched, dp0.matched_tokens);
    }

    #[test]
    fn test_per_tier_aggregation() {
        let indexer = Indexer::new(ScoringConfig::default());
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
        let resp = indexer.query("model-b", "t1", &tokens_a, 4).unwrap();
        let tenant = &resp.tenants["t1"];

        let imd1 = &tenant["inst-1"];
        let dp0 = &imd1.dp["0"];
        assert!(
            dp0.xpu_blocks > 0,
            "inst-1 should have XPU match for tokens_a"
        );
        assert_eq!(dp0.cpu_blocks, 0, "inst-1 should have no CPU match");

        // Query with tokens_b — should match inst-2 (CPU) only
        let resp = indexer.query("model-b", "t1", &tokens_b, 4).unwrap();
        let tenant = &resp.tenants["t1"];

        let imd2 = &tenant["inst-2"];
        let dp2 = &imd2.dp["0"];
        assert_eq!(dp2.xpu_blocks, 0, "inst-2 should have no XPU match");
        assert!(
            dp2.cpu_blocks > 0,
            "inst-2 should have CPU match for tokens_b"
        );
    }

    #[test]
    fn test_no_indexer_error() {
        let indexer = Indexer::new(ScoringConfig::default());
        let err = indexer.query("no-such-model", "default", &[1, 2, 3, 4], 4);
        assert!(err.is_err());
        assert!(matches!(
            err.unwrap_err(),
            KvConductorError::NoIndexer { .. }
        ));
    }
}
