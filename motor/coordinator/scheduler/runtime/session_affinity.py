# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Session-to-instance affinity table for endpoint stickiness.

Motor's KV-cache affinity is content-prefix driven; when the prefix hash
misses, or a spreading policy (load_balance) scatters a session's requests
across instances, the engine-side prefix cache stops hitting and prefill is
recomputed. The session affinity table adds endpoint stickiness on top: once
a request of a session has been scheduled onto an instance, later requests
carrying the same session_id are pinned back onto that instance so the KV
cache there is reused.

Phase 2 semantics:

- Role scoping: entries are keyed by ``(role, session_id)``. P/D separation
  schedules the same session against two disjoint instance pools, so a
  prefill pin must never leak into the decode pool (and vice versa).
- Epoch awareness: each entry stores the request's ``session.epoch``. When a
  later request bumps the epoch (context rewrite/compaction) the stored pin
  is invalidated — the old prefix cache is stale — and the request falls back
  to normal policy selection.
- Parent propagation: when a session has no own pin, its parent's pin (same
  role) is reused so parent/child contexts share one instance's prefix cache.

The table is in-process state (like ``_endpoint_rr_counters``) and is only
created when ``MOTOR_SESSION_AFFINITY=1`` — off by default, so baseline
scheduling is untouched. A pinned instance that has left the pool or is
circuit-broken is dropped and the request falls back to the normal policy:
affinity must never fail a request.
"""

import os
from collections import OrderedDict
from collections.abc import Iterable
from typing import NamedTuple

from motor.common.logger import get_logger
from motor.coordinator.scheduler.runtime.session_affinity_shared import (
    _DEFAULT_MAX_ENTRIES,
    SessionPin,
    select_balanced_instance,
)

logger = get_logger(__name__)

AFFINITY_ENV_FLAG = "MOTOR_SESSION_AFFINITY"
AFFINITY_MAX_ENV = "MOTOR_SESSION_AFFINITY_MAX"
AFFINITY_SHARED_ENV_FLAG = "MOTOR_SESSION_AFFINITY_SHARED"
AFFINITY_SHM_KEY_ENV = "MOTOR_SESSION_AFFINITY_SHM_KEY"
AFFINITY_PLACEMENT_ENV = "MOTOR_SESSION_AFFINITY_PLACEMENT"

# Placement modes for decisions that create a NEW pin (fresh/stale/epoch):
#   policy   — default; fall through to the normal scheduling policy (historical behavior)
#   balanced — pick the least-sticky-loaded instance so a burst of fresh sessions
#              spreads instead of clustering on one engine (pin skew is then locked
#              in by stickiness for the whole session lifetime)
PLACEMENT_POLICY = "policy"
PLACEMENT_BALANCED = "balanced"
_PLACEMENT_MODES = (PLACEMENT_POLICY, PLACEMENT_BALANCED)


class SessionAffinityTable:
    """Bounded LRU mapping of (role, session_id) to a pinned instance."""

    def __init__(self, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[tuple[str, str], SessionPin] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    @staticmethod
    def _key(session_id: str, role: str) -> tuple[str, str]:
        return (str(role), session_id)

    def get(self, session_id: str, role: str) -> SessionPin | None:
        """Return the pinned entry, refreshing recency on hit; None when absent."""
        if not isinstance(session_id, str) or not session_id:
            return None
        key = self._key(session_id, role)
        entry = self._entries.get(key)
        if entry is not None:
            self._entries.move_to_end(key)
        return entry

    def record(
        self,
        session_id: str,
        instance_id: int,
        role: str,
        epoch: int | None = None,
        endpoint_id: int | None = None,
    ) -> None:
        """Pin session_id to instance_id for role, evicting the least-recently-used entry when full."""
        if not isinstance(session_id, str) or not session_id:
            return
        normalized_epoch = int(epoch) if epoch is not None else None
        self._entries[self._key(session_id, role)] = SessionPin(
            int(instance_id),
            normalized_epoch,
            None if endpoint_id is None else int(endpoint_id),
        )
        self._entries.move_to_end(self._key(session_id, role))
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def drop(self, session_id: str, role: str) -> None:
        """Forget the pin for (role, session_id); no-op when absent."""
        self._entries.pop(self._key(session_id, role), None)

    def count_pins_by_instance(self, role: str) -> dict[int, int]:
        """Count pinned sessions per instance for one role pool.

        The balanced placement uses this as the stickiness "committed load":
        pins outlive their requests' in-flight tokens, so a session count is
        the only signal that sees how many future calls each engine is
        already promised to serve.
        """
        counts: dict[int, int] = {}
        for (entry_role, _session_id), pin in self._entries.items():
            if entry_role == role:
                counts[pin.instance_id] = counts.get(pin.instance_id, 0) + 1
        return counts

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
        """Select-and-commit one balanced fresh pin.

        Duck-compatible with :meth:`SharedSessionAffinityTable.record_balanced`:
        the shared variant performs count-read, selection and commit under one
        file lock (review 194528732); in-process there is no cross-process
        race, so this simply selects then records.

        A missing/blank session id has nothing to pin (e.g. a request carrying
        only a parent id whose parent pin is absent): return None so the caller
        falls back to the normal policy instead of committing the request to an
        instance that was never written to the table (PR #1196 review).
        """
        if not isinstance(session_id, str) or not session_id:
            return None
        counts = self.count_pins_by_instance(role)
        active_tokens = load_fn()
        loads = {int(iid): float(active_tokens.get(int(iid), 0.0)) for iid in candidate_instance_ids}
        chosen = select_balanced_instance(candidate_instance_ids, counts, loads, placement_seq)
        if chosen is None:
            return None
        self.record(session_id, chosen, role, epoch=epoch, endpoint_id=endpoint_id)
        return chosen

    def drop_all_roles(self, session_id: str) -> int:
        """Forget every role pin for session_id (out-of-band finalize, Phase 4).

        Returns the number of entries removed so the caller can log the
        release; no-op (0) when the session has no pin in any role pool.
        """
        if not isinstance(session_id, str) or not session_id:
            return 0
        dropped_keys = [key for key in self._entries if key[1] == session_id]
        for key in dropped_keys:
            del self._entries[key]
        return len(dropped_keys)


# Outcome of a pin resolution:
#   hit    — the session's own pin was applied
#   parent — the parent session's pin was propagated
#   fresh  — no usable pin existed; the normal policy decides (pin recorded after allocation)
#   stale  — the own pin pointed at an instance that left the pool / is circuit-broken
#   epoch  — the own pin was invalidated by a session.epoch bump
#   none   — affinity disabled or the request carries no session identity
_AFFINITY_OUTCOME_HIT = "hit"
_AFFINITY_OUTCOME_PARENT = "parent"
_AFFINITY_OUTCOME_FRESH = "fresh"
_AFFINITY_OUTCOME_STALE = "stale"
_AFFINITY_OUTCOME_EPOCH = "epoch"
_AFFINITY_OUTCOME_NONE = "none"

# Outcomes that create a new pin and therefore re-run the placement decision.
NEW_PIN_OUTCOMES = frozenset(
    {
        _AFFINITY_OUTCOME_FRESH,
        _AFFINITY_OUTCOME_STALE,
        _AFFINITY_OUTCOME_EPOCH,
    }
)


class AffinityDecision(NamedTuple):
    """Result of :func:`resolve_session_pin`."""

    instance_id: int | None
    outcome: str
    endpoint_id: int | None = None


def resolve_session_pin(
    table: SessionAffinityTable | None,
    session_id: str | None,
    parent_session_id: str | None,
    epoch: int | None,
    role: str,
    eligible_instance_ids: set[int] | frozenset[int] | None,
) -> AffinityDecision:
    """Resolve the instance pin for one request (pure, side effects only on ``table``).

    Resolution order: own-session pin (epoch-checked) → parent-session pin.
    A dropped own pin does not block parent propagation — a context rewrite on
    the child does not sever the shared-prefix link to its parent. Pins are
    only applied when the target is in ``eligible_instance_ids`` (in the role's
    pool and not circuit-broken).
    """
    if table is None or (not session_id and not parent_session_id):
        return AffinityDecision(None, _AFFINITY_OUTCOME_NONE)
    eligible = eligible_instance_ids or set()

    own_dropped: str | None = None
    if session_id:
        entry = table.get(session_id, role)
        if entry is not None:
            if entry.epoch is not None and epoch is not None and entry.epoch != epoch:
                table.drop(session_id, role)
                own_dropped = _AFFINITY_OUTCOME_EPOCH
            elif entry.instance_id in eligible:
                return AffinityDecision(entry.instance_id, _AFFINITY_OUTCOME_HIT, entry.endpoint_id)
            else:
                table.drop(session_id, role)
                own_dropped = _AFFINITY_OUTCOME_STALE

    if parent_session_id:
        entry = table.get(parent_session_id, role)
        if entry is not None and entry.instance_id in eligible:
            return AffinityDecision(entry.instance_id, _AFFINITY_OUTCOME_PARENT, entry.endpoint_id)

    if own_dropped is not None:
        return AffinityDecision(None, own_dropped)
    return AffinityDecision(None, _AFFINITY_OUTCOME_FRESH)


def session_affinity_placement_mode() -> str:
    """Return the placement mode for new-pin decisions (env-read, cheap per call).

    Unset or ``policy`` keeps the historical behavior (normal policy decides);
    ``balanced`` enables least-sticky-loaded placement. An unknown value falls
    back to ``policy`` with a warning so a typo can never break scheduling.
    """
    raw = os.getenv(AFFINITY_PLACEMENT_ENV, "").strip().lower()
    if raw in ("", PLACEMENT_POLICY):
        return PLACEMENT_POLICY
    if raw == PLACEMENT_BALANCED:
        return PLACEMENT_BALANCED
    logger.warning("Invalid %s=%r; using default %r", AFFINITY_PLACEMENT_ENV, raw, PLACEMENT_POLICY)
    return PLACEMENT_POLICY


def create_session_affinity_from_env() -> SessionAffinityTable | None:
    """Build the affinity table when MOTOR_SESSION_AFFINITY=1; otherwise None.

    MOTOR_SESSION_AFFINITY_MAX optionally bounds the table (default
    65536); an unparseable value falls back to the default.
    """
    if os.getenv(AFFINITY_ENV_FLAG, "").strip() != "1":
        return None
    raw_max = os.getenv(AFFINITY_MAX_ENV, "").strip()
    try:
        max_entries = int(raw_max) if raw_max else _DEFAULT_MAX_ENTRIES
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", AFFINITY_MAX_ENV, raw_max, _DEFAULT_MAX_ENTRIES)
        max_entries = _DEFAULT_MAX_ENTRIES
    if os.getenv(AFFINITY_SHARED_ENV_FLAG, "").strip() == "1":
        # Cross-process variant: the coordinator dispatches requests across
        # multiple InferenceWorker processes (spawn context), so an in-process
        # table cannot provide session stickiness — the state must live in
        # shared memory keyed per coordinator.
        shm_key = os.getenv(AFFINITY_SHM_KEY_ENV, "").strip() or "default"
        try:
            from motor.coordinator.scheduler.runtime.session_affinity_shared import (
                SharedSessionAffinityTable,
            )

            logger.info("session affinity table enabled (shared shm) max_entries=%s shm_key=%s", max_entries, shm_key)
            return SharedSessionAffinityTable(max_entries, shm_key=shm_key)
        except (OSError, ImportError) as exc:
            logger.warning("shared session affinity table unavailable (%s); falling back to in-process table", exc)
    logger.info("session affinity table enabled max_entries=%s", max_entries)
    return SessionAffinityTable(max_entries)
