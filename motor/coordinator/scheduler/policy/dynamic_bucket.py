# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from motor.common.logger import get_logger
from motor.common.resources.endpoint import Endpoint, Workload
from motor.common.resources.instance import Instance, PDRole
from motor.coordinator.domain import InstanceProvider
from motor.coordinator.models.constants import OpenAIField
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.scheduler.policy.base import BaseSchedulingPolicy
from motor.coordinator.scheduler.policy.kv_cache_affinity import TokenizerManager
from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy


logger = get_logger(__name__)


@dataclass(frozen=True)
class BucketEndpoint:
    """Endpoint assigned to one of the dynamic sequence-length buckets."""

    instance: Instance
    endpoint: Endpoint
    bucket: int


class _BucketAffinityScorer:
    """Shared sigmoid affinity formulas used by the bucket selector."""

    @staticmethod
    def _sigmoid_affinity(x: float) -> float:
        """Calculate 1 / (1 + exp(x)) without overflowing for large positive x."""
        if x >= 0:
            exp_neg_x = math.exp(-x)
            return exp_neg_x / (1.0 + exp_neg_x)
        return 1.0 / (1.0 + math.exp(x))

    def _length_affinity(self, distance: float, bucket_size: float, length_scale: float) -> float:
        return self._sigmoid_affinity(length_scale * (distance - bucket_size))

    def _load_affinity(self, load_rate: float, load_threshold: float, load_scale: float) -> float:
        """Use the destination bucket's scale for the sigmoid slope and load_threshold for its midpoint."""
        return self._sigmoid_affinity(load_scale * (load_rate - load_threshold))


