# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may obtain a copy of the License at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO, THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# See the Mulan PSL v2 for more details.

"""Engine-neutral Program-level admission for agentic requests.

The native engine remains responsible for batching and physical KV blocks.  This
module only controls the logical working set before a request is forwarded.
"""

from __future__ import annotations

import time
import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from motor.common.logger import get_logger

logger = get_logger(__name__)


class ProgramState(str, Enum):
    """Logical admission state of a Program."""

    ACTIVE = "active"
    PAUSED = "paused"
    TERMINATED = "terminated"


class ProgramStatus(str, Enum):
    """Whether a live Program currently needs model service."""

    REASONING = "reasoning"
    ACTING = "acting"


class AdmissionDisposition(str, Enum):
    """Result returned for one request arrival."""

    ADMITTED = "admitted"
    QUEUED = "queued"
    BYPASS = "bypass"
    TERMINATED = "terminated"


class ProgressTTLMode(str, Enum):
    """Progress-TTL operating mode, matching the RFC reference implementation."""

    ON = "on"
    OFF = "off"
    AUTO = "auto"


@dataclass(frozen=True)
class ProgramRef:
    """Generation-safe Program identity."""

    program_id: str
    generation: int


@dataclass(frozen=True)
class ProgramIdentity:
    """Normalized identity received from the API adapter."""

    program_id: str
    parent_program_id: str | None = None


@dataclass(frozen=True)
class CapacitySnapshot:
    """Conservative logical capacity facts for one endpoint."""

    total_kv_tokens: int
    native_used_kv_tokens: int = 0
    native_waiting_kv_tokens: int = 0
    safety_ratio: float = 0.95

    @property
    def usable_kv_tokens(self) -> int:
        return max(0, int(self.total_kv_tokens * self.safety_ratio))


@dataclass(frozen=True)
class AdmissionRequest:
    """Facts required to admit one request."""

    request_id: str
    identity: ProgramIdentity
    prompt_tokens: int
    max_output_tokens: int = 0
    shared_prefix_tokens: int = 0
    arrived_at: float | None = None

    @property
    def arrival_time(self) -> float:
        return self.arrived_at if self.arrived_at is not None else time.monotonic()


@dataclass(frozen=True)
class AdmissionDecision:
    """Observable admission result."""

    disposition: AdmissionDisposition
    request_id: str
    program: ProgramRef | None = None
    reason: str = ""


@dataclass
class _RollingStats:
    """Bounded workload observations used by adaptive Progress-TTL decisions."""

    window_size: int = 100
    intervals: deque[float] = field(default_factory=deque)
    utilities: deque[float] = field(default_factory=deque)
    request_latencies: deque[float] = field(default_factory=deque)
    input_growth: deque[int] = field(default_factory=deque)
    rounds_since_pause: deque[int] = field(default_factory=deque)

    def _append(self, values: deque, value) -> None:
        values.append(value)
        while len(values) > self.window_size:
            values.popleft()

    def observe_request(self, latency: float, growth: int, rounds: int) -> None:
        self._append(self.request_latencies, max(0.0, latency))
        self._append(self.input_growth, max(0, growth))
        self._append(self.rounds_since_pause, max(0, rounds))

    def observe_continuity(self, interval: float, impact: float, ttl: float) -> None:
        interval = max(0.0, interval)
        utility = impact - interval if interval <= ttl else -ttl
        self._append(self.intervals, interval)
        self._append(self.utilities, utility)

    @property
    def window_complete(self) -> bool:
        return len(self.intervals) >= self.window_size

    @property
    def utility(self) -> float:
        return statistics.fmean(self.utilities) if self.utilities else 0.0

    @property
    def avg_growth(self) -> float:
        return statistics.fmean(self.input_growth) if self.input_growth else 0.0

    @property
    def avg_rounds_since_pause(self) -> float:
        return statistics.fmean(self.rounds_since_pause) if self.rounds_since_pause else 1.0

    def fitted_ttl(self, minimum: float, maximum: float) -> float:
        if not self.intervals:
            return minimum
        # The RFC uses a fitted log-normal distribution.  A conservative p90
        # over the bounded window is a stable approximation for Coordinator.
        percentile = (
            statistics.quantiles(self.intervals, n=10, method="inclusive")[8]
            if len(self.intervals) > 1
            else self.intervals[0]
        )
        return min(maximum, max(minimum, percentile))


