// SPDX-FileCopyrightText: Copyright (c) 2024-2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-FileCopyrightText: Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// SPDX-License-Identifier: Apache-2.0
//
// This file is a Derivative Work of NVIDIA Dynamo kv-router
// (https://github.com/ai-dynamo/dynamo), originally licensed under the
// Apache License, Version 2.0. Upstream source path:
//   lib/kv-router/src/indexer/lower_tier.rs
//
// You may obtain a copy of the Apache License at:
//   http://www.apache.org/licenses/LICENSE-2.0
// Local copy: licenses/Apache-2.0.txt
// Attribution: THIRD_PARTY_NOTICES.md
//
// Modified by Huawei Technologies Co., Ltd. for MindIE-PyMotor KV Conductor
// (RwLock + per-worker reverse index, ContiguousHit API, WorkerKey/medium
// integration). Huawei modifications are also available under Mulan PSL v2
// (http://license.coscl.org.cn/MulanPSL2). Redistribution of this file must
// still comply with Apache License 2.0.

//! Lower-tier (CPU / Disk) continuation-edge index.
//!
//! Derived from NVIDIA Dynamo kv-router `LowerTierIndexer`
//! (`lib/kv-router/src/indexer/lower_tier.rs`, Apache-2.0). See
//! `THIRD_PARTY_NOTICES.md`.
//!
//! Stores worker ownership over shared continuation edges:
//! ``(parent_sequence_hash, local_hash) -> child_sequence_hash``.
//!
//! Unlike the HBM radix tree, this index does **not** score from root by
//! default. Queries continue from caller-provided per-worker continuation
//! points (HBM → CPU; max(HBM, CPU) → Disk) and count how many
//! **consecutive** lower-tier blocks are present. The caller also issues an
//! unconditional root-walk candidate per worker owning the first edge, so a
//! longer full replica on this tier is never hidden by a shorter upstream
//! hit; the walk keeps the candidate with the farthest absolute end.

use parking_lot::RwLock;
use rustc_hash::{FxHashMap, FxHashSet};

use crate::protocols::{KvCacheStoreData, LocalBlockHash, SequenceBlockHash, WorkerKey};

type WorkerSet = FxHashSet<WorkerKey>;

/// Edge key in the continuation graph.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
struct TransitionKey {
    parent_hash: Option<SequenceBlockHash>,
    local_hash: LocalBlockHash,
}

#[derive(Debug, Clone)]
enum EdgeOwnersEntry {
    Single {
        child_hash: SequenceBlockHash,
        owner: WorkerKey,
    },
    Multi {
        child_hash: SequenceBlockHash,
        owners: WorkerSet,
    },
}

impl EdgeOwnersEntry {
    fn new(child_hash: SequenceBlockHash, owner: WorkerKey) -> Self {
        Self::Single { child_hash, owner }
    }

    fn child_hash(&self) -> SequenceBlockHash {
        match self {
            Self::Single { child_hash, .. } | Self::Multi { child_hash, .. } => *child_hash,
        }
    }

    /// Insert `owner` for this edge. Returns `false` if `child_hash` conflicts
    /// with the existing mapping (first-writer wins).
    fn insert(&mut self, child_hash: SequenceBlockHash, owner: WorkerKey) -> bool {
        match self {
            Self::Single {
                child_hash: existing_hash,
                owner: existing_owner,
            } => {
                if *existing_hash != child_hash {
                    return false;
                }
                if *existing_owner == owner {
                    return true;
                }
                let mut owners = WorkerSet::default();
                owners.insert(existing_owner.clone());
                owners.insert(owner);
                *self = Self::Multi { child_hash, owners };
                true
            }
            Self::Multi {
                child_hash: existing_hash,
                owners,
            } => {
                if *existing_hash != child_hash {
                    return false;
                }
                owners.insert(owner);
                true
            }
        }
    }

    /// Remove `owner`. Returns `true` if the edge should be deleted.
    fn remove(&mut self, owner: &WorkerKey) -> bool {
        match self {
            Self::Single {
                owner: existing_owner,
                ..
            } => existing_owner == owner,
            Self::Multi { child_hash, owners } => {
                if !owners.remove(owner) {
                    return false;
                }
                if owners.is_empty() {
                    return true;
                }
                if owners.len() == 1 {
                    let remaining = owners.iter().next().cloned().unwrap();
                    *self = Self::Single {
                        child_hash: *child_hash,
                        owner: remaining,
                    };
                }
                false
            }
        }
    }

