// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Single-threaded Radix Tree for KV cache block indexing.
//!
//! Uses `Rc<RefCell<>>` for shared ownership within a single thread. This is
//! primarily a reference implementation used for testing. Production code
//! should use [`ConcurrentRadixTree`](crate::concurrent_tree::ConcurrentRadixTree).

use std::cell::RefCell;
use std::collections::VecDeque;
use std::rc::Rc;

use rustc_hash::{FxHashMap, FxHashSet};

use crate::error::KvConductorError;
use crate::protocols::*;

/// Shared reference to a [`RadixBlock`].
type SharedRadixBlock = Rc<RefCell<RadixBlock>>;

/// A node in the radix tree.
#[derive(Debug)]
pub(crate) struct RadixBlock {
    /// Child blocks keyed by their local block hash (token-content hash).
    pub(crate) children: FxHashMap<LocalBlockHash, SharedRadixBlock>,
    /// Workers that have this block cached.
    pub(crate) workers: FxHashSet<WorkerKey>,
    /// The sequence-level block hash for this node (None for root).
    pub(crate) block_hash: Option<SequenceBlockHash>,
    /// Recent access times for frequency tracking (optional).
    #[allow(dead_code)]
    pub(crate) recent_uses: VecDeque<std::time::Instant>,
}

impl RadixBlock {
    pub fn new() -> Self {
        Self {
            children: FxHashMap::default(),
            workers: FxHashSet::default(),
            block_hash: None,
            recent_uses: VecDeque::new(),
        }
    }

    pub fn with_hash(block_hash: SequenceBlockHash) -> Self {
        Self {
            children: FxHashMap::default(),
            workers: FxHashSet::default(),
            block_hash: Some(block_hash),
            recent_uses: VecDeque::new(),
        }
    }

    /// Remove a worker from this block. If the block becomes empty, clear children.
    #[inline]
    fn drop_worker(&mut self, worker: &WorkerKey) {
        self.workers.remove(worker);
        if self.workers.is_empty() {
            self.children.clear();
        }
    }
}

/// A single-threaded radix (prefix) tree for KV cache block indexing.
///
/// Provides O(path_length) lookup for matching token sequences against cached
/// blocks, and O(1) per-worker block access via a reverse-lookup table.
pub struct RadixTree {
    /// Root node (contains no block_hash, only children).
    pub(crate) root: SharedRadixBlock,

    /// Per-worker lookup: WorkerKey -> (SequenceBlockHash -> Block).
    /// Provides O(1) access to any cached block for event processing.
    pub(crate) lookup: FxHashMap<WorkerKey, FxHashMap<SequenceBlockHash, SharedRadixBlock>>,
}

impl Default for RadixTree {
    fn default() -> Self {
        Self::new()
    }
}

// Custom drop to avoid stack overflow from recursive Rc drops.
impl Drop for RadixTree {
    fn drop(&mut self) {
        let mut stack: Vec<SharedRadixBlock> = Vec::new();

        // Drain root's children
        {
            let mut root = self.root.borrow_mut();
            stack.extend(root.children.drain().map(|(_, v)| v));
        }

        // Drain all lookup references
        for (_, worker_blocks) in self.lookup.drain() {
            stack.extend(worker_blocks.into_values());
        }

        // Iteratively free uniquely-owned blocks
        while let Some(block) = stack.pop() {
            match Rc::try_unwrap(block) {
                Ok(cell) => {
                    let inner: RadixBlock = cell.into_inner();
                    stack.extend(inner.children.into_values());
                }
                Err(rc) => {
                    drop(rc);
                }
            }
        }
    }
}

impl RadixTree {
    /// Create a new empty radix tree.
    pub fn new() -> Self {
        Self {
            root: Rc::new(RefCell::new(RadixBlock::new())),
            lookup: FxHashMap::default(),
        }
    }

