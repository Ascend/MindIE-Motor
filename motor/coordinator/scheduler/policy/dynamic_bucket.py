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
from collections.abc import Iterable
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


class DynamicBucketSelector:
    """Choose the short or long bucket from request length and current bucket loads."""

    def __init__(
        self,
        short_bucket_median: int = 16 * 1024,
        long_bucket_median: int = 96 * 1024,
        bucket_border: int = 32 * 1024,
        length_scale: float = 1.0,
        load_scale: float = 4.0,
        short_bucket_count: int = 0,
        long_bucket_count: int = 0,
    ) -> None:
        if load_scale <= 0.0:
            raise ValueError("load_scale must be positive")
        if short_bucket_median <= 0 or long_bucket_median <= bucket_border:
            raise ValueError("invalid dynamic bucket length settings")
        if short_bucket_count < 0 or long_bucket_count < 0:
            raise ValueError("dynamic bucket counts must be non-negative")

        self.bucket_border = bucket_border
        self.short_bucket_median = short_bucket_median
        self.long_bucket_median = long_bucket_median
        self.short_bucket_count = short_bucket_count
        self.long_bucket_count = long_bucket_count
        self.short_bucket_load = 0.0
        self.long_bucket_load = 0.0
        self.length_scale = length_scale
        self.load_scale = load_scale
        self.short_load_threshold = 1.0
        self.long_load_threshold = 1.0 / load_scale
        self.long_bucket_size = long_bucket_median - bucket_border
        self.short_bucket_size = short_bucket_median
        self.long_length_scale = length_scale / self.long_bucket_size
        self.short_length_scale = length_scale / self.short_bucket_size

    def _sigmoid_affinity(self, x: float) -> float:
        """Calculate 1 / (1 + exp(x)) without overflowing for large positive x."""
        if x >= 0:
            exp_neg_x = math.exp(-x)
            return exp_neg_x / (1.0 + exp_neg_x)
        return 1.0 / (1.0 + math.exp(x))

    def _length_affinity(self, distance: float, bucket_size: float, length_scale: float) -> float:
        return self._sigmoid_affinity(length_scale * (distance - bucket_size))

    def _load_affinity(self, load_rate: float, load_threshold: float) -> float:
        """Use load_scale for the sigmoid slope and load_threshold for its midpoint."""
        return self._sigmoid_affinity(self.load_scale * (load_rate - load_threshold))

    def select_bucket(self, length: int) -> int:
        """Return 0 for the short bucket or 1 for the long bucket."""
        if length <= self.bucket_border:
            selected_bucket = 0
            distance = length
            bucket_size = self.short_bucket_size
            length_scale = self.short_length_scale
            load_rate = self.long_bucket_load / self.short_bucket_load if self.short_bucket_load > 0 else math.inf
            load_threshold = self.long_load_threshold
        else:
            selected_bucket = 1
            distance = length - self.bucket_border
            bucket_size = self.long_bucket_size
            length_scale = self.long_length_scale
            load_rate = self.short_bucket_load / self.long_bucket_load if self.long_bucket_load > 0 else math.inf
            load_threshold = self.short_load_threshold

        length_affinity = self._length_affinity(distance, bucket_size, length_scale)
        load_affinity = self._load_affinity(load_rate, load_threshold)
        if length_affinity + load_affinity > 1.0:
            return 1 - selected_bucket
        return selected_bucket

    def update_bucket_loads(self, short_loads: Iterable[float], long_loads: Iterable[float]) -> None:
        """Update the average active-token load for both buckets."""
        short_list = list(short_loads)
        long_list = list(long_loads)
        if short_list:
            self.short_bucket_load = sum(short_list) / len(short_list)
        if long_list:
            self.long_bucket_load = sum(long_list) / len(long_list)


class DynamicBucketPolicy(BaseSchedulingPolicy):
    """Route decode requests to dynamic long/short sequence buckets."""

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
        selector: DynamicBucketSelector,
    ) -> tuple[Instance, Endpoint] | None:
        candidates = DynamicBucketPolicy.build_bucket_endpoints(
            instances,
            selector.short_bucket_count,
            selector.long_bucket_count,
        )
        if not candidates:
            return None

        selector.update_bucket_loads(
            (item.endpoint.workload.active_tokens for item in candidates if item.bucket == 0),
            (item.endpoint.workload.active_tokens for item in candidates if item.bucket == 1),
        )
        target_bucket = selector.select_bucket(req_length)
        selected = DynamicBucketPolicy.select_lightest(candidates, target_bucket)
        if selected is None:
            selected = DynamicBucketPolicy.select_lightest(candidates, 1 - target_bucket)
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
    def build_bucket_endpoints(
        instances: Iterable[Instance],
        configured_short_count: int = 0,
        configured_long_count: int = 0,
    ) -> list[BucketEndpoint]:
        """Assign deterministically ordered endpoints to the configured bucket ratio."""
        pairs = [(instance, endpoint) for instance in instances for endpoint in instance.get_all_endpoints()]
        pairs.sort(key=lambda item: (item[0].id, item[1].id))
        if not pairs:
            return []
        short_count = DynamicBucketPolicy.resolve_short_bucket_count(
            len(pairs),
            configured_short_count,
            configured_long_count,
        )
        return [
            BucketEndpoint(instance=instance, endpoint=endpoint, bucket=0 if index < short_count else 1)
            for index, (instance, endpoint) in enumerate(pairs)
        ]

    @staticmethod
    def resolve_short_bucket_count(
        total_count: int,
        configured_short_count: int,
        configured_long_count: int,
    ) -> int:
        """Resolve a non-empty short/long split whenever at least two endpoints exist."""
        if total_count <= 1:
            return total_count
        if configured_short_count <= 0 and configured_long_count <= 0:
            return max(1, total_count // 2)
        if configured_short_count > 0 and configured_long_count > 0:
            configured_total = configured_short_count + configured_long_count
            short_count = round(total_count * configured_short_count / configured_total)
        elif configured_short_count > 0:
            short_count = configured_short_count
        else:
            short_count = total_count - configured_long_count
        return min(max(1, short_count), total_count - 1)

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
