// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Root identity and lossless engine-hash bindings for the existing indexes.

use super::*;
use crate::hashing::compute_identity_root_hash;
use crate::identity::EngineBlockHash;

#[derive(Default)]
pub(super) struct RootIdentityState {
    /// Complete engine values share a single internal ID across supported media.
    ids: FxHashMap<EngineBlockHash, u64>,
    next_id: u64,
    bindings: FxHashMap<u64, BlockContent>,
    /// Only roots carry identity. None quarantines a conflicting root key.
    roots: FxHashMap<LocalBlockHash, Option<CacheIdentity>>,
}

fn invalid(message: &str) -> KvConductorError {
    KvConductorError::InvalidIdentity(message.into())
}

impl RootIdentityState {
    fn intern(&mut self, hash: &EngineBlockHash) -> u64 {
        *self.ids.entry(hash.clone()).or_insert_with(|| {
            self.next_id += 1;
            self.next_id
        })
    }

    fn quarantine(&mut self, mut id: u64) {
        while let Some(content) = self.bindings.get(&id) {
            if let Some(parent) = content.parent_hash {
                id = parent;
            } else {
                self.roots.insert(LocalBlockHash(content.tokens_hash), None);
                break;
            }
        }
    }
}

impl IndexerEntry {
    /// Apply a validated engine event. Only an actual root supplies identity.
    #[allow(clippy::too_many_arguments)]
    pub fn apply_identity_store(
        &self,
        worker: &WorkerKey,
        hashes: &[EngineBlockHash],
        parent: Option<&EngineBlockHash>,
        token_hashes: &[u64],
        root_identity: Option<&CacheIdentity>,
        offload: bool,
    ) -> Result<(), KvConductorError> {
        if hashes.is_empty() || hashes.len() != token_hashes.len() {
            return Err(invalid("stored hashes and token blocks must align"));
        }
        let mut state = self.identity_state.write();
        let parent_id = parent
            .map(|hash| {
                state
                    .ids
                    .get(hash)
                    .copied()
                    .filter(|id| state.bindings.contains_key(id))
                    .ok_or_else(|| invalid("unknown parent hash"))
            })
            .transpose()?;
        if !offload && parent_id.is_none() {
            let identity = root_identity.ok_or_else(|| invalid("root identity is required"))?;
            identity.validate()?;
            let key = compute_identity_root_hash(identity, LocalBlockHash(token_hashes[0]));
            if let Some(existing) = state.roots.get(&key) {
                if existing.as_ref() != Some(identity) {
                    state.roots.insert(key, None);
                    return Err(invalid("conflicting root identity"));
                }
            }
        } else if root_identity.is_some() {
            return Err(invalid("only an HBM root may supply identity"));
        }

        let mut previous = parent_id;
        let mut triples = Vec::with_capacity(hashes.len());
        let mut seen = FxHashSet::default();
        for (hash, &raw_hash) in hashes.iter().zip(token_hashes) {
            let id = if offload {
                *state
                    .ids
                    .get(hash)
                    .ok_or_else(|| invalid("unbound offload hash"))?
            } else {
                state.intern(hash)
            };
            if previous == Some(id) || !seen.insert(id) {
                return Err(invalid("cyclic or repeated engine hash"));
            }
            let local = if previous.is_none() {
                let identity = if offload {
                    let bound = state
                        .bindings
                        .get(&id)
                        .ok_or_else(|| invalid("unbound offload root"))?;
                    state
                        .roots
                        .get(&LocalBlockHash(bound.tokens_hash))
                        .and_then(Option::as_ref)
                        .ok_or_else(|| invalid("unknown offload root identity"))?
                } else {
                    root_identity.expect("root identity checked above")
                };
                compute_identity_root_hash(identity, LocalBlockHash(raw_hash)).0
            } else {
                raw_hash
            };
            let content = BlockContent {
                tokens_hash: local,
                parent_hash: previous,
            };
            if let Some(bound) = state.bindings.get(&id) {
                if *bound != content {
                    state.quarantine(id);
                    return Err(invalid("engine hash changed its parent or local hash"));
                }
            } else if offload {
                return Err(invalid("offload requires a confirmed block binding"));
            }
            if offload {
                let in_source = self.lookups.read().iter().any(|(source, lookup)| {
                    source.instance_id == worker.instance_id
                        && source.dp_rank == worker.dp_rank
                        && lookup.contains_key(&SequenceBlockHash(id))
                });
                let pending = self.offload_pool_state.read();
                if !in_source
                    && !pending.offload.contains_key(&id)
                    && !pending.content.contains_key(&id)
                    && !self.cpu_tiers.contains_block(id)
                    && !self.disk_tiers.contains_block(id)
                {
                    return Err(invalid("offload has no confirmed source binding"));
                }
            }
            triples.push((id, local, previous));
            previous = Some(id);
        }

        if offload {
            for (target, blocks) in self.ingest_offload_blocks(&triples) {
                self.apply_pool_matches(&target, blocks)?;
            }
        } else {
            self.apply_event(
                worker,
                &KvCacheEventData::Stored(KvCacheStoreData {
                    parent_hash: parent_id,
                    start_position: None,
                    blocks: triples
                        .iter()
                        .map(|&(block_hash, tokens_hash, _)| KvCacheStoredBlockData {
                            block_hash,
                            tokens_hash,
                        })
                        .collect(),
                }),
            )?;
            for (id, tokens_hash, parent_hash) in &triples {
                state.bindings.insert(
                    *id,
                    BlockContent {
                        tokens_hash: *tokens_hash,
                        parent_hash: *parent_hash,
                    },
                );
            }
            if let Some(identity) = root_identity {
                state
                    .roots
                    .insert(LocalBlockHash(triples[0].1), Some(identity.clone()));
            }
        }
        Ok(())
    }

