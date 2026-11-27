# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Registry of Motor's trusted, in-process endpoint selection plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
import time
from types import MappingProxyType
from typing import TYPE_CHECKING

from motor.common.logger import get_logger
from motor.common.resources.endpoint import Endpoint, Workload
from motor.common.resources.instance import Instance, PDRole
from motor.config.coordinator import KV_AFFINITY_MODE_UNIFIED
from motor.coordinator.domain.workload_calculator import calculate_demand_workload
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.scheduler.allocate_arbitration import log_load_balance_all_candidates
from motor.coordinator.scheduler.policy.dynamic_bucket import (
    DynamicBucketPolicy,
    get_request_token_length,
    make_dynamic_bucket_allocation_workload,
)
from motor.coordinator.scheduler.policy.kv_cache_affinity import KvCacheAffinityPolicy
from motor.coordinator.scheduler.policy.load_balance import LoadBalancePolicy
from motor.coordinator.scheduler.policy.round_robin import RoundRobinPolicy
from motor.coordinator.scheduler.runtime.kv_usage import get_endpoint_kv_cache_usage
from motor.coordinator.scheduler.runtime.scheduling_obs_metrics import get_scheduling_obs_metrics
from motor.coordinator.scheduler.runtime.zmq_protocol import (
    CANDIDATE_POLICY_DYNAMIC_BUCKET,
    CANDIDATE_POLICY_KV_CACHE_AFFINITY,
    CANDIDATE_POLICY_LOAD_BALANCE,
    CANDIDATE_POLICY_ROUND_ROBIN,
)

if TYPE_CHECKING:
    from motor.coordinator.scheduler.runtime.scheduler_client import AsyncSchedulerClient

logger = get_logger(__name__)
_AFFINITY_ROLES = frozenset({PDRole.ROLE_P, PDRole.ROLE_U})
_AFFINITY_CANDIDATE_TOPK = 3

# These plugins share the Worker's scheduling state for compatibility with its
# CAS allocation path. Customer plugins only receive the public snapshot DTO.
# pylint: disable=protected-access
CandidateList = list[tuple[Instance, Endpoint, float]]
RankResult = tuple[CandidateList, str]
AllocationCandidates = list[dict[str, int | float]]


class BuiltinEndpointPolicy(ABC):
    """Rank live Worker candidates and declare the native allocation behavior."""

    name: str

    @abstractmethod
    def rank(
        self, client: AsyncSchedulerClient, instances: list[Instance], role: PDRole, req_info: RequestInfo, top_k: int
    ) -> RankResult:
        """Return candidate instances and endpoints and the policy that ranked them."""

    def prepare_candidates(
        self,
        client: AsyncSchedulerClient,
        candidates: CandidateList,
        role: PDRole,
        req_info: RequestInfo,
        required_engine_type: str | None,
        required_dispatch_capability: str | None,
    ) -> tuple[AllocationCandidates, bool]:
        """Project candidates into the records used by core CAS arbitration."""
        return [
            {"instance_id": instance.id, "endpoint_id": endpoint.id} for instance, endpoint, _score in candidates
        ], False

    def calculate_demand(self, role: PDRole, req_info: RequestInfo) -> Workload:
        return calculate_demand_workload(role, req_info)

    def candidate_count(self, client: AsyncSchedulerClient, role: PDRole) -> int:
        return 1

    def select_endpoint(self, client: AsyncSchedulerClient, instance: Instance) -> Endpoint | None:
        """Select an endpoint after the fallback path chose an instance."""
        endpoints = instance.get_all_endpoints()
        if not endpoints:
            return None
        return LoadBalancePolicy.select_endpoint_from_instance(instance) or endpoints[0]


class _RoundRobin(BuiltinEndpointPolicy):
    name = CANDIDATE_POLICY_ROUND_ROBIN

    def rank(
        self, client: AsyncSchedulerClient, instances: list[Instance], role: PDRole, req_info: RequestInfo, top_k: int
    ) -> RankResult:
        offset = (len(instances) * client._client_index) // client._client_count
        counter = client._instance_rr_counters.get(role, 0)
        instance, next_counter = RoundRobinPolicy.select_instance_from_list(instances, counter + offset)
        client._instance_rr_counters[role] = next_counter - offset
        selected = client._select_endpoint_for_instance(instance, role) if instance else None
        if selected is None:
            return [], self.name
        instance, endpoint = selected
        return [(instance, endpoint, 0.0)], self.name

    def calculate_demand(self, role: PDRole, req_info: RequestInfo) -> Workload:
        return Workload()

    def select_endpoint(self, client: AsyncSchedulerClient, instance: Instance) -> Endpoint | None:
        if not instance.get_all_endpoints():
            return None
        return RoundRobinPolicy.select_endpoint_from_instance(
            instance, client._endpoint_rr_counters, is_blocked=client.is_instance_blocked
        )