    fn contains(&self, owner: &WorkerKey) -> bool {
        match self {
            Self::Single {
                owner: existing_owner,
                ..
            } => existing_owner == owner,
            Self::Multi { owners, .. } => owners.contains(owner),
        }
    }

    fn collect_workers(&self) -> Vec<WorkerKey> {
        match self {
            Self::Single { owner, .. } => vec![owner.clone()],
            Self::Multi { owners, .. } => owners.iter().cloned().collect(),
        }
    }
}

/// Where a lower-tier walk should resume for one worker.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LowerTierContinuation {
    pub start_pos: usize,
    pub last_matched_hash: Option<SequenceBlockHash>,
}

impl LowerTierContinuation {
    pub fn new(start_pos: usize, last_matched_hash: SequenceBlockHash) -> Self {
        Self {
            start_pos,
            last_matched_hash: Some(last_matched_hash),
        }
    }

    pub fn from_root(start_pos: usize) -> Self {
        Self {
            start_pos,
            last_matched_hash: None,
        }
    }
}

/// Result of a contiguous lower-tier walk for one worker.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ContiguousHit {
    /// Number of blocks matched from ``start_pos``.
    pub count: usize,
    /// Absolute start index in the query hash sequence.
    pub start_pos: usize,
    /// Sequence hash of the last matched block (for the next tier).
    pub last_matched_hash: Option<SequenceBlockHash>,
}

impl ContiguousHit {
    /// Absolute end index (exclusive) — next tier continues here.
    pub fn end_pos(&self) -> usize {
        self.start_pos.saturating_add(self.count)
    }
}

/// Continuation-edge index for one lower-tier medium (CPU or Disk).
#[derive(Debug, Default)]
pub struct LowerTierIndexer {
    edges: RwLock<FxHashMap<TransitionKey, EdgeOwnersEntry>>,
    /// Per-worker reverse lookup: ``block_hash ->TransitionKey`` for O(1) remove.
    worker_blocks: RwLock<FxHashMap<WorkerKey, FxHashMap<SequenceBlockHash, TransitionKey>>>,
}

impl LowerTierIndexer {
    pub fn new() -> Self {
        Self::default()
    }

    /// Workers owning the root edge for ``local_hash`` (parent = None).
    pub fn root_workers(&self, local_hash: LocalBlockHash) -> Vec<WorkerKey> {
        self.edge_owners(None, local_hash)
    }

    /// Workers owning edge ``(parent_hash, local_hash)``.
    pub fn edge_owners(
        &self,
        parent_hash: Option<SequenceBlockHash>,
        local_hash: LocalBlockHash,
    ) -> Vec<WorkerKey> {
        let key = TransitionKey {
            parent_hash,
            local_hash,
        };
        self.edges
            .read()
            .get(&key)
            .map(|e| e.collect_workers())
            .unwrap_or_default()
    }

    /// Insert a stored chain as continuation edges.
    pub fn store_blocks(&self, worker: &WorkerKey, store_data: &KvCacheStoreData) {
        let mut parent_hash = store_data.parent_hash.map(SequenceBlockHash);
        let mut worker_blocks = self.worker_blocks.write();
        let worker_map = worker_blocks.entry(worker.clone()).or_default();
        let mut edges = self.edges.write();

        for block in &store_data.blocks {
            let child = SequenceBlockHash(block.block_hash);
            let key = TransitionKey {
                parent_hash,
                local_hash: LocalBlockHash(block.tokens_hash),
            };

            // Conflicting reverse mapping for the same block_hash ->stop chain.
            if worker_map
                .get(&child)
                .is_some_and(|existing| *existing != key)
            {
                break;
            }

            let inserted = match edges.get_mut(&key) {
                Some(edge) => edge.insert(child, worker.clone()),
                None => {
                    edges.insert(key, EdgeOwnersEntry::new(child, worker.clone()));
                    true
                }
            };

            if !inserted {
                break;
            }

            worker_map.insert(child, key);
            parent_hash = Some(child);
        }
    }

