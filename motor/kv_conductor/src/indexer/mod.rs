// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Per-(model, tenant) radix tree indexer with per-medium block matching.
//!
//! Each `IndexerEntry` manages:
//!
//! - **HBM tree** (`hbm_tree`) — prefix-chain radix tree for NPU blocks.
//! - **CPU / Disk continuation indexes** (`cpu_tiers` / `disk_tiers`) —
//!   ``(parent_seq_hash, tokens_hash) → child`` edges (see `lower_tier`
//!   and `THIRD_PARTY_NOTICES.md`). CPU continues from the HBM
//!   breakpoint; Disk continues from ``max(HBM, CPU)`` (CPU preferred
//!   when it extends further). Root chains are walked unconditionally so
//!   longer lower-tier replicas are never hidden by shorter upstream hits.
//! - **offload_pool_state** — bidirectional offload/pool event matching
//!   (see [`OffloadPoolState`]). The `offload` side now also carries the
//!   originating `parent_hash` so that lower-tier continuation edges are
//!   correctly chained once the pool backend confirms placement.
//!
//! Query results report exclusive per-medium matched blocks and unweighted
//! coverage `matched_tokens` (sum of exclusive blocks × `block_size`).
//! Tier affinity weights are applied by the Coordinator scheduler.

use std::collections::HashMap;
use std::sync::Arc;

use dashmap::DashMap;
use parking_lot::RwLock;
use rustc_hash::FxHashMap;
use rustc_hash::FxHashSet;
use serde::Serialize;

use crate::concurrent_tree::{ConcurrentRadixTree, PrefixMatch, WorkerLookup};
use crate::error::KvConductorError;
use crate::hashing::compute_block_hash_for_seq;
use crate::lower_tier::{ContiguousHit, LowerTierContinuation, LowerTierIndexer};
use crate::protocols::*;

/// TTL for stale pending pool entries (60 seconds).
const PENDING_TTL: std::time::Duration = std::time::Duration::from_secs(60);
/// TTL for retained `content` entries. Content must survive the CPU→Disk
/// migration window (CPU tier eviction before the Disk store event arrives),
/// so it is deliberately longer than [`PENDING_TTL`]; entries are cleared
/// once the window closes.
const CONTENT_TTL: std::time::Duration = std::time::Duration::from_secs(300);
const OFFLOAD_TTL: std::time::Duration = std::time::Duration::from_secs(600);

/// Retention limits for the asynchronous offload/pool matching caches.
#[derive(Debug, Clone)]
pub struct CacheMaintenanceConfig {
    pub pending_ttl: std::time::Duration,
    pub content_ttl: std::time::Duration,
    pub offload_ttl: std::time::Duration,
}

impl Default for CacheMaintenanceConfig {
    fn default() -> Self {
        Self {
            pending_ttl: PENDING_TTL,
            content_ttl: CONTENT_TTL,
            offload_ttl: OFFLOAD_TTL,
        }
    }
}

/// Per-DP absolute coverage ends (in blocks) on each storage medium.
#[derive(Debug, Clone, Copy, Default)]
struct MediumEnds {
    npu: u32,
    cpu: u32,
    disk: u32,
}

/// Upstream-tier match breakpoint used to continue into the next lower tier.
#[derive(Debug, Clone)]
struct TierBreakpoint {
    instance_id: String,
    dp_rank: DpRank,
    /// Absolute index in the query hash sequence where the next tier starts.
    end_pos: usize,
    /// Sequence hash of the last matched upstream block.
    last_seq: SequenceBlockHash,
}

// ---------------------------------------------------------------------------
// Two-phase offload/pool matching protocol
// ---------------------------------------------------------------------------

/// Hash content shared by the timed offload and retained-content caches.
#[derive(Debug, Clone, Copy)]
pub(crate) struct BlockContent {
    pub(crate) tokens_hash: u64,
    pub(crate) parent_hash: Option<u64>,
}

/// Cached Phase-1 offload mapping: block content plus insertion time.
/// `BlockContent` carries `tokens_hash` plus the optional
/// `parent_hash` (the engine's immediately preceding block in the offload
/// chain, or the chain's original `parent_block_hash` for the first block).
///
/// Carrying `parent_hash` alongside `tokens_hash` allows Phase-2 confirmation
/// to insert this block as a continuation edge from the correct predecessor,
/// rather than always chaining from root.
#[derive(Debug, Clone)]
pub(crate) struct OffloadCacheEntry {
    pub(crate) content: BlockContent,
    pub(crate) inserted_at: std::time::Instant,
}

