# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""KV Conductor feature normalization for scheduling policy plugins."""

from __future__ import annotations

from motor.common.resources.instance import Instance
from motor.common.logger import get_logger
from motor.coordinator.api_client.conductor_api_client import (
    ConductorApiClient,
    TENANT_ID,
    conductor_instance_id,
)
from motor.coordinator.scheduler.policy.api import CandidateId, KvMatch
from motor.coordinator.scheduler.policy.kv_cache_affinity import KvCacheAffinityPolicy

logger = get_logger(__name__)


class KvFeatureProvider:
    """Query KV Conductor once per request and normalize per-candidate KvMatch."""

    def __init__(
        self,
        overlap_credit: float = 1.0,
        w_npu: float = 1.0,
        w_cpu: float = 1.0,
        w_disk: float = 0.0,
    ) -> None:
        self._overlap_credit = max(0.0, float(overlap_credit))
        self._w_npu = max(0.0, float(w_npu))
        self._w_cpu = max(0.0, float(w_cpu))
        self._w_disk = max(0.0, float(w_disk))

    def build(
        self,
        token_ids: list[int],
        instances: list[Instance],
        candidate_ids: list[CandidateId],
    ) -> tuple[dict[CandidateId, KvMatch], bool]:
        """Return (kv_match_by_candidate, kv_available)."""
        if not instances or not candidate_ids:
            return {}, False
        prompt_tokens = len(token_ids)
        block_size = KvCacheAffinityPolicy._conductor_block_size()
        if block_size > 0 and prompt_tokens < block_size:
            tenant = {conductor_instance_id(inst): {"DP": {}} for inst in instances}
            kv_available = True
        else:
            try:
                rsp = ConductorApiClient.query_conductor(instances, token_ids)
            except Exception as exc:
                logger.warning("KvFeatureProvider conductor query failed: %s", exc)
                return {}, False
            tenant = rsp.get(TENANT_ID)
            if tenant is None:
                logger.warning(
                    "KvFeatureProvider conductor query returned no tenant data tenant_id=%s instances=%d",
                    TENANT_ID,
                    len(instances),
                )
                return {}, False
            kv_available = True
        matches: dict[CandidateId, KvMatch] = {}
        for candidate_id in candidate_ids:
            instance = next((inst for inst in instances if inst.id == candidate_id.instance_id), None)
            if instance is None:
                continue
            instance_data = tenant.get(conductor_instance_id(instance))
            if instance_data is None:
                continue
            dp_map = instance_data.get("DP", {})
            matched_raw = dp_map.get("%s" % candidate_id.endpoint_id, 0)
            matched_tokens = KvCacheAffinityPolicy._weighted_matched_tokens(
                matched_raw,
                block_size,
                self._w_npu,
                self._w_cpu,
                self._w_disk,
            )
            matched_tokens = min(matched_tokens, prompt_tokens) if prompt_tokens > 0 else 0
            prefill_cost = max(0.0, float(prompt_tokens) - self._overlap_credit * matched_tokens)
            hit_ratio = (matched_tokens / prompt_tokens) if prompt_tokens > 0 else 0.0
            npu_blocks = cpu_blocks = disk_blocks = None
            if isinstance(matched_raw, dict):
                npu_blocks = matched_raw.get("npu_blocks")
                cpu_blocks = matched_raw.get("cpu_blocks")
                disk_blocks = matched_raw.get("disk_blocks")
            matches[candidate_id] = KvMatch(
                matched_tokens=matched_tokens,
                prefill_cost=prefill_cost,
                hit_ratio=hit_ratio,
                npu_blocks=int(npu_blocks) if npu_blocks is not None else None,
                cpu_blocks=int(cpu_blocks) if cpu_blocks is not None else None,
                disk_blocks=int(disk_blocks) if disk_blocks is not None else None,
            )
        return matches, kv_available
