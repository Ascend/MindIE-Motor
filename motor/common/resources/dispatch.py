# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from enum import Enum
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, field_validator


MOTOR_DISPATCH_KEY = "_motor_dispatch"
MOTOR_PREFILL_RESULT_KEY = "_motor_prefill_result"
MOTOR_DISPATCH_SCHEMA_VERSION = "1.0"


DispatchRole = Literal["prefill", "decode", "single"]
PrefillStatus = Literal["prepared", "completed", "skipped"]
PrefillMode = Literal["trigger", "handoff", "bootstrap"]


class DispatchPlan(str, Enum):
    """Coordinator dispatch execution plan for P/D separated inference."""

    CONCURRENT_ENGINE_SYNC = "concurrent_engine_sync"
    PREFILL_HANDOFF_DECODE = "prefill_handoff_decode"


DISPATCH_PROFILE_KEY = "dispatch_profile"
KV_TRANSFER_CONFIG_KEY = "kv_transfer_config"
KV_CONNECTOR_KEY = "kv_connector"
KV_CONNECTOR_EXTRA_CONFIG_KEY = "kv_connector_extra_config"
KV_CONNECTORS_KEY = "connectors"


class DispatchProfile(str, Enum):
    """Engine-side P/D coordination profile inferred from kv_transfer configuration."""

    TRIGGER = "trigger"
    HANDOFF = "handoff"
    BOOTSTRAP = "bootstrap"
    UNKNOWN = "unknown"


_VLLM_HANDOFF_CONNECTORS = frozenset(
    {
        "mooncakeconnectorv1",
        "mooncakehybridconnector",
        "nixlconnector",
    }
)
_VLLM_TRIGGER_CONNECTORS = frozenset({"mooncakelayerwiseconnector"})


def classify_vllm_dispatch_profile(
    engine_config: Any,
    explicit_profile: str | None = None,
) -> DispatchProfile:
    """Classify vLLM P/D coordination semantics from explicit config or whitelist."""
    profile = _parse_explicit_profile(explicit_profile or _config_get(engine_config, DISPATCH_PROFILE_KEY))
    if profile is not None:
        return profile

    kv_transfer_config = _config_get(engine_config, KV_TRANSFER_CONFIG_KEY, {})
    return _classify_vllm_kv_transfer_config(kv_transfer_config)


def infer_vllm_dispatch_profile_from_config(config: Any) -> DispatchProfile:
    """Resolve vLLM dispatch profile from an engine-server IConfig-like object."""
    get_endpoint_config = getattr(config, "get_endpoint_config", None)
    if get_endpoint_config is None:
        return DispatchProfile.UNKNOWN

    endpoint_config = get_endpoint_config()
    if endpoint_config is None:
        return DispatchProfile.UNKNOWN

    if _normalized(getattr(endpoint_config, "engine_type", None)) != "vllm":
        return DispatchProfile.UNKNOWN

    deploy_config = getattr(endpoint_config, "deploy_config", None)
    if deploy_config is None:
        return DispatchProfile.UNKNOWN

    engine_config = getattr(deploy_config, "engine_config", None)
    explicit_profile = getattr(deploy_config, "dispatch_profile", None)
    return classify_vllm_dispatch_profile(engine_config, explicit_profile=explicit_profile)


def dispatch_capabilities_for_profile(profile: DispatchProfile) -> list[str]:
    if profile == DispatchProfile.HANDOFF:
        return [DispatchPlan.PREFILL_HANDOFF_DECODE.value]
    if profile in (DispatchProfile.TRIGGER, DispatchProfile.BOOTSTRAP):
        return [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]
    return []


def dispatch_plans_from_capabilities(values: Iterable[object] | None) -> set[DispatchPlan]:
    """Normalize advertised capability values into supported dispatch plans."""
    plans: set[DispatchPlan] = set()
    if values is None:
        return plans
    if isinstance(values, (str, bytes)):
        values = (values,)
    try:
        iterator = iter(values)
    except TypeError:
        return plans
    for value in iterator:
        try:
            plans.add(DispatchPlan(str(value)))
        except ValueError:
            continue
    return plans


def shared_dispatch_plans(prefill: Any, decode: Any) -> set[DispatchPlan]:
    """Return dispatch plans advertised by both instances."""
    prefill_plans = dispatch_plans_from_capabilities(getattr(prefill, "dispatch_capabilities", None))
    decode_plans = dispatch_plans_from_capabilities(getattr(decode, "dispatch_capabilities", None))
    return prefill_plans & decode_plans


def dispatch_plan_union(instances: Iterable[Any]) -> set[DispatchPlan]:
    """Union of dispatch plans advertised across instances; parses each instance once."""
    union: set[DispatchPlan] = set()
    for instance in instances:
        union |= dispatch_plans_from_capabilities(getattr(instance, "dispatch_capabilities", None))
    return union


