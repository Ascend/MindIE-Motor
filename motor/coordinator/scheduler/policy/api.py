# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Public DTOs and LoadBalancingPolicy base class for scheduling policy plugins."""

from __future__ import annotations

from abc import ABC
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from motor.common.resources.instance import PDRole

SUPPORTED_POLICY_API_VERSION = 1


@dataclass(frozen=True, slots=True)
class CandidateId:
    instance_id: int
    endpoint_id: int


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    role: PDRole
    model_name: str
    prompt_tokens: int
    max_output_tokens: int | None
    required_engine_type: str | None = None


@dataclass(frozen=True, slots=True)
class KvMatch:
    matched_tokens: int
    prefill_cost: float
    hit_ratio: float
    npu_blocks: int | None = None
    cpu_blocks: int | None = None
    disk_blocks: int | None = None


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    id: CandidateId
    active_tokens: float
    instance_active_tokens: float
    blocked: bool
    kv_match: KvMatch | None = None
    attributes: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class SelectionInput:
    request: RequestContext
    candidates: tuple[CandidateSnapshot, ...]
    excluded: frozenset[CandidateId]
    attempt: int
    kv_available: bool


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    id: CandidateId
    score: float


class LoadBalancingPolicy(ABC):
    """Base class for synchronous, in-process scheduling policies."""

    api_version = 1
    requires_kv_match = False

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    def rank(self, selection: SelectionInput) -> Sequence[RankedCandidate]:
        """Return candidates in best-first order; lower score is better."""
        raise NotImplementedError
