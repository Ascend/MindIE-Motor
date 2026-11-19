# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Wire models for the coordinator-unified profiling API.

The target/result dimensions deliberately mirror the Coordinator metrics labels:
``instance_id`` / ``role`` / ``dp_rank`` (and ``pod_ip`` in the ``dp`` view).
``service`` carries the instance ``model_name``, i.e. the model service name.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from motor.common.resources.instance import PDRole

# Historical / user-facing aliases for the role filter.
_ROLE_ALIASES: dict[str, str] = {"both": PDRole.ROLE_U.value, "hybrid": PDRole.ROLE_U.value}
_ALLOWED_ROLES: tuple[str, ...] = tuple(role.value for role in PDRole)
_MAX_SELECTOR_ITEMS = 4096


def canonical_role(value: str) -> str:
    """Canonicalize role aliases for comparison without changing response labels."""
    candidate = value.strip().lower()
    return _ROLE_ALIASES.get(candidate, candidate)


def normalize_role(value: str) -> str:
    """Normalize one role filter value and reject unknown roles."""
    candidate = canonical_role(str(value))
    if candidate not in _ALLOWED_ROLES:
        raise ValueError(f"unsupported role {value!r}; allowed values: {', '.join(_ALLOWED_ROLES)}")
    return candidate


def _dedupe_ints(values: list[Any], field_name: str) -> list[int]:
    """Validate a non-empty list of non-negative integer ids, preserving order."""
    if not isinstance(values, list) or not values:
        raise ValueError(f"{field_name} must be a non-empty list of non-negative integers")
    if len(values) > _MAX_SELECTOR_ITEMS:
        raise ValueError(f"{field_name} must contain at most {_MAX_SELECTOR_ITEMS} entries")
    result: list[int] = []
    seen: set[int] = set()
    for item in values:
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise ValueError(f"{field_name} must contain non-negative integers")
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


class ProfilingAction(str, Enum):
    """Profiling control action."""

    START = "start"
    STOP = "stop"


class ProfilingTargetStatus(str, Enum):
    """Per-target profiling request outcome."""

    STARTED = "started"
    STOPPED = "stopped"
    FAILED = "failed"


class ProfilingTargetSelector(BaseModel):
    """Target selection shared by start and stop.

    All filters are optional and combine with AND:
    ``service`` (model service name), ``roles`` (P/D role), ``instances``
    (instance ids) and ``dps`` (DP ranks, either for every selected instance or
    per instance id). Omitting all filters selects every routable DP.
    """

    # Reject unknown top-level keys: a misspelled selector (e.g. "role" instead of
    # "roles") must fail with 400 instead of silently widening the fan-out to all DPs.
    model_config = ConfigDict(extra="forbid")

    service: str | None = Field(default=None, description="Model service name (instance model_name)")
    roles: list[str] | None = Field(default=None, description="Role filter: prefill/decode/union/encode")
    instances: list[int] | None = Field(default=None, description="Instance id filter")
    dps: list[int] | dict[int, list[int]] | None = Field(
        default=None,
        description="DP rank filter: a rank list for all selected instances, or instance_id -> rank list",
    )

    @field_validator("service")
    @classmethod
    def _validate_service(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("service must be a non-empty string")
        return value.strip()

    @field_validator("roles", mode="before")
    @classmethod
    def _validate_roles(cls, value: Any) -> list[str] | None:
        if value is None:
            return None
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, list) or not items:
            raise ValueError("roles must be a non-empty list of role names")
        normalized: list[str] = []
        for item in items:
            role = normalize_role(item)
            if role not in normalized:
                normalized.append(role)
        return normalized

    @field_validator("instances", mode="before")
    @classmethod
    def _validate_instances(cls, value: Any) -> list[int] | None:
        if value is None:
            return None
        return _dedupe_ints(value, "instances")

    @field_validator("dps", mode="before")
    @classmethod
    def _validate_dps(cls, value: Any) -> list[int] | dict[int, list[int]] | None:
        if value is None:
            return None
        if isinstance(value, list):
            return _dedupe_ints(value, "dps")
        if isinstance(value, dict):
            if not value:
                raise ValueError("dps must not be an empty object")
            if len(value) > _MAX_SELECTOR_ITEMS:
                raise ValueError(f"dps must contain at most {_MAX_SELECTOR_ITEMS} entries")
            ranks_by_instance: dict[int, list[int]] = {}
            rank_count = 0
            for raw_instance_id, ranks in value.items():
                if isinstance(ranks, list):
                    rank_count += len(ranks)
                    if rank_count > _MAX_SELECTOR_ITEMS:
                        raise ValueError(f"dps must contain at most {_MAX_SELECTOR_ITEMS} total rank entries")
                if isinstance(raw_instance_id, bool):
                    raise ValueError("dps keys must be instance ids")
                try:
                    instance_id = int(raw_instance_id)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"dps key {raw_instance_id!r} is not an instance id") from exc
                if instance_id < 0:
                    raise ValueError(f"dps key {instance_id} must be a non-negative instance id")
                ranks_by_instance[instance_id] = _dedupe_ints(ranks, f"dps[{instance_id}]")
            return ranks_by_instance
        raise ValueError("dps must be a list of DP ranks or an object mapping instance ids to DP rank lists")


class ProfilingStartRequest(ProfilingTargetSelector):
    """Start profiling on the selected targets."""

    profiler_options: dict[str, Any] | None = Field(
        default=None,
        description="Optional engine profiler options forwarded with /start_profile",
    )

    @field_validator("profiler_options")
    @classmethod
    def _validate_profiler_options(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("profiler_options must be an object")
        for key in value:
            if not isinstance(key, str) or not key:
                raise ValueError("profiler_options keys must be non-empty strings")
        return value


class ProfilingTarget(BaseModel):
    """One concrete profiling target, labelled like a metrics DP sample."""

    service: str = Field(description="Model service name, i.e. instance model_name")
    instance_id: int = Field(description="Instance id (metrics label instance_id)")
    role: str = Field(description="P/D role (metrics label role)")
    dp_rank: int = Field(description="DP rank (metrics label dp_rank)")
    pod_ip: str = Field(description="Pod IP of the DP endpoint")
    address: str = Field(description="Inference endpoint host:port")


class ProfilingTargetResult(ProfilingTarget):
    """Profiling request outcome for one target."""

    status: ProfilingTargetStatus = Field(description="Per-target outcome")
    detail: str = Field(default="", description="Failure detail when the outcome is not successful")
    profile_prefix: str = Field(default="", description="Dimension-tagged profile prefix used for the result files")


class ProfilingResponse(BaseModel):
    """Result of one start or stop request."""

    action: ProfilingAction
    service: str
    targets: list[ProfilingTargetResult]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def succeeded(self) -> int:
        """Number of targets for which the requested action succeeded."""
        success = (
            ProfilingTargetStatus.STARTED if self.action == ProfilingAction.START else ProfilingTargetStatus.STOPPED
        )
        return sum(1 for target in self.targets if target.status == success)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def failed(self) -> int:
        """Number of targets for which the requested action did not succeed."""
        return len(self.targets) - self.succeeded
