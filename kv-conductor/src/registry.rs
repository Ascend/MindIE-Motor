// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Worker registry for the KV conductor.
//!
//! Manages instance registration, unregistration, ZMQ subscriptions for
//! Mooncake-type publishers, and provides the query interface.

use std::collections::HashMap;
use std::sync::Arc;

use parking_lot::RwLock as ParkingRwLock;

use crate::backend::{MatchMode, StoreBackend};
use crate::error::KvConductorError;
use crate::indexer::Indexer;
use crate::protocols::*;

/// Extract the IP/host portion from a ZMQ endpoint URL.
/// e.g. "tcp://10.0.0.1:5557" → "10.0.0.1"
///      "tcp://[::1]:5557"   → "::1"
fn extract_ip_from_endpoint(endpoint: &str) -> Option<String> {
    let without_prefix = endpoint.strip_prefix("tcp://")?;
    // IPv6 bracket notation: tcp://[::1]:5557
    if let Some(rest) = without_prefix.strip_prefix('[') {
        let close_bracket = rest.find(']')?;
        return Some(rest[..close_bracket].to_string());
    }
    // IPv4 or hostname: tcp://10.0.0.1:5557
    let colon_pos = without_prefix.rfind(':')?;
    Some(without_prefix[..colon_pos].to_string())
}

/// Add `(instance_id, dp_rank)` entries to `hbm_ip_index` for each XPU endpoint.
fn add_hbm_ip_index_entries(
    ip_index: &HbmIpIndex,
    medium_endpoints: &HashMap<String, String>,
    instance_id: &str,
    dp_rank: u32,
) {
    for (medium_str, ep_url) in medium_endpoints {
        if !medium_str.eq_ignore_ascii_case("xpu") {
            continue;
        }
        let Some(ref ip) = extract_ip_from_endpoint(ep_url) else {
            continue;
        };
        ip_index
            .write()
            .entry(ip.clone())
            .or_default()
            .push((instance_id.to_string(), dp_rank));
        tracing::info!(instance_id = %instance_id, dp_rank, ip = %ip, "HBM IP indexed for pool auto-attach");
    }
}

/// For each XPU endpoint in `medium_endpoints`, remove the (instance_id, dp_rank)
/// entry from `hbm_ip_index`. If a given IP has no remaining DPs, the IP key is
/// dropped entirely.
fn remove_hbm_ip_index_entries(
    ip_index: &HbmIpIndex,
    medium_endpoints: &HashMap<String, String>,
    instance_id: &str,
    dp_rank: u32,
) {
    for (medium_str, ep_url) in medium_endpoints {
        if !medium_str.eq_ignore_ascii_case("xpu") {
            continue;
        }
        let Some(ref ip) = extract_ip_from_endpoint(ep_url) else {
            continue;
        };
        let mut idx = ip_index.write();
        if let Some(dps) = idx.get_mut(ip.as_str()) {
            dps.retain(|(iid, dp)| iid != instance_id || dp != &dp_rank);
            if dps.is_empty() {
                idx.remove(ip.as_str());
            }
        }
        tracing::info!(instance_id = %instance_id, dp_rank, ip = %ip, "HBM IP removed from index");
    }
}

/// Information about a registered endpoint for a worker.
#[derive(Debug, Clone, Serialize)]
pub struct EndpointInfo {
    /// Per-medium ZMQ PUB endpoints, e.g. {"xpu": "tcp://...:5557", "cpu": "tcp://...:5558"}.
    pub medium_endpoints: HashMap<String, String>,
    pub engine_type: String,
    pub dp_rank: DpRank,
    pub replay_endpoint: Option<String>,
}

/// A registered worker entry.
#[derive(Debug, Clone)]
pub struct WorkerEntry {
    pub instance_id: InstanceId,
    pub model_name: String,
    pub tenant_id: String,
    pub block_size: u32,
    pub store_backend: String,
    /// dp_rank -> EndpointInfo
    pub endpoints: HashMap<DpRank, EndpointInfo>,
}

