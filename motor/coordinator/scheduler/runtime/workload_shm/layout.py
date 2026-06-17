# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Shared memory layout for workload data.
Header 64B + Entry 32B × N. Little-endian.
Sequence in header follows seqlock semantics: odd means writer in progress,
even means readers may accept the snapshot after a matching second header read.
Instance_version in header: bumped on instance list change (REFRESH_INSTANCES);
Infer uses it to refresh local instance cache when version changes.
"""

import struct
from dataclasses import dataclass

# Magic: 4-byte format signature at start of shared memory header.
# "WKLD" (WorkLoad) as ASCII -> 0x57 0x4B 0x4C 0x44 = 0x574B4C44 (little-endian).
# Readers check this to ensure the buffer is our workload shm layout, not other data or corruption.
MAGIC = 0x574B4C44

# Schema version for layout compatibility
SCHEMA_VERSION = 1

# Role mapping: prefill=0, decode=1, hybrid=2, encode=3
ROLE_PREFILL = 0
ROLE_DECODE = 1
ROLE_HYBRID = 2
ROLE_ENCODE = 3

# Header: 64 bytes
# magic 4B, schema 2B, padding 2B, sequence 8B (seqlock), entry_count 4B, max_entries 4B,
# instance_version 8B (bumped when instance/endpoint set changes),
# heartbeat_sequence 8B (Scheduler bumps ~1/s), reserved 24B
HEADER_SIZE = 64
HEADER_FMT = "<I H H q I I Q Q 24x"  # little-endian
HEARTBEAT_OFFSET = 32  # bytes 32-40: heartbeat_sequence (Q)
HEARTBEAT_STALE_SEC = 5.0  # If heartbeat unchanged for this long, Infer treats shm as stale

# Entry: 32 bytes
# instance_id 4B, endpoint_id 4B, role 1B, padding 3B, active_tokens 8B, active_kv_cache 8B, padding 4B
ENTRY_SIZE = 32
ENTRY_FMT = "<i i B 3x d d 4x"

# Max number of (instance, endpoint) workload entries in shared memory. Not user-configurable.
DEFAULT_WORKLOAD_SHM_MAX_ENTRIES = 10240


@dataclass(frozen=True)
class WorkloadShmEntry:
    """Single workload entry (32 bytes). Used by pack_entry/unpack_entry and writer."""

    instance_id: int
    endpoint_id: int
    role: int
    active_tokens: float
    active_kv_cache: float


@dataclass(frozen=True)
class WorkloadShmHeader:
    """Header fields for workload shared memory (64 bytes). Used by pack_header/unpack_header."""

    magic: int
    schema_version: int
    sequence: int
    entry_count: int
    max_entries: int
    instance_version: int = 0
    heartbeat_sequence: int = 0


def pack_header(header: WorkloadShmHeader) -> bytes:
    """Pack header into 64 bytes. instance_version/heartbeat_sequence in 0..2^64-1 (unsigned)."""
    instance_version = header.instance_version
    heartbeat_sequence = header.heartbeat_sequence
    if instance_version < 0 or instance_version > (1 << 64) - 1:
        instance_version = instance_version % (1 << 64)
    if heartbeat_sequence < 0 or heartbeat_sequence > (1 << 64) - 1:
        heartbeat_sequence = heartbeat_sequence % (1 << 64)
    return struct.pack(
        HEADER_FMT,
        header.magic,
        header.schema_version,
        0,  # padding
        header.sequence,
        header.entry_count,
        header.max_entries,
        instance_version,
        heartbeat_sequence,
    )


def unpack_header(buf: memoryview) -> WorkloadShmHeader:
    """Parse 64-byte header from buffer. Returns WorkloadShmHeader."""
    if len(buf) < HEADER_SIZE:
        raise ValueError(f"Buffer too small for header: {len(buf)} < {HEADER_SIZE}")
    t = struct.unpack(HEADER_FMT, buf[:HEADER_SIZE])
    return WorkloadShmHeader(
        magic=t[0],
        schema_version=t[1],
        sequence=t[3],
        entry_count=t[4],
        max_entries=t[5],
        instance_version=t[6],
        heartbeat_sequence=t[7],
    )


def pack_entry(entry: WorkloadShmEntry) -> bytes:
    """Pack single entry into 32 bytes."""
    return struct.pack(
        ENTRY_FMT,
        entry.instance_id,
        entry.endpoint_id,
        entry.role,
        entry.active_tokens,
        entry.active_kv_cache,
    )


def unpack_entry(buf: memoryview, slot: int) -> WorkloadShmEntry:
    """Unpack entry at slot. Returns WorkloadShmEntry."""
    offset = HEADER_SIZE + slot * ENTRY_SIZE
    if offset + ENTRY_SIZE > len(buf):
        raise ValueError(f"Entry slot {slot} out of range")
    t = struct.unpack(ENTRY_FMT, buf[offset: offset + ENTRY_SIZE])
    return WorkloadShmEntry(
        instance_id=t[0],
        endpoint_id=t[1],
        role=t[2],
        active_tokens=t[3],
        active_kv_cache=t[4],
    )


def total_size(max_entries: int) -> int:
    """Total shared memory size in bytes."""
    return HEADER_SIZE + max_entries * ENTRY_SIZE
