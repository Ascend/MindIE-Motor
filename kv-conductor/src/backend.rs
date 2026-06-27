// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Storage backend abstraction for KV event pooling architectures.
//!
//! Three backends are supported, each with different event broadcast semantics:
//!
//! | Backend   | Pool model       | Auto-attach          | Usage                                           |
//! |-----------|------------------|----------------------|-------------------------------------------------|
//! | Mooncake  | Centralized      | IP → all DPs on node | One pool subscriber, events carry backend_id=IP |
//! | Memcache  | Centralized      | IP → all DPs on node | Same as Mooncake                                |
//! | YuanRong  | Per-node ports   | None (port = DP)     | Per-DP multi-port subscribers                   |
//!
//! The `StoreBackend` enum acts as a lightweight factory: it drives
//! registration behaviour (whether to index HBM IPs) and event-processing
//! behaviour (which `MatchMode` the pool subscriber uses).
//!
//! Note: For both Mooncake and Memcache, KV events do not carry an exact
//! dp_rank — instead every DP on the target node records the event's hash.
//! This avoids the overhead of per-DP event routing.

use crate::protocols::{HbmIpIndex, WorkerKey};

// ---------------------------------------------------------------------------
// StoreBackend
// ---------------------------------------------------------------------------

/// Supported KV storage / pooling backends.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StoreBackend {
    /// Mooncake: centralized master broadcasts one ZMQ PUB stream per cluster.
    /// Events carry `backend_id` = node IP.  The conductor matches that IP
    /// against all HBM-registered DPs on the node.
    Mooncake,
    /// Memcache: same semantics as Mooncake.  KV events carry `backend_id` =
    /// node IP but do **not** carry an exact `dp_rank`; every DP on the
    /// target node records the event hash.
    Memcache,
    /// YuanRong: each node has independent ZMQ PUB ports per storage medium.
    /// HBM, DDR and SSD events arrive on separate ports tied to a specific DP.
    YuanRong,
    /// Catch-all for unknown / future backends.  Treated as YuanRong.
    Unknown,
}

impl StoreBackend {
    /// Parse from the `store_backend` field in a registration request.
    pub fn parse(s: &str) -> Self {
        match s {
            s if s.eq_ignore_ascii_case("Mooncake") => Self::Mooncake,
            s if s.eq_ignore_ascii_case("Memcache") => Self::Memcache,
            s if s.eq_ignore_ascii_case("YuanRong") => Self::YuanRong,
            _ => Self::Unknown,
        }
    }

    /// Whether HBM registrations for this backend should be indexed
    /// in `hbm_ip_index` so pool subscribers can look them up.
    pub fn index_hbm_ip(&self) -> bool {
        matches!(self, Self::Mooncake | Self::Memcache)
    }

    /// Whether a pool registration (legacy `endpoint` only, no
    /// `medium_endpoints`) is treated as a pool subscriber with
    /// auto-attach enabled.
    pub fn is_pool_auto_attach(&self) -> bool {
        matches!(self, Self::Mooncake | Self::Memcache)
    }

    /// The matching strategy for pool-subscriber event processing.
    pub fn match_mode(&self) -> MatchMode {
        match self {
            Self::Mooncake => MatchMode::IpOnly,
            Self::Memcache => MatchMode::IpOnly,
            Self::YuanRong | Self::Unknown => MatchMode::None,
        }
    }
}

// ---------------------------------------------------------------------------
// MatchMode
// ---------------------------------------------------------------------------

/// How a pool subscriber resolves events into target `WorkerKey`s.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MatchMode {
    /// No auto-attach — the subscriber is tied to a fixed dp_rank (YuanRong).
    None,
    /// Match by IP only.  An event with `backend_id=<ip>` is applied to
    /// **every** HBM-registered DP whose XPU endpoint IP matches.
    /// (Mooncake: one master per cluster, backend_id=node IP).
    IpOnly,
    /// Match by IP **and** dp_rank.  An event with `backend_id=<ip>` and
    /// a specific `dp_rank` is applied to only the exact matching DP.
    /// (Currently unused; reserved for future backends that carry per-DP rank).
    IpAndDpRank,
}