/// The central worker registry.
///
/// Thread-safe via internal locking (tokio::sync::RwLock for the inner
/// HashMap, protecting both registration metadata and indexer lifecycle).
pub struct WorkerRegistry {
    /// Instance ID -> WorkerEntry
    instances: tokio::sync::RwLock<HashMap<InstanceId, WorkerEntry>>,
    /// The shared indexer for all models/tenants
    indexer: Arc<Indexer>,
    /// Count of active replay sessions. Queries are rejected while > 0
    /// to avoid returning incomplete results during prefix-tree rebuild.
    /// Wrapped in Arc so it can be shared with spawn_blocking tasks.
    replay_in_progress: Arc<std::sync::atomic::AtomicU64>,
    /// Active ZMQ subscribers, keyed by (instance_id, dp_rank, endpoint_url).
    /// Multiple subscribers may exist per dp_rank when a backend uses separate
    /// ports for different storage media (e.g. YuanRong: HBM port vs DDR/SSD port).
    /// The endpoint URL in the key ensures each unique connection gets its own
    /// subscriber even when media share an endpoint (deduplication).
    zmq_subscribers: tokio::sync::RwLock<
        HashMap<(InstanceId, DpRank, String), crate::zmq_subscriber::ZmqSubscriber>,
    >,
    /// IP → list of (instance_id, dp_rank) lookup for Mooncake auto-attach.
    /// Built from HBM endpoint registrations; consumed by the pool subscriber
    /// to map `backend_id` in events to the correct DP(s).
    hbm_ip_index: HbmIpIndex,
}

impl WorkerRegistry {
    /// Resolve endpoint→media mapping from either the new `medium_endpoints`
    /// protocol or the legacy `endpoint` field (Mooncake Master compat).
    ///
    /// Returns a map of endpoint_url → Vec<StorageMedium>, deduplicated by URL.
    fn resolve_endpoint_media(
        req: &RegisterRequest,
    ) -> Result<HashMap<String, Vec<StorageMedium>>, KvConductorError> {
        // New protocol: per-medium endpoints
        if !req.medium_endpoints.is_empty() {
            let mut map: HashMap<String, Vec<StorageMedium>> = HashMap::new();
            for (medium_str, ep) in &req.medium_endpoints {
                let medium = StorageMedium::parse(medium_str);
                map.entry(ep.clone()).or_default().push(medium);
            }
            return Ok(map);
        }

        // Legacy protocol: single endpoint for all media (Mooncake Master)
        if let Some(ref ep) = req.endpoint {
            if ep.is_empty() {
                return Err(KvConductorError::Internal(
                    "endpoint is empty; either medium_endpoints or a valid endpoint is required"
                        .to_string(),
                ));
            }
            let mut map: HashMap<String, Vec<StorageMedium>> = HashMap::new();
            // Mooncake master publishes CPU and DISK events (never XPU) on one
            // port. We include XPU here so the indexer is ready for HBM events
            // from engine-level publishers that may share the same endpoint.
            map.insert(
                ep.clone(),
                vec![StorageMedium::Xpu, StorageMedium::Cpu, StorageMedium::Disk],
            );
            return Ok(map);
        }

        // No endpoint configured — HTTP-events-only publisher.
        // The instance is registered for /events ingestion without ZMQ subscription.
        Ok(HashMap::new())
    }

    pub fn new(scoring: ScoringConfig) -> Self {
        Self {
            instances: tokio::sync::RwLock::new(HashMap::new()),
            indexer: Arc::new(Indexer::new(scoring)),
            replay_in_progress: Arc::new(std::sync::atomic::AtomicU64::new(0)),
            zmq_subscribers: tokio::sync::RwLock::new(HashMap::new()),
            hbm_ip_index: Arc::new(ParkingRwLock::new(HashMap::new())),
        }
    }

