// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Stable integer status codes returned across the C ABI.
//!
//! The numbering matches `docs/zh/design/coordinator_scheduler_rust.md` §8.4. Schema-4 CAS
//! returns Changed / Blocked / SlotInvalid on the allocate/release hot path.

/// C ABI status code (`shm_status`).
pub type ShmStatus = i32;

pub const OK: ShmStatus = 0;
pub const CHANGED: ShmStatus = 1;
pub const BLOCKED: ShmStatus = 2;
pub const SLOT_INVALID: ShmStatus = 3;
pub const SCHEMA_MISMATCH: ShmStatus = 4;
pub const NOT_ATTACHED: ShmStatus = 5;
pub const NO_SPACE: ShmStatus = 6;
pub const SYSCALL: ShmStatus = 7;
pub const BAD_ARG: ShmStatus = 8;
