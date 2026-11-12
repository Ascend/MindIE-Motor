# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Agent-hint KV-cache (KVC) lifecycle controller.

The controller is driven by out-of-band ``/agent-status`` events and never sits
on the inference request path. The lifecycle has four canonical states:

- ``pause`` starts Motor-owned idle timers (``tool``/``suspend`` are deprecated
  aliases and arrive here already normalized to ``pause``);
- ``idle_offload_s`` -> engine ``offload``, ``idle_evict_s`` -> engine ``evict``
  (0 disables each stage);
- ``resume`` cancels idle timers and, when the session was offloaded, sends
  ``prefetch`` before the next turn; plain ``running`` never prefetches;
- ``finalize`` sends ``evict``. A later ``running`` with a newer ``epoch``
  reuses the same session_id as a fresh context generation and clears the
  finalize/offload/evict ledgers; ``resume`` never reopens finalize.

All engine actions are best-effort, serialized through one bounded queue, and
gated on capacity pressure (``offload_kv_usage_high_water``). Failures only log
and count; they never retry in a storm and never fail the status report.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from motor.common.logger import get_logger
from motor.config.coordinator import KvcLifecycleConfig
from motor.coordinator.domain.agent_session_status import AgentSessionStateMachine

logger = get_logger(__name__)

_EDIT_TYPES = frozenset({"offload", "prefetch", "evict"})
_EDIT_TARGETS = frozenset({"session", "messages", "tools"})
_PAUSE_STATE = "pause"
_RESUME_STATE = "resume"
_RUNNING_STATE = "running"
_FINALIZE_STATE = "finalize"
_IDLE_STATES = frozenset({_PAUSE_STATE})
_LEGACY_STATE_ALIASES = {"tool": _PAUSE_STATE, "suspend": _PAUSE_STATE}

_ACTION_OFFLOAD = "offload"
_ACTION_EVICT_IDLE = "evict_idle"
_ACTION_EVICT_FINALIZE = "evict_finalize"
_ACTION_PREFETCH = "prefetch"


class KvcSessionState(str, Enum):
    """Lifecycle state of one session's KV cache."""

    UNKNOWN = "unknown"
    RESIDENT = "resident"
    IDLE = "idle"
    OFFLOADED = "offloaded"
    EVICTED = "evicted"
    FINALIZED = "finalized"


# Async callback: (session_id, edit_type) -> list of engine edit_results dicts.
ManageSender = Callable[[str, str], Awaitable[list[dict[str, Any]]]]
PressureProvider = Callable[[], bool]


def build_manage_request_body(
    session_id: str,
    edit_type: str,
    *,
    model: str,
    target: str = "session",
    block_start: int | None = None,
    block_end: int | None = None,
) -> dict[str, Any]:
    """Build one engine ``context_management`` management request.

    ``target="session"`` needs no block coordinates (the engine expands the
    whole session); ``messages``/``tools`` must carry them, otherwise the
    engine rejects the edit.
    """
    if edit_type not in _EDIT_TYPES:
        raise ValueError(f"unsupported edit_type: {edit_type!r}")
    if target not in _EDIT_TARGETS:
        raise ValueError(f"unsupported target: {target!r}")
    edit: dict[str, Any] = {"type": edit_type, "target": target}
    if target != "session":
        if block_start is None or block_end is None:
            raise ValueError(f"target={target!r} requires block_start/block_end")
        edit["block_start"] = int(block_start)
        edit["block_end"] = int(block_end)
    return {
        "model": model,
        "messages": [{"role": "user", "content": ""}],
        "max_tokens": 1,
        "agent_hint": {
            "session_id": session_id,
            "context_management": {"manage_request": True, "edits": [edit]},
        },
    }


def summarize_edit_results(results: list[dict[str, Any]] | None) -> tuple[int, int]:
    """Return ``(succeeded, failed)`` from ``agent_hint_response.edit_results``."""
    succeeded = 0
    failed = 0
    for result in results or []:
        if not isinstance(result, dict) or "op_status" not in result:
            continue
        if result.get("op_status"):
            succeeded += 1
        else:
            failed += 1
    return succeeded, failed