/// Confirmed content kept for a possible later pool medium (Disk promotion),
/// with insert time for TTL eviction ([`CONTENT_TTL`]).
///
/// Unlike unconfirmed `offload` entries, content survives lower-tier
/// removal so a Disk store event arriving after CPU eviction can still resolve
/// the mapping; the TTL bounds how long it lingers.
#[derive(Debug, Clone)]
pub(crate) struct ContentEntry {
    pub(crate) content: BlockContent,
    pub(crate) inserted_at: std::time::Instant,
}

/// A pool backend event waiting for its corresponding offload event.
///
/// Equality and hashing consider **only** the `worker` field — `inserted_at`
/// is excluded so that `FxHashSet` deduplication works correctly even when
/// the same pool event is delivered multiple times (ZMQ at-most-once).
#[derive(Debug, Clone)]
pub(crate) struct PendingPoolEvent {
    worker: WorkerKey,
    /// When this entry was inserted (for TTL-based eviction).
    inserted_at: std::time::Instant,
}

impl PartialEq for PendingPoolEvent {
    fn eq(&self, other: &Self) -> bool {
        self.worker == other.worker
    }
}
impl Eq for PendingPoolEvent {}

impl std::hash::Hash for PendingPoolEvent {
    fn hash<H: std::hash::Hasher>(&self, state: &mut H) {
        self.worker.hash(state);
    }
}

/// Combined state for the two-phase offload/pool matching protocol.
///
/// **Invariant**: a `block_hash` is never in both `offload` and `content`.
/// After the first pool confirmation, the entry moves from `offload` into
/// `content` — always retained for a possible later Disk promotion (the
/// mapping is a copy of the tier data plus a bounded migration-window
/// residue; `content` is TTL-swept so memory stays bounded).
///
/// Lifecycle:
/// - `offload`: unconfirmed engine offloads. TTL-bounded so a lost pool
///   confirmation/removal event cannot grow memory indefinitely; also cleared
///   eagerly on match or explicit removal.
/// - `content`: confirmed `(tokens_hash, parent_hash)` kept for a later pool
///   medium. **TTL-evicted** ([`CONTENT_TTL`]) — it must survive lower-tier
///   removal so a Disk store arriving after CPU eviction still resolves, and
///   the TTL bounds how long a block that never reaches Disk lingers.
/// - `pending_pool`: pool-first arrivals, TTL [`PENDING_TTL`].
///
/// Uses a single `RwLock` so that cross-cache operations are atomic without
/// lock-ordering deadlock risk.
#[derive(Debug, Default)]
pub(crate) struct OffloadPoolState {
    /// `block_hash → OffloadCacheEntry`: offload events waiting for the
    /// **first** pool confirmation. Swept with the configured offload TTL.
    pub(crate) offload: FxHashMap<u64, OffloadCacheEntry>,
    /// `block_hash → ContentEntry`: retained after the first lower-tier
    /// insert (always, see struct docs). Swept with [`CONTENT_TTL`].
    pub(crate) content: FxHashMap<u64, ContentEntry>,
    /// `block_hash → workers`: pool events waiting for offload `tokens_hash`.
    /// Values are `FxHashSet` to deduplicate repeated deliveries.
    pub(crate) pending_pool: FxHashMap<u64, FxHashSet<PendingPoolEvent>>,
}

/// Key identifying a unique indexer instance: (model_name, tenant_id).
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct IndexerKey {
    pub model_name: String,
    pub tenant_id: String,
}

/// An indexer entry for one (model, tenant) pair.
pub struct IndexerEntry {
    /// HBM prefix-chain radix tree (NPU workers).
    pub hbm_tree: Arc<ConcurrentRadixTree>,
    /// HBM per-worker reverse lookups: WorkerKey → WorkerLookup.
    pub lookups: Arc<RwLock<FxHashMap<WorkerKey, WorkerLookup>>>,

    /// CPU continuation-edge index.
    pub cpu_tiers: Arc<LowerTierIndexer>,
    /// Disk continuation-edge index.
    pub disk_tiers: Arc<LowerTierIndexer>,

    /// Bidirectional offload/pool event matching state.
    /// See [`OffloadPoolState`] for the invariant.
    pub(crate) offload_pool_state: Arc<RwLock<OffloadPoolState>>,

    maintenance: CacheMaintenanceConfig,
}

impl Default for IndexerEntry {
    fn default() -> Self {
        Self::new()
    }
}

impl IndexerEntry {
    pub fn new() -> Self {
        Self::with_config(CacheMaintenanceConfig::default())
    }