    fn apply_pool_matches(
        &self,
        worker: &WorkerKey,
        blocks: Vec<(Option<u64>, KvCacheStoredBlockData)>,
    ) -> Result<(), KvConductorError> {
        for (parent_hash, block) in blocks {
            self.apply_event(
                worker,
                &KvCacheEventData::Stored(KvCacheStoreData {
                    parent_hash,
                    start_position: None,
                    blocks: vec![block],
                }),
            )?;
        }
        Ok(())
    }

    pub fn apply_identity_pool_store(
        &self,
        worker: &WorkerKey,
        hashes: &[EngineBlockHash],
    ) -> Result<(), KvConductorError> {
        let mut state = self.identity_state.write();
        let ids: Vec<_> = hashes.iter().map(|hash| state.intern(hash)).collect();
        self.apply_pool_matches(worker, self.ingest_pool_blocks(&ids, worker))
    }

    pub fn apply_identity_remove(
        &self,
        workers: &[WorkerKey],
        hashes: &[EngineBlockHash],
    ) -> Result<(), KvConductorError> {
        let mut state = self.identity_state.write();
        let ids: Vec<_> = hashes
            .iter()
            .filter_map(|hash| state.ids.get(hash).copied())
            .collect();
        for worker in workers {
            if !matches!(worker.medium, StorageMedium::Npu | StorageMedium::Unknown) {
                self.evict_pending_blocks(&ids, worker);
            }
        }
        // Re-offload can coexist with a previously confirmed tier edge.
        // Removal must revoke each worker's edge even when pending state existed.
        let event = KvCacheEventData::Removed { block_hashes: ids };
        for worker in workers {
            self.apply_event(worker, &event)?;
        }
        self.prune_identity_state(&mut state);
        Ok(())
    }

    pub fn apply_identity_clear(&self, workers: &[WorkerKey]) -> Result<(), KvConductorError> {
        let mut state = self.identity_state.write();
        for worker in workers {
            self.apply_event(worker, &KvCacheEventData::Cleared)?;
            self.remove_pending_worker(worker);
        }
        self.prune_identity_state(&mut state);
        Ok(())
    }

