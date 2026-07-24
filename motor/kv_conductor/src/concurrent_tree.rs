// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Thread-safe Concurrent Radix Tree for KV cache block indexing.
//!
//! Uses `Arc<parking_lot::RwLock<Block>>` per node, enabling:
//! - Multiple concurrent `find_matches` (read locks only)
//! - Exclusive `apply_event` (write locks with hand-over-hand ordering)
//!
//! The reverse lookup table is maintained externally (by [`Indexer`](crate::indexer::Indexer))
//! and passed in during event application.

use std::sync::Arc;

use parking_lot::RwLock;
use rustc_hash::{FxHashMap, FxHashSet};

use crate::error::KvConductorError;
use crate::protocols::*;

/// Thread-safe shared reference to a Block node.
pub type SharedBlock = Arc<RwLock<Block>>;

/// A node in the concurrent radix tree.
#[derive(Debug, Default)]
pub struct Block {
    /// Child blocks keyed by their local block hash (token-content hash).
    pub children: FxHashMap<LocalBlockHash, SharedBlock>,
    /// Workers that have this block cached.
    /// Uses `Arc` copy-on-write: the query hot path (`find_matches`) only
    /// bumps a reference count instead of cloning the entire set (including
    /// all inner `String` fields). Mutations use `Arc::make_mut`.
    pub workers: Arc<FxHashSet<WorkerKey>>,
    /// The sequence-level block hash for this node (None for root).
    pub block_hash: Option<SequenceBlockHash>,
}

impl Block {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_hash(block_hash: SequenceBlockHash) -> Self {
        Self {
            children: FxHashMap::default(),
            workers: Arc::new(FxHashSet::default()),
            block_hash: Some(block_hash),
        }
    }

    /// Remove a worker from this block. If the block becomes empty, also clear
    /// children to allow memory reclamation.
    #[inline]
    pub fn drop_worker(&mut self, worker: &WorkerKey) {
        Arc::make_mut(&mut self.workers).remove(worker);
        if self.workers.is_empty() {
            self.children.clear();
        }
    }
}

/// Per-worker reverse lookup table type.
///
/// Maps `SequenceBlockHash -> SharedBlock` for each worker, enabling O(1)
/// parent-block lookup during event application.
pub type WorkerLookup = FxHashMap<SequenceBlockHash, SharedBlock>;

/// A thread-safe radix tree for concurrent KV cache lookups and updates.
///
/// # Concurrency Model
///
/// - `find_matches`: Acquires read locks on traversed nodes only. Multiple
///   concurrent callers can traverse the tree simultaneously.
/// - `apply_event`: Acquires write locks with hand-over-hand ordering
///   (always lock parent before child). The external `lookup` must already
///   be locked by the caller.
/// - `remove_worker`: Acquires write locks on affected nodes.
pub struct ConcurrentRadixTree {
    /// Root node of the tree. Contains no `block_hash`, only children.
    root: SharedBlock,
}

impl Default for ConcurrentRadixTree {
    fn default() -> Self {
        Self::new()
    }
}

impl ConcurrentRadixTree {
    /// Create a new empty concurrent radix tree.
    pub fn new() -> Self {
        Self {
            root: Arc::new(RwLock::new(Block::new())),
        }
    }

    // -----------------------------------------------------------------------
    // Query
    // -----------------------------------------------------------------------