    pub fn with_config(maintenance: CacheMaintenanceConfig) -> Self {
        Self {
            hbm_tree: Arc::new(ConcurrentRadixTree::new()),
            lookups: Arc::new(RwLock::new(FxHashMap::default())),
            cpu_tiers: Arc::new(LowerTierIndexer::new()),
            disk_tiers: Arc::new(LowerTierIndexer::new()),
            offload_pool_state: Arc::new(RwLock::new(OffloadPoolState::default())),
            maintenance,
        }
    }

    // -----------------------------------------------------------------------
    // Query
    // -----------------------------------------------------------------------

    pub fn find_matches(&self, token_ids: &[i64], block_size: u32) -> OverlapBlocks {
        let t_hash = std::time::Instant::now();
        let block_hashes = compute_block_hash_for_seq(token_ids, block_size);
        let hash_us = t_hash.elapsed().as_micros();

        let overlap = self.find_matches_by_hash(&block_hashes);

        tracing::debug!(
            num_tokens = token_ids.len(),
            block_size,
            num_hashes = block_hashes.len(),
            hash_us,
            matched_workers = overlap.blocks.len(),
            "hash_computed"
        );
        overlap
    }

    pub fn find_matches_by_hash(&self, block_hashes: &[LocalBlockHash]) -> OverlapBlocks {
        self.find_matches_with_coverage(block_hashes).0
    }

    /// Query with per-DP absolute coverage ends per medium (in blocks).
    ///
    /// Matching still records per-worker segment lengths in `OverlapBlocks`
    /// (for diagnostics / unit tests). Response assembly uses `MediumEnds`
    /// absolute ends, then exclusive-partitions them into `*_blocks`.
    fn find_matches_with_coverage(
        &self,
        block_hashes: &[LocalBlockHash],
    ) -> (OverlapBlocks, FxHashMap<(String, DpRank), MediumEnds>) {
        let mut overlap = OverlapBlocks::default();
        let mut medium_ends: FxHashMap<(String, DpRank), MediumEnds> = FxHashMap::default();

        // 1) HBM prefix match.
        let hbm: FxHashMap<WorkerKey, PrefixMatch> =
            self.hbm_tree.find_matches_detailed(block_hashes);
        for (worker, m) in &hbm {
            if m.depth == 0 {
                continue;
            }
            overlap.add_blocks(worker.clone(), m.depth);
            Self::note_medium_end(
                &mut medium_ends,
                &worker.instance_id,
                worker.dp_rank,
                StorageMedium::Npu,
                m.depth,
            );
        }

        // Breakpoints need last_seq_hash for continuation.
        let hbm_breaks: Vec<TierBreakpoint> = hbm
            .iter()
            .filter(|(_, m)| m.depth > 0)
            .filter_map(|(w, m)| {
                Some(TierBreakpoint {
                    instance_id: w.instance_id.clone(),
                    dp_rank: w.dp_rank,
                    end_pos: m.depth as usize,
                    last_seq: m.last_seq_hash?,
                })
            })
            .collect();

        // 2) CPU: continue from HBM breakpoints; root walk runs for every
        //    worker owning the first edge so longer lower-tier replicas are
        //    never hidden by a shorter upstream hit.
        let cpu_hits = self.lower_tier_lookup(
            block_hashes,
            &hbm_breaks,
            &self.cpu_tiers,
            &mut overlap,
            &mut medium_ends,
        );

        let cpu_breaks: Vec<TierBreakpoint> = cpu_hits
            .iter()
            .filter(|(_, h)| h.count > 0)
            .filter_map(|(w, h)| {
                Some(TierBreakpoint {
                    instance_id: w.instance_id.clone(),
                    dp_rank: w.dp_rank,
                    end_pos: h.end_pos(),
                    last_seq: h.last_matched_hash?,
                })
            })
            .collect();

        // 3) Disk: continue from max(HBM, CPU) per DP (CPU wins when it
        //    extends further — matches vLLM lookup: CPU then Disk after NPU).
        let disk_breaks = Self::merge_tier_breakpoints(&hbm_breaks, &cpu_breaks);
        self.lower_tier_lookup(
            block_hashes,
            &disk_breaks,
            &self.disk_tiers,
            &mut overlap,
            &mut medium_ends,
        );

        (overlap, medium_ends)
    }

