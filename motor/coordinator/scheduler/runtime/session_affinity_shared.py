# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Cross-process session affinity table backed by POSIX shared memory.

The coordinator dispatches requests to multiple InferenceWorker *processes*
(spawn context — no shared memory inheritance), so the in-process
:class:`SessionAffinityTable` cannot provide session stickiness: each worker
only sees the pins it recorded itself. This module implements the same
interface on top of a file-backed shared-memory table (``/dev/shm``) guarded
by an advisory file lock, so every worker process — and any worker restarted
later — sees the same pins.

Slot layout (128 bytes, little-endian):

======  ================================================================
offset  field
======  ================================================================
0       key_lo (u64 — first half of blake2b-16 of ``session_id+NUL+role``)
8       key_hi (u64 — second half of the same digest)
16      session tag (64 bytes: sha256(session_id) hex[:32], NUL-padded)
80      role (16 bytes, NUL-padded UTF-8, truncated)
96      instance_id (i64)
104     epoch (i64, -1 when the client did not send one)
112     last_used (f64, ``time.monotonic()`` at last record/hit — LRU ordering only)
120     renewed_at (f64, ``time.time()`` at last record only — untouched by
        get() touches)
128     retention_expires (f64, wall-clock expiry of the strongest retention
        declaration made for this pin via :meth:`declare_retention`; 0 when
        none. The declaration keeper compares this against its own declared
        expiry: a larger value means another worker holds a longer promise
        and the release must be deferred)
136     endpoint_id (i64, the endpoint/DP that owns the pin's KV; -1 when not
        carried — multi-endpoint (DP) instances cache per endpoint, so a hit
        must return to the recorded endpoint, not re-pick one by load)
======  ================================================================

Slot states: ``key_lo==key_hi==0`` marks an EMPTY slot; the reserved pair
``(_TOMB_LO, _TOMB_HI)`` marks a TOMBSTONE (a deleted slot that must stay in
the probe chain so later keys hashing into the same run remain reachable —
linear probing breaks otherwise). Everything else is a live pin.

The session tag is a digest of the full session id, so arbitrarily long or
multi-byte ids are stored and looked up without truncation. Tags exist for
``drop_all_roles`` (which matches on the tag instead of the digest, so one
digest collision across roles cannot false-positive) and for diagnostics.

File layout: ``MAGIC(8) | reserved(8) | counters[4 roles][64 instances + 1
overflow] u64`` followed by the slot array. ``counters`` lets
:meth:`count_pins_by_instance` answer in O(1) on the new-pin placement path
instead of scanning all slots under the file lock; they are maintained
incrementally on record/drop and rebuilt once per attach (compensating any
lost update from a crashed writer). The per-role overflow counter counts pins
whose instance id is outside the 64-slot region, forcing a scan in that case.