    /// Find matches for a sequence of `LocalBlockHash` values.
    ///
    /// Returns per-worker overlap scores indicating the depth of the longest
    /// matching prefix for each worker.
    pub fn find_matches(&self, sequence: &[LocalBlockHash]) -> OverlapScores {
        let t0 = std::time::Instant::now();
        let mut scores = OverlapScores::default();

        if sequence.is_empty() {
            return scores;
        }

        // Get first child from root (read lock)
        let first_child = {
            let root_guard = self.root.read();
            root_guard.children.get(&sequence[0]).cloned()
        };

        let Some(first_child) = first_child else {
            tracing::trace!(seq_len = sequence.len(), "tree miss at root");
            return scores;
        };

        // Initialize active workers from first child.
        // Arc clone is O(1) refcount bump instead of O(n) set clone.
        let mut active: FxHashSet<WorkerKey> = {
            let guard = first_child.read();
            (*guard.workers).clone()
        };

        if active.is_empty() {
            return scores;
        }

        let mut current = first_child;
        let mut matched_depth = 1u32;

        // Traverse remaining levels with read locks
        for item in sequence.iter().skip(1) {
            let next_block = {
                let current_guard = current.read();
                current_guard.children.get(item).cloned()
            };

            let Some(block) = next_block else {
                break;
            };

            // Short-circuit: if only 1 worker remains, just check whether
            // this single worker is in the child's set.
            if active.len() == 1 {
                let w = active.iter().next().cloned().unwrap();
                let in_child = {
                    let guard = block.read();
                    guard.workers.contains(&w)
                };
                if in_child {
                    current = block;
                    matched_depth += 1;
                    continue;
                } else {
                    scores.update_score(w, matched_depth);
                    active.clear();
                    break;
                }
            }

            // Reconcile: remove workers that don't have this child block.
            // Use retain to avoid a second Vec allocation.
            let guard = block.read();
            active.retain(|w| {
                if guard.workers.contains(w) {
                    true
                } else {
                    scores.update_score(w.clone(), matched_depth);
                    false
                }
            });
            drop(guard);

            if active.is_empty() {
                break;
            }

            current = block;
            matched_depth += 1;
        }

        // Drain surviving workers into scores (avoid clone)
        for worker in active.drain() {
            scores.update_score(worker, matched_depth);
        }

        tracing::debug!(
            seq_len = sequence.len(),
            depth = matched_depth,
            active_workers = scores.scores.len(),
            elapsed_us = t0.elapsed().as_micros(),
            "find_matches"
        );
        scores
    }

    // -----------------------------------------------------------------------
    // Mutation
    // -----------------------------------------------------------------------

    /// Apply a `Stored` event to the tree.
    ///
    /// `lookup` is the per-worker reverse-lookup table (must already be
    /// locked for exclusive access by the caller).
    pub fn apply_store(
        &self,
        worker: &WorkerKey,
        lookup: &mut WorkerLookup,
        store_data: &KvCacheStoreData,
    ) -> Result<(), KvConductorError> {
        // Find the parent block
        let mut current: SharedBlock = match store_data.parent_hash {
            Some(parent) => {
                let parent_key = SequenceBlockHash(parent);
                let block = lookup
                    .get(&parent_key)
                    .cloned()
                    .ok_or(KvConductorError::ParentBlockNotFound)?;
                tracing::trace!(
                    ?parent_key,
                    num_blocks = store_data.blocks.len(),
                    "chaining to parent block via parent_hash"
                );
                block
            }
            None => {
                tracing::trace!(
                    num_blocks = store_data.blocks.len(),
                    "starting new chain from root"
                );
                Arc::clone(&self.root)
            }
        };

        let mut needs_worker_insert = false;

        for block_data in &store_data.blocks {
            let tokens_hash = LocalBlockHash(block_data.tokens_hash);
            let seq_hash = SequenceBlockHash(block_data.block_hash);

            let child = {
                let mut parent_mut = current.write();

                // Insert worker into this block (deferred from previous iteration)
                if needs_worker_insert {
                    Arc::make_mut(&mut parent_mut.workers).insert(worker.clone());
                }
                needs_worker_insert = true;

                match parent_mut.children.get(&tokens_hash) {
                    Some(existing) => {
                        // A self-referencing block (existing == current) would
                        // deadlock on existing.read() below because current is
                        // already write-locked. Check first and bail out.
                        if Arc::ptr_eq(existing, &current) {
                            return Err(KvConductorError::InvalidBlockSequence);
                        }

                        // Verify block_hash consistency (debug check)
                        let existing_guard = existing.read();
                        if existing_guard.block_hash != Some(seq_hash) {
                            tracing::debug!(
                                instance_id = %worker.instance_id,
                                dp_rank = worker.dp_rank,
                                ?seq_hash,
                                ?existing_guard.block_hash,
                                "block_hash mismatch in store event"
                            );
                        }
                        drop(existing_guard);
                        Arc::clone(existing)
                    }
                    None => {
                        // Try to find existing block in lookup, or create new
                        let new_block = lookup
                            .get(&seq_hash)
                            .cloned()
                            .unwrap_or_else(|| Arc::new(RwLock::new(Block::with_hash(seq_hash))));

                        parent_mut
                            .children
                            .insert(tokens_hash, Arc::clone(&new_block));
                        new_block
                    }
                }
            };

            // Detect self-referencing blocks (child == current -> deadlock)
            if Arc::ptr_eq(&child, &current) {
                return Err(KvConductorError::InvalidBlockSequence);
            }

            // Update reverse lookup
            lookup.insert(seq_hash, Arc::clone(&child));
            tracing::trace!(
                seq_hash = ?seq_hash,
                lookup_len = lookup.len(),
                "stored block in lookup"
            );

            current = child;
        }

        // Insert worker into the final block
        if needs_worker_insert {
            Arc::make_mut(&mut current.write().workers).insert(worker.clone());
        }

        tracing::trace!(final_lookup_len = lookup.len(), "apply_store finished");

        Ok(())
    }