def has_compatible_dispatch_pair(prefill_instances: Iterable[Any], decode_instances: Iterable[Any]) -> bool:
    """Whether at least one P/D instance pair advertises a shared dispatch plan.

    A shared plan exists iff the prefill and decode plan unions intersect, so this
    runs in O(P+D) without enumerating instance pairs.
    """
    return bool(dispatch_plan_union(prefill_instances) & dispatch_plan_union(decode_instances))


def _classify_vllm_kv_transfer_config(kv_transfer_config: Any) -> DispatchProfile:
    if not isinstance(kv_transfer_config, dict):
        return DispatchProfile.UNKNOWN

    connector = _normalized(kv_transfer_config.get(KV_CONNECTOR_KEY))
    if connector == "multiconnector":
        return _classify_vllm_multi_connector(kv_transfer_config)

    if connector in _VLLM_HANDOFF_CONNECTORS:
        return DispatchProfile.HANDOFF
    if connector in _VLLM_TRIGGER_CONNECTORS:
        return DispatchProfile.TRIGGER
    return DispatchProfile.UNKNOWN


def _classify_vllm_multi_connector(kv_transfer_config: dict[str, Any]) -> DispatchProfile:
    extra_config = kv_transfer_config.get(KV_CONNECTOR_EXTRA_CONFIG_KEY, {})
    if not isinstance(extra_config, dict):
        return DispatchProfile.UNKNOWN
    connectors = extra_config.get(KV_CONNECTORS_KEY, [])
    if not isinstance(connectors, list) or len(connectors) < 2:
        return DispatchProfile.UNKNOWN

    transport_connector = connectors[0]
    if not isinstance(transport_connector, dict):
        return DispatchProfile.UNKNOWN
    return _classify_vllm_kv_transfer_config(transport_connector)


def _parse_explicit_profile(value: Any) -> DispatchProfile | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized == DispatchPlan.PREFILL_HANDOFF_DECODE.value:
        return DispatchProfile.HANDOFF
    if normalized == DispatchPlan.CONCURRENT_ENGINE_SYNC.value:
        return DispatchProfile.TRIGGER
    try:
        profile = DispatchProfile(normalized)
    except ValueError as exc:
        allowed = ", ".join(profile.value for profile in DispatchProfile if profile != DispatchProfile.UNKNOWN)
        raise ValueError(f"Unsupported dispatch_profile {value!r}. Allowed values: {allowed}.") from exc
    if profile == DispatchProfile.UNKNOWN:
        return None
    return profile