    /// Register a worker endpoint.
    ///
    /// Creates the indexer entry for the (model, tenant) pair if it doesn't
    /// exist yet. Spawns ZMQ subscribers for each unique per-medium endpoint
    /// declared in `medium_endpoints`.
    pub async fn register(&self, req: &RegisterRequest) -> Result<(), KvConductorError> {
        let endpoint_media = Self::resolve_endpoint_media(req)?;

        let sb = StoreBackend::parse(&req.store_backend);
        let is_pool =
            sb.is_pool_auto_attach() && req.medium_endpoints.is_empty() && req.endpoint.is_some();

        // Detect whether this instance_id already exists (for replay gating).
        let instance_exists = {
            let instances = self.instances.read().await;
            instances.contains_key(&req.instance_id)
        };

        // ── Handle re-registration ────────────────────────────────────
        // If the same (instance_id, dp_rank) is already registered, stop the
        // old ZMQ subscribers.  If the backend type changed, drop the radix
        // tree data for the old registration.  If the backend is the same,
        // the tree data is preserved and only endpoint info is updated.
        // This allows clients to fix misconfigured endpoints by simply
        // re-registering, without a restart or explicit unregister.
        {
            let old_info: Option<(bool, HashMap<String, String>)> = {
                let instances = self.instances.read().await;
                let entry = instances.get(&req.instance_id);
                entry.and_then(|e| {
                    e.endpoints.get(&req.dp_rank).map(|info| {
                        (
                            !e.store_backend.eq_ignore_ascii_case(&req.store_backend),
                            info.medium_endpoints.clone(),
                        )
                    })
                })
            };

            if let Some((backend_changed, old_medium_endpoints)) = old_info {
                tracing::info!(
                    instance_id = %req.instance_id,
                    dp_rank = req.dp_rank,
                    old_backend = %req.store_backend,
                    backend_changed,
                    "re-registering existing worker"
                );

                // Stop old subscribers.
                let mut subs = self.zmq_subscribers.write().await;
                let matching: Vec<_> = subs
                    .keys()
                    .filter(|(iid, dp, _)| iid == &req.instance_id && dp == &req.dp_rank)
                    .cloned()
                    .collect();
                for key in matching {
                    if let Some(subscriber) = subs.remove(&key) {
                        subscriber.shutdown().await;
                    }
                }
                drop(subs);

                // Drop radix tree data if backend changed.
                if backend_changed {
                    let ie = self.indexer.get(&req.modelname, &req.tenant_id);
                    if let Some(ie) = &ie {
                        ie.remove_worker_all_media(&req.instance_id, req.dp_rank);
                    }
                    remove_hbm_ip_index_entries(
                        &self.hbm_ip_index,
                        &old_medium_endpoints,
                        &req.instance_id,
                        req.dp_rank,
                    );
                }
            }
        }

        // Index HBM endpoint IPs for pool backends.
        if sb.index_hbm_ip() && !is_pool {
            add_hbm_ip_index_entries(
                &self.hbm_ip_index,
                &req.medium_endpoints,
                &req.instance_id,
                req.dp_rank,
            );
        }

        // Pre-create ZMQ subscribers before acquiring write lock.
        let subscribers: Vec<(String, crate::zmq_subscriber::ZmqSubscriber)> = {
            let mut subs = Vec::with_capacity(endpoint_media.len());
            let backend_id = req.instance_id.clone();
            let match_mode = if is_pool {
                sb.match_mode()
            } else {
                MatchMode::None
            };
            let ip_index = if is_pool {
                Some(Arc::clone(&self.hbm_ip_index))
            } else {
                None
            };
            for (ep_url, default_media) in &endpoint_media {
                let sub = crate::zmq_subscriber::ZmqSubscriber::connect(
                    ep_url.clone(),
                    req.modelname.clone(),
                    req.tenant_id.clone(),
                    Arc::clone(&self.indexer),
                    req.block_size,
                    backend_id.clone(),
                    req.dp_rank,
                    default_media.clone(),
                    match_mode,
                    ip_index.clone(),
                )?;
                subs.push((ep_url.clone(), sub));
            }
            subs
        };

        // Build normalized medium_endpoints for storage.
        let mut normalized_mediums: HashMap<String, String> = HashMap::new();
        for (ep_url, media) in &endpoint_media {
            for m in media {
                normalized_mediums.insert(m.as_str().to_lowercase(), ep_url.clone());
            }
        }

        self.indexer.get_or_create(&req.modelname, &req.tenant_id);

        let mut instances = self.instances.write().await;
        let entry = instances
            .entry(req.instance_id.clone())
            .or_insert_with(|| WorkerEntry {
                instance_id: req.instance_id.clone(),
                model_name: req.modelname.clone(),
                tenant_id: req.tenant_id.clone(),
                block_size: req.block_size,
                store_backend: req.store_backend.clone(),
                endpoints: HashMap::new(),
            });

        entry.store_backend = req.store_backend.clone();
        entry.endpoints.insert(
            req.dp_rank,
            EndpointInfo {
                medium_endpoints: normalized_mediums,
                engine_type: req.engine_type.clone(),
                dp_rank: req.dp_rank,
                replay_endpoint: req.replay_endpoint.clone(),
            },
        );

        tracing::info!(
            instance_id = %req.instance_id, dp_rank = req.dp_rank,
            model = %req.modelname, tenant = %req.tenant_id,
            engine_type = %req.engine_type, store_backend = %req.store_backend,
            num_endpoints = endpoint_media.len(),
            "worker registered"
        );

        {
            let mut subs = self.zmq_subscribers.write().await;
            for (ep_url, sub) in subscribers {
                subs.insert((req.instance_id.clone(), req.dp_rank, ep_url), sub);
            }
        }

        if let Some(ref replay_ep) = req.replay_endpoint {
            if !replay_ep.is_empty() && !instance_exists {
                self.replay_in_progress
                    .fetch_add(1, std::sync::atomic::Ordering::Release);

                let replay_ep = replay_ep.clone();
                let modelname = req.modelname.clone();
                let tenant_id = req.tenant_id.clone();
                let block_size = req.block_size;
                let indexer = Arc::clone(&self.indexer);
                let instance_id = req.instance_id.clone();
                let replay_counter = Arc::clone(&self.replay_in_progress);

                // Offload blocking ZMQ I/O to a dedicated thread so the
                // tokio runtime worker is not starved during replay.
                tokio::task::spawn_blocking(move || {
                    crate::zmq_subscriber::replay_events(
                        &replay_ep,
                        &modelname,
                        &tenant_id,
                        block_size,
                        &indexer,
                        &instance_id,
                    );
                    replay_counter.fetch_sub(1, std::sync::atomic::Ordering::Release);
                });
            }
        }

        Ok(())
    }