@dataclass
class _Program:
    ref: ProgramRef
    identity: ProgramIdentity
    state: ProgramState = ProgramState.PAUSED
    status: ProgramStatus = ProgramStatus.REASONING
    context_tokens: int = 0
    shared_prefix_tokens: int = 0
    segment_served_rounds: int = 0
    lifetime_generated_tokens: int = 0
    last_completed_at: float | None = None
    acting_until: float | None = None
    paused_at: float | None = None
    shared_prefix_fresh_until: float | None = None
    wait_started_at: float | None = None
    segment_started_at: float | None = None
    marked_for_pause: bool = False
    is_privileged: bool = False
    privilege_reason: str | None = None
    request_ids: set[str] = field(default_factory=set)
    pending: dict[str, AdmissionRequest] = field(default_factory=dict)

    @property
    def private_tokens(self) -> int:
        return max(0, self.context_tokens - self.shared_prefix_tokens)


@dataclass(frozen=True)
class ProgramSchedulerConfig:
    """Progress-TTL policy configuration aligned with the AgentInfer RFC."""

    enabled: bool = False
    ttl_seconds: float = 10.0
    force_resume_timeout_seconds: float = 1800.0
    paused_program_ttl_seconds: float = 1800.0
    decode_buffer_tokens: int = 100
    resume_capacity_ratio: float = 0.95
    pause_capacity_ratio: float = 1.0
    target_min_segment_rounds: int = 9
    target_max_segment_rounds: int = 14
    mode: ProgressTTLMode = ProgressTTLMode.ON
    rolling_window_size: int = 100
    auto_enable_utility_seconds: float = 20.0
    auto_disable_utility_seconds: float = 5.0
    min_ttl_seconds: float = 10.0
    max_ttl_seconds: float = 120.0
    ttl_decode_throughput_alpha: float = 0.15
    ttl_prefill_seconds_per_1k_uncached_tokens: float = 0.29
    ttl_max_cache_miss_impact_ratio: float = 1.0
    shared_prefix_freshness_warmup_seconds: float = 100.0
    shared_prefix_freshness_kv_turnovers: float = 2.0
    capacity_safety_margin_tokens: int = 0
    pause_capacity_lookahead_rounds: float = 2.0
    privileged_lookahead_rounds: float = 14.0
    privileged_max_context_tokens: int = 262144

    def __post_init__(self) -> None:
        if self.ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        if self.force_resume_timeout_seconds < 0 or self.paused_program_ttl_seconds < 0:
            raise ValueError("Program deadlines must be non-negative")
        if self.decode_buffer_tokens < 0:
            raise ValueError("decode_buffer_tokens must be non-negative")
        if not 0 < self.resume_capacity_ratio <= 1:
            raise ValueError("resume_capacity_ratio must be in (0, 1]")
        if not 0 < self.pause_capacity_ratio <= 1:
            raise ValueError("pause_capacity_ratio must be in (0, 1]")
        if self.target_max_segment_rounds <= 0:
            raise ValueError("target_max_segment_rounds must be positive")
        if self.target_min_segment_rounds <= 0 or self.target_max_segment_rounds < self.target_min_segment_rounds:
            raise ValueError("target_max_segment_rounds must be >= target_min_segment_rounds")
        if self.rolling_window_size <= 0:
            raise ValueError("rolling_window_size must be positive")
        if self.auto_disable_utility_seconds > self.auto_enable_utility_seconds:
            raise ValueError("auto_disable_utility_seconds must be <= auto_enable_utility_seconds")
        if self.min_ttl_seconds < 0 or self.max_ttl_seconds < self.min_ttl_seconds:
            raise ValueError("invalid TTL bounds")
        if not 0 <= self.ttl_decode_throughput_alpha <= 1:
            raise ValueError("ttl_decode_throughput_alpha must be in [0, 1]")
        if self.ttl_prefill_seconds_per_1k_uncached_tokens < 0 or not 0 <= self.ttl_max_cache_miss_impact_ratio <= 1:
            raise ValueError("invalid TTL cache-miss impact settings")
        if self.shared_prefix_freshness_warmup_seconds < 0 or self.shared_prefix_freshness_kv_turnovers <= 0:
            raise ValueError("invalid shared-prefix freshness settings")
        if self.capacity_safety_margin_tokens < 0 or self.privileged_max_context_tokens <= 0:
            raise ValueError("invalid capacity/privilege settings")


