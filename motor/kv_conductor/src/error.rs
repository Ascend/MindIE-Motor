// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

/// Errors returned by the KV conductor service.
#[derive(Debug, thiserror::Error)]
pub enum KvConductorError {
    #[error("instance {instance_id} dp_rank {dp_rank} already registered")]
    DuplicateRegistration { instance_id: String, dp_rank: u32 },

    #[error("instance {instance_id} not found")]
    InstanceNotFound { instance_id: String },

    #[error("no indexer found for model={model_name} tenant={tenant_id}")]
    NoIndexer {
        model_name: String,
        tenant_id: String,
    },

    #[error("no workers registered for model={model_name} tenant={tenant_id}")]
    NoWorkers {
        model_name: String,
        tenant_id: String,
    },

    #[error("KV cache event error: {0}")]
    KvCacheEvent(String),

    #[error("invalid block sequence")]
    InvalidBlockSequence,

    #[error("parent block not found")]
    ParentBlockNotFound,

    #[error("block not found")]
    BlockNotFound,

    #[error("internal error: {0}")]
    Internal(String),
}