    /// Unregister a worker endpoint.
    pub async fn unregister(&self, req: &UnregisterRequest) -> Result<(), KvConductorError> {
        // Remove HBM IP index entries for this DP.
        {
            let instances = self.instances.read().await;
            if let Some(entry) = instances.get(&req.instance_id) {
                if let Some(info) = entry.endpoints.get(&req.dp_rank) {
                    remove_hbm_ip_index_entries(
                        &self.hbm_ip_index,
                        &info.medium_endpoints,
                        &req.instance_id,
                        req.dp_rank,
                    );
                }
            }
        }

        // Shut down ALL ZMQ subscribers for this (instance_id, dp_rank).
        // A single dp_rank may have multiple subscribers when the backend
        // uses separate ports per storage medium.
        {
            let mut subs = self.zmq_subscribers.write().await;
            // Collect keys matching this (instance_id, dp_rank)
            let matching_keys: Vec<(InstanceId, DpRank, String)> = subs
                .keys()
                .filter(|(iid, dp, _)| iid == &req.instance_id && dp == &req.dp_rank)
                .cloned()
                .collect();
            for key in matching_keys {
                if let Some(subscriber) = subs.remove(&key) {
                    subscriber.shutdown().await;
                    tracing::info!(
                        instance_id = %req.instance_id,
                        dp_rank = req.dp_rank,
                        endpoint = %key.2,
                        "ZMQ subscriber stopped"
                    );
                }
            }
        }

        let mut instances = self.instances.write().await;

        let entry = instances.get_mut(&req.instance_id).ok_or_else(|| {
            KvConductorError::InstanceNotFound {
                instance_id: req.instance_id.clone(),
            }
        })?;

        entry.endpoints.remove(&req.dp_rank);

        // Remove from indexer tree across all storage media (XPU/CPU/DISK)
        // Use the *registered* model/tenant (not the request's) to ensure
        // cleanup targets the correct indexer entry.
        let indexer_entry = self.indexer.get(&entry.model_name, &entry.tenant_id);
        if let Some(ie) = &indexer_entry {
            ie.remove_worker_all_media(&req.instance_id, req.dp_rank);
        }

        tracing::info!(
            instance_id = %req.instance_id,
            dp_rank = req.dp_rank,
            "worker unregistered"
        );

        if entry.endpoints.is_empty() {
            instances.remove(&req.instance_id);
            self.indexer.remove_if_empty(&req.modelname, &req.tenant_id);
        }

        Ok(())
    }

    /// Query KV cache overlap for a token sequence.
    pub async fn query(&self, req: &QueryRequest) -> Result<QueryResponse, KvConductorError> {
        self.indexer
            .query(&req.model, &req.tenant_id, &req.token_ids, req.block_size)
    }

    /// Query KV cache overlap using pre-computed block hashes.
    pub async fn query_by_hash(
        &self,
        req: &QueryByHashRequest,
    ) -> Result<QueryResponse, KvConductorError> {
        let hashes: Vec<LocalBlockHash> = req
            .block_hashes
            .iter()
            .map(|&h| LocalBlockHash(h))
            .collect();
        self.indexer
            .query_by_hash(&req.model, &req.tenant_id, &hashes)
    }