class ProgramScheduler:
    """Single-endpoint logical Program scheduler.

    Calls are expected to be serialized by the SchedulerServer request loop.
    A future multi-endpoint manager can own one instance per endpoint without
    changing this state machine.
    """

    def __init__(self, config: ProgramSchedulerConfig | None = None) -> None:
        self.config = config or ProgramSchedulerConfig()
        self._programs: dict[ProgramRef, _Program] = {}
        self._current_generation: dict[str, int] = {}
        self._requests: dict[str, tuple[ProgramRef, AdmissionRequest]] = {}
        self._queue: deque[str] = deque()
        self._sequence = 0
        self._stats = _RollingStats(self.config.rolling_window_size)
        self._auto_enabled = False

    @property
    def queued_request_count(self) -> int:
        return sum(request_id in self._requests for request_id in self._queue)

    @property
    def programs(self) -> tuple[ProgramRef, ...]:
        return tuple(program.ref for program in self._programs.values() if program.state is not ProgramState.TERMINATED)

    def _materialize(self, identity: ProgramIdentity) -> _Program:
        generation = self._current_generation.get(identity.program_id, 0) + 1
        ref = ProgramRef(identity.program_id, generation)
        self._current_generation[identity.program_id] = generation
        program = _Program(ref=ref, identity=identity)
        self._programs[ref] = program
        return program

    def _current(self, program_id: str) -> _Program | None:
        generation = self._current_generation.get(program_id)
        if generation is None:
            return None
        return self._programs.get(ProgramRef(program_id, generation))

    def _required_tokens(self, request: AdmissionRequest, program: _Program) -> int:
        context = max(program.context_tokens, 0, request.prompt_tokens)
        shared = max(program.shared_prefix_tokens, request.shared_prefix_tokens)
        private = max(0, context - shared)
        return private + max(0, request.max_output_tokens) + self.config.decode_buffer_tokens

    def _used_tokens(self) -> int:
        """Return logical acting reservation, with a conservative no-metrics fallback."""
        total = 0
        for program in self._programs.values():
            if program.state is ProgramState.ACTIVE and program.status is ProgramStatus.ACTING:
                total += program.private_tokens + self.config.decode_buffer_tokens
        return total

    def _growth_reserve_tokens(self, candidate: _Program | None = None) -> int:
        growth = max(0.0, self._stats.avg_growth)
        if growth <= 0:
            return 0
        programs = [
            p for p in self._programs.values() if p.state is ProgramState.ACTIVE and p.status is ProgramStatus.REASONING
        ]
        if candidate is not None and candidate not in programs:
            programs.append(candidate)
        reserve = 0.0
        for program in programs:
            remaining = max(
                0.0,
                float(self.config.target_max_segment_rounds - program.segment_served_rounds),
            )
            reserve += growth * remaining
        return int(math.ceil(reserve))

    def _accounted_used_tokens(self, capacity: CapacitySnapshot) -> int:
        """Combine native usage and coordinator reservations using the RFC sum."""
        native = max(0, capacity.native_used_kv_tokens) + max(0, capacity.native_waiting_kv_tokens)
        logical = self._used_tokens()
        if native == 0:
            logical += sum(
                p.private_tokens + self.config.decode_buffer_tokens
                for p in self._programs.values()
                if p.state is ProgramState.ACTIVE and p.status is ProgramStatus.REASONING
            )
        return native + logical

    def _fits(self, request: AdmissionRequest, program: _Program, capacity: CapacitySnapshot) -> bool:
        required = self._required_tokens(request, program)
        used = self._accounted_used_tokens(capacity)
        available = capacity.usable_kv_tokens - used
        return available >= required + self._growth_reserve_tokens(program)

    def _transitions_enabled(self) -> bool:
        if not self.config.enabled or self.config.mode is ProgressTTLMode.OFF:
            return False
        if self.config.mode is ProgressTTLMode.ON:
            return True
        if self._stats.utility >= self.config.auto_enable_utility_seconds:
            self._auto_enabled = True
        elif self._stats.utility <= self.config.auto_disable_utility_seconds:
            self._auto_enabled = False
        return self._auto_enabled

    def _ttl_for(self, program: _Program, request: AdmissionRequest) -> float:
        if not self.config.enabled or self.config.mode is ProgressTTLMode.OFF:
            return 0.0
        if self._stats.window_complete:
            return self._stats.fitted_ttl(self.config.min_ttl_seconds, self.config.max_ttl_seconds)
        return min(self.config.max_ttl_seconds, max(0.0, self.config.ttl_seconds))

    def _privileged_limit(self, capacity: CapacitySnapshot) -> int:
        slots = capacity.total_kv_tokens // self.config.privileged_max_context_tokens
        return max(1, slots // 2)

    def _privileged_count(self) -> int:
        return sum(p.is_privileged for p in self._programs.values() if p.state is ProgramState.ACTIVE)

    def _promote_if_eligible(self, program: _Program, capacity: CapacitySnapshot) -> bool:
        if program.segment_served_rounds < self.config.target_min_segment_rounds:
            return False
        if self._privileged_count() >= self._privileged_limit(capacity) and not program.is_privileged:
            return False
        if self._queue and not program.is_privileged:
            return False
        program.is_privileged = True
        program.privilege_reason = "progress_admission"
        return True

    def _repair_capacity(self, capacity: CapacitySnapshot, now: float) -> None:
        """Pause idle victims when native usage exceeds the pause headroom."""
        if not self._transitions_enabled():
            return
        target = max(0, int(capacity.total_kv_tokens * self.config.pause_capacity_ratio))
        target -= self.config.capacity_safety_margin_tokens
        used = self._accounted_used_tokens(capacity)
        if used <= target:
            return
        victims = [
            p
            for p in self._programs.values()
            if p.state is ProgramState.ACTIVE and p.status is ProgramStatus.ACTING and not p.is_privileged
        ]

        def pause_score(program: _Program) -> tuple[float, int, str]:
            elapsed = max(0.0, now - (program.segment_started_at or program.last_completed_at or now))
            input_scale = max(1.0, float(program.context_tokens) / max(1.0, self._stats.avg_growth or 1.0))
            return elapsed * input_scale, program.segment_served_rounds, program.ref.program_id

        victims.sort(key=pause_score, reverse=True)
        for victim in victims:
            victim.state = ProgramState.PAUSED
            victim.paused_at = now
            victim.acting_until = None
            victim.marked_for_pause = False
            if self._accounted_used_tokens(capacity) <= target:
                break
        if self._accounted_used_tokens(capacity) > target:
            reasoning = [
                p
                for p in self._programs.values()
                if p.state is ProgramState.ACTIVE and p.status is ProgramStatus.REASONING and not p.is_privileged
            ]
            reasoning.sort(key=lambda p: (p.segment_served_rounds, p.ref.program_id))
            for victim in reasoning:
                victim.marked_for_pause = True
                if self._accounted_used_tokens(capacity) <= target:
                    break

    def _promote_progress_privileges(self, capacity: CapacitySnapshot) -> None:
        """Give bounded privilege to the most progressed active Program under no queue pressure."""
        target = max(0, int(capacity.total_kv_tokens * self.config.pause_capacity_ratio))
        if self._accounted_used_tokens(capacity) > target:
            return
        if self._queue or self._privileged_count() >= self._privileged_limit(capacity):
            return
        candidates = [
            p
            for p in self._programs.values()
            if p.state is ProgramState.ACTIVE and not p.is_privileged and p.segment_served_rounds > 0
        ]
        candidates.sort(key=lambda p: (p.lifetime_generated_tokens, p.segment_served_rounds), reverse=True)
        for candidate in candidates[: max(0, self._privileged_limit(capacity) - self._privileged_count())]:
            candidate.is_privileged = True
            candidate.privilege_reason = "progress_promote"

    def _expire_shared_prefix(self, program: _Program, now: float) -> None:
        if program.shared_prefix_fresh_until is not None and program.shared_prefix_fresh_until <= now:
            program.shared_prefix_tokens = 0

    def _terminate(self, program: _Program) -> None:
        program.state = ProgramState.TERMINATED
        program.acting_until = None
        program.is_privileged = False
        self._queue = deque(
            request_id
            for request_id in self._queue
            if request_id in self._requests and self._requests[request_id][0] != program.ref
        )
        for request_id, (ref, _) in tuple(self._requests.items()):
            if ref == program.ref:
                self._requests.pop(request_id)
        self._programs.pop(program.ref, None)

    def _admit(self, request: AdmissionRequest, program: _Program) -> AdmissionDecision:
        now = request.arrival_time
        self._expire_shared_prefix(program, now)
        program.shared_prefix_tokens = max(program.shared_prefix_tokens, request.shared_prefix_tokens)
        program.state = ProgramState.ACTIVE
        program.status = ProgramStatus.REASONING
        program.paused_at = None
        program.acting_until = None
        program.wait_started_at = None
        program.segment_started_at = program.segment_started_at or now
        program.context_tokens = max(program.context_tokens, 0, request.prompt_tokens)
        program.request_ids.add(request.request_id)
        program.pending.pop(request.request_id, None)
        self._requests[request.request_id] = (program.ref, request)
        return AdmissionDecision(AdmissionDisposition.ADMITTED, request.request_id, program.ref, "capacity_fit")

    def arrive(self, request: AdmissionRequest, capacity: CapacitySnapshot) -> AdmissionDecision:
        """Record an arrival and admit immediately when capacity permits."""
        if request.request_id in self._requests:
            ref, _ = self._requests[request.request_id]
            return AdmissionDecision(AdmissionDisposition.ADMITTED, request.request_id, ref, "duplicate")
        self._sequence += 1
        if not self.config.enabled:
            logger.info(
                "ProgressTTL admission disposition=bypass request_id=%s program_id=%s reason=disabled",
                request.request_id,
                request.identity.program_id,
            )
            return AdmissionDecision(AdmissionDisposition.BYPASS, request.request_id, None, "disabled")
        program = self._current(request.identity.program_id)
        if program is None or program.state is ProgramState.TERMINATED:
            program = self._materialize(request.identity)
        program.context_tokens = max(program.context_tokens, 0, request.prompt_tokens)
        program.pending[request.request_id] = request
        if not self._transitions_enabled():
            return self._admit(request, program)
        # A related request can inherit the bounded privilege of an acting
        # parent/child, matching the RFC's privilege handoff rule.
        related = [
            candidate
            for candidate in self._programs.values()
            if candidate.state is ProgramState.ACTIVE
            and candidate.status is ProgramStatus.ACTING
            and candidate.is_privileged
            and (
                candidate.identity.program_id == request.identity.parent_program_id
                or candidate.identity.parent_program_id == request.identity.program_id
            )
        ]
        if related:
            source = related[0]
            source.state = ProgramState.PAUSED
            source.paused_at = request.arrival_time
            source.acting_until = None
            source.is_privileged = False
            program.is_privileged = True
            program.privilege_reason = "related_program_handoff"
            decision = self._admit(request, program)
            logger.info(
                "ProgressTTL admission disposition=admitted request_id=%s program_id=%s reason=related_program_handoff queued=%d used_kv=%d native_used=%d native_waiting=%d",
                request.request_id,
                program.ref.program_id,
                len(self._queue),
                self._accounted_used_tokens(capacity),
                capacity.native_used_kv_tokens,
                capacity.native_waiting_kv_tokens,
            )
            return decision
        if self.queued_request_count and program.state is not ProgramState.ACTIVE:
            program.wait_started_at = request.arrival_time
            self._requests[request.request_id] = (program.ref, request)
            self._queue.append(request.request_id)
            logger.info(
                "ProgressTTL admission disposition=queued request_id=%s program_id=%s queue_depth=%d reason=queue_precedence",
                request.request_id,
                program.ref.program_id,
                len(self._queue),
            )
            return AdmissionDecision(AdmissionDisposition.QUEUED, request.request_id, program.ref, "queue_precedence")
        self._expire_shared_prefix(program, request.arrival_time)
        if program.state is ProgramState.ACTIVE or self._fits(request, program, capacity):
            if program.state is not ProgramState.ACTIVE:
                self._promote_if_eligible(program, capacity)
            decision = self._admit(request, program)
            logger.info(
                "ProgressTTL admission disposition=admitted request_id=%s program_id=%s reason=%s queued=%d used_kv=%d native_used=%d native_waiting=%d",
                request.request_id,
                program.ref.program_id,
                decision.reason,
                len(self._queue),
                self._accounted_used_tokens(capacity),
                capacity.native_used_kv_tokens,
                capacity.native_waiting_kv_tokens,
            )
            return decision
        program.wait_started_at = request.arrival_time
        self._requests[request.request_id] = (program.ref, request)
        self._queue.append(request.request_id)
        logger.info(
            "ProgressTTL admission disposition=queued request_id=%s program_id=%s queue_depth=%d prompt_tokens=%d max_output_tokens=%d used_kv=%d capacity_kv=%d native_used=%d native_waiting=%d",
            request.request_id,
            program.ref.program_id,
            len(self._queue),
            request.prompt_tokens,
            request.max_output_tokens,
            self._accounted_used_tokens(capacity),
            capacity.total_kv_tokens,
            capacity.native_used_kv_tokens,
            capacity.native_waiting_kv_tokens,
        )
        return AdmissionDecision(AdmissionDisposition.QUEUED, request.request_id, program.ref, "capacity_wait")

    def _priority(self, request_id: str, now: float) -> tuple[int, int, float, int]:
        ref, request = self._requests[request_id]
        program = self._programs[ref]
        waited = max(0.0, now - request.arrival_time)
        forced = int(waited >= self.config.force_resume_timeout_seconds)
        privileged = int(program.is_privileged)
        return forced, privileged, float(program.lifetime_generated_tokens), -self._sequence

    def tick(self, capacity: CapacitySnapshot, now: float | None = None) -> tuple[AdmissionDecision, ...]:
        """Expire TTLs, repair retention, and admit as many queued requests as fit."""
        now = time.monotonic() if now is None else now
        decisions: list[AdmissionDecision] = []
        self._repair_capacity(capacity, now)
        self._promote_progress_privileges(capacity)
        for program in tuple(self._programs.values()):
            if program.state is ProgramState.ACTIVE and program.status is ProgramStatus.ACTING:
                if program.acting_until is not None and program.acting_until <= now:
                    program.state = ProgramState.PAUSED
                    program.paused_at = now
                    program.acting_until = None
                    program.is_privileged = False
                    program.privilege_reason = "ttl_expiry"
                    logger.info(
                        "ProgressTTL transition=active_to_paused reason=ttl_expiry program_id=%s generation=%d",
                        program.ref.program_id,
                        program.ref.generation,
                    )
            if program.state is ProgramState.PAUSED and program.paused_at is not None:
                if now - program.paused_at >= self.config.paused_program_ttl_seconds and not program.pending:
                    self._terminate(program)
                    logger.info(
                        "ProgressTTL transition=paused_to_terminated reason=paused_ttl program_id=%s generation=%d",
                        program.ref.program_id,
                        program.ref.generation,
                    )
        pending = [request_id for request_id in self._queue if request_id in self._requests]
        pending.sort(key=lambda request_id: self._priority(request_id, now), reverse=True)
        self._queue = deque(pending)
        while self._queue:
            request_id = self._queue[0]
            ref, request = self._requests[request_id]
            program = self._programs[ref]
            priority = self._priority(request_id, now)
            forced = priority[0] == 1
            self._expire_shared_prefix(program, now)
            if not forced and not self._fits(request, program, capacity):
                break
            self._queue.popleft()
            decisions.append(self._admit(request, program))
        if decisions or self._queue:
            logger.info(
                "ProgressTTL tick decisions=%d queue_depth=%d active=%d paused=%d privileged=%d used_kv=%d capacity_kv=%d native_used=%d native_waiting=%d",
                len(decisions),
                len(self._queue),
                sum(p.state is ProgramState.ACTIVE for p in self._programs.values()),
                sum(p.state is ProgramState.PAUSED for p in self._programs.values()),
                self._privileged_count(),
                self._accounted_used_tokens(capacity),
                capacity.total_kv_tokens,
                capacity.native_used_kv_tokens,
                capacity.native_waiting_kv_tokens,
            )
        return tuple(decisions)

    def complete(
        self,
        request_id: str,
        total_tokens: int,
        now: float | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> bool:
        """Complete one admitted request and arm the acting TTL after the last overlap."""
        binding = self._requests.pop(request_id, None)
        if binding is None:
            return False
        ref, request = binding
        program = self._programs.get(ref)
        if program is None:
            return False
        program.request_ids.discard(request_id)
        program.pending.pop(request_id, None)
        completed_at = time.monotonic() if now is None else now
        previous_completed_at = program.last_completed_at
        previous_context = program.context_tokens
        effective_prompt = max(0, request.prompt_tokens if prompt_tokens is None else prompt_tokens)
        effective_completion = (
            max(0, total_tokens - effective_prompt) if completion_tokens is None else max(0, completion_tokens)
        )
        effective_total = effective_prompt + effective_completion
        program.context_tokens = max(program.context_tokens, 0, total_tokens, effective_total)
        program.lifetime_generated_tokens += effective_completion
        program.last_completed_at = completed_at
        program.segment_served_rounds += 1
        if request.arrived_at is not None:
            self._stats.observe_request(
                max(0.0, completed_at - request.arrival_time),
                max(0, program.context_tokens - previous_context),
                program.segment_served_rounds,
            )
        if not program.request_ids and program.state is ProgramState.ACTIVE:
            if program.marked_for_pause:
                program.state = ProgramState.PAUSED
                program.status = ProgramStatus.ACTING
                program.paused_at = completed_at
                program.marked_for_pause = False
                program.is_privileged = False
                return True
            program.status = ProgramStatus.ACTING
            interval = max(0.0, completed_at - previous_completed_at) if previous_completed_at is not None else 0.0
            ttl = self._ttl_for(program, request)
            uncached_tokens = max(0, request.prompt_tokens - request.shared_prefix_tokens)
            cold_prefill = uncached_tokens / 1000.0 * self.config.ttl_prefill_seconds_per_1k_uncached_tokens
            impact = cold_prefill * (2.0 - self.config.ttl_decode_throughput_alpha)
            impact = min(impact, cold_prefill * self.config.ttl_max_cache_miss_impact_ratio)
            self._stats.observe_continuity(interval, impact, ttl)
            program.shared_prefix_fresh_until = completed_at + max(
                self.config.shared_prefix_freshness_warmup_seconds,
                self.config.shared_prefix_freshness_kv_turnovers * max(1.0, ttl),
            )
            if self._transitions_enabled() and ttl > 0:
                program.acting_until = completed_at + ttl
            else:
                program.state = ProgramState.PAUSED
                program.paused_at = completed_at
        return True

    def cancel(self, request_id: str, now: float | None = None) -> bool:
        """Cancel an admitted or queued request without fabricating progress."""
        binding = self._requests.pop(request_id, None)
        if binding is None:
            return False
        ref, request = binding
        program = self._programs.get(ref)
        if program is None:
            return False
        program.pending.pop(request_id, None)
        program.request_ids.discard(request_id)
        if not program.request_ids and not program.pending:
            if program.state is ProgramState.PAUSED:
                self._terminate(program)
            elif program.state is ProgramState.ACTIVE:
                program.status = ProgramStatus.ACTING
                completed_at = time.monotonic() if now is None else now
                ttl = self._ttl_for(program, request)
                program.acting_until = completed_at + ttl if ttl > 0 else None
                if ttl <= 0:
                    program.state = ProgramState.PAUSED
                    program.paused_at = completed_at
        return True

    def release(self, program_id: str, generation: int | None = None) -> bool:
        """Release an idle exact generation after an API terminal lifecycle signal."""
        program = (
            self._current(program_id) if generation is None else self._programs.get(ProgramRef(program_id, generation))
        )
        if program is None or program.request_ids or program.pending:
            return False
        program.state = ProgramState.TERMINATED
        program.acting_until = None
        program.is_privileged = False
        self._programs.pop(program.ref, None)
        return True

    def snapshot(self) -> dict[str, int | float | bool | str]:
        """Return low-cardinality stats suitable for observability."""
        live = [p for p in self._programs.values() if p.state is not ProgramState.TERMINATED]
        return {
            "active_programs": sum(p.state is ProgramState.ACTIVE for p in live),
            "paused_programs": sum(p.state is ProgramState.PAUSED for p in live),
            "reasoning_programs": sum(p.status is ProgramStatus.REASONING for p in live),
            "acting_programs": sum(p.status is ProgramStatus.ACTING for p in live),
            "queued_requests": self.queued_request_count,
            "used_kv_tokens": self._used_tokens(),
            "continuity_window_samples": len(self._stats.intervals),
            "continuity_window_complete": self._stats.window_complete,
            "continuity_utility_seconds": self._stats.utility,
            "continuity_enabled": self._transitions_enabled(),
            "avg_input_growth_per_round": self._stats.avg_growth,
            "privileged_programs": self._privileged_count(),
            "mode": self.config.mode.value,
        }
