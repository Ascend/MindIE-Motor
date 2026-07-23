// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! KV Conductor — Radix-tree-based KV cache indexer for MindIE-PyMotor.
//!
//! Provides a standalone HTTP service that maintains prefix trees of cached
//! KV blocks per (model, tenant) pair. Workers register their endpoints and
//! send KV cache events; the conductor answers overlap queries to guide
//! cache-aware request routing.

pub mod backend;
pub mod concurrent_tree;
pub mod error;
pub mod events;
pub mod hashing;
pub mod indexer;
pub mod protocols;
pub mod registry;
pub mod server;

pub mod zmq_subscriber;

// Re-export key types for convenience
pub use backend::{MatchMode, StoreBackend};
pub use concurrent_tree::ConcurrentRadixTree;
pub use error::KvConductorError;
pub use hashing::compute_block_hash_for_seq;
pub use indexer::Indexer;
pub use protocols::{
    DpRank, HbmIpIndex, InstanceId, InstanceMatchData, KvCacheEvent, KvCacheEventData,
    KvCacheStoreData, KvCacheStoredBlockData, KvEventBatch, KvEventWirePayload, LocalBlockHash,
    OverlapScores, QueryByHashRequest, QueryRequest, QueryResponse, RegisterRequest,
    SequenceBlockHash, StorageMedium, UnregisterRequest, WorkerKey,
};
pub use registry::WorkerRegistry;
pub use server::create_router;