    /// Find matches for a sequence of `LocalBlockHash` values.
    ///
    /// Traverses the tree from root, tracking which workers have all blocks in
    /// the prefix. Returns per-worker match depths.
    pub fn find_matches(&self, sequence: &[LocalBlockHash]) -> OverlapScores {
        let mut scores = OverlapScores::new();

        if sequence.is_empty() {
            return scores;
        }

        // Get first child from root
        let first_child = {
            let root_borrow = self.root.borrow();
            root_borrow.children.get(&sequence[0]).cloned()
        };

        let Some(first_child) = first_child else {
            return scores;
        };

        // Initialize active worker set from first child
        let mut active: FxHashSet<WorkerKey> = {
            let borrow = first_child.borrow();
            borrow.workers.clone()
        };

        if active.is_empty() {
            return scores;
        }

        let mut current = first_child;
        let mut matched_depth = 1u32;

        // Traverse remaining levels
        for item in sequence.iter().skip(1) {
            let next_block = {
                let current_borrow = current.borrow();
                current_borrow.children.get(item).cloned()
            };

            let Some(block) = next_block else {
                break;
            };

            // Reconcile active workers with child's workers (intersection).
            // Workers can only drop out (never join) as we descend along a
            // single path, so we record scores for workers that disappear at
            // this level. We always perform the membership check — cardinality
            // alone is not sufficient because different worker sets may have
            // the same size.
            {
                let borrow = block.borrow();
                let child_workers = &borrow.workers;

                let dropouts: Vec<WorkerKey> = active
                    .iter()
                    .filter(|w| !child_workers.contains(w))
                    .cloned()
                    .collect();
                for w in &dropouts {
                    scores.update_score(w.clone(), matched_depth);
                    active.remove(w);
                }
            }

            if active.is_empty() {
                break;
            }

            current = block;
            matched_depth += 1;
        }

        // Record scores for surviving workers
        for worker in &active {
            scores.update_score(worker.clone(), matched_depth);
        }

        scores
    }

    /// Apply a single KV cache event to the radix tree.
    ///
    /// Supports `Stored` (insert blocks), `Removed` (remove blocks), and
    /// `Cleared` (remove all blocks for a worker).
    pub fn apply_event(
        &mut self,
        instance_id: &str,
        dp_rank: u32,
        event: &KvCacheEventData,
    ) -> Result<(), KvConductorError> {
        let worker = WorkerKey {
            instance_id: instance_id.to_string(),
            backend_id: instance_id.to_string(),
            dp_rank,
            medium: StorageMedium::Xpu,
        };

        let worker_lookup = self.lookup.entry(worker.clone()).or_default();

        match event {
            KvCacheEventData::Stored(store_data) => {
                // Find parent block
                let mut current = match store_data.parent_hash {
                    Some(parent) => {
                        let parent_key = SequenceBlockHash(parent.try_into().unwrap());
                        match worker_lookup.get(&parent_key) {
                            Some(block) => block.clone(),
                            None => {
                                return Err(KvConductorError::ParentBlockNotFound);
                            }
                        }
                    }
                    None => self.root.clone(),
                };

                let mut needs_worker_insert = false;

                for block_data in &store_data.blocks {
                    let tokens_hash = LocalBlockHash(block_data.tokens_hash);
                    let seq_hash = SequenceBlockHash(block_data.block_hash);

                    let mut parent_mut = current.borrow_mut();

                    // Insert worker into this block (deferred from previous iteration)
                    if needs_worker_insert {
                        parent_mut.workers.insert(worker.clone());
                    }
                    needs_worker_insert = true;

                    let child = match parent_mut.children.get(&tokens_hash) {
                        Some(block) => {
                            // Verify block_hash consistency
                            if block.borrow().block_hash != Some(seq_hash) {
                                tracing::warn!(
                                    instance_id = %worker.instance_id,
                                    dp_rank = worker.dp_rank,
                                    expected = ?seq_hash,
                                    actual = ?block.borrow().block_hash,
                                    "block_hash mismatch in radix tree"
                                );
                            }
                            block.clone()
                        }
                        None => {
                            // Look for existing block in worker's lookup or create new
                            let new_block =
                                worker_lookup.get(&seq_hash).cloned().unwrap_or_else(|| {
                                    Rc::new(RefCell::new(RadixBlock::with_hash(seq_hash)))
                                });

                            parent_mut.children.insert(tokens_hash, new_block.clone());
                            new_block
                        }
                    };

                    // Detect self-referencing blocks
                    if child.try_borrow_mut().is_err() {
                        return Err(KvConductorError::InvalidBlockSequence);
                    }

                    // Update reverse lookup
                    worker_lookup.insert(seq_hash, child.clone());

                    drop(parent_mut);
                    current = child;
                }

                // Insert worker into the last block
                if needs_worker_insert {
                    current.borrow_mut().workers.insert(worker);
                }

                Ok(())
            }

            KvCacheEventData::Removed { block_hashes } => {
                for block_hash in block_hashes {
                    let seq_hash = SequenceBlockHash(*block_hash);
                    if let Some(block) = worker_lookup.remove(&seq_hash) {
                        block.borrow_mut().drop_worker(&worker);
                    } else {
                        tracing::debug!(
                            instance_id = %worker.instance_id,
                            dp_rank = worker.dp_rank,
                            ?block_hash,
                            "block not found for removal (already evicted or never stored)"
                        );
                    }
                }
                Ok(())
            }

            KvCacheEventData::Cleared => {
                self.clear_all_blocks(&worker);
                Ok(())
            }
        }
    }