class _LoadBalance(BuiltinEndpointPolicy):
    name = CANDIDATE_POLICY_LOAD_BALANCE

    def rank(
        self, client: AsyncSchedulerClient, instances: list[Instance], role: PDRole, req_info: RequestInfo, top_k: int
    ) -> RankResult:
        return self.rank_with_fallback(client, instances, role, req_info, top_k, req_id=req_info.req_id)

    def rank_with_fallback(
        self,
        client: AsyncSchedulerClient,
        instances: list[Instance],
        role: PDRole,
        req_info: RequestInfo,
        top_k: int,
        *,
        req_id: str | None,
    ) -> RankResult:
        t0 = time.perf_counter()
        candidates = self._rank_candidates(client, instances, role, top_k, req_id=req_id)
        if candidates:
            return candidates, self.name
        logger.warning("load_balance unavailable, falling back to round-robin")
        get_scheduling_obs_metrics().record_degradation(role, "load_balance_to_round_robin", time.perf_counter() - t0)
        return _ROUND_ROBIN.rank(client, instances, role, req_info, top_k)

    @staticmethod
    def _rank_candidates(
        client: AsyncSchedulerClient,
        instances: list[Instance],
        role: PDRole,
        top_k: int = 1,
        *,
        req_id: str | None = None,
    ) -> list[tuple[Instance, Endpoint, float]]:
        # Equal-score (tie) requests are broken uniformly at random by the policy itself, so
        # equal-load requests spread across nodes without a per-client rotation offset.
        log_load_balance_all_candidates(
            instances,
            role,
            req_id,
            is_blocked=client.is_instance_blocked,
            instance_score_weight=client._endpoint_instance_score_weight,
            exclude_highest_kv_usage=(role == PDRole.ROLE_D),
            kv_usage_provider=get_endpoint_kv_cache_usage if role == PDRole.ROLE_D else None,
        )
        candidates = LoadBalancePolicy.select_endpoint_candidates_from_list(
            instances,
            role,
            top_k=max(1, top_k),
            instance_score_weight=client._endpoint_instance_score_weight,
            is_blocked=client.is_instance_blocked,
            # D 实例：评分上叠加 KV usage 惩罚项，避免调度到 KV cache 快满的实例。
            exclude_highest_kv_usage=(role == PDRole.ROLE_D),
            kv_usage_provider=get_endpoint_kv_cache_usage if role == PDRole.ROLE_D else None,
        )
        return [(candidate.instance, candidate.endpoint, candidate.score) for candidate in candidates]


class _KvCacheAffinity(BuiltinEndpointPolicy):
    name = CANDIDATE_POLICY_KV_CACHE_AFFINITY

    def candidate_count(self, client: AsyncSchedulerClient, role: PDRole) -> int:
        if role in _AFFINITY_ROLES and client._kv_affinity_mode != KV_AFFINITY_MODE_UNIFIED:
            return _AFFINITY_CANDIDATE_TOPK
        return 1

    def rank(
        self, client: AsyncSchedulerClient, instances: list[Instance], role: PDRole, req_info: RequestInfo, top_k: int
    ) -> RankResult:
        if role in _AFFINITY_ROLES:
            t0 = time.perf_counter()
            ranked = KvCacheAffinityPolicy.select_endpoint_candidates_from_list(
                instances,
                req_info,
                mode=client._kv_affinity_mode,
                overlap_credit=client._kv_affinity_overlap_credit,
                prefill_load_scale=client._kv_affinity_prefill_load_scale,
                load_weight=client._kv_affinity_load_weight,
                load_gate_topn=client._kv_affinity_load_gate_topn,
                w_npu=client._kv_affinity_w_npu,
                w_cpu=client._kv_affinity_w_cpu,
                w_disk=client._kv_affinity_w_disk,
                hit_rate_threshold=client._kv_affinity_hit_rate_threshold,
                top_k=max(1, top_k),
            )
            if ranked:
                return ranked, self.name
            elapsed = time.perf_counter() - t0
            if ranked is None:
                logger.warning("kv_cache_affinity unavailable (no conductor match), falling back to load_balance")
                get_scheduling_obs_metrics().record_degradation(role, "kv_conductor_unavailable", elapsed)
            else:
                get_scheduling_obs_metrics().record_degradation(role, "kv_hit_rate_below_threshold", elapsed)
        return _LOAD_BALANCE.rank(client, instances, role, req_info, top_k)

    def prepare_candidates(
        self,
        client: AsyncSchedulerClient,
        candidates: CandidateList,
        role: PDRole,
        req_info: RequestInfo,
        required_engine_type: str | None,
        required_dispatch_capability: str | None,
    ) -> tuple[AllocationCandidates, bool]:
        affinity_debug = getattr(req_info, "kv_affinity_debug", None)
        if not isinstance(affinity_debug, dict):
            return super().prepare_candidates(
                client, candidates, role, req_info, required_engine_type, required_dispatch_capability
            )
        if any(rec[2] is not None for rec in affinity_debug.values()):
            allowed_instance_ids = {
                instance.id
                for instance in client._filter_instances(
                    client._cache.get_instances(role), required_engine_type, required_dispatch_capability
                )
            }
            return [
                {"instance_id": ins_id, "endpoint_id": ep_id, "matched_tokens": rec[0], "prefill_cost": rec[2]}
                for (ins_id, ep_id), rec in affinity_debug.items()
                if rec[2] is not None and ins_id in allowed_instance_ids
            ], True

        records: AllocationCandidates = []
        for instance, endpoint, _score in candidates:
            item: dict[str, int | float] = {"instance_id": instance.id, "endpoint_id": endpoint.id}
            rec = affinity_debug.get((instance.id, endpoint.id))
            if rec is not None:
                item["matched_tokens"] = rec[0]
            records.append(item)
        return records, False


