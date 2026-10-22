# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Bounded per-session ``cache_control`` declarations with TTL expiry (design 20261015-03).

The spec ``cache_control`` block (§2.5) is parsed into ``AgentHintInfo`` but had no
consumer: the parsed declaration died with the request, so a client saying "keep this
prefix for 12 seconds" was indistinguishable from one saying "keep it forever" — the
session-affinity binding is only bounded by LRU capacity, ``finalize`` and instance loss.

This table gives the declaration a memory so the keeper can, at expiry, release the
session's affinity pin (``drop_all_roles``) — the client said it no longer needs it.

Scope discipline (design 20261015-03 §1.4.2 / §7 D1)
----------------------------------------------------
Only declarations carrying an **explicit** ``ttl_s`` enter this table. A declaration
without ``ttl_s`` keeps today's behaviour (binding lives until capacity/LRU evicts it),
so baseline traffic is unaffected — none of the three harnesses sends ``ttl_s`` today.

Expiry is never lost: lazy expiry (triggered by ``get``/``declare``) moves a declaration
into a pending-release queue instead of dropping it, so the keeper still sees it on its
next ``sweep`` even if the request path raced ahead of the keeper.

Storage and failure model mirror ``AgentSessionStateMachine``: a lock-protected
``OrderedDict``, LRU eviction at ``max_declarations``, hot-reloadable limits, and
**it never raises** — callers decide what an outcome means. Timing uses
``time.monotonic`` so an NTP step cannot extend or shorten a retention.
"""

import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from motor.common.logger import get_logger

logger = get_logger(__name__)

# Environment knobs, following the session-affinity component's pattern
# (``session_affinity.AFFINITY_ENV_FLAG`` / ``AFFINITY_MAX_ENV``): the feature is
# off unless explicitly enabled, so baseline deployments are untouched.
LIFECYCLE_ENV_FLAG = "MOTOR_CACHE_CONTROL_LIFECYCLE"
LIFECYCLE_MAX_ENV = "MOTOR_CACHE_CONTROL_MAX"
LIFECYCLE_TTL_MAX_ENV = "MOTOR_CACHE_CONTROL_TTL_MAX_S"
LIFECYCLE_SWEEP_ENV = "MOTOR_CACHE_CONTROL_SWEEP_INTERVAL_S"

_DEFAULT_SWEEP_INTERVAL_S = 5
_MIN_SWEEP_INTERVAL_S = 1

DECL_TYPE_PIN = "pin"
DECL_TYPE_EPHEMERAL = "ephemeral"
DECL_TYPES = frozenset({DECL_TYPE_PIN, DECL_TYPE_EPHEMERAL})

# Kept in sync with agent_hint._CACHE_TTL_MIN/_CACHE_TTL_MAX. Re-clamped here so a
# caller bypassing the parser cannot shorten retention below the spec floor or extend
# it past the configured ceiling.
_TTL_MIN_S = 60
_TTL_MAX_S = 3600

# Default bound, matched to the affinity table's default capacity.
_DEFAULT_MAX_DECLARATIONS = 65536


def _monotonic_ms() -> int:
    """Monotonic milliseconds; immune to wall-clock steps."""
    return int(time.monotonic() * 1000)


@dataclass
class CacheDeclaration:
    """One session's declared cache retention (server-side view).

    ``roles`` records which role pools this session declared in. The keeper needs them
    to *protect* the binding (step ② of the design): the affinity tables refresh recency
    on ``get(session_id, role)``, and the role is only known here — the tables offer no
    "enumerate this session's roles" API, and this design deliberately does not add one
    (that file carries another workstream's uncommitted changes).
    """

    session_id: str
    decl_type: str
    ttl_s: int
    msg_offset: int | None
    created_ms: int
    expire_ms: int
    roles: tuple[str, ...] = ()
    # Wall-clock stamp of the last declare/renew, comparable against affinity
    # slot ``renewed_at`` (time.time() domain) for cross-worker renewal checks.
    wall_created_s: float = 0.0
    # Diagnostic: pins released when the keeper acted on this declaration.
    dropped: int = 0

    def remaining_s(self, now_ms: int) -> int:
        """Seconds until expiry; 0 once expired."""
        return max(0, (self.expire_ms - now_ms) // 1000)

    def age_s(self, now_ms: int) -> int:
        """Seconds since the declaration was (last) recorded."""
        return max(0, (now_ms - self.created_ms) // 1000)

    def is_expired(self, now_ms: int) -> bool:
        """True when the declared retention has elapsed."""
        return now_ms >= self.expire_ms

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly diagnostic view (no raw monotonic stamps)."""
        return {
            "session_id": self.session_id,
            "type": self.decl_type,
            "ttl_s": self.ttl_s,
            "msg_offset": self.msg_offset,
            "roles": list(self.roles),
        }


class CacheDeclarationTable:
    """Bounded LRU map of ``session_id`` to :class:`CacheDeclaration`.

    Lock-protected: the keeper runs on the event loop while the request path records
    (and tests exercise the table directly).
    """

    def __init__(
        self,
        max_declarations: int = _DEFAULT_MAX_DECLARATIONS,
        ttl_max_s: int = _TTL_MAX_S,
    ) -> None:
        self._max_declarations = max(1, int(max_declarations))
        self._ttl_max_s = self._normalize_ttl_max(ttl_max_s)
        self._declarations: OrderedDict[str, CacheDeclaration] = OrderedDict()
        # Expired-but-not-yet-reported declarations, drained by ``sweep``.
        self._pending_release: OrderedDict[str, CacheDeclaration] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_ttl_max(ttl_max_s: int) -> int:
        """Clamp the configured ceiling into ``[_TTL_MIN_S, ...]``."""
        try:
            return max(_TTL_MIN_S, int(ttl_max_s))
        except (TypeError, ValueError):
            return _TTL_MAX_S

    def update_limits(self, max_declarations: int, ttl_max_s: int, now_ms: int | None = None) -> None:
        """Hot-reload capacity and TTL ceiling (coordinator config watcher).

        Shrinking capacity evicts the least-recently-declared entries on the spot.
        Eviction only drops bookkeeping — it deliberately does **not** queue an affinity
        release, so a capacity overflow degrades to today's behaviour instead of taking
        a destructive action the client did not ask for.

        ``now_ms`` is injectable so the expiry check uses the same clock as the
        declarations being pruned (production always passes the default).
        """
        self._max_declarations = max(1, int(max_declarations))
        self._ttl_max_s = self._normalize_ttl_max(ttl_max_s)
        now_ms = _monotonic_ms() if now_ms is None else now_ms
        with self._lock:
            self._expire_locked(now_ms)
            while len(self._declarations) > self._max_declarations:
                evicted_id, _ = self._declarations.popitem(last=False)
                logger.debug("cache_control declaration evicted (capacity shrink): session_id=%s", evicted_id)

    def __len__(self) -> int:
        return len(self._declarations)

    @property
    def max_declarations(self) -> int:
        """Configured capacity bound (diagnostics / tests)."""
        return self._max_declarations

    @property
    def ttl_max_s(self) -> int:
        """Normalized TTL ceiling in seconds (diagnostics / tests)."""
        return self._ttl_max_s

    @property
    def pending_release_count(self) -> int:
        """Number of expired declarations awaiting the keeper's next sweep."""
        with self._lock:
            return len(self._pending_release)

    def declare(
        self,
        session_id: str,
        decl_type: str,
        ttl_s: int,
        msg_offset: int | None = None,
        role: str | None = None,
        now_ms: int | None = None,
    ) -> CacheDeclaration | None:
        """Record (or renew) one session's retention; returns None when rejected.

        Rejection is silent to the caller by design — a malformed declaration must never
        fail a request (spec: malformed input degrades, it does not error).

        ``role`` accumulates across renewals: one agent session may be scheduled in
        several role pools (prefill/decode/union), and protection must cover each.
        """
        if not isinstance(session_id, str) or not session_id:
            return None
        if decl_type not in DECL_TYPES:
            return None
        ttl = self._clamp_ttl(ttl_s)
        if ttl is None:
            return None
        if msg_offset is not None and (not isinstance(msg_offset, int) or isinstance(msg_offset, bool)):
            logger.warning("Invalid cache_control msg_offset=%r; storing None.", msg_offset)
            msg_offset = None
        if role is not None and (not isinstance(role, str) or not role):
            role = None
        now_ms = _monotonic_ms() if now_ms is None else now_ms
        with self._lock:
            self._expire_locked(now_ms)
            previous = self._declarations.get(session_id)
            roles = previous.roles if previous is not None else ()
            if role is not None and role not in roles:
                roles = (*roles, role)
            declaration = CacheDeclaration(
                session_id=session_id,
                decl_type=decl_type,
                ttl_s=ttl,
                msg_offset=msg_offset,
                created_ms=now_ms,
                expire_ms=now_ms + ttl * 1000,
                roles=roles,
                wall_created_s=time.time(),
            )
            # Renewal overwrites in place and refreshes recency; it also cancels a
            # pending release, since the client just asked for the prefix again.
            self._pending_release.pop(session_id, None)
            self._declarations[session_id] = declaration
            self._declarations.move_to_end(session_id)
            while len(self._declarations) > self._max_declarations:
                evicted_id, _ = self._declarations.popitem(last=False)
                logger.debug("cache_control declaration evicted (capacity): session_id=%s", evicted_id)
        return declaration

    def get(self, session_id: str, now_ms: int | None = None) -> CacheDeclaration | None:
        """Return the active declaration without touching recency.

        Recency means "most recently declared", not "most recently read": the keeper
        reads every declaration each period, so refreshing on read would destroy LRU
        ordering and let a stale declaration outlive newer ones.
        """
        if not isinstance(session_id, str) or not session_id:
            return None
        now_ms = _monotonic_ms() if now_ms is None else now_ms
        with self._lock:
            self._expire_locked(now_ms)
            return self._declarations.get(session_id)

    def drop(self, session_id: str) -> bool:
        """Forget everything recorded for one session (``finalize`` path).

        Returns True when an active declaration or a pending release was removed.
        Cancelling a pending release matters: ``finalize`` already releases the pin, so
        the keeper must not release it a second time.
        """
        if not isinstance(session_id, str) or not session_id:
            return False
        with self._lock:
            released = self._pending_release.pop(session_id, None) is not None
            active = self._declarations.pop(session_id, None) is not None
        return active or released

    def sweep(self, now_ms: int | None = None) -> list[CacheDeclaration]:
        """Return every declaration whose retention ended, oldest expiry first.

        Drains the pending-release queue after moving newly expired declarations into
        it. The caller owns the follow-up action (releasing the session's affinity pin);
        this layer stays free of scheduler dependencies.
        """
        now_ms = _monotonic_ms() if now_ms is None else now_ms
        with self._lock:
            self._expire_locked(now_ms)
            released = list(self._pending_release.values())
            self._pending_release.clear()
        released.sort(key=lambda decl: (decl.expire_ms, decl.session_id))
        return released

    def requeue(self, declaration: CacheDeclaration) -> None:
        """Put a swept-but-not-released declaration back for the next keeper tick.

        Used when the release is deferred (bounded batch, or the pin was renewed
        by another worker): the declaration must stay visible to ``sweep`` so the
        follow-up action is not lost.
        """
        with self._lock:
            self._pending_release[declaration.session_id] = declaration

    def snapshot(self) -> list[CacheDeclaration]:
        """Return a copy of the active declarations, most recent first."""
        with self._lock:
            return list(reversed(self._declarations.values()))

    def active(self, now_ms: int | None = None) -> list[CacheDeclaration]:
        """Return the declarations still inside their window, most recent first.

        Used by the keeper's protection pass (step ②): reading each one's affinity entry
        refreshes its recency, which is how a declared session resists LRU eviction
        without changing the eviction algorithm.
        """
        now_ms = _monotonic_ms() if now_ms is None else now_ms
        with self._lock:
            self._expire_locked(now_ms)
            return list(reversed(self._declarations.values()))

    def _clamp_ttl(self, ttl_s: int) -> int | None:
        """Clamp a requested TTL into the allowed window; None when unusable."""
        if ttl_s is None or isinstance(ttl_s, bool):
            return None
        try:
            value = int(ttl_s)
        except (TypeError, ValueError):
            return None
        return max(_TTL_MIN_S, min(self._ttl_max_s, value))

    def _expire_locked(self, now_ms: int) -> None:
        """Move expired declarations into the pending-release queue (holds the lock)."""
        expired = [decl for decl in self._declarations.values() if decl.is_expired(now_ms)]
        for decl in expired:
            del self._declarations[decl.session_id]
            self._pending_release[decl.session_id] = decl
        # Bound the queue so a keeper that never runs cannot grow it without limit.
        while len(self._pending_release) > self._max_declarations:
            dropped_id, _ = self._pending_release.popitem(last=False)
            logger.warning("cache_control pending release overflow; dropping session_id=%s", dropped_id)


def _env_flag_enabled(name: str) -> bool:
    """Return whether a boolean env flag is set to a truthy value."""
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_positive_int(name: str, default: int) -> int:
    """Read a positive-int env var, falling back to ``default`` with a warning."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Invalid %s=%r (must be positive); using default %s", name, raw, default)
        return default
    return value


def create_cache_declaration_table_from_env() -> CacheDeclarationTable | None:
    """Build the declaration table from the environment; None when the feature is off.

    Returning None (rather than an empty table) keeps the request path and the keeper
    completely inert unless the operator opted in — the same contract as
    ``create_session_affinity_from_env``.
    """
    if not _env_flag_enabled(LIFECYCLE_ENV_FLAG):
        return None
    max_declarations = _env_positive_int(LIFECYCLE_MAX_ENV, _DEFAULT_MAX_DECLARATIONS)
    ttl_max_s = _env_positive_int(LIFECYCLE_TTL_MAX_ENV, _TTL_MAX_S)
    table = CacheDeclarationTable(max_declarations=max_declarations, ttl_max_s=ttl_max_s)
    logger.info(
        "%s enabled max_declarations=%s ttl_max_s=%s sweep_interval_s=%s",
        LIFECYCLE_ENV_FLAG,
        max_declarations,
        table.ttl_max_s,
        cache_lifecycle_sweep_interval_s(),
    )
    return table


def cache_lifecycle_sweep_interval_s() -> int:
    """Keeper period in seconds (``MOTOR_CACHE_CONTROL_SWEEP_INTERVAL_S``)."""
    return max(_MIN_SWEEP_INTERVAL_S, _env_positive_int(LIFECYCLE_SWEEP_ENV, _DEFAULT_SWEEP_INTERVAL_S))