    /// Remove all blocks for a specific worker.
    pub fn clear_all_blocks(&mut self, worker: &WorkerKey) {
        if let Some(blocks) = self.lookup.get_mut(worker) {
            for (_, block) in blocks.iter() {
                block.borrow_mut().drop_worker(worker);
            }
            blocks.clear();
        }
    }

    /// Completely remove a worker from the tree.
    pub fn remove_worker(&mut self, instance_id: &str, dp_rank: u32) {
        let key = WorkerKey {
            instance_id: instance_id.to_string(),
            backend_id: instance_id.to_string(),
            dp_rank,
            medium: StorageMedium::Xpu,
        };
        if let Some((_, blocks)) = self.lookup.remove_entry(&key) {
            for (_, block) in blocks {
                block.borrow_mut().drop_worker(&key);
            }
        }
    }

    /// Get the total number of cached blocks across all workers.
    pub fn total_blocks(&self) -> usize {
        self.lookup.values().map(|m| m.len()).sum()
    }

    /// Get the set of all tracked worker keys.
    pub fn get_workers(&self) -> Vec<WorkerKey> {
        self.lookup.keys().cloned().collect()
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

    fn make_store_event(parent_hash: Option<i64>, blocks: Vec<(u64, u64)>) -> KvCacheEventData {
        KvCacheEventData::Stored(KvCacheStoreData {
            parent_hash,
            start_position: None,
            blocks: blocks
                .into_iter()
                .map(|(bh, th)| KvCacheStoredBlockData {
                    block_hash: bh,
                    tokens_hash: th,
                })
                .collect(),
        })
    }

    fn make_remove_event(block_hashes: Vec<u64>) -> KvCacheEventData {
        KvCacheEventData::Removed { block_hashes }
    }

    #[test]
    fn test_empty_tree() {
        let tree = RadixTree::new();
        let scores = tree.find_matches(&[LocalBlockHash(1)]);
        assert!(scores.is_empty());
    }

    #[test]
    fn test_single_block_store_and_match() {
        let mut tree = RadixTree::new();

        // Store: root -> block(tokens_hash=1, seq_hash=100) for worker-A dp0
        tree.apply_event("A", 0, &make_store_event(None, vec![(100, 1)]))
            .unwrap();

        // Query: exact match
        let scores = tree.find_matches(&[LocalBlockHash(1)]);
        assert_eq!(scores.scores.get(&make_worker("A", 0)).copied(), Some(1));
    }

    #[test]
    fn test_multi_block_chain() {
        let mut tree = RadixTree::new();

        // Store chain: root -> block(1) -> block(2) -> block(3)
        tree.apply_event("W1", 0, &make_store_event(None, vec![(100, 1)]))
            .unwrap();
        tree.apply_event("W1", 0, &make_store_event(Some(100), vec![(200, 2)]))
            .unwrap();
        tree.apply_event("W1", 0, &make_store_event(Some(200), vec![(300, 3)]))
            .unwrap();

        // Full match
        let scores = tree.find_matches(&[LocalBlockHash(1), LocalBlockHash(2), LocalBlockHash(3)]);
        assert_eq!(scores.scores.get(&make_worker("W1", 0)).copied(), Some(3));

        // Partial match
        let scores = tree.find_matches(&[LocalBlockHash(1), LocalBlockHash(2)]);
        assert_eq!(scores.scores.get(&make_worker("W1", 0)).copied(), Some(2));

        // No match
        let scores = tree.find_matches(&[LocalBlockHash(99)]);
        assert!(scores.is_empty());
    }

    #[test]
    fn test_multiple_workers() {
        let mut tree = RadixTree::new();

        // Both workers share prefix block(1), then diverge
        tree.apply_event("W1", 0, &make_store_event(None, vec![(100, 1)]))
            .unwrap();
        tree.apply_event("W1", 0, &make_store_event(Some(100), vec![(200, 2)]))
            .unwrap();

        tree.apply_event("W2", 0, &make_store_event(None, vec![(100, 1)]))
            .unwrap();
        tree.apply_event("W2", 0, &make_store_event(Some(100), vec![(300, 3)]))
            .unwrap();

        // Both match on first block
        let scores = tree.find_matches(&[LocalBlockHash(1)]);
        assert_eq!(scores.scores.get(&make_worker("W1", 0)).copied(), Some(1));
        assert_eq!(scores.scores.get(&make_worker("W2", 0)).copied(), Some(1));

        // W1 matches deeper on path (1,2), W2 only matches first
        let scores = tree.find_matches(&[LocalBlockHash(1), LocalBlockHash(2)]);
        assert_eq!(scores.scores.get(&make_worker("W1", 0)).copied(), Some(2));
        assert_eq!(scores.scores.get(&make_worker("W2", 0)).copied(), Some(1));
    }

    #[test]
    fn test_dp_rank_isolation() {
        let mut tree = RadixTree::new();

        tree.apply_event("W1", 0, &make_store_event(None, vec![(100, 1)]))
            .unwrap();
        tree.apply_event("W1", 1, &make_store_event(None, vec![(200, 2)]))
            .unwrap();

        // dp_rank=0 has block 1
        let scores = tree.find_matches(&[LocalBlockHash(1)]);
        assert_eq!(scores.scores.get(&make_worker("W1", 0)).copied(), Some(1));
        assert!(!scores.scores.contains_key(&make_worker("W1", 1)));

        // dp_rank=1 has block 2
        let scores = tree.find_matches(&[LocalBlockHash(2)]);
        assert_eq!(scores.scores.get(&make_worker("W1", 1)).copied(), Some(1));
        assert!(!scores.scores.contains_key(&make_worker("W1", 0)));
    }

    #[test]
    fn test_remove_event() {
        let mut tree = RadixTree::new();

        tree.apply_event("W1", 0, &make_store_event(None, vec![(100, 1), (200, 2)]))
            .unwrap();

        assert_eq!(tree.total_blocks(), 2);

        // Remove block 200
        tree.apply_event("W1", 0, &make_remove_event(vec![200]))
            .unwrap();

        assert_eq!(tree.total_blocks(), 1);

        // Query: only block 1 remains
        let scores = tree.find_matches(&[LocalBlockHash(1)]);
        assert_eq!(scores.scores.get(&make_worker("W1", 0)).copied(), Some(1));

        let scores = tree.find_matches(&[LocalBlockHash(1), LocalBlockHash(2)]);
        assert_eq!(scores.scores.get(&make_worker("W1", 0)).copied(), Some(1));
    }

    #[test]
    fn test_clear_event() {
        let mut tree = RadixTree::new();

        tree.apply_event("W1", 0, &make_store_event(None, vec![(100, 1), (200, 2)]))
            .unwrap();

        tree.apply_event("W1", 0, &KvCacheEventData::Cleared)
            .unwrap();

        assert_eq!(tree.total_blocks(), 0);
        let scores = tree.find_matches(&[LocalBlockHash(1)]);
        assert!(scores.is_empty());
    }

    #[test]
    fn test_remove_worker() {
        let mut tree = RadixTree::new();

        tree.apply_event("W1", 0, &make_store_event(None, vec![(100, 1)]))
            .unwrap();
        tree.apply_event("W2", 0, &make_store_event(None, vec![(100, 1)]))
            .unwrap();

        assert_eq!(tree.lookup.len(), 2);

        tree.remove_worker("W1", 0);

        assert_eq!(tree.lookup.len(), 1);
        assert!(tree.lookup.contains_key(&make_worker("W2", 0)));

        let scores = tree.find_matches(&[LocalBlockHash(1)]);
        assert!(!scores.scores.contains_key(&make_worker("W1", 0)));
        assert_eq!(scores.scores.get(&make_worker("W2", 0)).copied(), Some(1));
    }
}