    /// Apply a batch of KV cache events (engine-style, HTTP POST /events).
    ///
    /// When `model_name` / `tenant_id` are provided, they take precedence over
    /// the registered values. If neither a registration nor explicit model
    /// exist, the call returns [`KvConductorError::InstanceNotFound`].
    pub async fn apply_events(
        &self,
        instance_id: &str,
        dp_rank: u32,
        events: &[KvCacheEvent],
        model_name: Option<&str>,
        tenant_id: Option<&str>,
    ) -> Result<usize, KvConductorError> {
        // Resolve model_name / tenant_id:
        //   explicit args > registered values > defaults
        let (model_name, tenant_id) = {
            let instances = self.instances.read().await;
            match instances.get(instance_id) {
                Some(entry) => (
                    model_name.unwrap_or(&entry.model_name).to_string(),
                    tenant_id.unwrap_or(&entry.tenant_id).to_string(),
                ),
                None => {
                    let mn = model_name.ok_or_else(|| KvConductorError::InstanceNotFound {
                        instance_id: instance_id.to_string(),
                    })?;
                    let tid = tenant_id.unwrap_or("default");
                    (mn.to_string(), tid.to_string())
                }
            }
        };

        let indexer_entry = self.indexer.get_or_create(&model_name, &tenant_id);

        let mut applied = 0;
        for event in events {
            let (canonical, medium_str, backend_id_opt) = event.data.normalize();

            let event_desc = match &canonical {
                KvCacheEventData::Stored(s) => format!("Stored({} blocks)", s.blocks.len()),
                KvCacheEventData::Removed { block_hashes } => {
                    format!("Removed({} hashes)", block_hashes.len())
                }
                KvCacheEventData::Cleared => "Cleared".to_string(),
            };
            tracing::debug!(
                instance_id = %instance_id,
                dp_rank,
                event_type = %event_desc,
                medium = ?medium_str,
                backend_id = ?backend_id_opt,
                "normalized event"
            );

            let medium = medium_str
                .as_deref()
                .map(StorageMedium::parse)
                .unwrap_or(StorageMedium::Xpu);

            let backend_id = backend_id_opt.as_deref().unwrap_or(instance_id).to_string();

            let worker = WorkerKey {
                instance_id: instance_id.to_string(),
                backend_id,
                dp_rank,
                medium,
            };

            indexer_entry.apply_event(&worker, &canonical)?;
            applied += 1;
        }

        Ok(applied)
    }

    /// List all registered workers (debug endpoint).
    pub async fn list_workers(&self) -> Vec<WorkerSummary> {
        let instances = self.instances.read().await;
        instances
            .values()
            .map(|entry| WorkerSummary {
                instance_id: entry.instance_id.clone(),
                model_name: entry.model_name.clone(),
                tenant_id: entry.tenant_id.clone(),
                block_size: entry.block_size,
                endpoints: entry.endpoints.clone(),
            })
            .collect()
    }

    /// Get indexer summary (debug endpoint).
    pub fn indexer_summary(&self) -> Vec<crate::indexer::IndexerSummary> {
        self.indexer.summary()
    }

    /// Access the underlying indexer (for advanced use).
    pub fn indexer(&self) -> &Arc<Indexer> {
        &self.indexer
    }
}

impl Default for WorkerRegistry {
    fn default() -> Self {
        Self::new(ScoringConfig::default())
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct WorkerSummary {
    pub instance_id: String,
    pub model_name: String,
    pub tenant_id: String,
    pub block_size: u32,
    pub endpoints: HashMap<u32, EndpointInfo>,
}

use serde::Serialize;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_extract_ip_ipv4() {
        assert_eq!(
            extract_ip_from_endpoint("tcp://10.0.0.1:5557"),
            Some("10.0.0.1".into())
        );
    }

    #[test]
    fn test_extract_ip_ipv6_bracketed() {
        assert_eq!(
            extract_ip_from_endpoint("tcp://[::1]:5557"),
            Some("::1".into())
        );
        assert_eq!(
            extract_ip_from_endpoint("tcp://[2001:db8::1]:15557"),
            Some("2001:db8::1".into())
        );
    }

    #[test]
    fn test_extract_ip_hostname() {
        assert_eq!(
            extract_ip_from_endpoint("tcp://kv-conductor:13333"),
            Some("kv-conductor".into())
        );
    }

    #[test]
    fn test_extract_ip_invalid() {
        assert_eq!(extract_ip_from_endpoint("invalid"), None);
        assert_eq!(extract_ip_from_endpoint("tcp://"), None);
        assert_eq!(extract_ip_from_endpoint("tcp://[:5557"), None);
    }
}