    /// Remove blocks by engine sequence hash.
    pub fn remove_blocks(&self, worker: &WorkerKey, block_hashes: &[u64]) {
        let mut worker_blocks = self.worker_blocks.write();
        let Some(worker_map) = worker_blocks.get_mut(worker) else {
            return;
        };
        let mut edges = self.edges.write();

        for &h in block_hashes {
            let seq = SequenceBlockHash(h);
            let Some(key) = worker_map.remove(&seq) else {
                continue;
            };
            if let Some(edge) = edges.get_mut(&key) {
                if edge.remove(worker) {
                    edges.remove(&key);
                }
            }
        }

        if worker_map.is_empty() {
            worker_blocks.remove(worker);
        }
    }

    /// Drop all edges owned by ``worker``.
    pub fn clear_worker(&self, worker: &WorkerKey) {
        let mut worker_blocks = self.worker_blocks.write();
        let Some(worker_map) = worker_blocks.remove(worker) else {
            return;
        };
        let mut edges = self.edges.write();
        for (_, key) in worker_map {
            if let Some(edge) = edges.get_mut(&key) {
                if edge.remove(worker) {
                    edges.remove(&key);
                }
            }
        }
    }

    /// Look up `(parent_hash, tokens_hash)` for an engine `block_hash` owned
    /// by any worker in this tier.
    ///
    /// Used when a later pool medium (e.g. Disk) confirms a block that was
    /// already indexed on another lower tier (e.g. CPU): reuse the content
    /// mapping without requiring a fresh engine offload event.
    pub fn lookup_block(&self, block_hash: u64) -> Option<(Option<u64>, u64)> {
        let seq = SequenceBlockHash(block_hash);
        let worker_blocks = self.worker_blocks.read();
        for worker_map in worker_blocks.values() {
            if let Some(key) = worker_map.get(&seq) {
                return Some((key.parent_hash.map(|h| h.0), key.local_hash.0));
            }
        }
        None
    }

    /// Whether any worker currently owns ``block_hash``.
    pub fn contains_block(&self, block_hash: u64) -> bool {
        let seq = SequenceBlockHash(block_hash);
        self.worker_blocks
            .read()
            .values()
            .any(|m| m.contains_key(&seq))
    }

    /// Number of blocks tracked for ``worker``.
    pub fn worker_block_count(&self, worker: &WorkerKey) -> usize {
        self.worker_blocks
            .read()
            .get(worker)
            .map(|m| m.len())
            .unwrap_or(0)
    }

    /// Total blocks across all workers (sum of reverse-lookup sizes).
    pub fn total_blocks(&self) -> usize {
        self.worker_blocks.read().values().map(|m| m.len()).sum()
    }

    /// All workers that currently own at least one edge.
    pub fn worker_keys(&self) -> Vec<WorkerKey> {
        self.worker_blocks.read().keys().cloned().collect()
    }

    pub fn is_empty(&self) -> bool {
        self.worker_blocks.read().is_empty()
    }

