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
HPA capacity planner (demand-side windowed statistics).

Pure-logic module for horizontal-autoscaling capacity planning in PD-disaggregated
serving.  The demand side differentiates per-role counters (request success total /
token total) into arrival-rate (λ) and average-length (L̄) samples over snapshot
intervals, smoothed with an exponential moving average (EMA).  The supply side
calibrates per-role capacity online: C_p from Δtokens/Δprefill_time_sum smoothed
with an EMA, C_d from per-instance TPS peaks with a slow decay.  On top of these,
``compute()`` derives the required replica counts under a dual throughput + KV
constraint and reports per-role utilization.

Semantics:

* The first valid sample initializes the corresponding EMA; afterwards
  ``value += alpha * (sample - value)``.
* Ratio-like signals hold on zero counter increments: when a counter delta is
  <= 0 (no progress, or a counter drop caused by scale-in), the affected EMAs
  are not updated.
* Capacity estimates start from the configured prior (if any); a role counts as
  calibrated when its prior is positive or at least one valid sample was seen.
* The decode capacity decay is bounded below by a floor:
  ``max(prior, peak of the last capacity_window_cycles samples)``, so a
  sustained low load cannot decay the estimate to zero, and an old peak is
  forgotten once it slides out of the window.
* The KV wait time W_kv is the per-request queue time plus decode time.  The
  ``queue_time_sum`` / ``queue_time_count`` and ``decode_time_sum`` /
  ``decode_time_count`` pairs are cumulative counters; each component is
  re-sampled as Δsum/Δcount only when both of its deltas are > 0 (otherwise
  it holds), the per-snapshot sample is mean_queue + mean_decode, and the
  samples are smoothed with an EMA.