@dataclass
class _Session:
    state: str = "unknown"
    epoch: int = 0
    finalized: bool = False
    finalized_epoch: int | None = None
    offloaded: bool = False
    evicted: bool = False
    idle_generation: int = 0
    timers: list[asyncio.Task] = field(default_factory=list)


class AgentHintLifecycleManager:
    """Bounded per-session KVC state machine + serialized engine dispatch."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        sender: ManageSender | None = None,
        max_sessions: int = 65536,
        idle_offload_s: float = 0.0,
        idle_evict_s: float = 0.0,
        queue_size: int = 1024,
        prefetch_enabled: bool = True,
        pressure_provider: PressureProvider | None = None,
    ) -> None:
        self._enabled = bool(enabled)
        self._sender = sender
        self._max_sessions = max(1, int(max_sessions))
        self._idle_offload_s = max(0.0, float(idle_offload_s))
        self._idle_evict_s = max(0.0, float(idle_evict_s))
        self._queue_size = max(1, int(queue_size))
        self._prefetch_enabled = bool(prefetch_enabled)
        self._pressure_provider = pressure_provider
        self._stats_listener: Callable[[dict[str, int]], None] | None = None
        self._sessions: OrderedDict[str, _Session] = OrderedDict()
        self._lock = threading.RLock()
        self._counters: dict[str, int] = {
            "evict_total": 0,
            "offload_total": 0,
            "prefetch_total": 0,
            "edit_success": 0,
            "edit_fail": 0,
            "send_fail": 0,
            "gate_skip": 0,
            "queue_full": 0,
            "epoch_revive": 0,
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        """Whether engine management actions are dispatched."""
        return self._enabled

    def set_sender(self, sender: ManageSender | None) -> None:
        """Inject the sender lazily (the scheduler client connects after start)."""
        self._sender = sender

    def set_pressure_provider(self, provider: PressureProvider | None) -> None:
        """Inject the capacity-pressure probe (None means always act)."""
        self._pressure_provider = provider

    def set_stats_listener(self, listener: Callable[[dict[str, int]], None] | None) -> None:
        """Publish counter snapshots (e.g. to the cross-process metrics shm)."""
        self._stats_listener = listener
        if listener is not None:
            listener(self.stats())

    def _publish_stats(self) -> None:
        listener = self._stats_listener
        if listener is None:
            return
        try:
            listener(self.stats())
        except Exception as error:  # noqa: BLE001 — telemetry must not break KVC
            logger.debug("kvc stats listener failed: %s", error)

    def stats(self) -> dict[str, int]:
        """Snapshot of the KVC counters (for logs / metrics export)."""
        with self._lock:
            return dict(self._counters)

    def record_state(self, session_id: str, state: str) -> KvcSessionState:
        """Record one agent lifecycle state without dispatching engine work."""
        if not isinstance(session_id, str) or not session_id:
            return KvcSessionState.UNKNOWN
        state = _LEGACY_STATE_ALIASES.get(state, state)
        mapped = {
            _PAUSE_STATE: KvcSessionState.IDLE,
            _RESUME_STATE: KvcSessionState.RESIDENT,
            _RUNNING_STATE: KvcSessionState.RESIDENT,
            _FINALIZE_STATE: KvcSessionState.FINALIZED,
        }.get(state, KvcSessionState.UNKNOWN)
        with self._lock:
            self._session_locked(session_id).state = state
            return mapped

    async def on_status(
        self,
        session_id: str,
        state: str,
        *,
        epoch: int = 0,
        applied: bool = True,
        previous_state: str | None = None,
    ) -> None:
        """Apply one out-of-band status event.

        Idle actions are timer-driven and not awaited. Terminal ``finalize`` and
        resume ``prefetch`` are awaited through the bounded queue so tests and
        callers can observe the dispatch result without reading internal state.
        """
        if not self._enabled or not applied or not isinstance(session_id, str) or not session_id:
            return
        state = _LEGACY_STATE_ALIASES.get(state, state)
        if state not in {_PAUSE_STATE, _RESUME_STATE, _RUNNING_STATE, _FINALIZE_STATE}:
            logger.warning("kvc unsupported state ignored: session_id=%s state=%s", session_id, state)
            return
        epoch = max(0, int(epoch))
        action: str | None = None
        revived = False
        offloaded_without_resume = False
        with self._lock:
            session = self._session_locked(session_id)
            if epoch < session.epoch:
                logger.debug(
                    "kvc stale epoch ignored: session_id=%s state=%s epoch=%d current=%d",
                    session_id,
                    state,
                    epoch,
                    session.epoch,
                )
                return
            previous = session.state if previous_state is None else _LEGACY_STATE_ALIASES.get(
                previous_state, previous_state
            )
            if session.finalized:
                if state == _FINALIZE_STATE:
                    logger.debug("kvc repeated finalize ignored: session_id=%s", session_id)
                    return
                if state == _RUNNING_STATE and epoch > (session.finalized_epoch or 0):
                    revived = True
                    self._counters["epoch_revive"] += 1
                else:
                    logger.warning(
                        "kvc event after finalize ignored: session_id=%s state=%s epoch=%d finalized_epoch=%s",
                        session_id,
                        state,
                        epoch,
                        session.finalized_epoch,
                    )
                    return
            epoch_changed = epoch > session.epoch
            session.epoch = max(session.epoch, epoch)
            session.state = state
            if state == _PAUSE_STATE:
                # Repeated pause events are idempotent: only a real transition
                # into pause (or a new epoch) arms the idle timers.
                if previous != _PAUSE_STATE or epoch_changed:
                    self._cancel_timers_locked(session)
                    session.idle_generation += 1
                    self._schedule_idle_locked(session_id, session, session.epoch)
            else:
                self._cancel_timers_locked(session)
                session.idle_generation += 1
                if state == _RESUME_STATE:
                    session.finalized = False
                    session.finalized_epoch = None
                    session.evicted = False
                    if session.offloaded and self._prefetch_enabled:
                        action = _ACTION_PREFETCH
                elif state == _RUNNING_STATE:
                    session.finalized = False
                    session.finalized_epoch = None
                    session.evicted = False
                    # Explicit resume is the only prefetch trigger. Keep the
                    # offloaded ledger so a later resume can still pull it back.
                    offloaded_without_resume = (
                        previous == _PAUSE_STATE and session.offloaded and self._prefetch_enabled
                    )
                elif state == _FINALIZE_STATE:
                    session.finalized = True
                    session.finalized_epoch = session.epoch
                    session.offloaded = False
                    action = _ACTION_EVICT_FINALIZE
        if revived:
            logger.info("kvc session epoch reused: session_id=%s epoch=%d", session_id, epoch)
        if offloaded_without_resume:
            logger.warning(
                "kvc running while offloaded without explicit resume; waiting for resume: session_id=%s epoch=%d",
                session_id,
                epoch,
            )
        if action is not None:
            await self._enqueue(action, session_id, epoch, wait=True)
        self._publish_stats()

    def _session_locked(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            session = _Session()
            self._sessions[session_id] = session
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > self._max_sessions:
            _, evicted = self._sessions.popitem(last=False)
            self._cancel_timers_locked(evicted)
        return session

    @staticmethod
    def _cancel_timers_locked(session: _Session) -> None:
        for timer in session.timers:
            timer.cancel()
        session.timers.clear()

    def _schedule_idle_locked(self, session_id: str, session: _Session, epoch: int) -> None:
        loop = asyncio.get_running_loop()
        generation = session.idle_generation
        if self._idle_offload_s > 0:
            session.timers.append(
                loop.create_task(
                    self._idle_after(session_id, self._idle_offload_s, _ACTION_OFFLOAD, epoch, generation)
                )
            )
        if self._idle_evict_s > 0:
            session.timers.append(
                loop.create_task(
                    self._idle_after(session_id, self._idle_evict_s, _ACTION_EVICT_IDLE, epoch, generation)
                )
            )

    async def _idle_after(
        self,
        session_id: str,
        delay_s: float,
        action: str,
        epoch: int,
        generation: int,
    ) -> None:
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        with self._lock:
            session = self._sessions.get(session_id)
            if (
                session is None
                or session.idle_generation != generation
                or session.epoch != epoch
                or session.state not in _IDLE_STATES
            ):
                return
        await self._enqueue(action, session_id, epoch, wait=False)

    def _ensure_worker(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._queue is None or self._worker is None or self._worker.done():
            self._loop = loop
            self._queue = asyncio.Queue(maxsize=self._queue_size)
            self._worker = loop.create_task(self._drain())

    async def _enqueue(self, action: str, session_id: str, epoch: int, *, wait: bool) -> Any:
        self._ensure_worker()
        assert self._loop is not None
        assert self._queue is not None
        future = self._loop.create_future() if wait else None
        try:
            self._queue.put_nowait((action, session_id, epoch, future))
        except asyncio.QueueFull:
            with self._lock:
                self._counters["queue_full"] += 1
            logger.warning(
                "kvc action queue full; dropping %s: session_id=%s", action, session_id
            )
            self._publish_stats()
            return None
        if future is None:
            return None
        return await future

    async def _drain(self) -> None:
        assert self._queue is not None
        while True:
            item = await self._queue.get()
            if item is None:
                return
            action, session_id, epoch, future = item
            try:
                result = await self._execute(action, session_id, epoch)
            except Exception as error:  # noqa: BLE001 — the drain loop must survive
                logger.warning(
                    "kvc action crashed: action=%s session_id=%s error=%s",
                    action,
                    session_id,
                    error,
                )
                result = None
            if future is not None and not future.done():
                future.set_result(result)

    async def _execute(self, action: str, session_id: str, epoch: int) -> Any:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.epoch != epoch:
                return None
            if action == _ACTION_PREFETCH:
                if not session.offloaded or session.state != _RESUME_STATE:
                    return None
            elif action == _ACTION_OFFLOAD:
                if session.state not in _IDLE_STATES or session.offloaded or session.evicted or session.finalized:
                    return None
            elif action == _ACTION_EVICT_IDLE:
                if session.state not in _IDLE_STATES or session.evicted or session.finalized:
                    return None
            elif action == _ACTION_EVICT_FINALIZE:
                if not session.finalized or session.finalized_epoch != epoch or session.evicted:
                    return None
            else:
                return None
        if not self._pressure_ok():
            with self._lock:
                self._counters["gate_skip"] += 1
            self._publish_stats()
            return None

        edit_type = "prefetch" if action == _ACTION_PREFETCH else (
            "offload" if action == _ACTION_OFFLOAD else "evict"
        )
        results = await self._send_edit(session_id, edit_type)
        if results is None:
            self._publish_stats()
            return None
        with self._lock:
            self._counters[
                "prefetch_total" if edit_type == "prefetch" else (
                    "offload_total" if edit_type == "offload" else "evict_total"
                )
            ] += 1
        succeeded, failed = summarize_edit_results(results)
        if failed or succeeded <= 0:
            return results
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.epoch != epoch:
                return results
            if edit_type == "offload":
                session.offloaded = True
                session.evicted = False
                if session.state == _RESUME_STATE and self._prefetch_enabled:
                    # The session resumed while the offload was in flight; pull
                    # the blocks back before the next turn.
                    self._ensure_worker()
                    assert self._queue is not None
                    try:
                        self._queue.put_nowait((_ACTION_PREFETCH, session_id, epoch, None))
                    except asyncio.QueueFull:
                        self._counters["queue_full"] += 1
            elif edit_type == "evict":
                session.evicted = True
                session.offloaded = False
            elif edit_type == "prefetch":
                session.offloaded = False
                session.evicted = False
        return results

    def _pressure_ok(self) -> bool:
        provider = self._pressure_provider
        if provider is None:
            return True
        try:
            return bool(provider())
        except Exception as error:  # noqa: BLE001 — a broken probe must not stop KVC
            logger.warning("kvc capacity probe failed; allowing action: %s", error)
            return True

    async def _send_edit(self, session_id: str, edit_type: str) -> list[dict[str, Any]] | None:
        """Send one best-effort management edit and update counters."""
        sender = self._sender
        if sender is None:
            logger.warning("kvc %s skipped: no management sender configured: session_id=%s", edit_type, session_id)
            return None
        try:
            results = await sender(session_id, edit_type)
        except Exception as e:  # noqa: BLE001 — KVC must never break the status path
            with self._lock:
                self._counters["send_fail"] += 1
            logger.warning("kvc %s failed: session_id=%s error=%s", edit_type, session_id, e)
            return None
        succeeded, failed = summarize_edit_results(results)
        with self._lock:
            self._counters["edit_success"] += succeeded
            self._counters["edit_fail"] += failed
        if failed:
            logger.warning(
                "kvc %s partially failed: session_id=%s success=%d failed=%d results=%s",
                edit_type,
                session_id,
                succeeded,
                failed,
                results,
            )
        else:
            logger.info("kvc %s done: session_id=%s success=%d", edit_type, session_id, succeeded)
        return list(results or [])

    async def aclose(self) -> None:
        """Cancel timers and stop the serial queue worker."""
        with self._lock:
            for session in self._sessions.values():
                self._cancel_timers_locked(session)
        if self._queue is not None:
            self._queue.put_nowait(None)
        if self._worker is not None:
            try:
                await asyncio.wait_for(self._worker, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker.cancel()
        self._queue = None
        self._worker = None
        self._loop = None



class CentralAgentLifecycle:
    """Single-owner agent status + KVC lifecycle for multi-worker coordinators.

    ``/agent-status`` is served by every inference worker through SO_REUSEPORT,
    so a per-worker state machine/timer lets one worker offload while another
    sees ``running``. Workers in central mode forward every event to the Obs
    process, which owns this object: one state machine, one timer set, one
    manage-dispatch queue for the whole coordinator.

    The owner is transport-agnostic; ``finalize_release`` is injected by the
    caller (Obs) so pin release/PUB fan-out stays with the scheduler client.
    """

    def __init__(
        self,
        *,
        session_ttl_s: int = 3600,
        max_sessions: int = 65536,
        kvc_config: KvcLifecycleConfig | None = None,
    ) -> None:
        self._machine = AgentSessionStateMachine(session_ttl_s=session_ttl_s, max_sessions=max_sessions)
        cfg = kvc_config
        self._lifecycle = (
            AgentHintLifecycleManager(
                enabled=True,
                max_sessions=max_sessions,
                idle_offload_s=cfg.idle_offload_s,
                idle_evict_s=cfg.idle_evict_s,
                queue_size=cfg.queue_size,
                prefetch_enabled=cfg.prefetch_enabled,
            )
            if cfg is not None and cfg.enabled
            else None
        )

    @property
    def lifecycle(self) -> AgentHintLifecycleManager | None:
        """The KVC timer/queue owner, or None when KVC is disabled."""
        return self._lifecycle

    def set_sender(self, sender: ManageSender | None) -> None:
        """Inject the engine manage sender after the scheduler client connects."""
        if self._lifecycle is not None:
            self._lifecycle.set_sender(sender)

    def set_pressure_provider(self, provider: PressureProvider | None) -> None:
        """Inject the capacity-pressure probe after the scheduler client connects."""
        if self._lifecycle is not None:
            self._lifecycle.set_pressure_provider(provider)

    async def apply(
        self,
        session_id: str,
        state: str,
        *,
        epoch: int | None = None,
        reason: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        state_since_ms: int | None = None,
        finalize_release: Callable[[str, int | None], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        """Apply one event on the single owner; never raises for valid input."""
        transition = self._machine.apply(
            session_id,
            state,
            reason=reason,
            tools=list(tools or []),
            state_since_ms=state_since_ms,
            epoch=epoch,
        )
        if transition.applied and self._lifecycle is not None:
            await self._lifecycle.on_status(
                session_id,
                transition.state,
                epoch=epoch if epoch is not None else 0,
                previous_state=transition.previous_state,
            )
        if transition.applied and transition.state == _FINALIZE_STATE and finalize_release is not None:
            await finalize_release(session_id, epoch)
        return {
            "status": "ok",
            "applied": transition.applied,
            "reason": transition.reason,
            "state": transition.state,
            "previous_state": transition.previous_state,
        }

    def update_limits(self, session_ttl_s: int, max_sessions: int) -> None:
        """Hot-reload state-machine TTL/capacity."""
        self._machine.update_limits(session_ttl_s, max_sessions)

    def get(self, session_id: str) -> dict[str, Any] | None:
        """Return the central lifecycle record for one session."""
        record = self._machine.get(session_id)
        return None if record is None else record.to_dict()

    def stats(self) -> dict[str, int] | None:
        """Return the KVC counters owned by this process (None when disabled)."""
        return None if self._lifecycle is None else self._lifecycle.stats()

    async def aclose(self) -> None:
        """Stop timers and the serial dispatch queue."""
        if self._lifecycle is not None:
            await self._lifecycle.aclose()