    /// For each worker, walk contiguous lower-tier hits from its continuations.
    ///
    /// A worker may have several candidate continuations (a root walk plus one
    /// or more upstream-breakpoint continuations). Each candidate is walked
    /// independently and the one with the **farthest absolute end** wins —
    /// coverage semantics stay correct no matter which candidate is longer.
    pub fn query_contiguous_hits(
        &self,
        local_hashes: &[LocalBlockHash],
        continuations: &FxHashMap<WorkerKey, Vec<LowerTierContinuation>>,
    ) -> FxHashMap<WorkerKey, ContiguousHit> {
        let mut hits = FxHashMap::default();
        let edges = self.edges.read();

        for (worker, conts) in continuations {
            let mut best: Option<ContiguousHit> = None;
            for cont in conts {
                let mut cur_pos = cont.start_pos;
                let mut cur_hash = cont.last_matched_hash;
                let start = cur_pos;

                while cur_pos < local_hashes.len() {
                    let key = TransitionKey {
                        parent_hash: cur_hash,
                        local_hash: local_hashes[cur_pos],
                    };
                    let Some(edge) = edges.get(&key) else {
                        break;
                    };
                    if !edge.contains(worker) {
                        break;
                    }
                    cur_hash = Some(edge.child_hash());
                    cur_pos += 1;
                }

                let hit = ContiguousHit {
                    count: cur_pos.saturating_sub(start),
                    start_pos: start,
                    last_matched_hash: if cur_pos > start { cur_hash } else { None },
                };
                // Keep the candidate with the farthest absolute end (ties:
                // later candidate wins — same end implies the same last hash).
                best = match best {
                    Some(b) if hit.end_pos() >= b.end_pos() => Some(hit),
                    Some(b) => Some(b),
                    None => Some(hit),
                };
            }
            if let Some(b) = best {
                hits.insert(worker.clone(), b);
            }
        }

        hits
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocols::{KvCacheStoredBlockData, StorageMedium};

    fn worker(id: &str) -> WorkerKey {
        WorkerKey {
            instance_id: id.into(),
            backend_id: id.into(),
            dp_rank: 0,
            medium: StorageMedium::Cpu,
        }
    }

    fn store(parent: Option<u64>, blocks: &[(u64, u64)]) -> KvCacheStoreData {
        KvCacheStoreData {
            parent_hash: parent,
            start_position: None,
            blocks: blocks
                .iter()
                .map(|&(bh, th)| KvCacheStoredBlockData {
                    block_hash: bh,
                    tokens_hash: th,
                })
                .collect(),
        }
    }

    #[test]
    fn root_chain_full_match() {
        let idx = LowerTierIndexer::new();
        let w = worker("w1");
        idx.store_blocks(&w, &store(None, &[(101, 11), (102, 12)]));

        let mut conts = FxHashMap::default();
        conts.insert(w.clone(), vec![LowerTierContinuation::from_root(0)]);
        let hits = idx.query_contiguous_hits(&[LocalBlockHash(11), LocalBlockHash(12)], &conts);
        assert_eq!(hits.get(&w).map(|h| h.count), Some(2));
    }

    #[test]
    fn mid_chain_continuation_from_parent() {
        let idx = LowerTierIndexer::new();
        let w = worker("w1");
        // Tail only: parent=999, then local 21,22
        idx.store_blocks(&w, &store(Some(999), &[(201, 21), (202, 22)]));

        let mut conts = FxHashMap::default();
        conts.insert(
            w.clone(),
            vec![LowerTierContinuation::new(2, SequenceBlockHash(999))],
        );
        let query = [
            LocalBlockHash(1),
            LocalBlockHash(2),
            LocalBlockHash(21),
            LocalBlockHash(22),
        ];
        let hits = idx.query_contiguous_hits(&query, &conts);
        assert_eq!(hits.get(&w).map(|h| h.count), Some(2));
        assert_eq!(
            hits.get(&w).and_then(|h| h.last_matched_hash),
            Some(SequenceBlockHash(202))
        );
        assert_eq!(hits.get(&w).map(|h| h.end_pos()), Some(4));
    }

    #[test]
    fn remove_breaks_contiguous_walk() {
        let idx = LowerTierIndexer::new();
        let w = worker("w1");
        idx.store_blocks(&w, &store(None, &[(101, 11), (102, 12), (103, 13)]));
        idx.remove_blocks(&w, &[102]);

        let mut conts = FxHashMap::default();
        conts.insert(w.clone(), vec![LowerTierContinuation::from_root(0)]);
        let hits = idx.query_contiguous_hits(
            &[LocalBlockHash(11), LocalBlockHash(12), LocalBlockHash(13)],
            &conts,
        );
        // First edge remains; walk stops at missing middle edge.
        assert_eq!(hits.get(&w).map(|h| h.count), Some(1));
    }

    #[test]
    fn shared_edge_remove_preserves_other_owner() {
        let idx = LowerTierIndexer::new();
        let a = worker("a");
        let b = worker("b");
        idx.store_blocks(&a, &store(None, &[(101, 11), (102, 12)]));
        idx.store_blocks(&b, &store(None, &[(101, 11), (102, 12)]));
        idx.remove_blocks(&a, &[101, 102]);

        let mut conts = FxHashMap::default();
        conts.insert(a.clone(), vec![LowerTierContinuation::from_root(0)]);
        conts.insert(b.clone(), vec![LowerTierContinuation::from_root(0)]);
        let hits = idx.query_contiguous_hits(&[LocalBlockHash(11), LocalBlockHash(12)], &conts);
        assert_eq!(hits.get(&a).map(|h| h.count), Some(0));
        assert_eq!(hits.get(&b).map(|h| h.count), Some(2));
    }
}