    #[inline]
    fn note_medium_end(
        medium_ends: &mut FxHashMap<(String, DpRank), MediumEnds>,
        instance_id: &str,
        dp_rank: DpRank,
        medium: StorageMedium,
        end: u32,
    ) {
        let entry = medium_ends
            .entry((instance_id.to_string(), dp_rank))
            .or_default();
        match medium {
            StorageMedium::Npu | StorageMedium::Unknown => {
                entry.npu = entry.npu.max(end);
            }
            StorageMedium::Cpu => {
                entry.cpu = entry.cpu.max(end);
            }
            StorageMedium::Disk => {
                entry.disk = entry.disk.max(end);
            }
        }
    }

    /// Per `(instance_id, dp_rank)`, keep the farther breakpoint.
    ///
    /// `preferred` (CPU) overwrites `fallback` (HBM) when ``end_pos`` is
    /// greater or equal — so Disk resumes after the longest upstream prefix.
    fn merge_tier_breakpoints(
        fallback: &[TierBreakpoint],
        preferred: &[TierBreakpoint],
    ) -> Vec<TierBreakpoint> {
        let mut best: HashMap<(String, DpRank), TierBreakpoint> = HashMap::new();
        for b in fallback {
            best.insert((b.instance_id.clone(), b.dp_rank), b.clone());
        }
        for b in preferred {
            let key = (b.instance_id.clone(), b.dp_rank);
            match best.get(&key) {
                Some(existing) if b.end_pos < existing.end_pos => {}
                _ => {
                    best.insert(key, b.clone());
                }
            }
        }
        best.into_values().collect()
    }

    /// Build continuations and count contiguous lower-tier hits.
    ///
    /// - Root walks run for **every** worker owning the first edge so a
    ///   longer replica on this tier is never hidden by an upstream
    ///   (possibly shorter) hit.
    /// - Continuation starts from each upstream ``TierBreakpoint``; a worker
    ///   may hold several candidates (root + breakpoints), and the one with
    ///   the farthest absolute end wins inside
    ///   [`LowerTierIndexer::query_contiguous_hits`].
    fn lower_tier_lookup(
        &self,
        block_hashes: &[LocalBlockHash],
        upstream_breaks: &[TierBreakpoint],
        tiers: &LowerTierIndexer,
        overlap: &mut OverlapBlocks,
        medium_ends: &mut FxHashMap<(String, DpRank), MediumEnds>,
    ) -> FxHashMap<WorkerKey, ContiguousHit> {
        if block_hashes.is_empty() {
            return FxHashMap::default();
        }

        let mut continuations: FxHashMap<WorkerKey, Vec<LowerTierContinuation>> =
            FxHashMap::default();

        // Root walk: unconditional, so a longer replica on this tier is never
        // hidden by an upstream (possibly shorter) hit.
        for w in tiers.root_workers(block_hashes[0]) {
            continuations
                .entry(w)
                .or_default()
                .push(LowerTierContinuation::from_root(0));
        }

        // Continue from each upstream breakpoint (candidate list — the walk
        // keeps the farthest end per worker).
        for b in upstream_breaks {
            if b.end_pos >= block_hashes.len() {
                continue;
            }
            for w in tiers.edge_owners(Some(b.last_seq), block_hashes[b.end_pos]) {
                continuations
                    .entry(w)
                    .or_default()
                    .push(LowerTierContinuation::new(b.end_pos, b.last_seq));
            }
        }

        if continuations.is_empty() {
            return FxHashMap::default();
        }

        let hits = tiers.query_contiguous_hits(block_hashes, &continuations);
        for (worker, hit) in &hits {
            if hit.count > 0 {
                overlap.add_blocks(worker.clone(), hit.count as u32);
                Self::note_medium_end(
                    medium_ends,
                    &worker.instance_id,
                    worker.dp_rank,
                    worker.medium,
                    hit.end_pos() as u32,
                );
            }
        }
        hits
    }

    // -----------------------------------------------------------------------
    // Offload/pool bidirectional matching
    // -----------------------------------------------------------------------