    /// Drop bindings after their existing tree/tier/matching records disappear.
    /// This retains no HBM node, worker ownership, or deleted lookup entry.
    pub(super) fn prune_identity_state(&self, state: &mut RootIdentityState) {
        let mut live: FxHashSet<u64> = self
            .lookups
            .read()
            .values()
            .flat_map(|lookup| lookup.keys().map(|id| id.0))
            .collect();
        live.extend(self.cpu_tiers.referenced_hashes());
        live.extend(self.disk_tiers.referenced_hashes());
        {
            let pending = self.offload_pool_state.read();
            live.extend(pending.pending_pool.keys().copied());
            for (&id, content) in pending
                .offload
                .iter()
                .map(|(id, e)| (id, &e.content))
                .chain(pending.content.iter().map(|(id, e)| (id, &e.content)))
            {
                live.insert(id);
                live.extend(content.parent_hash);
            }
        }
        // A surviving lookup/edge still names its predecessor ID. Keep that
        // binding stable until the referring records disappear, so a fresh
        // Stored cannot reuse an old ID under another parent. No deleted HBM
        // lookup or node ownership is retained by these value-only bindings.
        let mut referenced: Vec<_> = live.iter().copied().collect();
        while let Some(id) = referenced.pop() {
            if let Some(parent) = state.bindings.get(&id).and_then(|b| b.parent_hash) {
                if live.insert(parent) {
                    referenced.push(parent);
                }
            }
        }
        state.ids.retain(|_, id| live.contains(id));
        state.bindings.retain(|id, _| live.contains(id));
        let roots: FxHashSet<_> = state
            .bindings
            .values()
            .filter(|b| b.parent_hash.is_none())
            .map(|b| LocalBlockHash(b.tokens_hash))
            .collect();
        state.roots.retain(|root, _| roots.contains(root));
    }
}

impl Indexer {
    pub fn query_with_identity(
        &self,
        model: &str,
        tenant: &str,
        tokens: &[i64],
        block_size: u32,
        identity: Option<&CacheIdentity>,
    ) -> Result<QueryResponse, KvConductorError> {
        self.query_with_identity_profiled(model, tenant, tokens, block_size, identity)
            .0
    }

    pub fn query_with_identity_profiled(
        &self,
        model: &str,
        tenant: &str,
        tokens: &[i64],
        block_size: u32,
        identity: Option<&CacheIdentity>,
    ) -> (Result<QueryResponse, KvConductorError>, QueryObservation) {
        if !self.identity_config.enabled() {
            return self.query_profiled(model, tenant, tokens, block_size);
        }
        let mut obs = QueryObservation {
            query_tokens: tokens.len() as u64,
            ..Default::default()
        };
        let result = self.identity_query(
            model,
            tenant,
            identity,
            || compute_block_hash_for_seq(tokens, block_size),
            block_size,
            &mut obs,
        );
        (result, obs)
    }

    pub fn query_by_hash_with_identity(
        &self,
        model: &str,
        tenant: &str,
        hashes: &[LocalBlockHash],
        identity: Option<&CacheIdentity>,
    ) -> Result<QueryResponse, KvConductorError> {
        self.query_by_hash_with_identity_profiled(model, tenant, hashes, identity)
            .0
    }

    pub fn query_by_hash_with_identity_profiled(
        &self,
        model: &str,
        tenant: &str,
        hashes: &[LocalBlockHash],
        identity: Option<&CacheIdentity>,
    ) -> (Result<QueryResponse, KvConductorError>, QueryObservation) {
        if !self.identity_config.enabled() {
            return self.query_by_hash_profiled(model, tenant, hashes);
        }
        let mut obs = QueryObservation {
            query_tokens: hashes.len() as u64,
            ..Default::default()
        };
        let result = self.identity_query(model, tenant, identity, || hashes.to_vec(), 1, &mut obs);
        (result, obs)
    }

