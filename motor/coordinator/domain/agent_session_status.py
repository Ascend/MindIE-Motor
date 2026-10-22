# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Out-of-band agent session state machine (Agent Hint spec §2.2, Phase 4).

Status events arrive via ``POST /agent-status`` on the inference plane (the
DSH emit seam derives ``<baseURL origin>/agent-status``, spec §4/§5). The
machine keeps a bounded per-session record of the agent framework's lifecycle
state so ``finalize`` can release session-affinity pins (WP-A T-A4) and later
phases can hook engine KV actions (T-A2/A5, gated on engine capability B1).

Semantics (design 20261012-01 §2.3):

- Out-of-order tolerant: any state may follow any state — agent lifecycles
  are framework-specific, so there is no legal-transition table. ``running``
  arriving before ``tool`` is recorded as-is.
- ``finalize`` is terminal: a repeated ``finalize`` is idempotent (applied,
  state unchanged); any other state after finalize is not applied (WARN,
  applied=false) — terminal means terminal.
- Unknown sessions create a record (covers Motor restarts with DSH replaying
  pending events).
- TTL expiry uses the SERVER receipt clock; the client-reported
  ``state_since_ms`` is stored for observability only (clock skew).
- Storage is bounded: lazy TTL expiry on access plus LRU eviction at
  ``max_sessions``; no background sweeper thread.

Field-level validation (state whitelist, tools shape) belongs to the endpoint
(``AgentStatusHint``); this machine only guards types defensively and never
raises — callers decide the HTTP outcome.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from motor.common.logger import get_logger

logger = get_logger(__name__)

_FINAL_STATE = "finalize"


@dataclass
class AgentSessionTransition:
    """Outcome of applying one out-of-band status event."""

    applied: bool
    reason: str
    session_id: str
    state: str
    previous_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly view for the POST response."""
        return {
            "applied": self.applied,
            "reason": self.reason,
            "session_id": self.session_id,
            "state": self.state,
            "previous_state": self.previous_state,
        }


@dataclass
class AgentSessionRecord:
    """Bounded per-session lifecycle record (server receipt clock)."""

    state: str
    reason: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    # Client-reported entry timestamp (unix ms); observability only, never
    # used for expiry (clock skew between agent and server).
    state_since_ms: int | None = None
    # Server receipt time of the latest applied event (unix ms).
    updated_at_ms: int = 0
    transition_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-friendly view served by ``GET /agent-status``."""
        return {
            "state": self.state,
            "reason": self.reason,
            "tools": self.tools,
            "state_since_ms": self.state_since_ms,
            "updated_at_ms": self.updated_at_ms,
            "transition_count": self.transition_count,
        }


class AgentSessionStateMachine:
    """Bounded LRU map of session_id to lifecycle record with lazy TTL expiry.

    Lock-protected: the inference event loop is single-threaded, but tests
    exercise the machine directly and the endpoint may run under a threadpool.
    """

    def __init__(self, session_ttl_s: int = 3600, max_sessions: int = 65536) -> None:
        self._ttl_ms = max(1, int(session_ttl_s)) * 1000
        self._max_sessions = max(1, int(max_sessions))
        self._records: OrderedDict[str, AgentSessionRecord] = OrderedDict()
        # Session ids ordered by updated_at_ms (oldest first), advanced by
        # apply(). Expiry walks this from the oldest end and stops at the first
        # live record, so a POST/GET costs O(expired) instead of O(all records)
        # (review 194909359). Kept separate from _records, whose order is LRU
        # recency (get() touches do not affect TTL).
        self._expiry_order: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()

    def _drop_locked(self, session_id: str) -> None:
        """Remove a session from both the record map and the expiry index."""
        self._records.pop(session_id, None)
        self._expiry_order.pop(session_id, None)

    def update_limits(self, session_ttl_s: int, max_sessions: int) -> None:
        """Hot-reload TTL / capacity (coordinator config watcher).

        ``enabled`` is start-time (route registration is not hot-reloaded);
        the limits apply to subsequent accesses. Shrinking capacity evicts
        the least-recently-used records on the spot.
        """
        self._ttl_ms = max(1, int(session_ttl_s)) * 1000
        self._max_sessions = max(1, int(max_sessions))
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._expire_locked(now_ms)
            while len(self._records) > self._max_sessions:
                evicted, _ = self._records.popitem(last=False)
                self._expiry_order.pop(evicted, None)

    def __len__(self) -> int:
        return len(self._records)

    def apply(
        self,
        session_id: str,
        state: str,
        *,
        reason: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        state_since_ms: int | None = None,
    ) -> AgentSessionTransition:
        """Apply one out-of-band event; never raises.

        Returns the transition with ``applied``/``reason``: ``applied`` on a
        new/updated record or an idempotent duplicate ``finalize``;
        ``terminal`` (not applied) for any state after finalize; ``invalid``
        for a non-string/empty session or state.
        """
        if not isinstance(session_id, str) or not session_id:
            return AgentSessionTransition(False, "invalid_session", "", str(state))
        if not isinstance(state, str) or not state:
            return AgentSessionTransition(False, "invalid_state", session_id, "")
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._expire_locked(now_ms)
            record = self._records.get(session_id)
            if record is not None and record.state == _FINAL_STATE:
                if state == _FINAL_STATE:
                    # Repeated finalize: idempotent, record untouched.
                    return AgentSessionTransition(True, "idempotent", session_id, _FINAL_STATE)
                logger.warning(
                    "agent_status event after finalize ignored: session_id=%s state=%s",
                    session_id,
                    state,
                )
                return AgentSessionTransition(False, "terminal", session_id, state, _FINAL_STATE)
            previous_state = record.state if record is not None else None
            if record is None:
                record = AgentSessionRecord(
                    state=state,
                    reason=reason,
                    tools=list(tools or []),
                    state_since_ms=state_since_ms,
                    updated_at_ms=now_ms,
                    transition_count=1,
                )
                self._records[session_id] = record
            else:
                record.state = state
                record.reason = reason
                record.tools = list(tools or [])
                record.state_since_ms = state_since_ms
                record.updated_at_ms = now_ms
                record.transition_count += 1
            self._records.move_to_end(session_id)
            self._expiry_order[session_id] = None
            self._expiry_order.move_to_end(session_id)
            while len(self._records) > self._max_sessions:
                evicted, _ = self._records.popitem(last=False)
                self._expiry_order.pop(evicted, None)
            return AgentSessionTransition(True, "applied", session_id, state, previous_state)

    def get(self, session_id: str) -> AgentSessionRecord | None:
        """Return the current record, refreshing recency; None when absent/expired."""
        if not isinstance(session_id, str) or not session_id:
            return None
        now_ms = int(time.time() * 1000)
        with self._lock:
            self._expire_locked(now_ms)
            record = self._records.get(session_id)
            if record is not None:
                self._records.move_to_end(session_id)
            return record

    def _expire_locked(self, now_ms: int) -> None:
        """Drop records older than the TTL (caller holds the lock).

        Incremental: ``_expiry_order`` tracks session ids in updated_at_ms
        order, so this only inspects entries from the oldest end and stops at
        the first live record instead of scanning the whole table on every
        access (review 194909359).
        """
        deadline = now_ms - self._ttl_ms
        while self._expiry_order:
            session_id = next(iter(self._expiry_order))
            record = self._records.get(session_id)
            if record is None:
                # Stale index entry (e.g. LRU-evicted) — drop and continue.
                self._expiry_order.pop(session_id, None)
                continue
            if record.updated_at_ms >= deadline:
                break
            self._drop_locked(session_id)
