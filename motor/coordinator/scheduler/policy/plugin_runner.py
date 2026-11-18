# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Bounded external-policy execution with a killable subprocess and fail-open cooldown."""

import asyncio
import json
import math
import os
import signal
import sys
import time
from asyncio.subprocess import Process
from collections import Counter
from dataclasses import asdict

from motor.common.logger import get_logger
from motor.config.coordinator import PolicyPluginConfig
from motor.coordinator.scheduler.policy.api import CandidateId, RankedCandidate, SelectionInput
from motor.coordinator.scheduler.policy.plugin_worker import MAX_MESSAGE_BYTES, reject_json_constant

logger = get_logger(__name__)

_FALLBACK_REASONS = (
    "busy",
    "unavailable",
    "cooldown",
    "declined",
    "invalid_input",
    "timeout",
    "plugin_error",
    "no_eligible_candidates",
    "membership_changed",
    "cas_retry_limit",
    "snapshot_error",
)


class _PolicyInputError(ValueError):
    """Request data rejected before any bytes were sent to the child."""


class PolicyRunner:
    """One subprocess per configured worker role, with no unbounded task queue."""

    def __init__(self, config: PolicyPluginConfig, role: str | None = None) -> None:
        self.config = config
        self.role = role or (config.role[0] if config.role else "unknown")
        self._process: Process | None = None
        self._lock = asyncio.Lock()
        self._restart_task: asyncio.Task | None = None
        self._rank_task: asyncio.Task | None = None
        self._retry_after = 0.0
        self._closed = False
        self._counts: Counter = Counter()
        self._fallback_reasons: Counter = Counter()
        self._seconds = 0.0

    async def start(self) -> None:
        """Preload outside the request path. Load errors leave the built-in strategy active."""
        if self._closed or self._lock.locked() or self._process is not None or time.monotonic() < self._retry_after:
            return
        async with self._lock:
            try:
                self._process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    "import sys; stream = sys.stdout; sys.stdout = sys.stderr; "
                    "from motor.coordinator.scheduler.policy.plugin_worker import main; main(stream)",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    limit=MAX_MESSAGE_BYTES + 1,
                    start_new_session=True,
                )
                response = await asyncio.wait_for(
                    self._exchange({"name": self.config.name, "options": self.config.options}),
                    timeout=self.config.startup_timeout_ms / 1000,
                )
                if response.get("ready") is not True:
                    raise ValueError(response.get("error", "Missing plugin ready response"))
                self._counts["loads"] += 1
                logger.info("Scheduling plugin loaded name=%s role=%s api_version=2", self.config.name, self.role)
            except asyncio.CancelledError:
                await self._stop()
                raise
            except Exception as exc:
                if not self._closed:
                    self._counts["load_errors"] += 1
                    await self._fail(exc)

    async def rank(self, selection: SelectionInput) -> tuple[RankedCandidate, ...] | None:
        """Return None to request the existing built-in policy, never wait behind another call."""
        if self._closed:
            self.record_fallback("unavailable")
            return None
        if self._lock.locked():
            self.record_fallback("busy")
            return None
        if self._process is None:
            cooling_down = time.monotonic() < self._retry_after
            if not cooling_down and self._restart_task is None:
                self._restart_task = asyncio.create_task(self._restart())
            self.record_fallback("cooldown" if cooling_down else "unavailable")
            return None

        await self._lock.acquire()
        # Keep the single-flight slot until the reply is consumed or the timeout
        # expires. Caller cancellation does not cancel the protocol exchange.
        self._rank_task = asyncio.create_task(self._rank_once(selection))
        return await asyncio.shield(self._rank_task)

    async def _rank_once(self, selection: SelectionInput) -> tuple[RankedCandidate, ...] | None:
        started = time.monotonic()
        self._counts["calls"] += 1
        try:
            payload = asdict(selection)
            payload["excluded"] = [asdict(item) for item in selection.excluded]
            response = await asyncio.wait_for(self._exchange(payload), timeout=self.config.timeout_ms / 1000)
            if response == {"ranked": None}:
                self.record_fallback("declined")
                return None
            return self._validate(response, selection)
        except _PolicyInputError:
            self.record_fallback("invalid_input")
            return None
        except asyncio.CancelledError:
            await self._stop()
            raise
        except Exception as exc:
            if self._closed:
                self.record_fallback("unavailable")
                return None
            timed_out = isinstance(exc, asyncio.TimeoutError)
            self._counts["timeouts" if timed_out else "errors"] += 1
            self.record_fallback("timeout" if timed_out else "plugin_error")
            await self._fail(exc)
            return None
        finally:
            self._seconds += time.monotonic() - started
            self._rank_task = None
            self._lock.release()

    async def close(self) -> None:
        """Cancel recovery and reap the child during worker shutdown."""
        self._closed = True
        if self._restart_task is not None:
            self._restart_task.cancel()
            await asyncio.gather(self._restart_task, return_exceptions=True)
        # Interrupt an in-flight rank before waiting for its lock (its budget may be 60s).
        await self._stop()
        async with self._lock:
            await self._stop()

    def metrics_text(self) -> str:
        """Worker-local Prometheus counters; scrape every worker's metaserver."""
        label = '{policy=%s,role=%s}' % (
            json.dumps(self.config.name, ensure_ascii=False),
            json.dumps(self.role, ensure_ascii=False),
        )
        lines = []
        for event in ("loads", "load_errors", "calls", "errors", "timeouts", "fallbacks"):
            name = "motor_policy_%s_total" % event
            lines.extend(("# TYPE %s counter" % name, "%s%s %s" % (name, label, self._counts[event])))
        name = "motor_policy_fallbacks_by_reason_total"
        lines.append("# TYPE %s counter" % name)
        extra_reasons = sorted(r for r in self._fallback_reasons if r not in _FALLBACK_REASONS)
        for reason in (*_FALLBACK_REASONS, *extra_reasons):
            lines.append(
                "%s%s,reason=%s} %s"
                % (name, label[:-1], json.dumps(reason, ensure_ascii=False), self._fallback_reasons[reason])
            )
        lines.append("# TYPE motor_policy_selection_seconds summary")
        lines.append("motor_policy_selection_seconds_sum%s %s" % (label, self._seconds))
        lines.append("motor_policy_selection_seconds_count%s %s" % (label, self._counts["calls"]))
        return "\n".join(lines) + "\n"

    def record_fallback(self, reason: str) -> None:
        """Count both runner and core fallback decisions under a bounded reason label."""
        if reason not in _FALLBACK_REASONS:
            logger.warning(
                "Unknown scheduling plugin fallback reason name=%s role=%s reason=%s",
                self.config.name,
                self.role,
                reason,
            )
        self._counts["fallbacks"] += 1
        self._fallback_reasons[reason] += 1
        logger.debug("Scheduling plugin fallback name=%s role=%s reason=%s", self.config.name, self.role, reason)

    async def _exchange(self, payload: dict) -> dict:
        try:
            encoded = json.dumps(payload, allow_nan=False).encode("utf-8") + b"\n"
        except (TypeError, ValueError, UnicodeError) as exc:
            raise _PolicyInputError("Policy input is not JSON encodable") from exc
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise _PolicyInputError("Policy input exceeds message limit")
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("Policy process unavailable")
        process.stdin.write(encoded)
        await process.stdin.drain()
        line = await process.stdout.readline()
        if not line.endswith(b"\n") or len(line) > MAX_MESSAGE_BYTES:
            raise ValueError("Policy process exited or exceeded message limit")
        response = json.loads(line, parse_constant=reject_json_constant)
        if not isinstance(response, dict):
            raise ValueError("Policy response must be an object")
        return response

    @staticmethod
    def _validate(response: dict, selection: SelectionInput) -> tuple[RankedCandidate, ...]:
        raw = response.get("ranked")
        if not isinstance(raw, list) or not raw or len(raw) > len(selection.candidates):
            raise ValueError(response.get("error", "Empty or invalid policy ranking"))
        allowed = {item.id for item in selection.candidates} - selection.excluded
        seen = set()
        ranked = []
        for item in raw:
            try:
                candidate = CandidateId(**item["id"])
                score = item["score"]
            except (KeyError, TypeError) as exc:
                raise ValueError("Invalid policy ranking item") from exc
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (candidate.instance_id, candidate.endpoint_id)
            ):
                raise ValueError("Policy candidate IDs must be integers")
            if candidate not in allowed or candidate in seen:
                raise ValueError("Unknown, excluded or duplicate candidate")
            if isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score):
                raise ValueError("Policy score must be finite")
            seen.add(candidate)
            ranked.append(RankedCandidate(candidate, score))
        return tuple(ranked)

    async def _fail(self, exc: Exception) -> None:
        logger.warning(
            "Scheduling plugin failed name=%s role=%s reason=%s error=%s; falling back to configured built-in strategy",
            self.config.name,
            self.role,
            type(exc).__name__,
            str(exc)[:512],
        )
        self._retry_after = time.monotonic() + self.config.cooldown_seconds
        await self._stop()

    async def _stop(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await process.wait()

    async def _restart(self) -> None:
        try:
            await self.start()
        finally:
            self._restart_task = None
