// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Shared helpers for event application.

use crate::backend::MatchMode;
use crate::protocols::*;

/// Resolve the target storage media list from an event's optional `medium`
/// field, falling back to the subscriber's `default_media`.
pub(super) fn resolve_medium(
    event_medium: Option<&str>,
    default_media: &[StorageMedium],
) -> Vec<StorageMedium> {
    if let Some(m) = event_medium {
        vec![StorageMedium::parse(m)]
    } else {
        default_media.to_vec()
    }
}

/// Build the list of `WorkerKey` targets for an event.
///
/// - `MatchMode::None` (YuanRong): one worker per medium, using `backend_id`
///   directly as the instance identity.
/// - Other modes (Mooncake/Memcache): delegates to `MatchMode::resolve_workers`
///   which fans out via `hbm_ip_index`.
pub(super) fn resolve_workers(
    match_mode: MatchMode,
    hbm_ip_index: &Option<HbmIpIndex>,
    backend_id: &str,
    dp_rank: u32,
    target_media: &[StorageMedium],
) -> Vec<WorkerKey> {
    if match_mode == MatchMode::None {
        target_media
            .iter()
            .map(|&medium| WorkerKey {
                instance_id: backend_id.to_string(),
                backend_id: backend_id.to_string(),
                dp_rank,
                medium,
            })
            .collect()
    } else {
        match_mode.resolve_workers(hbm_ip_index.as_ref(), backend_id, dp_rank, target_media)
    }
}
