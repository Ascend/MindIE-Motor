# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
ctypes binding for the Rust ``libmindie_workload_shm`` shared-memory ledger (schema 4).

Mgmt owns the segment (create_v4 / membership snapshot / heartbeat / set_blocked). Infer Workers
attach and CAS tokens (cas_add / cas_sub_floor0) against per-slot AtomicU64 plus generation and
flags. If the ``.so`` is missing the loader raises ``NativeWorkloadShmUnavailable`` with a clear
message -- callers must fail loudly rather than silently fall back to a wrong ledger.
"""

import ctypes
import os
from typing import Any

from motor.common.logger import get_logger
from motor.common.resources.instance import PDRole
from motor.coordinator.scheduler.runtime.workload_shm.layout import (
    DEFAULT_WORKLOAD_SHM_MAX_ENTRIES,
    ROLE_DECODE,
    ROLE_ENCODE,
    ROLE_HYBRID,
    ROLE_PREFILL,
)

logger = get_logger(__name__)

_LIB_BASENAME = "libmindie_workload_shm.so"
_ENV_OVERRIDE = "WORKLOAD_SHM_LIB"

# C ABI status codes (must match src/error.rs).
_STATUS = {
    0: "Ok",
    1: "Changed",
    2: "Blocked",
    3: "SlotInvalid",
    4: "SchemaMismatch",
    5: "NotAttached",
    6: "NoSpace",
    7: "Syscall",
    8: "BadArg",
}
_STATUS_OK = 0

# Named status codes for CAS control flow (callers branch on these; they are not errors).
STATUS_OK = 0
STATUS_CHANGED = 1
STATUS_BLOCKED = 2
STATUS_SLOT_INVALID = 3
STATUS_BAD_ARG = 8

# Must match Rust SLOT_HINT_NONE: cas_add/cas_sub_floor0 linear-scan when the caller has no slot.
SLOT_HINT_NONE = 0xFFFFFFFF
# ctypes arg layout for cas_add/cas_sub/load_entries; older .so must not be bound.
MIN_ABI_VERSION = 2


class _LoadedEntry(ctypes.Structure):
    """24-byte schema-4 entry view returned by mindie_wl_load_entries."""

    _pack_ = 1
    _fields_ = [
        ("instance_id", ctypes.c_int32),
        ("endpoint_id", ctypes.c_int32),
        ("role", ctypes.c_uint8),
        ("flags", ctypes.c_uint8),
        ("generation", ctypes.c_uint16),
        ("reserved", ctypes.c_uint32),
        ("active_tokens", ctypes.c_double),
    ]


# Entry flag bits (must match layout.rs / schema 4).
FLAG_BLOCKED = 0b0000_0001
FLAG_VALID = 0b0000_0010

_PDROLE_TO_SHM = {
    PDRole.ROLE_E: ROLE_ENCODE,
    PDRole.ROLE_P: ROLE_PREFILL,
    PDRole.ROLE_D: ROLE_DECODE,
    PDRole.ROLE_U: ROLE_HYBRID,
}


class NativeWorkloadShmUnavailable(RuntimeError):
    """Raised when the native workload-shm library cannot be loaded."""


class NativeWorkloadShmError(RuntimeError):
    """Raised when a native workload-shm call returns a non-Ok status."""


def pdrole_to_shm_role(role: PDRole) -> int:
    """Map PDRole to the shm layout role byte (hybrid for unknowns, matching the Python writer)."""
    return _PDROLE_TO_SHM.get(role, ROLE_HYBRID)


def _candidate_paths() -> list[str]:
    """Search order: explicit env override, packaged wheel lib dir, then source build output."""
    paths: list[str] = []
    override = os.environ.get(_ENV_OVERRIDE)
    if override:
        paths.append(override)
    crate_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "workload_shm_rs"))
    paths.append(os.path.join(crate_dir, "lib", _LIB_BASENAME))  # packaged (setup.py package_data)
    paths.append(os.path.join(crate_dir, "target", "release", _LIB_BASENAME))  # source dev
    return paths


def _bind(lib: ctypes.CDLL) -> ctypes.CDLL:
    """Declare argtypes/restype for the C ABI so ctypes marshals arguments correctly."""
    lib.mindie_wl_abi_version.restype = ctypes.c_uint32
    lib.mindie_wl_abi_version.argtypes = []
    lib.mindie_wl_schema_version.restype = ctypes.c_uint32
    lib.mindie_wl_schema_version.argtypes = []
    lib.mindie_wl_attach.restype = ctypes.c_int32
    lib.mindie_wl_attach.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint64)]
    lib.mindie_wl_close.restype = ctypes.c_int32
    lib.mindie_wl_close.argtypes = [ctypes.c_uint64, ctypes.c_int32]
    lib.mindie_wl_snapshot_begin.restype = ctypes.c_int32
    lib.mindie_wl_snapshot_begin.argtypes = [ctypes.c_uint64]
    lib.mindie_wl_snapshot_commit.restype = ctypes.c_int32
    lib.mindie_wl_snapshot_commit.argtypes = [ctypes.c_uint64, ctypes.c_uint32, ctypes.c_int32]
    lib.mindie_wl_heartbeat.restype = ctypes.c_int32
    lib.mindie_wl_heartbeat.argtypes = [ctypes.c_uint64]
    lib.mindie_wl_read_header.restype = ctypes.c_int32
    lib.mindie_wl_read_header.argtypes = [
        ctypes.c_uint64,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_int64),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
    ]
    # Schema 4 (per-slot CAS) surface.
    lib.mindie_wl_create_v4.restype = ctypes.c_int32
    lib.mindie_wl_create_v4.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint64)]
    lib.mindie_wl_snapshot_write_entry_v4.restype = ctypes.c_int32
    lib.mindie_wl_snapshot_write_entry_v4.argtypes = [
        ctypes.c_uint64,
        ctypes.c_uint32,
        ctypes.c_int32,
        ctypes.c_int32,
        ctypes.c_uint8,
        ctypes.c_uint16,
        ctypes.c_uint8,
        ctypes.c_double,
    ]
    lib.mindie_wl_cas_add.restype = ctypes.c_int32
    lib.mindie_wl_cas_add.argtypes = [
        ctypes.c_uint64,
        ctypes.c_uint32,
        ctypes.c_int32,
        ctypes.c_int32,
        ctypes.c_uint16,
        ctypes.c_double,
        ctypes.c_double,
        ctypes.POINTER(ctypes.c_double),
    ]
    lib.mindie_wl_cas_sub_floor0.restype = ctypes.c_int32
    lib.mindie_wl_cas_sub_floor0.argtypes = [
        ctypes.c_uint64,
        ctypes.c_uint32,
        ctypes.c_int32,
        ctypes.c_int32,
        ctypes.c_uint16,
        ctypes.c_double,
        ctypes.POINTER(ctypes.c_double),
    ]
    lib.mindie_wl_set_blocked.restype = ctypes.c_int32
    lib.mindie_wl_set_blocked.argtypes = [
        ctypes.c_uint64,
        ctypes.c_int32,
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    lib.mindie_wl_load_entry.restype = ctypes.c_int32
    lib.mindie_wl_load_entry.argtypes = [
        ctypes.c_uint64,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.POINTER(ctypes.c_uint8),
        ctypes.POINTER(ctypes.c_uint16),
        ctypes.POINTER(ctypes.c_double),
    ]
    lib.mindie_wl_load_entries.restype = ctypes.c_int32
    lib.mindie_wl_load_entries.argtypes = [
        ctypes.c_uint64,
        ctypes.POINTER(_LoadedEntry),
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    return lib


_lib_cache: ctypes.CDLL | None = None


def load_native_library(path: str | None = None) -> ctypes.CDLL:
    """
    Load (and cache) the native workload-shm library.

    Raises NativeWorkloadShmUnavailable with the searched paths when it cannot be found/loaded, so
    the caller degrades loudly instead of writing a wrong ledger.
    """
    global _lib_cache
    if path is None and _lib_cache is not None:
        return _lib_cache
    candidates = [path] if path else _candidate_paths()
    errors: list[str] = []
    for candidate in candidates:
        if not candidate or not os.path.isfile(candidate):
            errors.append(f"{candidate}: not found")
            continue
        try:
            lib = _bind(ctypes.CDLL(candidate))
        except (OSError, AttributeError) as e:
            errors.append(f"{candidate}: {e}")
            continue
        abi = int(lib.mindie_wl_abi_version())
        if abi < MIN_ABI_VERSION:
            errors.append(f"{candidate}: ABI {abi} < {MIN_ABI_VERSION}")
            continue
        if path is None:
            _lib_cache = lib
        return lib
    raise NativeWorkloadShmUnavailable(
        "Could not load "
        + _LIB_BASENAME
        + " (build it via build.sh / `cargo build --release` in "
        + "motor/coordinator/workload_shm_rs, or set "
        + _ENV_OVERRIDE
        + "). Tried: "
        + "; ".join(errors)
    )


class WorkloadShm:
    """Thin OO wrapper over the C ABI. One instance owns one handle."""

    def __init__(self, lib: ctypes.CDLL, handle: int, *, created: bool, name: str = ""):
        self._lib = lib
        self._handle = handle
        self._created = created
        self._name = name

    @property
    def name(self) -> str:
        """POSIX SHM name this handle is attached to."""
        return self._name

    @property
    def handle(self) -> int:
        """Opaque native handle."""
        return self._handle

    @classmethod
    def create_v4(
        cls,
        name: str,
        max_entries: int = DEFAULT_WORKLOAD_SHM_MAX_ENTRIES,
        *,
        lib: ctypes.CDLL | None = None,
    ) -> "WorkloadShm":
        """Create and own a new schema-4 (per-slot CAS) segment."""
        lib = lib or load_native_library()
        handle = ctypes.c_uint64(0)
        _check(
            lib.mindie_wl_create_v4(name.encode("utf-8"), int(max_entries), ctypes.byref(handle)),
            "create_v4",
        )
        return cls(lib, handle.value, created=True, name=name)

    @classmethod
    def attach(cls, name: str, *, lib: ctypes.CDLL | None = None) -> "WorkloadShm":
        """Attach to an existing segment (does not own unlink)."""
        lib = lib or load_native_library()
        handle = ctypes.c_uint64(0)
        _check(lib.mindie_wl_attach(name.encode("utf-8"), ctypes.byref(handle)), "attach")
        return cls(lib, handle.value, created=False, name=name)

    def snapshot_begin(self) -> None:
        """Mark the segment writer-in-progress (odd seqlock)."""
        _check(self._lib.mindie_wl_snapshot_begin(self._handle), "snapshot_begin")

    def snapshot_commit(self, entry_count: int, bump_instance_version: bool = True) -> None:
        """Publish the snapshot (even seqlock) with ``entry_count`` valid slots."""
        _check(
            self._lib.mindie_wl_snapshot_commit(self._handle, int(entry_count), 1 if bump_instance_version else 0),
            "snapshot_commit",
        )

    def heartbeat(self) -> None:
        """Bump the heartbeat counter (~1/s) so readers can detect a dead writer."""
        _check(self._lib.mindie_wl_heartbeat(self._handle), "heartbeat")

    # ------------------------------------------------------------------
    # Schema 4: per-slot CAS data plane
    # ------------------------------------------------------------------

    def write_snapshot_v4(
        self,
        entries: list[tuple[int, int, int, int, int, float]],
        *,
        bump_instance_version: bool = True,
    ) -> None:
        """Write a schema-4 snapshot: (instance_id, endpoint_id, role, generation, flags, tokens)/slot.

        ``tokens`` seeds a new pair only. A live pair's in-flight CAS value is preserved by the
        native writer (stale caller tokens are ignored). ``(0, 0, *, *, 0, *)`` punches a hole.
        """
        self.snapshot_begin()
        for slot, (iid, eid, role, gen, flags, tokens) in enumerate(entries):
            _check(
                self._lib.mindie_wl_snapshot_write_entry_v4(
                    self._handle, int(slot), int(iid), int(eid), int(role), int(gen), int(flags), float(tokens)
                ),
                "snapshot_write_entry_v4",
            )
        self.snapshot_commit(len(entries), bump_instance_version=bump_instance_version)

    def cas_add(
        self,
        instance_id: int,
        endpoint_id: int,
        generation: int,
        expected: float,
        delta: float,
        slot: int | None = None,
    ) -> tuple[int, float]:
        """Atomic CAS-add. Returns (status, actual): OK (added), CHANGED/BLOCKED/SLOT_INVALID/BAD_ARG (not).

        ``slot`` is the known SHM index from the last batch load; omit to linear-scan.
        """
        actual = ctypes.c_double(0.0)
        status = self._lib.mindie_wl_cas_add(
            self._handle,
            SLOT_HINT_NONE if slot is None else int(slot),
            int(instance_id),
            int(endpoint_id),
            int(generation),
            float(expected),
            float(delta),
            ctypes.byref(actual),
        )
        return status, actual.value

    def cas_sub_floor0(
        self,
        instance_id: int,
        endpoint_id: int,
        generation: int,
        delta: float,
        slot: int | None = None,
    ) -> tuple[int, float]:
        """Atomic CAS-subtract flooring at 0 (release path). Returns (status, actual); BAD_ARG on invalid delta."""
        actual = ctypes.c_double(0.0)
        status = self._lib.mindie_wl_cas_sub_floor0(
            self._handle,
            SLOT_HINT_NONE if slot is None else int(slot),
            int(instance_id),
            int(endpoint_id),
            int(generation),
            float(delta),
            ctypes.byref(actual),
        )
        return status, actual.value

    def set_blocked(self, instance_id: int, blocked: bool) -> int:
        """Set/clear the BLOCKED flag on all VALID slots of an instance. Returns slots touched."""
        touched = ctypes.c_uint32(0)
        _check(
            self._lib.mindie_wl_set_blocked(self._handle, int(instance_id), 1 if blocked else 0, ctypes.byref(touched)),
            "set_blocked",
        )
        return touched.value

    def load_entry(self, slot: int) -> dict:
        """Read one schema-4 entry: instance_id, endpoint_id, role, flags, generation, active_tokens."""
        iid = ctypes.c_int32(0)
        eid = ctypes.c_int32(0)
        role = ctypes.c_uint8(0)
        flags = ctypes.c_uint8(0)
        generation = ctypes.c_uint16(0)
        tokens = ctypes.c_double(0.0)
        _check(
            self._lib.mindie_wl_load_entry(
                self._handle,
                int(slot),
                ctypes.byref(iid),
                ctypes.byref(eid),
                ctypes.byref(role),
                ctypes.byref(flags),
                ctypes.byref(generation),
                ctypes.byref(tokens),
            ),
            "load_entry",
        )
        return {
            "instance_id": iid.value,
            "endpoint_id": eid.value,
            "role": role.value,
            "flags": flags.value,
            "generation": generation.value,
            "active_tokens": tokens.value,
        }

    def load_entries(self, entry_count: int) -> list[dict[str, Any]]:
        """Read ``entry_count`` schema-4 slots in one FFI call. Each dict includes ``slot``."""
        cap = max(int(entry_count), 0)
        if cap == 0:
            return []
        buf = (_LoadedEntry * cap)()
        out_n = ctypes.c_uint32(0)
        _check(
            self._lib.mindie_wl_load_entries(self._handle, buf, cap, ctypes.byref(out_n)),
            "load_entries",
        )
        entries: list[dict[str, Any]] = []
        for slot in range(int(out_n.value)):
            row = buf[slot]
            entries.append(
                {
                    "slot": slot,
                    "instance_id": row.instance_id,
                    "endpoint_id": row.endpoint_id,
                    "role": row.role,
                    "flags": row.flags,
                    "generation": row.generation,
                    "active_tokens": row.active_tokens,
                }
            )
        return entries

    def read_header(self) -> dict[str, int]:
        """Read header scalars: schema_version, sequence, entry_count, instance_version, heartbeat."""
        schema = ctypes.c_uint32(0)
        sequence = ctypes.c_int64(0)
        entry_count = ctypes.c_uint32(0)
        instance_version = ctypes.c_uint64(0)
        heartbeat = ctypes.c_uint64(0)
        _check(
            self._lib.mindie_wl_read_header(
                self._handle,
                ctypes.byref(schema),
                ctypes.byref(sequence),
                ctypes.byref(entry_count),
                ctypes.byref(instance_version),
                ctypes.byref(heartbeat),
            ),
            "read_header",
        )
        return {
            "schema_version": schema.value,
            "sequence": sequence.value,
            "entry_count": entry_count.value,
            "instance_version": instance_version.value,
            "heartbeat": heartbeat.value,
        }

    def close(self, unlink: bool | None = None) -> None:
        """Close the handle; unlinks the segment when this instance created it (unless overridden)."""
        if self._handle == 0:
            return
        do_unlink = self._created if unlink is None else unlink
        self._lib.mindie_wl_close(self._handle, 1 if do_unlink else 0)
        self._handle = 0


def _check(status: int, op: str) -> None:
    if status != _STATUS_OK:
        raise NativeWorkloadShmError(f"{op} failed: status={status} ({_STATUS.get(status, 'Unknown')})")