Eviction: when the probe window finds no empty slot and no matching key, the
least-recently-used slot within the window is reused, skipping slots whose
``retention_expires`` is still in the future (a live ``cache_control`` pin must
not be LRU-evicted before its declared TTL); only when every candidate is still
promised does it fall back to the oldest of them. All operations hold an
exclusive ``flock`` on the backing file, so ops are serialized machine-wide
(microsecond-scale critical sections).
"""

import fcntl
import hashlib
import os
import struct
import time
from collections.abc import Iterable, Mapping
from typing import NamedTuple

from motor.common.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_MAX_ENTRIES = 65536


class SessionPin(NamedTuple):
    """A pinned instance for one (role, session) pair.

    ``epoch`` mirrors the session context version of the request that created
    the pin; ``None`` when the client did not send one (no invalidation).

    Defined here so the shared table and the in-process table share one type
    without a module-level cycle: this module must stay importable from
    ``session_affinity`` (which instantiates the shared table lazily), while
    it only depends on stdlib itself.
    """

    instance_id: int
    epoch: int | None
    endpoint_id: int | None = None


def select_balanced_instance(
    eligible_instance_ids: Iterable[int],
    pin_counts: Mapping[int, int],
    instance_active_tokens: Mapping[int, float],
    placement_seq: int,
) -> int | None:
    """Pick the new-pin instance minimizing (pinned sessions, active tokens, seq).

    Lexicographic keys: stickiness commitment, instantaneous in-flight load,
    then a monotonic per-worker counter as the race-free spread for sub-100ms
    bursts where concurrent coroutines read the same stale snapshot. Lives
    here (next to the shared table) so :meth:`record_balanced` can use it
    without importing the in-process table module; ``session_affinity``
    re-exports it.

    Returns the chosen instance id, or ``None`` when ``eligible`` is empty.
    """
    ids = sorted({int(iid) for iid in eligible_instance_ids})
    if not ids:
        return None

    def score(iid: int) -> tuple[int, float]:
        return (int(pin_counts.get(iid, 0)), float(instance_active_tokens.get(iid, 0.0)))

    best = min(score(iid) for iid in ids)
    tied = [iid for iid in ids if score(iid) == best]
    if len(tied) == 1:
        return tied[0]
    return tied[placement_seq % len(tied)]


_SHM_DIR = "/dev/shm"  # nosec B108 -- POSIX shared memory namespace, not a temp file
_SHM_NAME_PREFIX = "motor_session_affinity_"
_MAGIC = b"MSAFF7"
_HEADER_SIZE = 16
_COUNTER_INSTANCES = 64
# One extra counter per role block tracks pins whose instance id is outside
# the id-indexed region (>= _COUNTER_INSTANCES). Those pins have no per-id
# counter, so a non-zero overflow count tells count_pins_by_instance its O(1)
# counters are incomplete and it must fall back to a full slot scan.
_COUNTER_SLOTS = _COUNTER_INSTANCES + 1
_ROLES = ("prefill", "decode", "union", "encode")
_ROLE_COUNTER_IDX = {role: idx for idx, role in enumerate(_ROLES)}
_COUNTERS_SIZE = _COUNTER_SLOTS * len(_ROLES) * 8
_SLOT_OFFSET = _HEADER_SIZE + _COUNTERS_SIZE
_SLOT_SIZE = 144
_SESSION_BYTES = 64
_ROLE_BYTES = 16
_EPOCH_NONE = -1
_PROBE_WINDOW = 256
_SLOT_FMT = "<QQ64s16sqqdddq"
# Reserved digest pair marking a tombstone: a deleted slot that must stay in
# the probe chain (linear probing with clear-to-zero otherwise orphans every
# later key in the same run). Collision probability with a real blake2b-16
# digest is 2^-128 — negligible.
_TOMB_LO = 1
_TOMB_HI = 1


def _key_digest(session_id: str, role: str) -> bytes:
    """Return the 16-byte digest identifying one (role, session_id) pair."""
    return hashlib.blake2b((session_id + "\0" + role).encode("utf-8"), digest_size=16).digest()


def _slot_session(session_id: str) -> str:
    """Return the fixed-width slot tag for a session id.

    A sha256 prefix keeps arbitrary (long, multi-byte) session ids unique and
    identical between record and lookup — truncating the raw id to the slot's
    64-byte text area made those ids silently unmatchable afterwards.
    """
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _pack_text(value: str, size: int) -> bytes:
    return value.encode("utf-8")[:size].ljust(size, b"\0")


def _unpack_text(raw: bytes) -> str:
    return raw.rstrip(b"\0").decode("utf-8", errors="replace")


class SharedSessionAffinityTable:
    """Bounded (role, session_id) → instance_id mapping shared machine-wide.

    Drop-in replacement for :class:`SessionAffinityTable`: same public
    interface, but the state lives in a ``/dev/shm`` file shared by every
    process that attaches with the same ``shm_key`` — and it survives
    InferenceWorker restarts.
    """

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES, shm_key: str = "default") -> None:
        self._max_entries = max(1, int(max_entries))
        self._slot_count = self._max_entries
        self._path = os.path.join(_SHM_DIR, _SHM_NAME_PREFIX + shm_key + ".bin")
        self._total_size = _SLOT_OFFSET + self._slot_count * _SLOT_SIZE
        self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        self._init_file()
        self._rebuild_counters()

    def _init_file(self) -> None:
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            size = os.fstat(self._fd).st_size
            magic = os.pread(self._fd, len(_MAGIC), 0) if size >= len(_MAGIC) else b""
            if size != self._total_size or magic != _MAGIC:
                if size:
                    logger.warning(
                        "session affinity shm reset (had %s bytes, magic=%r); stale layout discarded",
                        size,
                        magic,
                    )
                os.ftruncate(self._fd, self._total_size)
                os.pwrite(self._fd, _MAGIC, 0)
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def _counter_offset(self, role: str, instance_id: int) -> int | None:
        """Byte offset of one (role, instance) counter; None when out of range."""
        role_idx = _ROLE_COUNTER_IDX.get(role)
        if role_idx is None or not 0 <= int(instance_id) < _COUNTER_INSTANCES:
            return None
        return _HEADER_SIZE + (role_idx * _COUNTER_SLOTS + int(instance_id)) * 8

    def _overflow_counter_offset(self, role: str) -> int | None:
        """Byte offset of the role's out-of-range marker counter; None for unknown roles."""
        role_idx = _ROLE_COUNTER_IDX.get(role)
        if role_idx is None:
            return None
        return _HEADER_SIZE + (role_idx * _COUNTER_SLOTS + _COUNTER_INSTANCES) * 8

    def _counter_add(self, role: str, instance_id: int, delta: int) -> None:
        offset = self._counter_offset(role, instance_id)
        if offset is None:
            # No per-id counter for this instance; track it in the role's
            # overflow marker so count_pins_by_instance knows to scan instead
            # of trusting the incomplete counter region.
            offset = self._overflow_counter_offset(role)
            if offset is None:
                return
        (current,) = struct.unpack_from("<Q", os.pread(self._fd, 8, offset))
        os.pwrite(self._fd, struct.pack("<Q", max(0, current + delta)), offset)

    def _read_counters_locked(self, role: str) -> tuple[dict[int, int], bool]:
        """Read one role's counters; caller must hold the file lock.

        Returns ``(counts, incomplete)``. ``incomplete`` is True when the role
        is outside the counter region or has pins with out-of-range instance
        ids, in which case the caller must scan the slot array for exact counts.
        """
        role_idx = _ROLE_COUNTER_IDX.get(role)
        if role_idx is None:
            return {}, True
        base = _HEADER_SIZE + role_idx * _COUNTER_SLOTS * 8
        raw = os.pread(self._fd, _COUNTER_SLOTS * 8, base)
        (overflow,) = struct.unpack_from("<Q", raw, _COUNTER_INSTANCES * 8)
        if overflow:
            return {}, True
        counts: dict[int, int] = {}
        for instance_id in range(_COUNTER_INSTANCES):
            (count,) = struct.unpack_from("<Q", raw, instance_id * 8)
            if count:
                counts[instance_id] = int(count)
        return counts, False

    def _scan_counts_locked(self, role: str) -> dict[int, int]:
        """Exact per-instance counts via a full slot scan; caller holds the lock."""
        counts: dict[int, int] = {}
        for slot in range(self._slot_count):
            key_lo, key_hi, _, role_raw, instance_id, _, _, _, _, _ = self._read_slot(slot)
            if (key_lo, key_hi) in ((0, 0), (_TOMB_LO, _TOMB_HI)):
                continue
            if _unpack_text(role_raw) == role:
                iid = int(instance_id)
                counts[iid] = counts.get(iid, 0) + 1
        return counts

    def _rebuild_counters(self) -> None:
        """Recompute the counter region from the slot array (once per attach).

        Heals any drift caused by a writer dying between the slot write and the
        counter update; cheap relative to worker start-up.
        """
        counts: dict[tuple[str, int], int] = {}
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            for slot in range(self._slot_count):
                key_lo, key_hi, _, role_raw, instance_id, _, _, _, _, _ = self._read_slot(slot)
                if (key_lo, key_hi) in ((0, 0), (_TOMB_LO, _TOMB_HI)):
                    continue
                role = _unpack_text(role_raw)
                key = (role, int(instance_id))
                counts[key] = counts.get(key, 0) + 1
            os.pwrite(self._fd, b"\0" * _COUNTERS_SIZE, _HEADER_SIZE)
            for (role, instance_id), count in counts.items():
                self._counter_add(role, instance_id, count)
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def __len__(self) -> int:
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            count = 0
            for slot in range(self._slot_count):
                key_lo, key_hi = struct.unpack_from("<QQ", self._read_raw(slot))
                if (key_lo, key_hi) not in ((0, 0), (_TOMB_LO, _TOMB_HI)):
                    count += 1
            return count
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def _read_raw(self, slot: int) -> bytes:
        return os.pread(self._fd, _SLOT_SIZE, _SLOT_OFFSET + slot * _SLOT_SIZE)

    def _read_slot(self, slot: int) -> tuple[int, int, bytes, bytes, int, int, float, float, float, int]:
        (
            key_lo,
            key_hi,
            session,
            role,
            instance_id,
            epoch,
            last_used,
            renewed_at,
            retention_expires,
            pinned_endpoint,
        ) = struct.unpack(_SLOT_FMT, self._read_raw(slot))
        return (
            key_lo,
            key_hi,
            session,
            role,
            instance_id,
            epoch,
            last_used,
            renewed_at,
            retention_expires,
            pinned_endpoint,
        )

    def _write_slot(
        self,
        slot: int,
        key_lo: int,
        key_hi: int,
        session: bytes,
        role: bytes,
        instance_id: int,
        epoch: int,
        last_used: float,
        renewed_at: float,
        retention_expires: float,
        pinned_endpoint: int,
    ) -> None:
        os.pwrite(
            self._fd,
            struct.pack(
                _SLOT_FMT,
                key_lo,
                key_hi,
                session,
                role,
                instance_id,
                epoch,
                last_used,
                renewed_at,
                retention_expires,
                pinned_endpoint,
            ),
            _SLOT_OFFSET + slot * _SLOT_SIZE,
        )

    def _make_tombstone(self, slot: int, role: str, instance_id: int) -> None:
        """Turn a live slot into a tombstone, keeping the probe chain intact."""
        self._counter_add(role, instance_id, -1)
        os.pwrite(
            self._fd,
            struct.pack(
                _SLOT_FMT,
                _TOMB_LO,
                _TOMB_HI,
                b"\0" * _SESSION_BYTES,
                b"\0" * _ROLE_BYTES,
                0,
                0,
                0.0,
                0.0,
                0.0,
                -1,
            ),
            _SLOT_OFFSET + slot * _SLOT_SIZE,
        )

    def _prepare_slot_overwrite_locked(self, slot: int, key_lo: int, key_hi: int, role: str, instance_id: int) -> float:
        """Adjust pin counters for the slot about to be overwritten; return the
        retention expiry the new pin inherits.

        :meth:`_find_slot` hands back either the same-key match, an
        empty/tombstone slot, or a live different-key slot chosen for eviction.
        Only a same-key rebind keeps the existing retention promise and moves the
        pinned-session count between instances. An eviction must subtract the
        *occupant's own* ``(role, instance)`` count (not the new pin's role) and
        drop the occupant's retention — carrying it onto the new session would
        make the keeper mistake the inherited promise for a fresher other-worker
        renewal and defer the new session's own declaration release (review
        194979025 / 194981811).
        """
        lo, hi, _session, role_raw, old_instance, _epoch, _last, _renewed, retention, _endpoint = self._read_slot(slot)
        if (lo, hi) in ((0, 0), (_TOMB_LO, _TOMB_HI)):
            self._counter_add(role, instance_id, 1)
            return 0.0
        occupant_role = _unpack_text(role_raw)
        if lo == key_lo and hi == key_hi and occupant_role == role:
            # Same-key rebind: keep the strongest promise made for this pin.
            if int(old_instance) != int(instance_id):
                self._counter_add(role, int(old_instance), -1)
                self._counter_add(role, int(instance_id), 1)
            return float(retention or 0.0)
        # Live different-key entry evicted from its slot.
        self._counter_add(occupant_role, int(old_instance), -1)
        self._counter_add(role, instance_id, 1)
        return 0.0

    def _find_slot(self, key_lo: int, key_hi: int) -> tuple[int | None, int | None]:
        """Return (matching_slot, free_slot_for_insert) for the key.

        Tombstones stay in the probe chain (they must not terminate the
        lookup), so a deleted entry no longer hides later keys in the same
        run. Insertion prefers the first tombstone seen, which keeps chains
        short; empty slots terminate the probe.

        Eviction picks the least-recently-used slot within the window, but only
        among slots whose ``retention_expires`` has already passed: a pin with
        a live ``cache_control`` promise must not be LRU-evicted before its
        declared TTL (review 194980525). When every candidate is still
        promised, the oldest promised slot is reused as a last resort so a
        full table never blocks insertion.
        """
        start = key_lo % self._slot_count
        now_wall = time.time()
        oldest_slot: int | None = None
        oldest_ts = float("inf")
        oldest_protected_slot: int | None = None
        oldest_protected_ts = float("inf")
        first_tombstone: int | None = None
        for offset in range(min(_PROBE_WINDOW, self._slot_count)):
            slot = (start + offset) % self._slot_count
            lo, hi = struct.unpack_from("<QQ", self._read_raw(slot))
            if lo == _TOMB_LO and hi == _TOMB_HI:
                if first_tombstone is None:
                    first_tombstone = slot
                continue
            if lo == 0 and hi == 0:
                return None, first_tombstone if first_tombstone is not None else slot
            if lo == key_lo and hi == key_hi:
                return slot, None
            _, _, _, _, _, _, last_used, _, retention_expires, _ = self._read_slot(slot)
            if retention_expires > now_wall:
                if last_used < oldest_protected_ts:
                    oldest_protected_ts = last_used
                    oldest_protected_slot = slot
            elif last_used < oldest_ts:
                oldest_ts = last_used
                oldest_slot = slot
        if first_tombstone is not None:
            return None, first_tombstone
        return None, oldest_slot if oldest_slot is not None else oldest_protected_slot

    def get(self, session_id: str, role: str) -> SessionPin | None:
        """Return the pinned entry, refreshing recency on hit; None when absent."""
        if not isinstance(session_id, str) or not session_id:
            return None
        tag = _slot_session(session_id)
        digest = _key_digest(session_id, role)
        key_lo = int.from_bytes(digest[:8], "little")
        key_hi = int.from_bytes(digest[8:], "little")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            slot, _ = self._find_slot(key_lo, key_hi)
            if slot is None:
                return None
            lo, hi, session, role_raw, instance_id, epoch, _, renewed_at, retention_expires, pinned_endpoint = (
                self._read_slot(slot)
            )
            if _unpack_text(session) != tag or _unpack_text(role_raw) != role:
                return None
            self._write_slot(
                slot,
                lo,
                hi,
                session,
                role_raw,
                instance_id,
                epoch,
                time.monotonic(),
                renewed_at,
                retention_expires,
                pinned_endpoint,
            )
            return SessionPin(
                instance_id=instance_id,
                epoch=None if epoch == _EPOCH_NONE else epoch,
                endpoint_id=None if pinned_endpoint == -1 else pinned_endpoint,
            )
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def record(
        self,
        session_id: str,
        instance_id: int,
        role: str,
        epoch: int | None = None,
        endpoint_id: int | None = None,
    ) -> None:
        """Pin session_id to instance_id for role (machine-wide, serialized)."""
        if not isinstance(session_id, str) or not session_id:
            return
        tag = _slot_session(session_id)
        digest = _key_digest(session_id, role)
        key_lo = int.from_bytes(digest[:8], "little")
        key_hi = int.from_bytes(digest[8:], "little")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            match, free = self._find_slot(key_lo, key_hi)
            slot = match if match is not None else free
            if slot is None:
                logger.warning("session affinity shm full (%s slots); pin dropped", self._slot_count)
                return
            slot_retention = self._prepare_slot_overwrite_locked(slot, key_lo, key_hi, role, int(instance_id))
            self._write_slot(
                slot,
                key_lo,
                key_hi,
                _pack_text(tag, _SESSION_BYTES),
                _pack_text(role, _ROLE_BYTES),
                int(instance_id),
                _EPOCH_NONE if epoch is None else int(epoch),
                time.monotonic(),
                time.time(),
                slot_retention,
                -1 if endpoint_id is None else int(endpoint_id),
            )
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def drop_all_roles(self, session_id: str) -> int:
        """Forget every role pin for session_id (out-of-band finalize, Phase 4).

        Slots carry the session tag next to the key digest, so the scan
        matches on the tag — a digest collision across roles of the same
        session cannot false-positive, and the release is fleet-wide by
        construction (every worker reads the same shm). Deleted slots become
        tombstones: zeroing them instead would truncate the linear-probe run
        and strand every later key hashing into it. Returns the number of
        cleared slots so the caller can log the release; no-op (0) when the
        session has no pin in any role pool.
        """
        if not isinstance(session_id, str) or not session_id:
            return 0
        tag = _slot_session(session_id)
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            dropped = 0
            for slot in range(self._slot_count):
                key_lo, key_hi, session, _, instance_id, _, _, _, _, _ = self._read_slot(slot)
                if (key_lo, key_hi) in ((0, 0), (_TOMB_LO, _TOMB_HI)):
                    continue
                if _unpack_text(session) == tag:
                    self._make_tombstone(slot, _unpack_text(self._read_slot(slot)[3]), int(instance_id))
                    dropped += 1
            return dropped
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def drop(self, session_id: str, role: str) -> None:
        """Forget the pin for (role, session_id); no-op when absent."""
        if not isinstance(session_id, str) or not session_id:
            return
        tag = _slot_session(session_id)
        digest = _key_digest(session_id, role)
        key_lo = int.from_bytes(digest[:8], "little")
        key_hi = int.from_bytes(digest[8:], "little")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            slot, _ = self._find_slot(key_lo, key_hi)
            if slot is not None:
                _, _, session, role_raw, instance_id, _, _, _, _, _ = self._read_slot(slot)
                if _unpack_text(session) == tag and _unpack_text(role_raw) == role:
                    self._make_tombstone(slot, role, int(instance_id))
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def record_balanced(
        self,
        session_id: str,
        candidate_instance_ids: Iterable[int],
        role: str,
        epoch: int | None,
        load_fn,
        placement_seq: int,
        endpoint_id: int | None = None,
    ) -> int | None:
        """Select-and-commit one balanced fresh pin under a single file lock.

        Count-read, selection and pin write used to be three separately locked
        operations: two workers could read the same stale counts with the same
        process-local seq and both commit to the same instance (review
        194528732, reproduced as ``{1: 2}``). Folding them into one critical
        section makes every burst decision see the previous one's commit.
        ``load_fn()`` returns the per-instance workload mapping read from the
        workload SHM (lock-free, cheap) inside the critical section.

        Returns the chosen instance id, or None when no candidate is eligible.
        A missing/blank session id has nothing to pin (e.g. a request that only
        carries a parent id whose parent pin does not exist), so it returns
        None and lets the caller fall back to the normal policy instead of
        raising on ``_slot_session(None)`` (PR #1196 review).
        """
        if not isinstance(session_id, str) or not session_id:
            return None
        ids = sorted({int(iid) for iid in candidate_instance_ids})
        if not ids:
            return None
        tag = _slot_session(session_id)
        digest = _key_digest(session_id, role)
        key_lo = int.from_bytes(digest[:8], "little")
        key_hi = int.from_bytes(digest[8:], "little")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            counts, incomplete = self._read_counters_locked(role)
            if incomplete:
                counts = self._scan_counts_locked(role)

            active_tokens = load_fn()
            loads = {int(iid): float(active_tokens.get(int(iid), 0.0)) for iid in ids}
            chosen = select_balanced_instance(ids, counts, loads, placement_seq)
            if chosen is None:
                return None

            match, free = self._find_slot(key_lo, key_hi)
            slot = match if match is not None else free
            if slot is None:
                logger.warning("session affinity shm full (%s slots); balanced pin dropped", self._slot_count)
                return None
            # Re-pointing an existing pin moves its session count to the chosen
            # instance; evicting another key subtracts that occupant's own count.
            # Both also decide whether the cache_control retention promise is
            # carried over (same-key rebind only) — see the helper docstring.
            slot_retention = self._prepare_slot_overwrite_locked(slot, key_lo, key_hi, role, int(chosen))
            self._write_slot(
                slot,
                key_lo,
                key_hi,
                _pack_text(tag, _SESSION_BYTES),
                _pack_text(role, _ROLE_BYTES),
                int(chosen),
                _EPOCH_NONE if epoch is None else int(epoch),
                time.monotonic(),
                time.time(),
                slot_retention,
                -1 if endpoint_id is None else int(endpoint_id),
            )
            return chosen
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def peek_last_used(self, session_id: str, role: str) -> float | None:
        """Return the slot's ``last_used`` wall stamp without refreshing it.

        Lets the cache-declaration keeper compare recency against its own
        declaration age (multi-worker renewal) without extending the pin's
        LRU lifetime as a side effect.
        """
        if not isinstance(session_id, str) or not session_id:
            return None
        tag = _slot_session(session_id)
        digest = _key_digest(session_id, role)
        key_lo = int.from_bytes(digest[:8], "little")
        key_hi = int.from_bytes(digest[8:], "little")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            slot, _ = self._find_slot(key_lo, key_hi)
            if slot is None:
                return None
            _, _, session, role_raw, _, _, last_used, _, _, _ = self._read_slot(slot)
            if _unpack_text(session) != tag or _unpack_text(role_raw) != role:
                return None
            return float(last_used)
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def peek_renewal(self, session_id: str, role: str) -> float | None:
        """Return the slot's ``renewed_at`` stamp without touching anything.

        ``renewed_at`` only advances on :meth:`record` (never on get-touches),
        so the declaration keeper can detect a renewal made by another worker
        after this worker's declaration was recorded.
        """
        if not isinstance(session_id, str) or not session_id:
            return None
        tag = _slot_session(session_id)
        digest = _key_digest(session_id, role)
        key_lo = int.from_bytes(digest[:8], "little")
        key_hi = int.from_bytes(digest[8:], "little")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            slot, _ = self._find_slot(key_lo, key_hi)
            if slot is None:
                return None
            _, _, session, role_raw, _, _, _, renewed_at, _, _ = self._read_slot(slot)
            if _unpack_text(session) != tag or _unpack_text(role_raw) != role:
                return None
            return float(renewed_at)
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def declare_retention(self, session_id: str, role: str, expires_at: float) -> None:
        """Record (max-merge) the wall expiry of a retention promise on the pin.

        The declaration ledgers are worker-local while this table is shared:
        writing the promise here makes the keeper see the strongest live
        commitment for the pin, so a worker with a shorter, already-expired
        declaration defers to the longer one instead of deleting the binding.
        No-op when the pin is absent (fresh declarations re-arm on the next
        record).
        """
        if not isinstance(session_id, str) or not session_id:
            return
        tag = _slot_session(session_id)
        digest = _key_digest(session_id, role)
        key_lo = int.from_bytes(digest[:8], "little")
        key_hi = int.from_bytes(digest[8:], "little")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            slot, _ = self._find_slot(key_lo, key_hi)
            if slot is None:
                return
            lo, hi, session, role_raw, instance_id, epoch, last_used, renewed_at, retention, pinned_endpoint = (
                self._read_slot(slot)
            )
            if _unpack_text(session) != tag or _unpack_text(role_raw) != role:
                return
            self._write_slot(
                slot,
                lo,
                hi,
                session,
                role_raw,
                instance_id,
                epoch,
                last_used,
                renewed_at,
                max(float(retention or 0.0), float(expires_at)),
                pinned_endpoint,
            )
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def peek_retention_expires(self, session_id: str, role: str) -> float:
        """Return the slot's retention promise expiry (0.0 when absent)."""
        if not isinstance(session_id, str) or not session_id:
            return 0.0
        tag = _slot_session(session_id)
        digest = _key_digest(session_id, role)
        key_lo = int.from_bytes(digest[:8], "little")
        key_hi = int.from_bytes(digest[8:], "little")
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            slot, _ = self._find_slot(key_lo, key_hi)
            if slot is None:
                return 0.0
            _, _, session, role_raw, _, _, _, _, retention, _ = self._read_slot(slot)
            if _unpack_text(session) != tag or _unpack_text(role_raw) != role:
                return 0.0
            return float(retention or 0.0)
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)

    def count_pins_by_instance(self, role: str) -> dict[int, int]:
        """Count pinned sessions per instance for one role pool (machine-wide).

        O(1) on the common path: the per-(role, instance) counters are
        maintained incrementally on record/drop under the same file lock, so
        the new-pin placement path no longer scans the slot array. Roles
        outside the counter region, or roles with pins at/above
        :data:`_COUNTER_INSTANCES` (flagged by the overflow counter), fall back
        to a full scan so no instance is silently missing (PR #1196 review).
        """
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            counts, incomplete = self._read_counters_locked(role)
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        if incomplete:
            return self._count_pins_by_instance_scan(role)
        return counts

    def _count_pins_by_instance_scan(self, role: str) -> dict[int, int]:
        fcntl.flock(self._fd, fcntl.LOCK_EX)
        try:
            return self._scan_counts_locked(role)
        finally:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
