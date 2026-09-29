// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
// MindIE is licensed under Mulan PSL v2.
// You can use this software according to the terms and conditions of the Mulan PSL v2.
// You may obtain a copy of Mulan PSL v2 at:
//         http://license.coscl.org.cn/MulanPSL2
// THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
// EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
// MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
// See the Mulan PSL v2 for more details.

//! Byte layout for the workload shared-memory segment.
//!
//! This mirrors, byte-for-byte, the Python layout in
//! `motor/coordinator/scheduler/runtime/workload_shm/layout.py` (SCHEMA_VERSION 4):
//! a 64-byte header followed by N 24-byte entries, little-endian. Membership is seqlock-
//! published by Mgmt; per-slot `active_tokens` is an AtomicU64 CAS'd by Infer Workers.

/// Magic "WKLD" (0x57 0x4B 0x4C 0x44) little-endian.
pub const MAGIC: u32 = 0x574B_4C44;
/// Layout schema version. Must match the Python reader/owner.
///
/// v4: per-slot CAS entry (24B). v5: entry widened to 32B with `total_requests`
/// (cumulative allocations, never decremented) at offset 24. v4 readers are hard-rejected
/// (Python reader header check / Rust attach check) — Mgmt and all Infer Workers must be
/// upgraded together; an old .so CAS-ing a v5 segment would corrupt the wider stride.
pub const SCHEMA_VERSION: u16 = 5;

pub const HEADER_SIZE: usize = 64;
pub const ENTRY_SIZE: usize = 32;
pub const DEFAULT_MAX_ENTRIES: u32 = 10240;

// Header field byte offsets (see layout.py HEADER_FMT "<I H H q I I Q Q Q Q Q").
pub const OFF_MAGIC: usize = 0; // u32
pub const OFF_SCHEMA: usize = 4; // u16
pub const OFF_SEQUENCE: usize = 8; // i64 (seqlock; odd = write in progress)
pub const OFF_ENTRY_COUNT: usize = 16; // u32
pub const OFF_MAX_ENTRIES: usize = 20; // u32
pub const OFF_INSTANCE_VERSION: usize = 24; // u64
pub const OFF_HEARTBEAT: usize = 32; // u64
pub const OFF_PREFILL_SEQ: usize = 40; // u64
pub const OFF_DECODE_SEQ: usize = 48; // u64
pub const OFF_HYBRID_SEQ: usize = 56; // u64

// shm role bytes (layout.py: prefill=0, decode=1, hybrid=2, encode=3).
pub const ROLE_PREFILL: u8 = 0;
pub const ROLE_DECODE: u8 = 1;
pub const ROLE_HYBRID: u8 = 2;
pub const ROLE_ENCODE: u8 = 3;

// ---------------------------------------------------------------------------
// Schema 4: per-slot atomic CAS layout. Header is 64B; seqlock covers only membership
// (token CAS does NOT bump it), so readers must atomic-load tokens on every scoring pass.
// ---------------------------------------------------------------------------

// Entry field byte offsets within a 32-byte slot for schema 5.
//
// active_tokens is placed at offset 16 so that, with an 8-aligned segment base and a 32B stride,
// it is always 8-byte aligned and can host a sound hardware `AtomicU64` CAS (mandatory on
// aarch64 / Ascend hosts, where a misaligned 8-byte atomic faults). total_requests sits at
// offset 24 (8-aligned too): the cumulative allocation counter, monotonic while the slot lives.
//
// NOTE: schema 4 placed active_tokens at offset 16 within a 24B stride for the same alignment
// reason; design §5.2's original offset-12 layout was never shippable (4-byte aligned only).
// Schema 5 keeps every schema-4 field at its offset and appends total_requests, so the first
// 24 bytes of each entry are byte-identical to schema 4.
pub const ENTRY_V4_OFF_INSTANCE_ID: usize = 0; // i32 (written on snapshot only)
pub const ENTRY_V4_OFF_ENDPOINT_ID: usize = 4; // i32 (written on snapshot only)
pub const ENTRY_V4_OFF_ROLE: usize = 8; // u8 (written on snapshot only)
pub const ENTRY_V4_OFF_FLAGS: usize = 9; // u8, AtomicU8 (BLOCKED / VALID)
pub const ENTRY_V4_OFF_GENERATION: usize = 10; // u16 (written on snapshot only; ABA guard)
pub const ENTRY_V4_OFF_REQUEST_COUNT: usize = 12; // u32, AtomicU32 (in-flight request count; CAS inc on allocate, dec on release)
pub const ENTRY_V4_OFF_ACTIVE_TOKENS: usize = 16; // u64 (f64::to_bits), AtomicU64, 8-aligned
pub const ENTRY_V4_OFF_TOTAL_REQUESTS: usize = 24; // u64, AtomicU64, 8-aligned (cumulative allocations, never decremented)

// Entry flags bits.
pub const FLAG_BLOCKED: u8 = 0b0000_0001; // circuit-breaker OPEN: allocate CAS must refuse
pub const FLAG_VALID: u8 = 0b0000_0010; // slot holds a live (instance, endpoint)

// Compile-time guarantee that the 8-byte atomics fit inside a 32B entry.
const _: () = assert!(ENTRY_V4_OFF_TOTAL_REQUESTS + 8 <= ENTRY_SIZE);

/// Total segment size in bytes for `max_entries` slots.
pub fn total_size(max_entries: u32) -> usize {
    HEADER_SIZE + (max_entries as usize) * ENTRY_SIZE
}

/// Byte offset of the given slot's entry.
pub fn entry_offset(slot: u32) -> usize {
    HEADER_SIZE + (slot as usize) * ENTRY_SIZE
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sizes_match_python_layout() {
        assert_eq!(HEADER_SIZE, 64);
        assert_eq!(ENTRY_SIZE, 32);
        assert_eq!(total_size(10240), 64 + 10240 * 32);
        assert_eq!(entry_offset(0), 64);
        assert_eq!(entry_offset(1), 96);
    }

    #[test]
    fn schema5_8_byte_fields_are_8_byte_aligned_for_every_slot() {
        // A sound AtomicU64 CAS requires the address be 8-aligned on all slots.
        for slot in 0..1024u32 {
            for (name, off) in [
                (
                    "active_tokens",
                    entry_offset(slot) + ENTRY_V4_OFF_ACTIVE_TOKENS,
                ),
                (
                    "total_requests",
                    entry_offset(slot) + ENTRY_V4_OFF_TOTAL_REQUESTS,
                ),
            ] {
                assert_eq!(off % 8, 0, "slot {slot} {name} offset {off} not 8-aligned");
            }
        }
    }
}