    fn identity_query(
        &self,
        model: &str,
        tenant: &str,
        identity: Option<&CacheIdentity>,
        hashes: impl FnOnce() -> Vec<LocalBlockHash>,
        multiplier: u32,
        obs: &mut QueryObservation,
    ) -> Result<QueryResponse, KvConductorError> {
        let identity = identity.ok_or_else(|| invalid("cache_identity is required in v1 mode"))?;
        identity.validate()?;
        let entry = self
            .get(model, tenant)
            .ok_or_else(|| KvConductorError::NoIndexer {
                model_name: model.into(),
                tenant_id: tenant.into(),
            })?;
        let state = entry.identity_state.read();
        let t_hash = std::time::Instant::now();
        let mut hashes = hashes();
        if let Some(root) = hashes.first_mut() {
            *root = compute_identity_root_hash(identity, *root);
            if state.roots.get(root).and_then(Option::as_ref) != Some(identity) {
                return Err(KvConductorError::NoWorkers {
                    model_name: model.into(),
                    tenant_id: tenant.into(),
                });
            }
        }
        obs.hash_secs = t_hash.elapsed().as_secs_f64();
        self.match_and_build_profiled(&entry, &hashes, model, tenant, multiplier, obs)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::identity::{AdapterIdentity, IdentityMode};
    use std::slice::from_ref;

    fn identity(salt: Option<&str>) -> CacheIdentity {
        CacheIdentity {
            schema_version: 1,
            adapter: AdapterIdentity::Base,
            cache_salt: salt.map(str::to_owned),
        }
    }

    fn worker(medium: StorageMedium) -> WorkerKey {
        WorkerKey {
            instance_id: "engine".into(),
            backend_id: "engine".into(),
            dp_rank: 0,
            medium,
        }
    }

    fn setup() -> (
        Indexer,
        Arc<IndexerEntry>,
        Vec<EngineBlockHash>,
        Vec<i64>,
        Vec<u64>,
    ) {
        let indexer = Indexer::with_identity_config(
            CacheMaintenanceConfig::default(),
            IdentityConfig {
                mode: IdentityMode::V1,
            },
        );
        let entry = indexer.get_or_create("model", "tenant");
        let tokens = vec![1, 2, 3, 4, 5, 6];
        let hashes = compute_block_hash_for_seq(&tokens, 2)
            .into_iter()
            .map(|h| h.0)
            .collect();
        (
            indexer,
            entry,
            vec![
                EngineBlockHash::U64(10),
                EngineBlockHash::U64(20),
                EngineBlockHash::U64(30),
            ],
            tokens,
            hashes,
        )
    }

    fn counts(indexer: &Indexer, tokens: &[i64], identity: &CacheIdentity) -> (u32, u32, u32) {
        match indexer.query_with_identity("model", "tenant", tokens, 2, Some(identity)) {
            Ok(response) => {
                let hit = &response.tenants["tenant"]["engine"].dp["0"];
                (hit.npu_blocks, hit.cpu_blocks, hit.disk_blocks)
            }
            Err(KvConductorError::NoWorkers { .. }) => (0, 0, 0),
            other => panic!("unexpected query: {other:?}"),
        }
    }

    #[test]
    fn v1_root_only_hash_and_hbm_delete_preserve_existing_tree_semantics() {
        let (indexer, entry, hashes, tokens, raw) = setup();
        let id = identity(Some("a"));
        let npu = worker(StorageMedium::Npu);
        entry
            .apply_identity_store(&npu, &hashes, None, &raw, Some(&id), false)
            .unwrap();
        let state = entry.identity_state.read();
        assert_eq!(
            state.bindings[&state.ids[&hashes[0]]].tokens_hash,
            compute_identity_root_hash(&id, LocalBlockHash(raw[0])).0
        );
        assert_eq!(state.bindings[&state.ids[&hashes[1]]].tokens_hash, raw[1]);
        assert_eq!(state.bindings[&state.ids[&hashes[2]]].tokens_hash, raw[2]);
        let root_id = state.ids[&hashes[0]];
        drop(state);
        assert_eq!(counts(&indexer, &tokens, &id), (3, 0, 0));
        assert_eq!(counts(&indexer, &tokens, &identity(Some("b"))), (0, 0, 0));
        entry
            .apply_identity_remove(from_ref(&npu), &hashes[..1])
            .unwrap();
        assert!(!entry.lookups.read()[&npu].contains_key(&SequenceBlockHash(root_id)));
        assert_eq!(counts(&indexer, &tokens, &id), (0, 0, 0));
        // A fresh root does not reattach the deleted parent's former children.
        entry
            .apply_identity_store(&npu, &hashes[..1], None, &raw[..1], Some(&id), false)
            .unwrap();
        assert_eq!(counts(&indexer, &tokens, &id), (1, 0, 0));
        entry
            .apply_identity_store(&npu, &hashes[1..], Some(&hashes[0]), &raw[1..], None, false)
            .unwrap();
        assert_eq!(counts(&indexer, &tokens, &id), (3, 0, 0));
        entry.apply_identity_clear(from_ref(&npu)).unwrap();
        assert!(entry.identity_state.read().ids.is_empty());
        assert!(entry.identity_state.read().roots.is_empty());
    }

    #[test]
    fn v1_hbm_cpu_disk_continuation_and_retained_content_survive_hbm_removal() {
        let (indexer, entry, hashes, tokens, raw) = setup();
        let id = identity(Some("tenant-salt"));
        let npu = worker(StorageMedium::Npu);
        let cpu = worker(StorageMedium::Cpu);
        let disk = worker(StorageMedium::Disk);
        entry
            .apply_identity_store(&npu, &hashes, None, &raw, Some(&id), false)
            .unwrap();
        entry
            .apply_identity_store(&cpu, &hashes, None, &raw, None, true)
            .unwrap();
        assert_eq!(counts(&indexer, &tokens, &id), (3, 0, 0));
        entry.apply_identity_pool_store(&cpu, &hashes[..2]).unwrap();
        entry
            .apply_identity_pool_store(&disk, &hashes[2..])
            .unwrap();
        entry
            .apply_identity_remove(from_ref(&npu), &hashes[1..])
            .unwrap();
        assert_eq!(counts(&indexer, &tokens, &id), (1, 1, 1));
        entry
            .apply_identity_remove(from_ref(&npu), &hashes[..1])
            .unwrap();
        assert_eq!(counts(&indexer, &tokens, &id), (0, 2, 1));
        entry
            .apply_identity_remove(from_ref(&cpu), &hashes[..2])
            .unwrap();
        assert_eq!(counts(&indexer, &tokens, &id), (0, 0, 0));
        // Disk confirmation uses the existing retained content after CPU eviction.
        entry
            .apply_identity_pool_store(&disk, &hashes[..2])
            .unwrap();
        assert_eq!(counts(&indexer, &tokens, &id), (0, 0, 3));
        assert_eq!(counts(&indexer, &tokens, &identity(None)), (0, 0, 0));
    }

    #[test]
    fn v1_pool_first_remove_clear_and_expiration_do_not_restore_old_availability() {
        let (indexer, entry, hashes, tokens, raw) = setup();
        let id = identity(None);
        let npu = worker(StorageMedium::Npu);
        let cpu = worker(StorageMedium::Cpu);
        entry.apply_identity_pool_store(&cpu, &hashes).unwrap();
        entry
            .apply_identity_remove(from_ref(&cpu), &hashes[..1])
            .unwrap();
        entry.apply_identity_clear(from_ref(&cpu)).unwrap();
        entry
            .apply_identity_store(&npu, &hashes, None, &raw, Some(&id), false)
            .unwrap();
        entry
            .apply_identity_store(&cpu, &hashes, None, &raw, None, true)
            .unwrap();
        entry.apply_identity_clear(from_ref(&npu)).unwrap();
        assert_eq!(counts(&indexer, &tokens, &id), (0, 0, 0));
        entry.sweep_stale_caches(
            std::time::Duration::ZERO,
            std::time::Duration::ZERO,
            std::time::Duration::ZERO,
        );
        entry.maintenance();
        assert!(entry.identity_state.read().ids.is_empty());
        assert!(entry.identity_state.read().roots.is_empty());
        entry.apply_identity_pool_store(&cpu, &hashes).unwrap();
        assert_eq!(counts(&indexer, &tokens, &id), (0, 0, 0));
        assert!(entry
            .apply_identity_store(&cpu, &hashes, None, &raw, None, true)
            .is_err());
    }

    #[test]
    fn v1_remove_revokes_confirmed_cpu_even_after_reoffload() {
        let (indexer, entry, hashes, tokens, raw) = setup();
        let id = identity(None);
        let npu = worker(StorageMedium::Npu);
        let cpu = worker(StorageMedium::Cpu);
        entry
            .apply_identity_store(&npu, &hashes, None, &raw, Some(&id), false)
            .unwrap();
        entry
            .apply_identity_store(&cpu, &hashes, None, &raw, None, true)
            .unwrap();
        entry.apply_identity_pool_store(&cpu, &hashes).unwrap();
        entry
            .apply_identity_store(&cpu, &hashes, None, &raw, None, true)
            .unwrap();
        entry
            .apply_identity_remove(from_ref(&cpu), &hashes)
            .unwrap();
        entry.apply_identity_clear(from_ref(&npu)).unwrap();
        assert_eq!(counts(&indexer, &tokens, &id), (0, 0, 0));
        assert_eq!(entry.cpu_tiers.total_blocks(), 0);
    }

    #[test]
    fn v1_conflicting_root_key_is_quarantined_until_its_records_are_gone() {
        for clear in [false, true] {
            let (indexer, entry, hashes, tokens, raw) = setup();
            let a = identity(Some("a"));
            let b = identity(Some("b"));
            let npu = worker(StorageMedium::Npu);
            let mut second = npu.clone();
            second.dp_rank = 1;
            let workers = [npu.clone(), second];
            for worker in &workers {
                entry
                    .apply_identity_store(worker, &hashes, None, &raw, Some(&a), false)
                    .unwrap();
            }
            let key = compute_identity_root_hash(&a, LocalBlockHash(raw[0]));
            // Inject a collision without relying on finding one in XXH3's 64-bit space.
            entry
                .identity_state
                .write()
                .roots
                .insert(key, Some(b.clone()));
            for _ in 0..2 {
                assert!(entry
                    .apply_identity_store(&npu, &hashes, None, &raw, Some(&a), false)
                    .is_err());
                assert_eq!(counts(&indexer, &tokens, &a), (0, 0, 0));
            }
            if clear {
                entry.apply_identity_clear(&workers).unwrap();
            } else {
                entry.apply_identity_remove(&workers, &hashes).unwrap();
            }
            assert!(entry.identity_state.read().ids.is_empty());
            assert!(entry.identity_state.read().roots.is_empty());
            // The same engine hashes can immediately bind to a new identity.
            entry
                .apply_identity_store(&npu, &hashes, None, &raw, Some(&b), false)
                .unwrap();
            assert_eq!(counts(&indexer, &tokens, &b), (3, 0, 0));
            assert_eq!(counts(&indexer, &tokens, &a), (0, 0, 0));
        }
    }

    #[test]
    fn v1_reused_engine_hash_cannot_change_identity_or_parent() {
        let (indexer, entry, hashes, tokens, raw) = setup();
        let a = identity(Some("a"));
        let b = identity(Some("b"));
        let npu = worker(StorageMedium::Npu);
        entry
            .apply_identity_store(&npu, &hashes, None, &raw, Some(&a), false)
            .unwrap();
        assert!(entry
            .apply_identity_store(&npu, &hashes, None, &raw, Some(&b), false)
            .is_err());
        assert_eq!(counts(&indexer, &tokens, &a), (0, 0, 0));
        assert_eq!(counts(&indexer, &tokens, &b), (0, 0, 0));
        assert!(entry
            .apply_identity_store(&npu, &hashes[2..], Some(&hashes[0]), &raw[2..], None, false)
            .is_err());
    }

    #[test]
    fn v1_profiled_queries_keep_identity_and_endpoint_units() {
        let (indexer, entry, hashes, tokens, raw) = setup();
        let id = identity(Some("a"));
        entry
            .apply_identity_store(
                &worker(StorageMedium::Npu),
                &hashes,
                None,
                &raw,
                Some(&id),
                false,
            )
            .unwrap();
        let raw_hashes = compute_block_hash_for_seq(&tokens, 2);
        assert!(matches!(
            indexer.query_profiled("model", "tenant", &tokens, 2).0,
            Err(KvConductorError::InvalidIdentity(_))
        ));
        assert!(matches!(
            indexer
                .query_by_hash_profiled("model", "tenant", &raw_hashes)
                .0,
            Err(KvConductorError::InvalidIdentity(_))
        ));
        let (result, obs) =
            indexer.query_with_identity_profiled("model", "tenant", &tokens, 2, Some(&id));
        result.unwrap();
        assert_eq!(
            (obs.query_tokens, obs.best_hbm_blocks, obs.best_any_tokens),
            (6, 3, 6)
        );
        let (result, obs) =
            indexer.query_by_hash_with_identity_profiled("model", "tenant", &raw_hashes, Some(&id));
        result.unwrap();
        assert_eq!(
            (obs.query_tokens, obs.best_hbm_blocks, obs.best_any_tokens),
            (3, 3, 3)
        );
    }

    #[test]
    fn v1_identity_is_required_even_when_query_is_empty() {
        let (indexer, _, _, _, _) = setup();
        let id = identity(None);
        assert!(indexer
            .query_with_identity("model", "tenant", &[], 2, None)
            .is_err());
        assert!(matches!(
            indexer.query_with_identity("model", "tenant", &[], 2, Some(&id)),
            Err(KvConductorError::NoWorkers { .. })
        ));
    }
}
