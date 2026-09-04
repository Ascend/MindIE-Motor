# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""
Native shared-memory writer contract (schema 4) and per-slot CAS.

Drives ``libmindie_workload_shm`` via ctypes and reads back with the production Python reader.
"""

import ctypes
import mmap
import multiprocessing
import os

import pytest

from motor.common.resources.instance import PDRole
from motor.coordinator.scheduler.runtime.workload_shm import native
from motor.coordinator.scheduler.runtime.workload_shm.layout import FLAG_VALID, ROLE_PREFILL, SCHEMA_VERSION
from motor.coordinator.scheduler.runtime.workload_shm.native import (
    MIN_ABI_VERSION,
    STATUS_BAD_ARG,
    STATUS_BLOCKED,
    STATUS_CHANGED,
    STATUS_OK,
    STATUS_SLOT_INVALID,
    NativeWorkloadShmError,
    NativeWorkloadShmUnavailable,
    WorkloadShm,
    load_native_library,
    pdrole_to_shm_role,
)
from motor.coordinator.scheduler.runtime.workload_shm.reader import WorkloadSharedMemoryReader


class _FakeCache:
    def __init__(self) -> None:
        self.patched: dict[tuple[int, int], tuple[PDRole, float]] = {}

    def patch_workload_from_shm(self, instance_id, endpoint_id, role, active_tokens) -> None:
        self.patched[(instance_id, endpoint_id)] = (role, active_tokens)


@pytest.fixture
def lib():
    try:
        return load_native_library()
    except NativeWorkloadShmUnavailable as e:
        pytest.skip(f"native workload-shm library not built: {e}")
        return None


def _unique(tag: str) -> str:
    return f"mw{os.getpid()}{tag}"[:24]


def _read_with_python(name: str, role: PDRole | None = None) -> tuple[tuple[int | None, bool], _FakeCache]:
    reader = WorkloadSharedMemoryReader(name)
    reader.attach()
    cache = _FakeCache()
    try:
        result = reader.read_and_patch_cache(cache, role=role)
    finally:
        reader.detach()
    return result, cache


def test_native_reports_abi(lib):
    """ABI version is stable; production segments are schema 4."""
    assert lib.mindie_wl_abi_version() >= MIN_ABI_VERSION
    assert MIN_ABI_VERSION == 2
    assert lib.mindie_wl_schema_version() == 4
    name = _unique("ab")
    shm = WorkloadShm.create_v4(name, 4, lib=lib)
    try:
        assert shm.read_header()["schema_version"] == SCHEMA_VERSION == 4
    finally:
        shm.close(unlink=True)


def test_native_writer_roundtrips_to_python_reader(lib):
    """Rust schema-4 snapshot -> production Python reader."""
    name = _unique("rt")
    shm = WorkloadShm.create_v4(name, 16, lib=lib)
    try:
        shm.write_snapshot_v4(
            [
                (1, 10, pdrole_to_shm_role(PDRole.ROLE_P), 0, FLAG_VALID, 7.0),
                (1, 11, pdrole_to_shm_role(PDRole.ROLE_P), 0, FLAG_VALID, 8.0),
                (2, 20, pdrole_to_shm_role(PDRole.ROLE_P), 0, FLAG_VALID, 9.0),
            ],
            bump_instance_version=True,
        )
        shm.heartbeat()

        header = shm.read_header()
        assert header["schema_version"] == 4
        assert header["sequence"] % 2 == 0
        assert header["entry_count"] == 3
        assert header["instance_version"] == 1
        assert header["heartbeat"] == 1

        (instance_version, stale), cache = _read_with_python(name)
        assert instance_version == 1
        assert stale is False
        assert cache.patched == {
            (1, 10): (PDRole.ROLE_P, 7.0),
            (1, 11): (PDRole.ROLE_P, 8.0),
            (2, 20): (PDRole.ROLE_P, 9.0),
        }
    finally:
        shm.close(unlink=True)


def test_native_odd_sequence_is_rejected_then_accepted(lib):
    """A begun-but-not-committed snapshot (odd seqlock) is refused; commit makes it readable."""
    name = _unique("od")
    shm = WorkloadShm.create_v4(name, 8, lib=lib)
    try:
        shm.snapshot_begin()
        assert shm.read_header()["sequence"] % 2 == 1

        (instance_version, stale), cache = _read_with_python(name)
        assert instance_version is None
        assert cache.patched == {}

        shm.snapshot_commit(0, bump_instance_version=False)
        shm.write_snapshot_v4(
            [(1, 10, pdrole_to_shm_role(PDRole.ROLE_P), 0, FLAG_VALID, 5.0)],
            bump_instance_version=True,
        )
        assert shm.read_header()["sequence"] % 2 == 0

        (instance_version2, _), cache2 = _read_with_python(name)
        assert instance_version2 == 1
        assert cache2.patched == {(1, 10): (PDRole.ROLE_P, 5.0)}
    finally:
        shm.close(unlink=True)


def test_heartbeat_stale_detected_even_when_sequence_stuck_odd(lib):
    """Staleness must be detectable even while sequence is stuck odd (writer crashed mid-snapshot,
    entries permanently unreadable).
    """
    from motor.coordinator.scheduler.runtime.workload_shm.layout import HEARTBEAT_STALE_SEC

    name = _unique("hbo")
    shm = WorkloadShm.create_v4(name, 4, lib=lib)
    shm.heartbeat()  # bump once, like the real _heartbeat_loop does right after start_control_plane
    reader = WorkloadSharedMemoryReader(name)
    reader.attach()
    try:
        cache = _FakeCache()
        _instance_version, stale = reader.read_and_patch_cache(cache)
        assert stale is False

        # Simulate elapsed time past the staleness threshold with no new heartbeat in between.
        reader._last_heartbeat_time -= HEARTBEAT_STALE_SEC + 1.0

        # Writer begins a snapshot and never commits: sequence is stuck odd, entries unreadable.
        shm.snapshot_begin()
        assert shm.read_header()["sequence"] % 2 == 1

        instance_version2, stale2 = reader.read_and_patch_cache(cache)
        assert instance_version2 is None  # entries still unreadable, as before
        assert stale2 is True  # but heartbeat staleness must still be detected
    finally:
        reader.detach()
        shm.close(unlink=True)


def test_native_create_v4_recovers_from_orphan(lib):
    """Creating over an existing (orphaned) segment unlinks and recreates it."""
    name = _unique("or")
    first = WorkloadShm.create_v4(name, 4, lib=lib)
    first.write_snapshot_v4([(1, 10, pdrole_to_shm_role(PDRole.ROLE_P), 0, FLAG_VALID, 1.0)])
    first.close(unlink=False)

    second = WorkloadShm.create_v4(name, 4, lib=lib)
    try:
        second.write_snapshot_v4([(2, 20, pdrole_to_shm_role(PDRole.ROLE_P), 0, FLAG_VALID, 2.0)])
        (_, _), cache = _read_with_python(name)
        assert cache.patched == {(2, 20): (PDRole.ROLE_P, 2.0)}
    finally:
        second.close(unlink=True)


def test_missing_library_raises_clear_error():
    """A missing .so must raise NativeWorkloadShmUnavailable (no silent fallback)."""
    with pytest.raises(NativeWorkloadShmUnavailable) as exc:
        load_native_library(path="/nonexistent/does-not-exist/libmindie_workload_shm.so")
    assert native._LIB_BASENAME in str(exc.value)


def _poke_schema_version(name: str, schema: int) -> None:
    """Overwrite header schema_version (offset 4, little-endian u16) on a live POSIX SHM."""
    libc = ctypes.CDLL(None)
    shm_open = libc.shm_open
    shm_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_uint]
    shm_open.restype = ctypes.c_int
    posix_name = name if name.startswith("/") else f"/{name}"
    fd = shm_open(posix_name.encode("utf-8"), os.O_RDWR, 0o600)
    if fd < 0:
        raise OSError("shm_open failed while poking schema_version")
    try:
        mm = mmap.mmap(fd, 8)
        try:
            mm[4:6] = schema.to_bytes(2, "little")
        finally:
            mm.close()
    finally:
        os.close(fd)


def test_schema_mismatch_is_refused(lib):
    """A non-schema-4 header is refused by the Reader."""
    name = _unique("sm")
    shm = WorkloadShm.create_v4(name, 8, lib=lib)
    reader = WorkloadSharedMemoryReader(name)
    try:
        shm.write_snapshot_v4([(1, 10, 0, 0, FLAG_VALID, 7.0)])
        _poke_schema_version(name, 3)
        reader.attach()
        cache = _FakeCache()
        instance_version, _stale = reader.read_and_patch_cache(cache, role=None)
        assert instance_version is None
        assert cache.patched == {}
    finally:
        reader.detach()
        shm.close(unlink=True)


def _single_entry_segment(lib, tag: str) -> WorkloadShm:
    shm = WorkloadShm.create_v4(_unique(tag), 8, lib=lib)
    shm.write_snapshot_v4([(1, 10, ROLE_PREFILL, 0, 0, 0.0)])
    return shm


def _cas_add_until_ok(shm: WorkloadShm, instance_id: int, endpoint_id: int, generation: int, delta: float) -> float:
    """Test-only: retry the same slot on CHANGED. Production allocate must not do this."""
    expected = 0.0
    for _ in range(1000):
        status, actual = shm.cas_add(instance_id, endpoint_id, generation, expected, delta)
        if status == STATUS_OK:
            return actual
        if status == STATUS_CHANGED:
            expected = actual
            continue
        raise NativeWorkloadShmError(f"cas_add refused: status={status}")
    raise NativeWorkloadShmError("cas_add did not converge after 1000 retries")


def test_snapshot_v4_does_not_clobber_cas_tokens(lib):
    """Membership rewrite must not store stale caller tokens over a live pair."""
    shm = _single_entry_segment(lib, "clob")
    try:
        assert shm.cas_add(1, 10, 0, 0.0, 11.0)[0] == STATUS_OK
        shm.write_snapshot_v4([(1, 10, ROLE_PREFILL, 0, FLAG_VALID, 0.0)])
        assert shm.load_entry(0)["active_tokens"] == 11.0
        status, _ = shm.cas_add(1, 10, 0, 11.0, float("nan"))
        assert status == STATUS_BAD_ARG
        status, _ = shm.cas_add(1, 10, 0, 11.0, -1.0)
        assert status == STATUS_BAD_ARG
        assert shm.load_entry(0)["active_tokens"] == 11.0
        status, _ = shm.cas_sub_floor0(1, 10, 0, float("nan"))
        assert status == STATUS_BAD_ARG
        assert shm.load_entry(0)["active_tokens"] == 11.0
    finally:
        shm.close(unlink=True)


def test_snapshot_v4_copies_tokens_when_pair_moves_slot(lib):
    """Compaction that relocates a pair must copy current tokens, not the stale snapshot argument."""
    shm = WorkloadShm.create_v4(_unique("mv"), 8, lib=lib)
    try:
        shm.write_snapshot_v4(
            [
                (1, 10, ROLE_PREFILL, 0, FLAG_VALID, 0.0),
                (2, 20, ROLE_PREFILL, 0, FLAG_VALID, 0.0),
            ]
        )
        assert shm.cas_add(2, 20, 0, 0.0, 7.0)[0] == STATUS_OK
        shm.write_snapshot_v4([(2, 20, ROLE_PREFILL, 0, FLAG_VALID, 0.0)])
        entry = shm.load_entry(0)
        assert entry["instance_id"] == 2
        assert entry["endpoint_id"] == 20
        assert entry["active_tokens"] == 7.0
    finally:
        shm.close(unlink=True)


def test_cas_add_ok_then_changed(lib):
    """expected match -> OK and value increments; stale expected -> CHANGED with fresh value, no add."""
    shm = _single_entry_segment(lib, "ok")
    try:
        status, actual = shm.cas_add(1, 10, 0, expected=0.0, delta=3.0)
        assert status == STATUS_OK
        assert actual == 3.0
        status, actual = shm.cas_add(1, 10, 0, expected=0.0, delta=100.0)
        assert status == STATUS_CHANGED
        assert actual == 3.0
    finally:
        shm.close(unlink=True)


def test_cas_sub_floor0(lib):
    """Release path floors at 0 and never goes negative."""
    shm = _single_entry_segment(lib, "floor0")
    try:
        assert shm.cas_add(1, 10, 0, 0.0, 5.0)[0] == STATUS_OK
        status, actual = shm.cas_sub_floor0(1, 10, 0, 9.0)
        assert status == STATUS_OK
        assert actual == 0.0
    finally:
        shm.close(unlink=True)


def test_blocked_flag_is_final_gate(lib):
    """set_blocked makes cas_add refuse (BLOCKED); clearing it re-enables allocation (C3)."""
    shm = _single_entry_segment(lib, "blocked")
    try:
        assert shm.set_blocked(1, True) == 1
        status, actual = shm.cas_add(1, 10, 0, 0.0, 1.0)
        assert status == STATUS_BLOCKED
        assert actual == 0.0
        shm.set_blocked(1, False)
        assert shm.cas_add(1, 10, 0, 0.0, 1.0)[0] == STATUS_OK
    finally:
        shm.close(unlink=True)


def test_generation_mismatch_is_slot_invalid(lib):
    """A stale generation (slot reused) yields SLOT_INVALID, guarding against ABA."""
    shm = _single_entry_segment(lib, "gen")
    try:
        status, _ = shm.cas_add(1, 10, 1, 0.0, 1.0)
        assert status == STATUS_SLOT_INVALID
    finally:
        shm.close(unlink=True)


def test_cas_add_until_ok_retries_on_changed(lib):
    """The CAS-expected retry helper converges even when the starting expected is stale."""
    shm = _single_entry_segment(lib, "retry")
    try:
        shm.cas_add(1, 10, 0, 0.0, 7.0)
        final = _cas_add_until_ok(shm, 1, 10, 0, 5.0)
        assert final == 12.0
    finally:
        shm.close(unlink=True)


def _cas_worker(name: str, instance_id: int, endpoint_id: int, generation: int, count: int) -> None:
    """Child process: attach the shared segment and CAS-add `count` times with retry-on-Changed."""
    # Spawned child has a fresh interpreter; re-import is required.
    from motor.coordinator.scheduler.runtime.workload_shm.native import (  # pylint: disable=reimported
        STATUS_CHANGED as _CHANGED,
        STATUS_OK as _OK,
        WorkloadShm as _WorkloadShm,
        load_native_library as _load,
    )

    shm = _WorkloadShm.attach(name, lib=_load())
    try:
        for _ in range(count):
            expected = 0.0
            for _ in range(1000):
                status, actual = shm.cas_add(instance_id, endpoint_id, generation, expected, 1.0)
                if status == _OK:
                    break
                if status == _CHANGED:
                    expected = actual
                    continue
                raise RuntimeError(f"cas_add refused: status={status}")
            else:
                raise RuntimeError("cas_add did not converge")
    finally:
        shm.close(unlink=False)


def test_multiprocess_cas_conserves_total(lib):
    """R1 core (A2): N spawned processes each add 1.0 M times; final == N*M exactly (no lost updates)."""
    ctx = multiprocessing.get_context("spawn")
    name = _unique("conserve")
    shm = WorkloadShm.create_v4(name, 8, lib=lib)
    n_procs = 4
    per_proc = 500
    try:
        shm.write_snapshot_v4([(1, 10, ROLE_PREFILL, 0, 0, 0.0)])
        procs = [ctx.Process(target=_cas_worker, args=(name, 1, 10, 0, per_proc)) for _ in range(n_procs)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=120)
            assert p.exitcode == 0, f"worker exited with {p.exitcode}"
        total = shm.load_entry(0)["active_tokens"]
        assert total == float(n_procs * per_proc)
    finally:
        shm.close(unlink=True)


def test_load_entries_matches_per_slot_and_cas_uses_slot(lib):
    """One FFI refresh must equal N load_entry calls; cas_add with a stale slot is SLOT_INVALID."""
    assert ctypes.sizeof(native._LoadedEntry) == 24
    shm = WorkloadShm.create_v4(_unique("batch"), 16, lib=lib)
    try:
        shm.write_snapshot_v4(
            [
                (1, 10, ROLE_PREFILL, 0, FLAG_VALID, 0.0),
                (2, 20, ROLE_PREFILL, 0, FLAG_VALID, 0.0),
            ]
        )
        batched = shm.load_entries(2)
        assert [row["slot"] for row in batched] == [0, 1]
        assert batched[0]["instance_id"] == 1
        assert batched[1]["instance_id"] == 2
        assert batched[0]["active_tokens"] == shm.load_entry(0)["active_tokens"]
        status, actual = shm.cas_add(1, 10, 0, 0.0, 3.0, slot=0)
        assert status == STATUS_OK
        assert actual == 3.0
        status, _ = shm.cas_add(1, 10, 0, 3.0, 1.0, slot=1)
        assert status == STATUS_SLOT_INVALID
    finally:
        shm.close(unlink=True)


@pytest.mark.parametrize("n_slots", [1, 100, 1000, 10240])
def test_load_entries_scales_with_live_slot_count(lib, n_slots):
    """Batch load of N live slots (reviewer 1/100/1000/10240) returns N rows in one FFI."""
    shm = WorkloadShm.create_v4(_unique(f"n{n_slots}"), n_slots, lib=lib)
    try:
        entries = [(i + 1, i + 1, ROLE_PREFILL, 0, FLAG_VALID, 0.0) for i in range(n_slots)]
        shm.write_snapshot_v4(entries)
        loaded = shm.load_entries(n_slots)
        assert len(loaded) == n_slots
        assert loaded[0]["slot"] == 0
        assert loaded[-1]["slot"] == n_slots - 1
        assert loaded[-1]["instance_id"] == n_slots
    finally:
        shm.close(unlink=True)