impl MatchMode {
    /// Look up target workers in the HBM IP index, returning the list of
    /// `WorkerKey`s that should receive this event.
    ///
    /// - `ip_index`: the shared IP → DP lookup table.
    /// - `lookup_ip`: the `backend_id` from the event (node IP).
    /// - `event_dp_rank`: the dp_rank from the event (only used by `IpAndDpRank`).
    /// - `media`: the target storage media for this event.
    pub fn resolve_workers(
        self,
        ip_index: Option<&HbmIpIndex>,
        lookup_ip: &str,
        event_dp_rank: u32,
        media: &[crate::protocols::StorageMedium],
    ) -> Vec<WorkerKey> {
        let index = match ip_index {
            Some(idx) => idx,
            None => return Vec::new(),
        };
        let idx = index.read();
        let dps = match idx.get(lookup_ip) {
            Some(dps) => dps,
            None => return Vec::new(),
        };

        let mut workers = Vec::new();
        for &(ref iid, dp) in dps {
            let include = match self {
                Self::None => unreachable!("resolve_workers called with MatchMode::None"),
                Self::IpOnly => true, // apply to all DPs on the node
                Self::IpAndDpRank => dp == event_dp_rank, // exact match
            };
            if include {
                for &medium in media {
                    workers.push(WorkerKey {
                        instance_id: iid.clone(),
                        backend_id: iid.clone(),
                        dp_rank: dp,
                        medium,
                    });
                }
            }
        }
        workers
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::Arc;

    use crate::protocols::StorageMedium;

    fn make_ip_index(entries: Vec<(&str, Vec<(&str, u32)>)>) -> HbmIpIndex {
        let map: HashMap<String, Vec<(String, u32)>> = entries
            .into_iter()
            .map(|(ip, dps)| {
                (
                    ip.to_string(),
                    dps.into_iter()
                        .map(|(iid, dp)| (iid.to_string(), dp))
                        .collect(),
                )
            })
            .collect();
        Arc::new(parking_lot::RwLock::new(map))
    }

    // ── StoreBackend parsing ──────────────────────────────────────────

    #[test]
    fn test_store_backend_from_str() {
        assert_eq!(StoreBackend::parse("Mooncake"), StoreBackend::Mooncake);
        assert_eq!(StoreBackend::parse("mooncake"), StoreBackend::Mooncake);
        assert_eq!(StoreBackend::parse("Memcache"), StoreBackend::Memcache);
        assert_eq!(StoreBackend::parse("memcache"), StoreBackend::Memcache);
        assert_eq!(StoreBackend::parse("YuanRong"), StoreBackend::YuanRong);
        assert_eq!(StoreBackend::parse("yuanrong"), StoreBackend::YuanRong);
        assert_eq!(StoreBackend::parse("unknown"), StoreBackend::Unknown);
        assert_eq!(StoreBackend::parse(""), StoreBackend::Unknown);
    }

    #[test]
    fn test_store_backend_index_hbm_ip() {
        assert!(StoreBackend::Mooncake.index_hbm_ip());
        assert!(StoreBackend::Memcache.index_hbm_ip());
        assert!(!StoreBackend::YuanRong.index_hbm_ip());
        assert!(!StoreBackend::Unknown.index_hbm_ip());
    }

    #[test]
    fn test_store_backend_is_pool_auto_attach() {
        assert!(StoreBackend::Mooncake.is_pool_auto_attach());
        assert!(StoreBackend::Memcache.is_pool_auto_attach());
        assert!(!StoreBackend::YuanRong.is_pool_auto_attach());
        assert!(!StoreBackend::Unknown.is_pool_auto_attach());
    }

    #[test]
    fn test_store_backend_match_mode() {
        assert_eq!(StoreBackend::Mooncake.match_mode(), MatchMode::IpOnly);
        assert_eq!(StoreBackend::Memcache.match_mode(), MatchMode::IpOnly);
        assert_eq!(StoreBackend::YuanRong.match_mode(), MatchMode::None);
        assert_eq!(StoreBackend::Unknown.match_mode(), MatchMode::None);
    }

    // ── MatchMode::resolve_workers ────────────────────────────────────

    #[test]
    fn test_resolve_workers_ip_only_fans_out_to_all_dps_on_node() {
        let index = make_ip_index(vec![
            ("10.0.0.1", vec![("prefill-0", 0), ("prefill-1", 1)]),
            ("10.0.0.2", vec![("prefill-2", 0)]),
        ]);
        let media = &[StorageMedium::Cpu, StorageMedium::Disk];

        let workers = MatchMode::IpOnly.resolve_workers(
            Some(&index),
            "10.0.0.1",
            /*dp_rank=*/ 99,
            media,
        );

        // dp_rank=99 is ignored by IpOnly — all DPs on 10.0.0.1 match
        assert_eq!(workers.len(), 4); // 2 DPs × 2 media
        let mut ids: Vec<String> = workers.iter().map(|w| w.instance_id.clone()).collect();
        ids.sort();
        ids.dedup();
        assert_eq!(ids, vec!["prefill-0", "prefill-1"]);
    }

    #[test]
    fn test_resolve_workers_ip_and_dp_rank_exact_match() {
        let index = make_ip_index(vec![("10.0.0.1", vec![("prefill-0", 0), ("prefill-1", 1)])]);
        let media = &[StorageMedium::Cpu];

        // dp_rank=1 → only prefill-1 matches
        let workers = MatchMode::IpAndDpRank.resolve_workers(Some(&index), "10.0.0.1", 1, media);
        assert_eq!(workers.len(), 1);
        assert_eq!(workers[0].instance_id, "prefill-1");
        assert_eq!(workers[0].dp_rank, 1);
    }

    #[test]
    fn test_resolve_workers_ip_and_dp_rank_no_match_when_rank_differs() {
        let index = make_ip_index(vec![("10.0.0.1", vec![("prefill-0", 0)])]);
        let media = &[StorageMedium::Cpu];

        // dp_rank=7 — no DP with that rank on 10.0.0.1
        let workers = MatchMode::IpAndDpRank.resolve_workers(Some(&index), "10.0.0.1", 7, media);
        assert!(workers.is_empty());
    }

    #[test]
    fn test_resolve_workers_returns_empty_when_ip_not_found() {
        let index = make_ip_index(vec![("10.0.0.1", vec![("prefill-0", 0)])]);
        let media = &[StorageMedium::Cpu];

        let workers = MatchMode::IpOnly.resolve_workers(Some(&index), "10.0.99.99", 0, media);
        assert!(workers.is_empty());

        let workers = MatchMode::IpAndDpRank.resolve_workers(Some(&index), "10.0.99.99", 0, media);
        assert!(workers.is_empty());
    }

    #[test]
    fn test_resolve_workers_returns_empty_when_index_is_none() {
        let media = &[StorageMedium::Cpu];
        let workers = MatchMode::IpOnly.resolve_workers(None, "10.0.0.1", 0, media);
        assert!(workers.is_empty());
    }

    #[test]
    fn test_resolve_workers_empty_ip_index() {
        let index = make_ip_index(vec![]);
        let media = &[StorageMedium::Xpu];
        let workers = MatchMode::IpOnly.resolve_workers(Some(&index), "10.0.0.1", 0, media);
        assert!(workers.is_empty());
    }

    #[test]
    fn test_resolve_workers_multiple_media() {
        let index = make_ip_index(vec![("10.0.0.1", vec![("prefill-0", 0)])]);
        let media = &[StorageMedium::Xpu, StorageMedium::Cpu, StorageMedium::Disk];

        let workers = MatchMode::IpOnly.resolve_workers(Some(&index), "10.0.0.1", 0, media);
        assert_eq!(workers.len(), 3); // 1 DP × 3 media
        let media_set: std::collections::HashSet<_> = workers.iter().map(|w| w.medium).collect();
        assert_eq!(media_set.len(), 3);
    }

    #[test]
    fn test_resolve_workers_same_ip_multiple_dps_with_same_rank() {
        // Two DPs on the same IP with same dp_rank (different instance_id).
        // IpAndDpRank mode should match BOTH.
        let index = make_ip_index(vec![("10.0.0.1", vec![("prefill-a", 0), ("prefill-b", 0)])]);
        let media = &[StorageMedium::Cpu];

        let workers = MatchMode::IpAndDpRank.resolve_workers(Some(&index), "10.0.0.1", 0, media);
        assert_eq!(workers.len(), 2);
        let ids: Vec<&str> = workers.iter().map(|w| w.instance_id.as_str()).collect();
        assert!(ids.contains(&"prefill-a"));
        assert!(ids.contains(&"prefill-b"));
    }
}
