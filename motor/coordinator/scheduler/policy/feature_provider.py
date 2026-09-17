# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Build immutable SelectionInput snapshots from instances and SHM."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import Instance, PDRole
from motor.coordinator.api_client.conductor_api_client import KVA_ELIGIBLE_ROLES
from motor.coordinator.models.request import RequestInfo
from motor.coordinator.scheduler.policy.api import (
    CandidateId,
    CandidateSnapshot,
    RequestContext,
    SelectionInput,
)
from motor.coordinator.scheduler.policy.kv_feature_provider import KvFeatureProvider
from motor.coordinator.scheduler.runtime.workload_shm.layout import FLAG_BLOCKED


@dataclass
class KvFeatureCache:
    """Caller-owned KV snapshot reused across CAS retries of one allocate."""

    matches: dict[CandidateId, object] | None = None
    available: bool = False


class PolicyContextBuilder:
    """Build SelectionInput from request, instance cache, and workload SHM."""

    def __init__(
        self,
        *,
        is_instance_blocked: Callable[[int], bool],
        kv_provider: KvFeatureProvider | None = None,
    ) -> None:
        self._is_instance_blocked = is_instance_blocked
        self._kv_provider = kv_provider

    def build(
        self,
        req_info: RequestInfo,
        role: PDRole,
        instances: list[Instance],
        *,
        excluded: frozenset[CandidateId],
        attempt: int,
        required_engine_type: str | None,
        required_dispatch_capability: str | None,
        workload_reader,
        policy_requires_kv: bool,
        kv_cache: KvFeatureCache | None = None,
    ) -> SelectionInput:
        normalized_engine = str(required_engine_type or "").strip().lower()
        normalized_capability = str(required_dispatch_capability or "").strip()
        filtered = [
            inst
            for inst in instances
            if (not normalized_engine or str(getattr(inst, "engine_type", "")).strip().lower() == normalized_engine)
            and (
                not normalized_capability
                or normalized_capability in (getattr(inst, "dispatch_capabilities", None) or [])
            )
        ]
        candidates: list[CandidateSnapshot] = []
        candidate_ids: list[CandidateId] = []
        for instance in filtered:
            if self._is_instance_blocked(instance.id):
                continue
            endpoints = instance.get_all_endpoints()
            if not endpoints:
                continue
            instance_total = 0.0
            endpoint_tokens: dict[int, float] = {}
            for endpoint in endpoints:
                active = self._read_active_tokens(workload_reader, instance.id, endpoint.id, endpoint)
                endpoint_tokens[endpoint.id] = active
                instance_total += active
            endpoint_count = max(1, len(endpoints))
            for endpoint in endpoints:
                cid = CandidateId(instance_id=instance.id, endpoint_id=endpoint.id)
                blocked = self._is_endpoint_blocked(workload_reader, instance.id, endpoint.id)
                candidates.append(
                    CandidateSnapshot(
                        id=cid,
                        active_tokens=endpoint_tokens[endpoint.id],
                        instance_active_tokens=instance_total,
                        blocked=blocked,
                        attributes={"endpoint_count": str(endpoint_count)},
                    )
                )
                candidate_ids.append(cid)
        kv_available = False
        kv_matches = {}
        should_query_kv = (
            policy_requires_kv and self._kv_provider is not None and bool(candidate_ids) and role in KVA_ELIGIBLE_ROLES
        )
        if should_query_kv:
            cache = kv_cache if kv_cache is not None else KvFeatureCache()
            if cache.matches is None:
                token_ids = getattr(req_info, "token_ids", None)
                if not isinstance(token_ids, list):
                    token_ids = []
                cache.matches, cache.available = self._kv_provider.build(
                    token_ids,
                    filtered,
                    candidate_ids,
                )
            kv_matches = cache.matches or {}
            kv_available = cache.available
        if kv_matches:
            enriched: list[CandidateSnapshot] = []
            for candidate in candidates:
                kv_match = kv_matches.get(candidate.id)
                enriched.append(
                    CandidateSnapshot(
                        id=candidate.id,
                        active_tokens=candidate.active_tokens,
                        instance_active_tokens=candidate.instance_active_tokens,
                        blocked=candidate.blocked,
                        kv_match=kv_match,
                        attributes=candidate.attributes,
                    )
                )
            candidates = enriched
        token_ids = getattr(req_info, "token_ids", None)
        prompt_tokens = len(token_ids) if isinstance(token_ids, list) else 0
        max_output = req_info.req_data.get("max_tokens") or req_info.req_data.get("max_completion_tokens")
        request_ctx = RequestContext(
            request_id=req_info.req_id,
            role=role,
            model_name=str(getattr(req_info, "model_name", "") or req_info.req_data.get("model", "")),
            prompt_tokens=prompt_tokens,
            max_output_tokens=max_output if isinstance(max_output, int) else None,
            required_engine_type=normalized_engine or None,
        )
        return SelectionInput(
            request=request_ctx,
            candidates=tuple(candidates),
            excluded=excluded,
            attempt=attempt,
            kv_available=kv_available,
        )

    @staticmethod
    def _read_active_tokens(workload_reader, instance_id: int, endpoint_id: int, endpoint: Endpoint) -> float:
        if workload_reader is not None:
            meta = workload_reader.entry_meta(instance_id, endpoint_id)
            if meta is not None:
                return float(meta.get("active_tokens", endpoint.workload.active_tokens))
        return float(endpoint.workload.active_tokens)

    @staticmethod
    def _is_endpoint_blocked(workload_reader, instance_id: int, endpoint_id: int) -> bool:
        if workload_reader is None:
            return False
        meta = workload_reader.entry_meta(instance_id, endpoint_id)
        if meta is None:
            return False
        return bool(int(meta.get("flags", 0)) & FLAG_BLOCKED)

    @staticmethod
    def resolve_instance_endpoint(
        instances: list[Instance],
        candidate_id: CandidateId,
    ) -> tuple[Instance, Endpoint] | None:
        for instance in instances:
            if instance.id != candidate_id.instance_id:
                continue
            for endpoint in instance.get_all_endpoints():
                if endpoint.id == candidate_id.endpoint_id:
                    return instance, endpoint
        return None
