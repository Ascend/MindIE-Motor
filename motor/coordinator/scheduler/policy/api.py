# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Public, serializable API for external endpoint selection policies."""

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SUPPORTED_POLICY_API_VERSION = 2


@dataclass(frozen=True, slots=True)
class CandidateId:
    instance_id: int
    endpoint_id: int


@dataclass(frozen=True, slots=True)
class RequestContext:
    request_id: str
    role: str
    model_name: str
    prompt_tokens: int
    max_output_tokens: int | None
    user: str | None = None
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    id: CandidateId
    active_tokens: float
    instance_active_tokens: float
    node_address: str
    engine_type: str
    kv_cache_usage: float | None = None


@dataclass(frozen=True, slots=True)
class SelectionInput:
    request: RequestContext
    candidates: tuple[CandidateSnapshot, ...]
    excluded: frozenset[CandidateId]
    attempt: int


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """An endpoint in plugin priority order, with a finite score for observability."""

    id: CandidateId
    score: float


class EndpointSelectionPolicy(ABC):
    """Return candidates in best-first order. Instances live in an isolated process."""

    api_version = SUPPORTED_POLICY_API_VERSION

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.options = dict(options or {})

    @abstractmethod
    def rank(self, selection: SelectionInput) -> Sequence[RankedCandidate] | None:
        """Return a nonempty subset in priority order, or None to use the built-in policy."""
        raise NotImplementedError


# Keep wheels built against the earlier API v2 name loadable.
LoadBalancingPolicy = EndpointSelectionPolicy