    /// Apply a `Removed` event to the tree.
    ///
    /// Blocks already absent (e.g. async delivery ordering) are silently
    /// skipped — this is not an error condition. Only debug-logged.
    pub fn apply_remove(
        &self,
        worker: &WorkerKey,
        lookup: &mut WorkerLookup,
        block_hashes: &[u64],
    ) -> Result<(), KvConductorError> {
        for block_hash in block_hashes {
            let seq_hash = SequenceBlockHash(*block_hash);

            if let Some(block) = lookup.remove(&seq_hash) {
                block.write().drop_worker(worker);
            } else {
                tracing::trace!(
                    instance_id = %worker.instance_id,
                    dp_rank = worker.dp_rank,
                    ?block_hash,
                    "block not found for removal (already evicted or never stored)"
                );
            }
        }

        Ok(())
    }

    /// Remove a worker entirely, cleaning up all tree references.
    pub fn remove_worker(&self, worker: &WorkerKey, lookup: &mut WorkerLookup) {
        for (_, block) in lookup.iter() {
            block.write().drop_worker(worker);
        }
        lookup.clear();
    }

    // -----------------------------------------------------------------------
    // Maintenance
    // -----------------------------------------------------------------------

    /// Sweep the tree and remove empty child nodes (no workers, no children).
    ///
    /// Each node checks its direct children for emptiness under a write lock
    /// and removes those that are fully stale. This is safe to call while
    /// concurrent reads and writes are in progress — a concurrent write that
    /// adds a worker to a child between the read and the write lock will cause
    /// that child to be kept (the `retain` predicate re-checks under write).
    ///
    /// Returns the number of pruned child references.
    pub fn sweep_stale_nodes(&self) -> usize {
        let mut pruned = 0usize;
        // Stack of (node, visited_children) for iterative DFS
        let mut stack: Vec<SharedBlock> = vec![Arc::clone(&self.root)];

        while let Some(block) = stack.pop() {
            // Prune empty direct children under write lock
            {
                let mut guard = block.write();
                let before = guard.children.len();
                guard.children.retain(|_, child| {
                    let c = child.read();
                    !c.workers.is_empty() || !c.children.is_empty()
                });
                pruned += before - guard.children.len();
            }

            // Enqueue remaining children for recursive pruning
            let guard = block.read();
            for child in guard.children.values() {
                stack.push(Arc::clone(child));
            }
        }

        pruned
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_worker(instance_id: &str, dp_rank: u32) -> WorkerKey {
        WorkerKey {
            instance_id: instance_id.to_string(),
            backend_id: instance_id.to_string(),
            dp_rank,
            medium: StorageMedium::Xpu,
        }
    }

    fn make_store(parent_hash: Option<u64>, blocks: Vec<(u64, u64)>) -> KvCacheStoreData {
        KvCacheStoreData {
            parent_hash,
            start_position: None,
            blocks: blocks
                .into_iter()
                .map(|(bh, th)| KvCacheStoredBlockData {
                    block_hash: bh,
                    tokens_hash: th,
                })
                .collect(),
        }
    }

    #[test]
    fn test_concurrent_find_matches_empty() {
        let tree = ConcurrentRadixTree::new();
        let scores = tree.find_matches(&[LocalBlockHash(1)]);
        assert!(scores.is_empty());
    }

    #[test]
    fn test_store_and_find() {
        let tree = ConcurrentRadixTree::new();
        let w1 = make_worker("W1", 0);
        let mut lookup: WorkerLookup = FxHashMap::default();

        tree.apply_store(&w1, &mut lookup, &make_store(None, vec![(100, 1)]))
            .unwrap();

        let scores = tree.find_matches(&[LocalBlockHash(1)]);
        assert_eq!(scores.scores.get(&w1).copied(), Some(1));
    }

    #[test]
    fn test_multi_block_chain_concurrent() {
        let tree = ConcurrentRadixTree::new();
        let w1 = make_worker("W1", 0);
        let mut lookup: WorkerLookup = FxHashMap::default();

        tree.apply_store(&w1, &mut lookup, &make_store(None, vec![(100, 1)]))
            .unwrap();
        tree.apply_store(&w1, &mut lookup, &make_store(Some(100), vec![(200, 2)]))
            .unwrap();
        tree.apply_store(&w1, &mut lookup, &make_store(Some(200), vec![(300, 3)]))
            .unwrap();

        let scores = tree.find_matches(&[LocalBlockHash(1), LocalBlockHash(2), LocalBlockHash(3)]);
        assert_eq!(scores.scores.get(&w1).copied(), Some(3));
    }

    #[test]
    fn test_remove_blocks() {
        let tree = ConcurrentRadixTree::new();
        let w1 = make_worker("W1", 0);
        let mut lookup: WorkerLookup = FxHashMap::default();

        tree.apply_store(
            &w1,
            &mut lookup,
            &make_store(None, vec![(100, 1), (200, 2)]),
        )
        .unwrap();

        assert_eq!(lookup.len(), 2);

        tree.apply_remove(&w1, &mut lookup, &[200]).unwrap();

        assert_eq!(lookup.len(), 1);

        let scores = tree.find_matches(&[LocalBlockHash(1), LocalBlockHash(2)]);
        // Only first block matches since second was removed
        assert_eq!(scores.scores.get(&w1).copied(), Some(1));
    }

    #[test]
    fn test_clear_worker() {
        let tree = ConcurrentRadixTree::new();
        let w1 = make_worker("W1", 0);
        let mut lookup: WorkerLookup = FxHashMap::default();

        tree.apply_store(
            &w1,
            &mut lookup,
            &make_store(None, vec![(100, 1), (200, 2)]),
        )
        .unwrap();

        tree.remove_worker(&w1, &mut lookup);

        assert!(lookup.is_empty());
        let scores = tree.find_matches(&[LocalBlockHash(1)]);
        assert!(scores.is_empty());
    }

    #[test]
    fn test_two_workers_share_prefix() {
        let tree = ConcurrentRadixTree::new();
        let w1 = make_worker("I1", 0);
        let w2 = make_worker("I2", 0);
        let mut lookup1: WorkerLookup = FxHashMap::default();
        let mut lookup2: WorkerLookup = FxHashMap::default();

        // Both share first block
        tree.apply_store(&w1, &mut lookup1, &make_store(None, vec![(100, 1)]))
            .unwrap();
        tree.apply_store(&w2, &mut lookup2, &make_store(None, vec![(100, 1)]))
            .unwrap();

        // Then diverge
        tree.apply_store(&w1, &mut lookup1, &make_store(Some(100), vec![(200, 2)]))
            .unwrap();
        tree.apply_store(&w2, &mut lookup2, &make_store(Some(100), vec![(300, 3)]))
            .unwrap();

        // Both match first block
        let scores = tree.find_matches(&[LocalBlockHash(1)]);
        assert_eq!(scores.scores.get(&w1).copied(), Some(1));
        assert_eq!(scores.scores.get(&w2).copied(), Some(1));

        // Path (1, 2) matches W1 deeper
        let scores = tree.find_matches(&[LocalBlockHash(1), LocalBlockHash(2)]);
        assert_eq!(scores.scores.get(&w1).copied(), Some(2));
        assert_eq!(scores.scores.get(&w2).copied(), Some(1));
    }
}