"""

import math
from collections import deque
from dataclasses import dataclass


@dataclass
class PlannerConfig:
    """Configuration for the capacity planner."""

    target_utilization: float = 0.8
    kv_target_utilization: float = 0.9
    # EMA 系数默认 0.85：样本本身已是 3s 采集窗口的聚合均值，无需重平滑，
    # 3 个采集周期内即可收敛（残留 0.15^2 ≈ 2%）；突发毛刺由下游 HPA 的
    # stabilization window 兜底。
    ema_alpha: float = 0.85
    capacity_decay: float = 0.005
    prefill_tps_capacity_prior: float = 0.0
    decode_tps_capacity_prior: float = 0.0
    capacity_window_cycles: int = 200

    def __post_init__(self) -> None:
        """Reject out-of-range values instead of degrading silently."""
        if not 0.0 < self.target_utilization <= 1.0:
            raise ValueError(f"target_utilization must be in (0, 1], got {self.target_utilization}")
        if not 0.0 < self.kv_target_utilization <= 1.0:
            raise ValueError(f"kv_target_utilization must be in (0, 1], got {self.kv_target_utilization}")
        if not 0.0 < self.ema_alpha <= 1.0:
            raise ValueError(f"ema_alpha must be in (0, 1], got {self.ema_alpha}")
        if not 0.0 <= self.capacity_decay < 1.0:
            raise ValueError(f"capacity_decay must be in [0, 1), got {self.capacity_decay}")
        if self.prefill_tps_capacity_prior < 0.0:
            raise ValueError(f"prefill_tps_capacity_prior must be >= 0, got {self.prefill_tps_capacity_prior}")
        if self.decode_tps_capacity_prior < 0.0:
            raise ValueError(f"decode_tps_capacity_prior must be >= 0, got {self.decode_tps_capacity_prior}")
        if self.capacity_window_cycles < 1:
            raise ValueError(f"capacity_window_cycles must be >= 1, got {self.capacity_window_cycles}")


@dataclass
class RoleSnapshot:
    """Role-level counter and metadata snapshot for one planning interval."""

    active_instances: int
    instance_tps: list[float]  # prompt TPS (P) / generation TPS (D) per instance
    request_success_total: float  # 角色级计数器和（baseline 已修正）
    tokens_total: float  # prompt (P) / generation (D) 计数器和
    prefill_time_sum: float = 0.0  # 仅 P
    queue_time_sum: float = 0.0  # 仅 D：排队时间累计计数器
    queue_time_count: float = 0.0  # 仅 D：排队时间样本数累计计数器
    decode_time_sum: float = 0.0  # 仅 D：decode 时间累计计数器
    decode_time_count: float = 0.0  # 仅 D：decode 时间样本数累计计数器
    kv_tokens_per_instance_decode: float = 0.0  # 仅 D：num_gpu_blocks × block_size，0=未知（P 侧暂不统计）


@dataclass
class PlannerSnapshot:
    """One planning-interval input: wall-clock delta plus per-role counters."""

    dt: float
    prefill: RoleSnapshot | None
    decode: RoleSnapshot | None


class _Ema:
    """Exponential moving average where the first sample is the initial value."""

    def __init__(self, alpha: float) -> None:
        self._alpha = alpha
        self._value: float | None = None

    def update(self, sample: float) -> None:
        """Blend a new sample into the average."""
        if self._value is None:
            self._value = sample
        else:
            self._value += self._alpha * (sample - self._value)

    @property
    def value(self) -> float | None:
        """Current EMA value, or None if no sample has been observed."""
        return self._value


class _DemandTracker:
    """Per-role demand-side state: last counter values plus EMAs for λ and L̄."""

    def __init__(self, alpha: float) -> None:
        self._seen = False
        self._last_req: float | None = None
        self._last_tokens: float | None = None
        self._lambda_ema = _Ema(alpha)
        self._length_ema = _Ema(alpha)

    def update(self, snapshot: RoleSnapshot, dt: float) -> None:
        """Consume one role snapshot and update the EMAs from counter deltas.

        Each counter delta is judged independently: a req delta <= 0 holds the
        λ EMA, and a token delta <= 0 holds the L̄ EMA (zero increments, or
        counter drops caused by scale-in, never feed negative samples into
        the EMAs).
        """
        lambda_sample = length_sample = None
        if self._last_req is not None and self._last_tokens is not None and dt > 0.0:
            lambda_sample, length_sample = self._rate_and_mean(
                snapshot.request_success_total - self._last_req,
                snapshot.tokens_total - self._last_tokens,
                dt,
            )
        self._seen = True
        self._last_req = snapshot.request_success_total
        self._last_tokens = snapshot.tokens_total
        if lambda_sample is not None:
            self._lambda_ema.update(lambda_sample)
        if length_sample is not None:
            self._length_ema.update(length_sample)

    @staticmethod
    def _rate_and_mean(req_delta: float, token_delta: float, dt: float) -> tuple[float | None, float | None]:
        """Derive (λ sample, L̄ sample) from counter deltas.

        The λ sample depends only on req_delta and is produced when
        req_delta > 0; the L̄ sample depends on both deltas and is produced
        only when both are > 0.  A None entry signals that the corresponding
        EMA holds its previous value.
        """
        lambda_sample = req_delta / dt if req_delta > 0.0 else None
        length_sample = token_delta / req_delta if req_delta > 0.0 and token_delta > 0.0 else None
        return lambda_sample, length_sample

    def demand_tps(self) -> float | None:
        """Demand in tokens per second: EMA(λ) × EMA(L̄).

        Returns 0.0 when the role has been observed but has no valid sample
        yet (e.g. the only delta so far was <= 0), and None when the role has
        never appeared in any snapshot.
        """
        if not self._seen:
            return None
        lambda_value = self._lambda_ema.value
        length_value = self._length_ema.value
        if lambda_value is None or length_value is None:
            return 0.0
        return lambda_value * length_value

    def arrival_rate(self) -> float:
        """EMA(λ) in requests per second, 0.0 when no valid sample exists yet."""
        return self._lambda_ema.value or 0.0

    def mean_length(self) -> float:
        """EMA(L̄) in tokens per request, 0.0 when no valid sample exists yet."""
        return self._length_ema.value or 0.0


class _CapacityEstimator:
    """Per-role supply-side capacity estimate (C) with online calibration.

    Prefill branch: the sample is Δtokens/Δprefill_time_sum, kept only when both
    deltas are > 0 (otherwise the estimate holds), and blended into an EMA that
    starts from the prior.  Decode branch: the sample is max(instance_tps) per
    snapshot (an empty list yields no sample, which is neither recorded nor
    decayed); a sample above the current peak is adopted immediately, otherwise
    the peak decays by ``(1 - decay)`` but never below the floor
    ``max(prior, window peak)``, where the window holds the last
    ``window_cycles`` samples.  The window peak doubles as the forgetting
    mechanism: once an old peak slides out of the window, decay can lower the
    estimate towards the new window peak.  A role is calibrated when its prior
    is positive or it has produced a valid sample (for the decode branch that
    means a positive TPS peak: an all-zero list carries no capacity evidence).
    """

    def __init__(
        self,
        alpha: float,
        decay: float,
        prior: float,
        window_cycles: int,
        *,
        is_prefill: bool,
    ) -> None:
        self._is_prefill = is_prefill
        self._decay = decay
        self._prior = prior
        self._has_prior = prior > 0.0
        self._seen = False
        self._calibrated = self._has_prior
        self._ema = _Ema(alpha)
        self._peak = prior
        self._samples: deque[float] = deque(maxlen=window_cycles)
        self._last_tokens: float | None = None
        self._last_prefill_time: float | None = None
        if self._has_prior:
            self._ema.update(prior)

    def update(self, snapshot: RoleSnapshot) -> None:
        """Consume one role snapshot and update the capacity estimate."""
        self._seen = True
        if self._is_prefill:
            self._update_prefill(snapshot)
        else:
            self._update_decode(snapshot)

    def _update_prefill(self, snapshot: RoleSnapshot) -> None:
        sample = None
        if self._last_tokens is not None and self._last_prefill_time is not None:
            token_delta = snapshot.tokens_total - self._last_tokens
            time_delta = snapshot.prefill_time_sum - self._last_prefill_time
            if token_delta > 0.0 and time_delta > 0.0:
                sample = token_delta / time_delta
        self._last_tokens = snapshot.tokens_total
        self._last_prefill_time = snapshot.prefill_time_sum
        if sample is not None:
            self._ema.update(sample)
            self._calibrated = True

    def _update_decode(self, snapshot: RoleSnapshot) -> None:
        if not snapshot.instance_tps:
            return
        sample = max(snapshot.instance_tps)
        # An all-zero TPS list carries no capacity evidence: record the sample
        # but do not count it as calibration.
        if sample > 0.0:
            self._calibrated = True
        self._samples.append(sample)
        if sample > self._peak:
            self._peak = sample
        else:
            floor = max(self._prior, *self._samples)
            self._peak = max(self._peak * (1.0 - self._decay), floor)

    def capacity_tps(self) -> float | None:
        """Current capacity estimate in tokens per second.

        Returns None when the role has never appeared in any snapshot and has no
        prior, 0.0 when it has been observed but has no valid sample yet.
        """
        if not self._seen and not self._has_prior:
            return None
        if self._is_prefill:
            return self._ema.value if self._ema.value is not None else 0.0
        return self._peak

    def calibrated(self) -> float | None:
        """1.0 when the estimate is calibrated (prior > 0 or a valid sample seen)."""
        if not self._seen and not self._has_prior:
            return None
        return 1.0 if self._calibrated else 0.0


class _KvWaitTracker:
    """Decode-side per-request KV wait time W_kv = mean queue + mean decode time.

    The queue/decode time sum/count pairs are cumulative counters.  Each
    component is re-sampled as Δsum/Δcount only when both of its deltas are
    > 0 (zero increments, or counter drops caused by scale-in, hold the
    component's previous mean).  A snapshot produces a W_kv sample
    (mean_queue + mean_decode; a component with no valid sample yet
    contributes 0) whenever at least one component was re-sampled, and the
    sample feeds an EMA.
    """

    def __init__(self, alpha: float) -> None:
        self._ema = _Ema(alpha)
        self._last_queue_sum: float | None = None
        self._last_queue_count: float | None = None
        self._last_decode_sum: float | None = None
        self._last_decode_count: float | None = None
        self._mean_queue = 0.0
        self._mean_decode = 0.0

    def update(self, snapshot: RoleSnapshot) -> None:
        """Consume one decode snapshot and update the W_kv EMA from counter deltas."""
        sampled = False
        if self._last_queue_sum is not None and self._last_queue_count is not None:
            queue_mean = self._component_sample(
                snapshot.queue_time_sum - self._last_queue_sum,
                snapshot.queue_time_count - self._last_queue_count,
            )
            if queue_mean is not None:
                self._mean_queue = queue_mean
                sampled = True
        if self._last_decode_sum is not None and self._last_decode_count is not None:
            decode_mean = self._component_sample(
                snapshot.decode_time_sum - self._last_decode_sum,
                snapshot.decode_time_count - self._last_decode_count,
            )
            if decode_mean is not None:
                self._mean_decode = decode_mean
                sampled = True
        self._last_queue_sum = snapshot.queue_time_sum
        self._last_queue_count = snapshot.queue_time_count
        self._last_decode_sum = snapshot.decode_time_sum
        self._last_decode_count = snapshot.decode_time_count
        if sampled:
            self._ema.update(self._mean_queue + self._mean_decode)

    @staticmethod
    def _component_sample(sum_delta: float, count_delta: float) -> float | None:
        """Per-request mean for one component, or None to hold on a bad delta."""
        if sum_delta > 0.0 and count_delta > 0.0:
            return sum_delta / count_delta
        return None

    def wait_seconds(self) -> float | None:
        """Current EMA of W_kv in seconds, or None if no sample has been seen."""
        return self._ema.value


class CapacityPlanner:
    """HPA capacity planner fed by periodic counter snapshots."""

    def __init__(self, config: PlannerConfig) -> None:
        self._config = config
        self._prefill_tracker = _DemandTracker(config.ema_alpha)
        self._decode_tracker = _DemandTracker(config.ema_alpha)
        self._prefill_capacity = _CapacityEstimator(
            config.ema_alpha,
            config.capacity_decay,
            config.prefill_tps_capacity_prior,
            config.capacity_window_cycles,
            is_prefill=True,
        )
        self._decode_capacity = _CapacityEstimator(
            config.ema_alpha,
            config.capacity_decay,
            config.decode_tps_capacity_prior,
            config.capacity_window_cycles,
            is_prefill=False,
        )
        self._prefill_instances = 0
        self._decode_instances = 0
        self._decode_kv_tokens_per_instance = 0.0
        self._kv_wait = _KvWaitTracker(config.ema_alpha)

    def update(self, snapshot: PlannerSnapshot) -> None:
        """Consume one planning-interval snapshot for the prefill/decode roles.

        A role absent from the snapshot (no instances reporting) resets its
        active-instance count to 0 so utilization is not emitted against a
        stale N.
        """
        if snapshot.prefill is not None:
            self._prefill_tracker.update(snapshot.prefill, snapshot.dt)
            self._prefill_capacity.update(snapshot.prefill)
            self._prefill_instances = snapshot.prefill.active_instances
        else:
            self._prefill_instances = 0
        if snapshot.decode is not None:
            self._decode_tracker.update(snapshot.decode, snapshot.dt)
            self._decode_capacity.update(snapshot.decode)
            self._decode_instances = snapshot.decode.active_instances
            # Sticky K: it is static per instance (num_gpu_blocks × block_size),
            # so a cycle where no decode instance exposes cache_config_info must
            # not drop the last known value (would flicker the KV constraint).
            if snapshot.decode.kv_tokens_per_instance_decode > 0.0:
                self._decode_kv_tokens_per_instance = snapshot.decode.kv_tokens_per_instance_decode
            self._kv_wait.update(snapshot.decode)
        else:
            self._decode_instances = 0

    def compute(self) -> dict[str, float]:
        """Compute the demand-side, supply-side, and replica-derivation outputs.

        Demand keys ``prefill_demand_tps`` / ``decode_demand_tps`` (= EMA(λ) ×
        EMA(L̄) per role) and supply keys ``*_capacity_tps`` /
        ``capacity_calibrated_*`` (1.0/0.0) are absent when the role has never
        appeared in any snapshot and has no prior, and 0.0 when the role has
        been observed but no valid sample exists yet.

        ``prefill_replicas_required`` / ``decode_replicas_required`` are always
        emitted (0.0 when uncalibrated or demand-free): N_p = ceil(D_p /
        (C_p · ρ)), 0 when C_p <= 0; N_d = max over the throughput constraint
        ceil(D_d / (C_d · ρ)) and, when the decode role reports
        kv_tokens_per_instance_decode (K) > 0, the KV
        constraint ceil(D_kv / (K · ρ_kv)) with D_kv = λ_d · W_kv ·
        (L̄_in + L̄_out).  ``pd_ratio_required_raw`` = N_p / N_d is emitted
        only when both role capacities are calibrated and N_d >= 1; otherwise
        it is omitted so downstream consumers fall back to the current ratio.
        ``kv_demand_tokens`` is
        emitted only when K > 0.  ``*_utilization`` = D / (N_current · C) is
        emitted only when C > 0 and the role has active instances.
        """
        out: dict[str, float] = {}
        prefill_demand = self._prefill_tracker.demand_tps()
        if prefill_demand is not None:
            out["prefill_demand_tps"] = prefill_demand
        decode_demand = self._decode_tracker.demand_tps()
        if decode_demand is not None:
            out["decode_demand_tps"] = decode_demand
        prefill_capacity = self._prefill_capacity.capacity_tps()
        if prefill_capacity is not None:
            out["prefill_capacity_tps"] = prefill_capacity
            out["capacity_calibrated_prefill"] = self._prefill_capacity.calibrated()
        decode_capacity = self._decode_capacity.capacity_tps()
        if decode_capacity is not None:
            out["decode_capacity_tps"] = decode_capacity
            out["capacity_calibrated_decode"] = self._decode_capacity.calibrated()

        prefill_required = self._required_by_throughput(prefill_demand, prefill_capacity)
        decode_required_tp = self._required_by_throughput(decode_demand, decode_capacity)
        decode_required_kv = 0
        if self._decode_kv_tokens_per_instance > 0.0:
            kv_demand = self._kv_demand_tokens()
            out["kv_demand_tokens"] = kv_demand
            decode_required_kv = math.ceil(
                kv_demand / (self._decode_kv_tokens_per_instance * self._config.kv_target_utilization)
            )
        decode_required = max(decode_required_tp, decode_required_kv)
        out["prefill_replicas_required"] = float(prefill_required)
        out["decode_replicas_required"] = float(decode_required)
        # Emit the ratio only when the derivation is meaningful: both role
        # capacities calibrated and at least one decode instance required.
        # Otherwise the key is omitted so consumers fall back to the current
        # ratio instead of acting on a meaningless (zero) suggestion.
        if self._prefill_capacity.calibrated() and self._decode_capacity.calibrated() and decode_required >= 1:
            out["pd_ratio_required_raw"] = prefill_required / decode_required

        prefill_utilization = self._utilization(prefill_demand, prefill_capacity, self._prefill_instances)
        if prefill_utilization is not None:
            out["prefill_utilization"] = prefill_utilization
        decode_utilization = self._utilization(decode_demand, decode_capacity, self._decode_instances)
        if decode_utilization is not None:
            out["decode_utilization"] = decode_utilization
        return out

    def _required_by_throughput(self, demand: float | None, capacity: float | None) -> int:
        """Replicas required by the throughput constraint; 0 when C <= 0 or unknown."""
        if demand is None or capacity is None or capacity <= 0.0:
            return 0
        return math.ceil(demand / (capacity * self._config.target_utilization))

    def _kv_demand_tokens(self) -> float:
        """KV token demand: EMA(λ_d) · EMA(W_kv) · (EMA(L̄_in) + EMA(L̄_out))."""
        wait_kv = self._kv_wait.wait_seconds() or 0.0
        mean_total_length = self._prefill_tracker.mean_length() + self._decode_tracker.mean_length()
        return self._decode_tracker.arrival_rate() * wait_kv * mean_total_length

    @staticmethod
    def _utilization(demand: float | None, capacity: float | None, active_instances: int) -> float | None:
        """D / (N_current · C), or None when C <= 0 or no instance is active."""
        if demand is None or capacity is None or capacity <= 0.0 or active_instances <= 0:
            return None
        return demand / (active_instances * capacity)