    /// Ingest offload blocks from vLLM non-HBM events.
    ///
    /// Each triple is `(block_hash, tokens_hash, parent_hash)` where
    /// `parent_hash` is the immediately preceding engine `block_hash` in
    /// this offload chain (or the chain's original `parent_block_hash` for
    /// the first block).  Checks whether there are pending pool backend
    /// events waiting for each block.  Matched entries are removed from
    /// `pending_pool` and returned (grouped by worker). Content is retained
    /// (always retained; TTL-swept via [`CONTENT_TTL`]). Unmatched
    /// entries are cached in `offload`.
    pub fn ingest_offload_blocks(
        &self,
        triples: &[(u64, u64, Option<u64>)],
    ) -> HashMap<WorkerKey, Vec<(Option<u64>, KvCacheStoredBlockData)>> {
        let matched = {
            let mut state = self.offload_pool_state.write();
            let mut matched: HashMap<WorkerKey, Vec<(Option<u64>, KvCacheStoredBlockData)>> =
                HashMap::new();

            for &(block_hash, tokens_hash, parent_hash) in triples {
                let cache_entry = OffloadCacheEntry {
                    content: BlockContent {
                        tokens_hash,
                        parent_hash,
                    },
                    inserted_at: std::time::Instant::now(),
                };
                if let Some(pending) = state.pending_pool.remove(&block_hash) {
                    state.content.insert(
                        block_hash,
                        ContentEntry {
                            content: cache_entry.content,
                            inserted_at: std::time::Instant::now(),
                        },
                    );
                    state.offload.remove(&block_hash);
                    for pending_entry in pending {
                        matched.entry(pending_entry.worker).or_default().push((
                            parent_hash,
                            KvCacheStoredBlockData {
                                block_hash,
                                tokens_hash,
                            },
                        ));
                    }
                } else {
                    // If this hash was previously confirmed
                    // and re-offloaded, drop the stale content mapping to keep
                    // the offload/content invariant — the tier still carries
                    // the mapping for later pool events.
                    state.content.remove(&block_hash);
                    state.offload.insert(block_hash, cache_entry);
                }
            }
            matched
        };
        matched
    }

    /// Resolve `(parent_hash, tokens_hash)` from offload / retained content.
    ///
    /// When taking from `offload`, the mapping is moved into `content` (with a
    /// fresh [`CONTENT_TTL`] window) so a later pool medium can reuse it. Tier
    /// lookups happen outside this lock in [`Self::ingest_pool_blocks`].
    fn resolve_pool_content(
        &self,
        state: &mut OffloadPoolState,
        block_hash: u64,
    ) -> Option<(Option<u64>, u64)> {
        if let Some(cached) = state.offload.remove(&block_hash) {
            state.content.insert(
                block_hash,
                ContentEntry {
                    content: cached.content,
                    inserted_at: std::time::Instant::now(),
                },
            );
            return Some((cached.content.parent_hash, cached.content.tokens_hash));
        }
        if let Some(cached) = state.content.get(&block_hash) {
            return Some((cached.content.parent_hash, cached.content.tokens_hash));
        }
        None
    }

    /// Ingest pool backend blocks from Mooncake / YuanRong stored events.
    ///
    /// For each `block_hash`, resolves `(tokens_hash, parent_hash)` from the
    /// offload cache, retained content map, or an already-indexed lower tier.
    /// Unmatched entries are queued in `pending_pool`.
    pub fn ingest_pool_blocks(
        &self,
        block_hashes: &[u64],
        worker: &WorkerKey,
    ) -> Vec<(Option<u64>, KvCacheStoredBlockData)> {
        let mut matched = Vec::with_capacity(block_hashes.len());
        let mut need_tier_lookup = Vec::new();

        {
            let mut state = self.offload_pool_state.write();
            for &bh in block_hashes {
                if let Some((parent_hash, tokens_hash)) = self.resolve_pool_content(&mut state, bh)
                {
                    matched.push((
                        parent_hash,
                        KvCacheStoredBlockData {
                            block_hash: bh,
                            tokens_hash,
                        },
                    ));
                } else {
                    need_tier_lookup.push(bh);
                }
            }
        } // release offload_pool_state before touching tier locks

        for &bh in &need_tier_lookup {
            if let Some((parent, tokens)) = self
                .cpu_tiers
                .lookup_block(bh)
                .or_else(|| self.disk_tiers.lookup_block(bh))
            {
                let mut state = self.offload_pool_state.write();
                state.content.insert(
                    bh,
                    ContentEntry {
                        content: BlockContent {
                            tokens_hash: tokens,
                            parent_hash: parent,
                        },
                        inserted_at: std::time::Instant::now(),
                    },
                );
                matched.push((
                    parent,
                    KvCacheStoredBlockData {
                        block_hash: bh,
                        tokens_hash: tokens,
                    },
                ));
            } else {
                let mut state = self.offload_pool_state.write();
                state
                    .pending_pool
                    .entry(bh)
                    .or_default()
                    .insert(PendingPoolEvent {
                        worker: worker.clone(),
                        inserted_at: std::time::Instant::now(),
                    });
            }
        }

        matched
    }