def _config_get(config: Any, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    getter = getattr(config, "get", None)
    if getter is not None:
        return getter(key, default)
    return getattr(config, key, default)


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower()


class DispatchEndpoint(BaseModel):
    """Network location of a scheduled engine endpoint for cross-engine dispatch."""

    instance_id: int = Field(..., ge=0, description="Scheduler instance identifier for the target engine")
    endpoint_id: int = Field(..., ge=0, description="Endpoint identifier within the instance")
    url: str = Field(..., min_length=1, description="Base HTTP URL for dispatch and stop calls to the engine")


class DispatchEndpoints(BaseModel):
    """Paired prefill and decode endpoints for a single P/D dispatch attempt."""

    prefill: DispatchEndpoint | None = Field(
        default=None,
        description="Prefill engine endpoint; omitted for decode-only or single-node roles",
    )
    decode: DispatchEndpoint | None = Field(
        default=None,
        description="Decode engine endpoint; omitted for prefill-only or single-node roles",
    )


class MotorDispatch(BaseModel):
    """Motor metadata embedded in inference request bodies under ``_motor_dispatch``."""

    schema_version: str = Field(
        default=MOTOR_DISPATCH_SCHEMA_VERSION,
        description="Dispatch envelope schema version; major version must match coordinator support",
    )
    root_request_id: str = Field(..., min_length=1, description="Client-visible request id assigned by the coordinator")
    engine_request_id: str = Field(
        ...,
        min_length=1,
        description="Per-attempt engine request id, typically ``{root_request_id}#a{attempt_seq}``",
    )
    pair_id: str = Field(..., min_length=1, description="Stable id linking prefill and decode peers for one attempt")
    attempt_seq: int = Field(..., ge=1, description="Monotonic attempt index within a root request, starting at 1")
    role: DispatchRole = Field(..., description="Engine role handling this request: prefill, decode, or single")
    dispatch_mode: str = Field(
        ...,
        min_length=1,
        description="Coordinator dispatch plan name, e.g. concurrent_engine_sync or prefill_handoff_decode",
    )
    endpoints: DispatchEndpoints = Field(
        ...,
        description="Peer endpoint addresses used for stop signals and handoff coordination",
    )

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        major = value.split(".", 1)[0]
        supported_major = MOTOR_DISPATCH_SCHEMA_VERSION.split(".", 1)[0]
        if major != supported_major:
            raise ValueError(f"Unsupported motor dispatch schema major version: {value}")
        return value


class PrefillResultStatus(str, Enum):
    """Lifecycle status of a prefill handoff result."""

    PREPARED = "prepared"
    COMPLETED = "completed"
    SKIPPED = "skipped"


class PrefillHandoffMode(str, Enum):
    """KV handoff mechanism used between prefill and decode engines."""

    TRIGGER = "trigger"
    HANDOFF = "handoff"
    BOOTSTRAP = "bootstrap"


class PrefillResult(BaseModel):
    """Prefill output envelope embedded under ``_motor_prefill_result`` for handoff decode."""

    object: str = Field(default="motor.prefill_result", description="Object type discriminator for prefill results")
    schema_version: str = Field(
        default=MOTOR_DISPATCH_SCHEMA_VERSION,
        description="Prefill result schema version; major version must match coordinator support",
    )
    root_request_id: str = Field(..., min_length=1, description="Client-visible request id assigned by the coordinator")
    engine_request_id: str = Field(
        ...,
        min_length=1,
        description="Per-attempt engine request id correlated with the paired MotorDispatch",
    )
    pair_id: str = Field(..., min_length=1, description="Stable id linking prefill and decode peers for one attempt")
    attempt_seq: int = Field(..., ge=1, description="Monotonic attempt index within a root request, starting at 1")
    status: PrefillStatus = Field(..., description="Whether prefill was prepared, completed, or skipped")
    handoff_mode: PrefillMode = Field(..., description="Trigger, handoff, or bootstrap coordination mode")
    payload: dict = Field(default_factory=dict, description="Engine-specific prefill handoff data, e.g. KV handles")
    usage: dict | None = Field(
        default=None,
        description=(
            "Prefill usage block (carries prompt_tokens_details for cached-token reporting); "
            "kept separate from payload because payload is consumed verbatim as kv_transfer_params"
        ),
    )
    expires_at_ms: int | None = Field(
        default=None,
        ge=0,
        description="Optional Unix timestamp in milliseconds after which the cached result is stale",
    )

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, value: str) -> str:
        major = value.split(".", 1)[0]
        supported_major = MOTOR_DISPATCH_SCHEMA_VERSION.split(".", 1)[0]
        if major != supported_major:
            raise ValueError(f"Unsupported motor prefill result schema major version: {value}")
        return value

    def matches_dispatch(self, dispatch: MotorDispatch) -> bool:
        return (
            self.root_request_id == dispatch.root_request_id
            and self.pair_id == dispatch.pair_id
            and self.attempt_seq == dispatch.attempt_seq
        )


class DispatchStopReason(str, Enum):
    """Reason the coordinator asked a peer engine to stop an in-flight dispatch attempt."""

    PEER_FAILED = "peer_failed"
    CLIENT_DISCONNECT = "client_disconnect"
    TIMEOUT = "timeout"
    RECOMPUTE = "recompute"
    RETRY_REPAIR = "retry_repair"
    OTHER = "other"


class DispatchStopState(str, Enum):
    """Outcome of a ``/v1/dispatch/stop`` request."""

    STOPPED = "stopped"
    ALREADY_STOPPED = "already_stopped"
    ALREADY_DONE = "already_done"
    NOT_FOUND = "not_found"
    STALE = "stale"


class DispatchStopRequest(BaseModel):
    """Request body for coordinator-initiated peer engine stop."""

    root_request_id: str = Field(..., min_length=1, description="Client-visible request id assigned by the coordinator")
    engine_request_id: str | None = Field(
        default=None,
        description="Optional per-attempt engine request id for finer-grained stop matching",
    )
    attempt_seq: int = Field(..., ge=1, description="Attempt index within the root request to stop")
    pair_id: str = Field(..., min_length=1, description="Pair id linking the prefill and decode peers for the attempt")
    reason: str = Field(
        default=DispatchStopReason.OTHER.value,
        description="Why the stop was requested; normalized via normalized_reason()",
    )
    sent_at_ms: int | None = Field(
        default=None,
        ge=0,
        description="Optional Unix timestamp in milliseconds when the stop request was sent",
    )

    def normalized_reason(self) -> DispatchStopReason:
        try:
            return DispatchStopReason(self.reason)
        except ValueError:
            return DispatchStopReason.OTHER


class DispatchStopResponse(BaseModel):
    """Response body confirming whether a dispatch stop was accepted."""

    root_request_id: str = Field(..., description="Client-visible request id echoed from the stop request")
    attempt_seq: int = Field(..., description="Attempt index echoed from the stop request")
    accepted: bool = Field(..., description="Whether the engine accepted and processed the stop request")
    state: DispatchStopState = Field(..., description="Current attempt state after processing the stop request")
    message: str = Field(default="", description="Optional human-readable detail about the stop outcome")