class MultiBucketSelector(_BucketAffinityScorer):
    """Choose among N configurable sequence-length buckets.

    Boundaries are inclusive upper bounds in tokens. With boundaries ``[32768, 65536]``
    the buckets are ``[0, 32768]``, ``(32768, 65536]`` and ``(65536, +inf)``. Each bucket
    has a representative length (median) used by the length affinity, and a configured
    instance count/ratio used by the stable instance split.
    """

    def __init__(
        self,
        boundaries: Sequence[int],
        medians: Sequence[int] | None = None,
        counts: Sequence[int] | None = None,
        length_scales: Sequence[float] | None = None,
        load_scales: Sequence[float] | None = None,
        legacy_layout: bool = False,
    ) -> None:
        if not boundaries:
            raise ValueError("multi-bucket mode requires at least one boundary")

        normalized_boundaries: list[int] = []
        previous_boundary = 0
        for boundary in boundaries:
            if isinstance(boundary, bool) or not isinstance(boundary, int) or boundary <= previous_boundary:
                raise ValueError("dynamic bucket boundaries must be strictly increasing positive integers")
            normalized_boundaries.append(boundary)
            previous_boundary = boundary

        bucket_total = len(normalized_boundaries) + 1
        if legacy_layout and bucket_total != 2:
            raise ValueError("legacy dynamic bucket layout requires exactly two buckets")
        lower_bounds = [0] + normalized_boundaries
        upper_bounds: list[int | None] = list(normalized_boundaries) + [None]

        if medians is None or len(medians) == 0:
            resolved_medians = self._default_medians(lower_bounds, upper_bounds)
        else:
            if len(medians) != bucket_total:
                raise ValueError("dynamic bucket medians must contain one value per bucket")
            resolved_medians = []
            previous_median = 0
            for index, median in enumerate(medians):
                if isinstance(median, bool) or not isinstance(median, int):
                    raise ValueError("dynamic bucket medians must be integers")
                if median <= lower_bounds[index] or (not legacy_layout and median <= previous_median):
                    raise ValueError("dynamic bucket medians must be greater than the bucket lower bound")
                resolved_medians.append(median)
                previous_median = median

        if counts is None or len(counts) == 0:
            configured_counts = [0] * bucket_total
        else:
            if len(counts) != bucket_total:
                raise ValueError("dynamic bucket counts must contain one value per bucket")
            configured_counts = []
            for count in counts:
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise ValueError("dynamic bucket counts must be non-negative integers")
                configured_counts.append(count)

        resolved_length_scales = self._resolve_scales(length_scales, bucket_total, 1.0, "length_scales")
        resolved_load_scales = self._resolve_scales(load_scales, bucket_total, 4.0, "load_scales")

        self.boundaries = tuple(normalized_boundaries)
        self.medians = tuple(resolved_medians)
        self.counts = tuple(configured_counts)
        self.legacy_layout = legacy_layout
        self.length_scales = tuple(resolved_length_scales)
        self.load_scales = tuple(resolved_load_scales)
        self.short_load_threshold = 1.0
        self.long_load_thresholds = tuple(1.0 / scale for scale in self.load_scales)
        self._lower_bounds = lower_bounds
        self._loads: list[float | None] = [None] * bucket_total

    @staticmethod
    def _default_medians(lower_bounds: list[int], upper_bounds: list[int | None]) -> list[int]:
        """Derive one representative length per bucket when the user does not configure them.

        Bounded buckets use the interval midpoint. The open-ended last bucket keeps the
        historical long-bucket span: twice the previous bucket width. With the default
        single 32K boundary this yields medians ``[16K, 96K]``, matching the legacy
        two-bucket selector.
        """
        medians: list[int] = []
        for index, lower in enumerate(lower_bounds):
            upper = upper_bounds[index]
            if upper is not None:
                medians.append(max(lower + 1, (lower + upper) // 2))
                continue
            previous_lower = lower_bounds[index - 1]
            medians.append(lower + max(1, 2 * (lower - previous_lower)))
        return medians

    @staticmethod
    def _resolve_scales(
        values: Sequence[float] | None,
        bucket_total: int,
        default: float,
        name: str,
    ) -> list[float]:
        """Broadcast the default or validate one positive scale per bucket."""
        if values is None:
            return [default] * bucket_total
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ValueError(f"dynamic bucket {name} must be a list of finite positive numbers")
        if len(values) == 0:
            return [default] * bucket_total
        if len(values) != bucket_total:
            raise ValueError(f"dynamic bucket {name} must contain one value per bucket")
        resolved: list[float] = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"dynamic bucket {name} must contain numbers")
            numeric = float(value)
            if numeric <= 0.0 or not math.isfinite(numeric):
                raise ValueError(f"dynamic bucket {name} values must be finite and positive")
            resolved.append(numeric)
        return resolved

    @property
    def bucket_count(self) -> int:
        """Number of configured buckets (boundaries plus the open-ended last bucket)."""
        return len(self.boundaries) + 1

    def bucket_of_length(self, length: int) -> int:
        """Return the initially selected bucket before load-based switching."""
        index = bisect_left(self.boundaries, length)
        return min(max(index, 0), self.bucket_count - 1)

    def resolve_bucket_counts(self, total_count: int) -> list[int]:
        """Resolve per-bucket instance counts for the current pool.

        Config counts are used as exact counts when their sum equals the pool size,
        otherwise as ratio weights on the whole pool, rounded by largest remainder.
        Migrated legacy layouts retain fixed single-side counts and historical rounding.
        ``total_count`` is the number of instances (the
        caller groups every endpoint of one instance into a single pool slot).
        Every bucket gets at least one instance whenever the pool is at least as
        large as the bucket count; a smaller pool fills the lowest buckets first
        and leaves the remaining buckets empty. Even-split leftovers go to the
        higher-index buckets, so the default two-bucket layout keeps the legacy
        ``short = floor(N / 2)`` split.
        """
        bucket_total = self.bucket_count
        if total_count <= 0:
            return [0] * bucket_total
        if total_count < bucket_total:
            return [1] * total_count + [0] * (bucket_total - total_count)

        weights = list(self.counts)
        if self.legacy_layout and any(weights):
            if all(weights):
                short_count = round(total_count * weights[0] / sum(weights))
            elif weights[0] > 0:
                short_count = weights[0]
            else:
                short_count = total_count - weights[1]
            short_count = min(max(1, short_count), total_count - 1)
            return [short_count, total_count - short_count]
        if not any(weights):
            weights = [1] * bucket_total
        total_weight = sum(weights)
        if total_weight == total_count and all(weight > 0 for weight in weights):
            return weights

        counts = [total_count * weight // total_weight for weight in weights]
        leftover = total_count - sum(counts)
        order = sorted(
            range(bucket_total),
            key=lambda index: (-(total_count * weights[index] % total_weight), -index),
        )
        for index in order[:leftover]:
            counts[index] += 1
        # Enforce the nonempty-bucket floor only when the proportional allocation needs it.
        for index, count in enumerate(counts):
            if count == 0:
                donor = max(
                    (bucket for bucket in range(bucket_total) if counts[bucket] > 1),
                    key=lambda bucket: (counts[bucket] * total_weight - total_count * weights[bucket], counts[bucket]),
                )
                counts[donor] -= 1
                counts[index] = 1
        return counts

    def update_bucket_loads(self, loads: Iterable[float | None]) -> None:
        """Replace the stored average load for every bucket (None means no endpoint)."""
        load_list = list(loads)
        if len(load_list) != self.bucket_count:
            raise ValueError("dynamic bucket loads must contain one value per bucket")
        self._loads = [None if load is None else float(load) for load in load_list]

    def update_loads_from_candidates(self, candidates: Iterable[BucketEndpoint]) -> None:
        """Refresh the stored bucket loads from the current endpoint set."""
        candidate_list = list(candidates)
        loads: list[float | None] = []
        for bucket in range(self.bucket_count):
            bucket_loads = [item.endpoint.workload.active_tokens for item in candidate_list if item.bucket == bucket]
            loads.append(sum(bucket_loads) / len(bucket_loads) if bucket_loads else None)
        self.update_bucket_loads(loads)

    def _length_affinity_for_bucket(self, bucket: int, length: int) -> float:
        lower = self._lower_bounds[bucket]
        span = self.medians[bucket] - lower
        return self._length_affinity(length - lower, span, self.length_scales[bucket] / span)

    def select_bucket(self, length: int) -> int:
        """Return the target bucket after length/load affinity evaluation.

        Only the two adjacent buckets are considered. Keeping the switch local
        preserves length locality while still letting an overloaded bucket hand a
        movable request to its neighbour. The destination bucket's load scale sets
        the sigmoid slope and the longer-direction midpoint, so moving to a longer
        bucket uses ``1 / load_scales[j]`` while moving to a shorter bucket uses
        ``1``.
        """
        initial_bucket = self.bucket_of_length(length)
        current_load = self._loads[initial_bucket]
        if current_load is None:
            return initial_bucket

        length_affinity = self._length_affinity_for_bucket(initial_bucket, length)
        best_bucket = initial_bucket
        best_score = 0.0
        for neighbor in (initial_bucket + 1, initial_bucket - 1):
            if neighbor < 0 or neighbor >= self.bucket_count:
                continue
            neighbor_load = self._loads[neighbor]
            if neighbor_load is None:
                continue
            load_rate = neighbor_load / current_load if current_load > 0 else math.inf
            load_scale = self.load_scales[neighbor]
            load_threshold = (
                self.long_load_thresholds[neighbor] if neighbor > initial_bucket else self.short_load_threshold
            )
            score = length_affinity + self._load_affinity(load_rate, load_threshold, load_scale)
            if score > best_score:
                best_score = score
                best_bucket = neighbor

        if best_bucket != initial_bucket and best_score > 1.0:
            return best_bucket
        return initial_bucket


BucketSelector = MultiBucketSelector


class DynamicBucketPolicy(BaseSchedulingPolicy):
    """Route requests to dynamic sequence-length buckets."""

    def __init__(self, instance_provider: InstanceProvider):
        super().__init__(instance_provider=instance_provider)
        logger.info("DynamicBucketPolicy started.")

    def select_instance_and_endpoint(self, role: PDRole = None, req_info: RequestInfo | None = None):
        active_instances = self._instance_provider.get_available_instances(role)
        return self.select_instance_and_endpoint_from_list(list(active_instances.values()), role, req_info)

    def select_instance_and_endpoint_from_list(
        self,
        instances: list[Instance],
        role: PDRole | None = None,
        req_info: RequestInfo | None = None,
    ):
        """Fall back to load balance when called outside the worker-owned selector path."""
        del req_info
        logger.warning("Dynamic bucket selector is worker-owned; falling back to load_balance")
        return LoadBalancePolicy.select_endpoint_from_list(instances, role)

    @staticmethod
    def select_endpoint_from_list(
        instances: Iterable[Instance],
        req_length: int,
        selector: BucketSelector,
    ) -> tuple[Instance, Endpoint] | None:
        candidates = DynamicBucketPolicy.build_bucket_endpoints_for_selector(instances, selector)
        if not candidates:
            return None

        selector.update_loads_from_candidates(candidates)
        target_bucket = selector.select_bucket(req_length)
        selected = DynamicBucketPolicy.select_lightest(candidates, target_bucket)
        if selected is None:
            selected = DynamicBucketPolicy.select_nearest_nonempty(candidates, target_bucket)
        if selected is None:
            return None

        logger.debug(
            "Dynamic bucket selected req_length=%s target_bucket=%s selected=%s-%s loads=%s",
            req_length,
            target_bucket,
            selected.instance.id,
            selected.endpoint.id,
            [
                (item.instance.id, item.endpoint.id, item.bucket, item.endpoint.workload.active_tokens)
                for item in candidates
            ],
        )
        return (selected.instance, selected.endpoint)

    @staticmethod
    def build_bucket_endpoints_for_selector(
        instances: Iterable[Instance],
        selector: BucketSelector,
    ) -> list[BucketEndpoint]:
        """Assign each instance to one bucket; all of its endpoints share that bucket.

        Instances are ordered by ``instance_id``. Splitting by instance keeps MoE /
        DP ranks of the same pod out of mixed long/short buckets.
        """
        instance_groups: dict[int, tuple[Instance, list[Endpoint]]] = {}
        for instance in instances:
            endpoints = sorted(instance.get_all_endpoints(), key=lambda endpoint: endpoint.id)
            if not endpoints:
                continue
            existing = instance_groups.get(instance.id)
            if existing is None:
                instance_groups[instance.id] = (instance, endpoints)
            else:
                existing[1].extend(endpoint for endpoint in endpoints if endpoint not in existing[1])

        ordered_instances = [instance_groups[instance_id] for instance_id in sorted(instance_groups)]
        if not ordered_instances:
            return []

        bucket_counts = selector.resolve_bucket_counts(len(ordered_instances))
        if len(bucket_counts) != selector.bucket_count:
            logger.warning(
                "Dynamic bucket count mismatch buckets=%s resolved=%s",
                selector.bucket_count,
                len(bucket_counts),
            )
            return []

        candidates: list[BucketEndpoint] = []
        instance_index = 0
        for bucket, count in enumerate(bucket_counts):
            for _ in range(count):
                if instance_index >= len(ordered_instances):
                    return candidates
                instance, endpoints = ordered_instances[instance_index]
                for endpoint in endpoints:
                    candidates.append(BucketEndpoint(instance=instance, endpoint=endpoint, bucket=bucket))
                instance_index += 1
        return candidates

    @staticmethod
    def select_lightest(candidates: list[BucketEndpoint], bucket: int) -> BucketEndpoint | None:
        """Pick the lowest active-token endpoint within one bucket."""
        bucket_candidates = [item for item in candidates if item.bucket == bucket]
        if not bucket_candidates:
            return None
        return min(
            bucket_candidates,
            key=lambda item: (
                item.endpoint.workload.active_tokens,
                item.instance.id,
                item.endpoint.id,
            ),
        )

    @staticmethod
    def select_nearest_nonempty(candidates: list[BucketEndpoint], target_bucket: int) -> BucketEndpoint | None:
        """Fall back to the closest bucket that still has an endpoint."""
        available_buckets = sorted(
            {item.bucket for item in candidates},
            key=lambda bucket: (abs(bucket - target_bucket), bucket),
        )
        for bucket in available_buckets:
            selected = DynamicBucketPolicy.select_lightest(candidates, bucket)
            if selected is not None:
                return selected
        return None

    def _select_instance(self, role: PDRole = None) -> Instance | None:
        active_instances = self._instance_provider.get_available_instances(role)
        return LoadBalancePolicy.select_instance_from_list(active_instances.values(), role)

    def _select_endpoint(self, instance: Instance) -> Endpoint | None:
        return LoadBalancePolicy.select_endpoint_from_instance(instance)


def make_dynamic_bucket_allocation_workload(req_info: RequestInfo | None) -> Workload:
    """Account dynamic-bucket requests by their tokenized prompt length."""
    req_length = get_request_token_length(req_info)
    return Workload(active_tokens=req_length) if req_length is not None else Workload()


def _is_token_id_sequence(prompt) -> bool:
    return isinstance(prompt, (list, tuple)) and all(isinstance(token_id, int) for token_id in prompt)


def get_request_token_length(req_info: RequestInfo | None) -> int | None:
    """Return tokenized prompt length for bucket selection and workload accounting."""
    if req_info is None:
        logger.warning("Dynamic bucket tokenization failed, req_info is None")
        return None

    cached_ids = getattr(req_info, "token_ids", None)
    if isinstance(cached_ids, list) and cached_ids:
        return len(cached_ids)

    req_data = req_info.req_data or {}
    messages = req_data.get(OpenAIField.MESSAGES)
    tools = req_data.get(OpenAIField.TOOLS)
    encoded_ids: list[int] = []
    try:
        if messages is not None:
            encoded_ids = TokenizerManager().apply_chat_template(messages, tools, req_data=req_data)
        else:
            prompt = req_data.get(OpenAIField.PROMPT)
            if _is_token_id_sequence(prompt):
                encoded_ids = list(prompt)
            elif prompt is not None:
                encoded_ids = TokenizerManager().encode(prompt)
    except Exception as error:  # pylint: disable=broad-exception-caught
        logger.warning("Dynamic bucket tokenization failed req_id=%s error=%s", req_info.req_id, error)
        return None

    if encoded_ids:
        req_info.token_ids = encoded_ids
        return len(encoded_ids)
    logger.warning(
        "Dynamic bucket tokenization failed req_id=%s keys=%s has_messages=%s has_prompt=%s tokenizer_ready=%s",
        req_info.req_id,
        sorted(str(key) for key in req_data),
        messages is not None,
        req_data.get(OpenAIField.PROMPT) is not None,
        getattr(TokenizerManager(), "tokenizer", None) is not None,
    )
    return None