    /// Evict blocks from pending caches (for removal events).
    ///
    /// Returns `block_hashes` that need lower-tier / tree removal — already
    /// confirmed into a tier, or with no matching pending state for this
    /// worker (tier removal is a no-op if the hash never entered a tier).
    /// Unconfirmed `offload` / `pending_pool`-only entries are dropped
    /// without tier removal. Retained `content` is left for the migration
    /// window (bounded by [`CONTENT_TTL`]) and may resolve a later Disk store.
    pub fn evict_pending_blocks(&self, block_hashes: &[u64], worker: &WorkerKey) -> Vec<u64> {
        let mut state = self.offload_pool_state.write();
        let mut need_tree_removal = Vec::new();

        for &bh in block_hashes {
            // Unconfirmed offload — never entered a tier.
            if state.offload.remove(&bh).is_some() {
                state.content.remove(&bh);
                continue;
            }
            // Pool-first pending for this worker — may or may not be in a tier.
            if let Some(entries) = state.pending_pool.get_mut(&bh) {
                let before = entries.len();
                entries.retain(|e| e.worker != *worker);
                if entries.len() != before {
                    if entries.is_empty() {
                        state.pending_pool.remove(&bh);
                    }
                    // Content means another medium already confirmed this hash.
                    if state.content.contains_key(&bh) {
                        need_tree_removal.push(bh);
                    }
                    continue;
                }
            }
            // Already confirmed (or unknown) — remove from the tier.
            need_tree_removal.push(bh);
        }
        need_tree_removal
    }

    /// Remove all pending entries for a worker from `pending_pool`.
    ///
    /// Called on worker disconnect / Cleared events.  Returns the number of
    /// block hashes whose `pending_pool` entries were fully cleared.
    ///
    /// Note: `offload` / `content` have no per-worker association. Content is
    /// TTL-evicted by periodic maintenance; it is
    /// kept across tier clears so a later Disk store can still promote the
    /// block. Unconfirmed `offload` entries are TTL-bounded and are also
    /// cleared on match, removal, or pool confirmation.
    pub fn remove_pending_worker(&self, worker: &WorkerKey) -> usize {
        let mut state = self.offload_pool_state.write();
        let mut removed = 0usize;

        state.pending_pool.retain(|_, entries| {
            entries.retain(|e| e.worker != *worker);
            if entries.is_empty() {
                removed += 1;
                false // remove the hash key entirely
            } else {
                true
            }
        });
        removed
    }

    /// Total pending + retained content entries (for diagnostics).
    pub fn pending_count(&self) -> usize {
        let state = self.offload_pool_state.read();
        state.offload.len() + state.pending_pool.len() + state.content.len()
    }

    /// Sweep stale matching-cache entries that exceed
    /// their TTLs, returning the total number of entries evicted.
    ///
    /// `content` uses a longer TTL because it must survive lower-tier removal
    /// during the CPU→Disk migration window.
    pub fn sweep_stale_caches(
        &self,
        pending_ttl: std::time::Duration,
        content_ttl: std::time::Duration,
        offload_ttl: std::time::Duration,
    ) -> usize {
        let mut state = self.offload_pool_state.write();
        let mut pruned = 0usize;
        let now = std::time::Instant::now();

        let before_offload = state.offload.len();
        state
            .offload
            .retain(|_, e| now.duration_since(e.inserted_at) < offload_ttl);
        pruned += before_offload - state.offload.len();

        let before_content = state.content.len();
        state
            .content
            .retain(|_bh, e| now.duration_since(e.inserted_at) < content_ttl);
        pruned += before_content - state.content.len();

        let before_pending = state.pending_pool.len();
        state.pending_pool.retain(|_bh, entries| {
            entries.retain(|e| {
                let keep = now.duration_since(e.inserted_at) < pending_ttl;
                if !keep {
                    pruned += 1;
                }
                keep
            });
            !entries.is_empty()
        });
        let expired_pending = before_pending - state.pending_pool.len();
        if expired_pending > 0 {
            tracing::debug!(
                expired = expired_pending,
                pending_ttl_secs = pending_ttl.as_secs(),
                "kv_event pending_expired"
            );
        }

        if pruned > 0 {
            tracing::debug!(
                pruned,
                remaining_offload = state.offload.len(),
                remaining_content = state.content.len(),
                remaining_pending_keys = state.pending_pool.len(),
                "swept stale offload/content/pending pool entries"
            );
        }

        pruned
    }