class _DynamicBucket(BuiltinEndpointPolicy):
    name = CANDIDATE_POLICY_DYNAMIC_BUCKET

    def rank(
        self, client: AsyncSchedulerClient, instances: list[Instance], role: PDRole, req_info: RequestInfo, top_k: int
    ) -> RankResult:
        if role != PDRole.ROLE_E:
            req_length = get_request_token_length(req_info)
            eligible_instances = [instance for instance in instances if not client.is_instance_blocked(instance.id)]
            if req_length is not None:
                selected = DynamicBucketPolicy.select_endpoint_from_list(
                    eligible_instances, req_length, client._dynamic_bucket_selector
                )
                if selected is not None:
                    instance, endpoint = selected
                    return [(instance, endpoint, 0.0)], self.name
            logger.warning("dynamic_bucket unavailable req_id=%s, falling back to load_balance", req_info.req_id)
        return _LOAD_BALANCE.rank_with_fallback(client, instances, role, req_info, top_k, req_id=None)

    def prepare_candidates(
        self,
        client: AsyncSchedulerClient,
        candidates: CandidateList,
        role: PDRole,
        req_info: RequestInfo,
        required_engine_type: str | None,
        required_dispatch_capability: str | None,
    ) -> tuple[AllocationCandidates, bool]:
        """Keep the selected bucket available for CAS retries against live workload."""
        instance, endpoint, _score = candidates[0]
        instance_id, endpoint_id = instance.id, endpoint.id
        role_instances = client._filter_instances(
            client._cache.get_instances(role),
            required_engine_type,
            required_dispatch_capability,
        )
        bucket_endpoints = DynamicBucketPolicy.build_bucket_endpoints_for_selector(
            [instance for instance in role_instances if not client.is_instance_blocked(instance.id)],
            client._dynamic_bucket_selector,
        )
        selected_bucket = next(
            (
                item.bucket
                for item in bucket_endpoints
                if item.instance.id == instance_id and item.endpoint.id == endpoint_id
            ),
            None,
        )
        if selected_bucket is None:
            return [{"instance_id": instance_id, "endpoint_id": endpoint_id}], False
        return [
            {"instance_id": item.instance.id, "endpoint_id": item.endpoint.id}
            for item in bucket_endpoints
            if item.bucket == selected_bucket
        ], False

    def calculate_demand(self, role: PDRole, req_info: RequestInfo) -> Workload:
        if role == PDRole.ROLE_E:
            return calculate_demand_workload(role, req_info)
        return make_dynamic_bucket_allocation_workload(req_info)


_ROUND_ROBIN = _RoundRobin()
_LOAD_BALANCE = _LoadBalance()
_BUILTIN_POLICIES = MappingProxyType(
    {policy.name: policy for policy in (_ROUND_ROBIN, _LOAD_BALANCE, _KvCacheAffinity(), _DynamicBucket())}
)


def get_builtin_endpoint_policy(name: str) -> BuiltinEndpointPolicy:
    """Resolve a Worker plugin by scheduler type, preserving the default round-robin path."""
    return _BUILTIN_POLICIES.get(name, _ROUND_ROBIN)