    /// Run one complete maintenance pass for this model/tenant index.
    pub fn maintenance(&self) -> usize {
        let mut pruned = self.sweep_stale_caches(
            self.maintenance.pending_ttl,
            self.maintenance.content_ttl,
            self.maintenance.offload_ttl,
        );
        pruned += self.hbm_tree.sweep_stale_nodes();
        pruned
    }

    // -----------------------------------------------------------------------
    // Event application
    // -----------------------------------------------------------------------

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
                    StorageMedium::Npu | StorageMedium::Unknown => {
                        // HBM: prefix-chain tree insert
                        let mut lookups = self.lookups.write();
                        let lookup = lookups.entry(worker.clone()).or_default();
                        self.hbm_tree.apply_store(worker, lookup, store_data)
                    }
                    StorageMedium::Cpu => {
                        self.cpu_tiers.store_blocks(worker, store_data);
                        Ok(())
                    }
                    StorageMedium::Disk => {
                        self.disk_tiers.store_blocks(worker, store_data);
                        Ok(())
                    }
                }
            }
            KvCacheEventData::Removed { block_hashes } => match worker.medium {
                StorageMedium::Npu | StorageMedium::Unknown => {
                    let mut lookups = self.lookups.write();
                    let lookup = lookups.entry(worker.clone()).or_default();
                    self.hbm_tree.apply_remove(worker, lookup, block_hashes)
                }
                // Retained content is deliberately NOT pruned here: a CPU
                // eviction may be the pool migrating the block to Disk, and
                // the Disk store event must still resolve via `content`
                // (bounded by `CONTENT_TTL` sweep instead).
                StorageMedium::Cpu => {
                    self.cpu_tiers.remove_blocks(worker, block_hashes);
                    Ok(())
                }
                StorageMedium::Disk => {
                    self.disk_tiers.remove_blocks(worker, block_hashes);
                    Ok(())
                }
            },
            KvCacheEventData::Cleared => {
                match worker.medium {
                    StorageMedium::Npu | StorageMedium::Unknown => {
                        let mut lookups = self.lookups.write();
                        if let Some(mut lookup) = lookups.remove(worker) {
                            self.hbm_tree.remove_worker(worker, &mut lookup);
                        }
                    }
                    // Same as Removed: retained content survives the clear so
                    // a later Disk store can still promote these blocks.
                    StorageMedium::Cpu => {
                        self.cpu_tiers.clear_worker(worker);
                    }
                    StorageMedium::Disk => {
                        self.disk_tiers.clear_worker(worker);
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
        // CPU / Disk continuation-edge indexes
        for wk in self.cpu_tiers.worker_keys() {
            if wk.instance_id == instance_id && wk.dp_rank == dp_rank {
                self.cpu_tiers.clear_worker(&wk);
            }
        }
        for wk in self.disk_tiers.worker_keys() {
            if wk.instance_id == instance_id && wk.dp_rank == dp_rank {
                self.disk_tiers.clear_worker(&wk);
            }
        }
        // Offload/pool pending state — clean up pool entries waiting for
        // this worker. Unconfirmed offload / retained content entries are not
        // per-worker; both are bounded by periodic maintenance.
        {
            let mut state = self.offload_pool_state.write();
            state.pending_pool.retain(|_, entries| {
                entries
                    .retain(|e| e.worker.instance_id != instance_id || e.worker.dp_rank != dp_rank);
                !entries.is_empty()
            });
        }
    }

    /// Get the total number of cached blocks across all workers and media.
    pub fn total_blocks(&self) -> usize {
        let hbm = self.lookups.read().values().map(|l| l.len()).sum::<usize>();
        hbm + self.cpu_tiers.total_blocks() + self.disk_tiers.total_blocks()
    }

    /// Get all registered worker keys.
    pub fn worker_keys(&self) -> Vec<WorkerKey> {
        let mut keys: Vec<WorkerKey> = self.lookups.read().keys().cloned().collect();
        keys.extend(self.cpu_tiers.worker_keys());
        keys.extend(self.disk_tiers.worker_keys());
        keys
    }
}

/// Top-level indexer managing multiple (model, tenant) trees.
pub struct Indexer {
    entries: DashMap<IndexerKey, Arc<IndexerEntry>>,
    maintenance: CacheMaintenanceConfig,
}

impl Indexer {
    /// Create an indexer.
    pub fn new() -> Self {
        Self::with_config(CacheMaintenanceConfig::default())
    }

    pub fn with_config(maintenance: CacheMaintenanceConfig) -> Self {
        Self {
            entries: DashMap::new(),
            maintenance,
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
            .or_insert_with(|| Arc::new(IndexerEntry::with_config(self.maintenance.clone())))
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

    /// Remove an indexer entry if it has no more workers across any medium
    /// and no pending offload / content / pool state remains.
    pub fn remove_if_empty(&self, model_name: &str, tenant_id: &str) {
        let key = IndexerKey {
            model_name: model_name.to_string(),
            tenant_id: tenant_id.to_string(),
        };
        let should_remove = self.entries.get(&key).is_some_and(|e| {
            let entry = e.value();
            entry.lookups.read().is_empty()
                && entry.cpu_tiers.is_empty()
                && entry.disk_tiers.is_empty()
                && entry.pending_count() == 0
        });
        if should_remove {
            self.entries.remove(&key);
        }
    }

    /// Sweep every entry and remove empty entries not protected by an active registration.
    pub fn maintenance(&self, protected: &FxHashSet<IndexerKey>) -> usize {
        let entries: Vec<(IndexerKey, Arc<IndexerEntry>)> = self
            .entries
            .iter()
            .map(|entry| (entry.key().clone(), entry.value().clone()))
            .collect();
        let mut pruned = 0;
        for (_, entry) in &entries {
            pruned += entry.maintenance();
        }
        for (key, _) in entries {
            if !protected.contains(&key) {
                self.remove_if_empty(&key.model_name, &key.tenant_id);
            }
        }
        pruned
    }

    /// Query matched block counts for a token sequence against a specific model/tenant.
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

        let t_hash = std::time::Instant::now();
        let block_hashes = compute_block_hash_for_seq(token_ids, block_size);
        let hash_us = t_hash.elapsed().as_micros();
        let (overlap, medium_ends) = entry.find_matches_with_coverage(&block_hashes);
        tracing::debug!(
            num_tokens = token_ids.len(),
            block_size,
            num_hashes = block_hashes.len(),
            hash_us,
            matched_workers = overlap.blocks.len(),
            "hash_computed"
        );
        let t_tree = t0.elapsed();

        let resp = self.build_response(&overlap, &medium_ends, model_name, tenant_id, block_size);
        let total = t0.elapsed();

        // Longest per-medium coverage across workers, in blocks.
        let npu_blocks = medium_ends.values().map(|m| m.npu).max().unwrap_or(0);
        let cpu_blocks = medium_ends.values().map(|m| m.cpu).max().unwrap_or(0);
        let disk_blocks = medium_ends.values().map(|m| m.disk).max().unwrap_or(0);

        tracing::debug!(
            num_tokens = token_ids.len(),
            block_size,
            hash_us,
            match_us = t_tree.as_micros(),
            total_us = total.as_micros(),
            npu_blocks,
            cpu_blocks,
            disk_blocks,
            "query profile"
        );
        resp
    }

    /// Query matched block counts using pre-computed `LocalBlockHash` values.
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

        let (overlap, medium_ends) = entry.find_matches_with_coverage(block_hashes);
        // Default to 1 token per hash (no scaling) since we don't know the
        // original block_size from the hash alone.
        self.build_response(&overlap, &medium_ends, model_name, tenant_id, 1)
    }

    /// Build a `QueryResponse` from per-DP absolute medium ends.
    ///
    /// `*_blocks` are exclusive contributions (priority NPU > CPU > Disk).
    /// `matched_tokens = (npu + cpu + disk) × block_size` (unweighted coverage;
    /// Coordinator applies tier affinity weights).
    fn build_response(
        &self,
        overlap: &OverlapBlocks,
        medium_ends: &FxHashMap<(String, DpRank), MediumEnds>,
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

        for ((instance_id, dp_rank), ends) in medium_ends {
            let npu = ends.npu;
            let cpu = ends.cpu.saturating_sub(ends.npu);
            let disk = ends.disk.saturating_sub(ends.npu.max(ends.cpu));
            let covered = npu.saturating_add(cpu).saturating_add(disk);

            let dp_rank_str = dp_rank.to_string();
            let imd = instance_data.entry(instance_id.clone()).or_default();
            let dp_match = imd.dp.entry(dp_rank_str).or_default();
            dp_match.npu_blocks = npu;
            dp_match.cpu_blocks = cpu;
            dp_match.disk_blocks = disk;
            dp_match.matched_tokens = covered.saturating_mul(block_size);
        }

        for imd in instance_data.values_mut() {
            imd.longest_matched = imd.dp.values().map(|d| d.matched_tokens).max().unwrap_or(0);
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

#[cfg(test)]
mod tests;
