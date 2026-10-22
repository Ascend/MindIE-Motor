# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""AgentHint for the request layer.

Defines the Pydantic schemas that parse and validate the ``agent_hint`` block
on incoming requests — session/parent-session identifiers, cache control,
context management, session control, latency control, and priority control —
and exposes the translator that converts ``CacheControl.msg_offset`` and
``ContextEdit.start/end`` from message-level indices into PagedAttention block
coordinates consumed by the scheduler.
"""

from enum import Enum, auto
import math
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from motor.common.logger import get_logger

logger = get_logger(__name__)

# Header fallback field names (lowercase to match Starlette's header normalization)
HEADER_SESSION_ID = "x-session-id"
HEADER_PARENT_SESSION_ID = "x-parent-session-id"
_AGENT_HINT_KNOWN_FIELDS = frozenset(
    {
        "session_id",
        "parent_session_id",
        "cache_control",
        "context_management",
        "session_control",
        "latency_control",
        "priority_control",
    }
)
_CACHE_TYPE_ALLOWED = frozenset({"ephemeral", "pin"})
_CACHE_TYPE_DEFAULT = "ephemeral"
_CACHE_TTL_DEFAULT = 300
_CACHE_TTL_MIN = 60
_CACHE_TTL_MAX = 3600
_CACHE_MSG_OFFSET_DEFAULT = None
_CACHE_RANGE_UNITS_ALLOWED = frozenset({"message"})
_CACHE_RANGE_UNIT_DEFAULT = "message"
_SESSION_RELATIONS_ALLOWED = frozenset({"fork", "spawn", "handoff"})
_SESSION_JOINS_ALLOWED = frozenset({"sync", "async"})
_SESSION_JOIN_DEFAULT = "sync"
_AGENT_STATES_ALLOWED = frozenset({"running", "tool", "pause", "suspend", "finalize"})
_EDIT_TYPES_ALLOWED = frozenset({"offload", "prefetch", "evict"})
_EDIT_TARGETS_ALLOWED = frozenset({"session", "messages", "tools"})
_EDIT_TARGET_DEFAULT = "session"
_SESSION_CONTROL_TYPES_ALLOWED = frozenset({"start", "pause", "stop", "compact", "resume"})
_SESSION_CONTROL_EDIT_TYPES = {
    "pause": "offload",
    "stop": "evict",
    "compact": "evict",
    "resume": "prefetch",
}
_CACHE_SERVER_ONLY_FIELDS = frozenset({"block_offset", "intra_block_offset", "token_offset"})
_EDIT_SERVER_ONLY_FIELDS = frozenset(
    {
        "block_start",
        "block_intra_start",
        "start_token",
        "block_end",
        "block_intra_end",
        "end_token",
    }
)


class CacheControl(BaseModel):
    """KV cache control.

    Shared model for two sources with different strictness (see
    ``_parse_cache_control`` / ``_parse_spec_cache_control``): the legacy body
    interface only ever produces ``type='ephemeral'`` with ``ttl`` /
    ``msg_offset``, while the spec header (X-Agent-Hint) additionally carries
    ``type='pin'``, ``ttl_s`` and ``range`` (``range_*`` fields; ``unit=message``
    maps onto ``msg_offset``). ``message`` is the only ``range.unit`` the protocol
    defines, so any other unit drops the range (see ``_normalize_spec_range``).
    """

    type: str = Field(default=_CACHE_TYPE_DEFAULT, description="Cache type; 'ephemeral' or 'pin'.")
    ttl: int = Field(default=_CACHE_TTL_DEFAULT, description="Cache TTL in seconds; clamped to [60, 3600].")
    msg_offset: int | None = Field(
        default=_CACHE_MSG_OFFSET_DEFAULT,
        description=(
            "1-based message index at which cache_control takes effect; None means no effect point is "
            "available (e.g. the request carried no messages), so no block coordinate is produced."
        ),
    )
    range_start: int | None = Field(
        default=None,
        description="Effective range start (inclusive); from spec range.start, default 0.",
    )
    range_end: int | None = Field(
        default=None,
        description="Effective range end (exclusive); from spec range.end. For unit=message drives msg_offset.",
    )
    range_unit: str | None = Field(
        default=None,
        description="Effective range unit; 'message' is the only protocol value. None when no range was supplied.",
    )
    # Server-side derived flag (never serialized into the engine payload): ``ttl`` has a
    # spec default of 300, so only this flag distinguishes "the client asked for a
    # retention" from "the client said nothing and we filled the default". The cache
    # lifecycle only acts on explicit retention (design 20261015-03 §7 D1).
    ttl_explicit: bool = Field(
        default=False,
        exclude=True,
        description="True when the client explicitly supplied ttl/ttl_s; derived at parse time.",
    )
    block_offset: int | None = Field(
        default=None,
        description="Block index (token_idx // block_size); filled by attach_block_offsets server-side.",
    )
    intra_block_offset: int | None = Field(
        default=None,
        description="Intra-block offset (token_idx % block_size); filled by attach_block_offsets server-side.",
    )
    token_offset: int | None = Field(
        default=None,
        description="Cumulative token count (= block_offset*block_size + intra_block_offset); filled by attach_block_offsets server-side.",
    )

    @field_validator("type", mode="before")
    @classmethod
    def _validate_type(cls, value: Any) -> str:
        if value is None or (isinstance(value, str) and value == ""):
            return _CACHE_TYPE_DEFAULT
        value = str(value)
        if value not in _CACHE_TYPE_ALLOWED:
            logger.warning(
                "Invalid cache_control.type=%r; dropping cache_control (allowed: %s).",
                value,
                sorted(_CACHE_TYPE_ALLOWED),
            )
            raise ValueError(f"unsupported cache_control.type: {value!r}")
        return value

    @field_validator("ttl", mode="before")
    @classmethod
    def _validate_ttl(cls, value: Any) -> int:
        value = _finite_int(value, "cache_control.ttl", default=None)
        if value is None:
            logger.warning(
                "Invalid cache_control.ttl=%s; using default %s",
                value,
                _CACHE_TTL_DEFAULT,
            )
            return _CACHE_TTL_DEFAULT
        if value < _CACHE_TTL_MIN or value > _CACHE_TTL_MAX:
            clamped = max(_CACHE_TTL_MIN, min(_CACHE_TTL_MAX, value))
            logger.warning(
                "cache_control.ttl=%s out of range [%s, %s]; clamped to %s",
                value,
                _CACHE_TTL_MIN,
                _CACHE_TTL_MAX,
                clamped,
            )
            return clamped
        return value

    @field_validator("msg_offset", mode="before")
    @classmethod
    def _validate_msg_offset(cls, value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid cache_control.msg_offset=%s; treating as unset (None).",
                value,
            )
            return None


class ContextEdit(BaseModel):
    """Context edit operation."""

    type: str = Field(..., description="One of 'offload' / 'prefetch' / 'evict'; must be specified explicitly.")
    start: int | None = Field(default=None, description="Inclusive start msg index; None = 0.")
    end: int | None = Field(
        default=None,
        description="Exclusive end msg index (Python-slice convention: edits cover messages[start:end]); None = len(messages) (include all).",
    )
    target: str = Field(
        default=_EDIT_TARGET_DEFAULT,
        description="What to operate on; 'session' (default) / 'messages' / 'tools'.",
    )
    block_start: int | None = Field(
        default=None,
        description="block_idx for start message index; filled by server.",
    )
    block_intra_start: int | None = Field(
        default=None,
        description="intra_block_offset for start message index; filled by server.",
    )
    start_token: int | None = Field(
        default=None,
        description="token_idx for start message index; filled by server.",
    )
    block_end: int | None = Field(
        default=None,
        description="block_idx for end message index; filled by server.",
    )
    block_intra_end: int | None = Field(
        default=None,
        description="intra_block_offset for end message index; filled by server.",
    )
    end_token: int | None = Field(
        default=None,
        description="token_idx for end message index; filled by server.",
    )

    @field_validator("type", mode="before")
    @classmethod
    def _validate_type(cls, value: Any) -> str:
        value = str(value)
        if value not in _EDIT_TYPES_ALLOWED:
            logger.warning(
                "Unsupported context_edit.type=%r; expected one of %s. Dropping edit.",
                value,
                sorted(_EDIT_TYPES_ALLOWED),
            )
            raise ValueError(f"unsupported context_edit.type: {value!r}")
        return value

    @field_validator("start", mode="before")
    @classmethod
    def _validate_start(cls, value: Any) -> int | None:
        if value is None:
            return None
        value = _finite_int(value, "context_edit.start")
        if value is None:
            logger.warning("Invalid context_edit.start=%s; using default %s", value, None)
            return None
        if value < 0:
            logger.warning("context_edit.start=%s less than %s", value, 0)
        return value

    @field_validator("end", mode="before")
    @classmethod
    def _validate_end(cls, value: Any) -> int | None:
        if value is None:
            return None
        value = _finite_int(value, "context_edit.end")
        if value is None:
            logger.warning("Invalid context_edit.end=%s; using default %s", value, None)
            return None
        if value < 0:
            logger.warning("context_edit.end=%s less than %s", value, 0)
        return value

    @field_validator("target", mode="before")
    @classmethod
    def _validate_target(cls, value: Any) -> str:
        if value is None:
            return _EDIT_TARGET_DEFAULT
        value = str(value)
        if value not in _EDIT_TARGETS_ALLOWED:
            logger.warning(
                "Invalid context_edit.target=%r; using default %r.",
                value,
                _EDIT_TARGET_DEFAULT,
            )
            return _EDIT_TARGET_DEFAULT
        return value


def parse_manage_request(value: Any) -> bool:
    """Parse a raw `manage_request` value into bool.

    Accepts JSON bool, 0/1 integers, and case-insensitive 'true'/'false' strings.
    Any other type falls back to False (with a warning logged).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    logger.warning(
        "Invalid context_management.manage_request=%r; using False",
        value,
    )
    return False


class ContextManagement(BaseModel):
    """Context management."""

    manage_request: bool = Field(
        default=False,
        description=(
            "True if this is a KVC management request; the request body itself "
            "is not executed, only the context edits are processed."
        ),
    )
    edits: list[ContextEdit] = Field(default_factory=list, description="List of context edit operations.")

    @field_validator("manage_request", mode="before")
    @classmethod
    def _validate_manage_request(cls, value: Any) -> bool:
        return parse_manage_request(value)


class SessionControl(BaseModel):
    """Session-level lifecycle control.

    ``type`` is one of 'start' / 'pause' / 'stop' / 'compact' / 'resume'.
    pause / stop / compact / resume are translated into a session-targeted
    context_management manage-request by apply_session_control_autofill;
    'start' carries no context semantics.
    """

    type: str = Field(..., description="One of 'start' / 'pause' / 'stop' / 'compact' / 'resume'.")

    @field_validator("type", mode="before")
    @classmethod
    def _validate_type(cls, value: Any) -> str:
        value = str(value)
        if value not in _SESSION_CONTROL_TYPES_ALLOWED:
            logger.warning(
                "Unsupported session_control.type=%r; expected one of %s. Dropping session_control.",
                value,
                sorted(_SESSION_CONTROL_TYPES_ALLOWED),
            )
            raise ValueError(f"unsupported session_control.type: {value!r}")
        return value


class LatencyControl(BaseModel):
    """Latency/SLO hint (design-only)."""

    latency_sensitivity: int | None = Field(default=None, description="Latency sensitivity hint (ms).")
    # More fields may be added in future versions.


class PriorityControl(BaseModel):
    """Priority hint (design-only)."""

    priority: int | None = Field(default=None, description="Priority hint; higher value means higher priority.")
    reason: str | None = Field(
        default=None, description="Why the level was set, e.g. 'user_waiting' (E3: retained for audit)."
    )
    # More fields may be added in future versions.


class PriorityHint(BaseModel):
    """Spec-form priority hint (``priority`` / ``strict_priority``).

    Stored for forward compatibility only. In particular ``strict_priority``
    carries an unresolved trust boundary (who may inject it) and must not
    drive any scheduling decision until that is settled (Phase 3 decision:
    trust mechanism deferred).
    """

    level: int | None = Field(default=None, description="Priority level; higher value means higher priority.")
    reason: str | None = Field(default=None, description="Reason the level was set, e.g. 'critical_path'.")


class DerivationHint(BaseModel):
    """How a session was derived from its parent (spec ``session.derivation``)."""

    relation: str = Field(..., description="One of 'fork' / 'spawn' / 'handoff'.")
    join: str = Field(
        default=_SESSION_JOIN_DEFAULT,
        description="'sync' (parent waits for the child) or 'async'; default 'sync'.",
    )
    fork_point: int | None = Field(
        default=None,
        description="For 'fork': child inherits parent messages [0, fork_point); None = not applicable.",
    )

    @field_validator("relation", mode="before")
    @classmethod
    def _validate_relation(cls, value: Any) -> str:
        value = str(value)
        if value not in _SESSION_RELATIONS_ALLOWED:
            logger.warning(
                "Invalid session.derivation.relation=%r; expected one of %s. Dropping derivation.",
                value,
                sorted(_SESSION_RELATIONS_ALLOWED),
            )
            raise ValueError(f"unsupported derivation.relation: {value!r}")
        return value

    @field_validator("join", mode="before")
    @classmethod
    def _validate_join(cls, value: Any) -> str:
        if value is None or (isinstance(value, str) and value == ""):
            return _SESSION_JOIN_DEFAULT
        value = str(value)
        if value not in _SESSION_JOINS_ALLOWED:
            logger.warning(
                "Invalid session.derivation.join=%r; expected one of %s. Using default %r.",
                value,
                sorted(_SESSION_JOINS_ALLOWED),
                _SESSION_JOIN_DEFAULT,
            )
            return _SESSION_JOIN_DEFAULT
        return value

    @field_validator("fork_point", mode="before")
    @classmethod
    def _validate_fork_point(cls, value: Any) -> int | None:
        if value is None:
            return None
        value = _finite_int(value, "session.derivation.fork_point")
        if value is None:
            logger.warning("Invalid session.derivation.fork_point=%s; treating as unset (None).", value)
            return None
        if value < 0:
            logger.warning("session.derivation.fork_point=%s less than 0; treating as unset (None).", value)
            return None
        return value


class SessionHint(BaseModel):
    """Spec-form nested ``session`` block.

    session_id / parent_session_id participate in the per-field resolution
    priority (nested > body flat > header fallback); the remaining fields are
    stored for the scheduler (Phase 2 consumes derivation / epoch / call_index).
    """

    session_id: str | None = Field(default=None, description="Session ID; stable within one context.")
    parent_session_id: str | None = Field(default=None, description="Parent session ID; None for a root session.")
    derivation: DerivationHint | None = Field(default=None, description="Derivation relation to the parent session.")
    agent_type: str | None = Field(default=None, description="Agent type identifier.")
    call_index: int | None = Field(default=None, description="Call ordinal within the session, starting at 1.")
    epoch: int | None = Field(
        default=None,
        description="Context version; incremented after context rewrite/compaction.",
    )

    @field_validator("call_index", mode="before")
    @classmethod
    def _validate_call_index(cls, value: Any) -> int | None:
        if value is None:
            return None
        value = _finite_int(value, "session.call_index")
        if value is None:
            logger.warning("Invalid session.call_index=%s; treating as unset (None).", value)
            return None
        if value < 1:
            logger.warning("session.call_index=%s less than 1; treating as unset (None).", value)
            return None
        return value

    @field_validator("epoch", mode="before")
    @classmethod
    def _validate_epoch(cls, value: Any) -> int | None:
        if value is None:
            return None
        value = _finite_int(value, "session.epoch")
        if value is None:
            logger.warning("Invalid session.epoch=%s; treating as unset (None).", value)
            return None
        if value < 0:
            logger.warning("session.epoch=%s less than 0; treating as unset (None).", value)
            return None
        return value


class AgentStatusTool(BaseModel):
    """Tool a ``tool``-state agent is waiting on (spec ``agent_status.tools[]``)."""

    name: str | None = Field(default=None, description="Tool name; 'agent:<agent_type>' when waiting on a child agent.")
    call_id: str | None = Field(default=None, description="Tool call ID.")
    child_session_id: str | None = Field(default=None, description="Child session ID when waiting on a child agent.")


class AgentStatusHint(BaseModel):
    """Agent lifecycle status (spec ``agent_status``); out-of-band event support lands in Phase 4."""

    state: str = Field(..., description="One of 'running' / 'tool' / 'pause' / 'suspend' / 'finalize'.")
    state_since_ms: int | None = Field(default=None, description="Unix ms timestamp when the state was entered.")
    reason: str | None = Field(default=None, description="State reason, e.g. 'await_tool', 'human_approval'.")
    tools: list[AgentStatusTool] = Field(default_factory=list, description="Tools awaited in 'tool' state.")

    @field_validator("state_since_ms", mode="before")
    @classmethod
    def _validate_state_since_ms(cls, value: Any) -> int | None:
        return _finite_int(value, "agent_status.state_since_ms")

    @field_validator("state", mode="before")
    @classmethod
    def _validate_state(cls, value: Any) -> str:
        value = str(value)
        if value not in _AGENT_STATES_ALLOWED:
            logger.warning(
                "Invalid agent_status.state=%r; expected one of %s. Dropping agent_status.",
                value,
                sorted(_AGENT_STATES_ALLOWED),
            )
            raise ValueError(f"unsupported agent_status.state: {value!r}")
        return value


class PredictionTool(BaseModel):
    """Predicted tool invocation (spec ``prediction.next_action.tools[]``)."""

    name: str | None = Field(default=None, description="Predicted tool name.")
    expected_duration_s: int | None = Field(default=None, description="Expected tool duration in seconds.")


class PredictionNextAction(BaseModel):
    """Predicted next action (spec ``prediction.next_action``)."""

    state: str | None = Field(default=None, description="Predicted next state; see agent_status states.")
    tools: list[PredictionTool] = Field(default_factory=list, description="Predicted tools with expected durations.")
    prob: float | None = Field(default=None, description="Probability of the next action, in [0, 1].")

    @field_validator("state", mode="before")
    @classmethod
    def _validate_state(cls, value: Any) -> str | None:
        if value is None:
            return None
        value = str(value)
        if value not in _AGENT_STATES_ALLOWED:
            logger.warning(
                "Invalid prediction.next_action.state=%r; expected one of %s. Dropping next_action.state.",
                value,
                sorted(_AGENT_STATES_ALLOWED),
            )
            return None
        return value

    @field_validator("prob", mode="before")
    @classmethod
    def _validate_prob(cls, value: Any) -> float | None:
        if value is None:
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            logger.warning("Invalid prediction.next_action.prob=%s; treating as unset (None).", value)
            return None
        if not 0.0 <= value <= 1.0:
            logger.warning("prediction.next_action.prob=%s out of [0, 1]; treating as unset (None).", value)
            return None
        return value


class PredictionHint(BaseModel):
    """Statistical prediction of the remaining agent workload (spec ``prediction``).

    Stored for forward compatibility; Phase 5 consumes output_tokens /
    remaining_calls (demand estimation) and interval_ms / next_action
    (predictive warm-up). ``latency_ms`` additionally keeps its existing
    latency_control mapping (wire layer).
    """

    remaining_calls: int | None = Field(default=None, description="Estimated remaining calls after this one.")
    output_tokens: int | None = Field(default=None, description="Estimated output tokens for this call.")
    interval_ms: int | None = Field(
        default=None, description="Estimated gap between this call's end and the next start."
    )
    latency_ms: int | None = Field(default=None, description="Estimated duration of this call.")
    remaining_time_ms: int | None = Field(default=None, description="Estimated remaining time until the session ends.")
    expected_resume_s: int | None = Field(
        default=None,
        description="Estimated seconds until resume after 'pause' / 'suspend'.",
    )
    next_action: PredictionNextAction | None = Field(default=None, description="Predicted next action.")

    @field_validator(
        "remaining_calls",
        "output_tokens",
        "interval_ms",
        "latency_ms",
        "remaining_time_ms",
        "expected_resume_s",
        mode="before",
    )
    @classmethod
    def _validate_non_negative_int(cls, value: Any) -> int | None:
        if value is None:
            return None
        value = _finite_int(value, "prediction value")
        if value is None:
            logger.warning("Invalid prediction value=%s; treating as unset (None).", value)
            return None
        if value < 0:
            logger.warning("prediction value=%s less than 0; treating as unset (None).", value)
            return None
        return value


class AgentHintInfo(BaseModel):
    """
    Structured info parsed from the OpenAI request's agent_hint.

    In the minimal version, only session_id / parent_session_id / cache_control
    are consumed by the Scheduler. context_management / latency_control /
    priority_control are parsed and populated for forward compatibility but
    do not currently drive scheduling decisions.
    """

    session_id: str | None = Field(default=None, description="Session ID; supplied by client or auto-generated.")
    parent_session_id: str | None = Field(
        default=None, description="Parent session ID; usually the main agent's session."
    )
    session_hint: SessionHint | None = Field(
        default=None,
        description="Nested spec-form session block (derivation / agent_type / call_index / epoch); Phase 2 consumes.",
    )
    cache_control: CacheControl | None = Field(default=None, description="KV cache control hint.")
    context_management: ContextManagement | None = Field(
        default=None, description="Context management hint (design-only)."
    )
    session_control: SessionControl | None = Field(
        default=None,
        description=(
            "Session-level lifecycle control (design-only). After "
            "apply_session_control_autofill, session_control and the injected "
            "context_management coexist as aliases of the same intent; "
            "consumers must not act on both."
        ),
    )
    latency_control: LatencyControl | None = Field(default=None, description="Latency control hint (design-only).")
    priority_control: PriorityControl | None = Field(default=None, description="Priority control hint (design-only).")
    agent_status: AgentStatusHint | None = Field(
        default=None,
        description="Agent lifecycle status from the request; parsed and stored (Phase 4 adds the event channel).",
    )
    prediction: PredictionHint | None = Field(
        default=None,
        description="Workload prediction; parsed and stored (Phase 5 consumes).",
    )
    strict_priority_trusted: bool = Field(
        default=False,
        description="WP-E2: True only when strict_priority survived the entry trust gate.",
    )
    strict_priority: PriorityHint | None = Field(
        default=None,
        description=(
            "Hard priority from a (future) trusted control plane; trust boundary "
            "unresolved — parsed and stored, never consumed."
        ),
    )
    raw_extra: dict | None = Field(
        default_factory=dict, description="Pass-through dict for unrecognized extension fields in agent_hint."
    )


def _normalize_spec_range(
    data: dict,
    msg_offset: int | None,
) -> tuple[int | None, int | None, str | None, int | None]:
    """Map spec ``cache_control.range`` onto range_* fields and (maybe) msg_offset.

    Returns ``(range_start, range_end, range_unit, derived_msg_offset)``:

    - ``unit=message`` with ``start`` absent/0: ``range.end`` (exclusive end of
      messages [0, end)) maps to ``msg_offset=end`` (1-based effective start) so
      the existing block-coordinate translation chain applies unchanged.
    - ``start > 0`` is not supported yet: the range is dropped with a WARNING.
    - Any other ``unit`` is rejected (``message`` is the only value the protocol
      defines): the range is dropped with a WARNING, so no effect point is derived
      from it and the declaration falls back to the no-range default.

    An explicit legacy ``msg_offset`` always wins over the derived value.
    """
    if not isinstance(data, dict):
        return None, None, None, None
    unit_raw = data.get("unit")
    unit = _CACHE_RANGE_UNIT_DEFAULT if unit_raw is None else str(unit_raw)
    if unit not in _CACHE_RANGE_UNITS_ALLOWED:
        logger.warning(
            "Invalid cache_control.range.unit=%r; expected one of %s. Dropping range.",
            unit_raw,
            sorted(_CACHE_RANGE_UNITS_ALLOWED),
        )
        return None, None, None, None

    def _to_int(key: str) -> int | None:
        value = data.get(key)
        if value is None:
            return None
        value = _finite_int(value, f"cache_control.range.{key}")
        if value is None:
            logger.warning("Invalid cache_control.range.%s=%s; treating as unset.", key, data.get(key))
            return None
        if value < 0:
            logger.warning("cache_control.range.%s=%s less than 0; treating as unset.", key, value)
            return None
        return value

    range_start = _to_int("start")
    range_end = _to_int("end")
    derived_msg_offset = None
    # Only "message" passes the whitelist check above.
    if range_start not in (None, 0):
        logger.warning(
            "cache_control.range.start=%s with unit=message is not supported yet; dropping range.",
            range_start,
        )
        return None, None, None, None
    if range_end is not None:
        # [0, end) messages == effective from 1-based message `end`.
        derived_msg_offset = range_end
    if msg_offset is not None and derived_msg_offset is not None and msg_offset != derived_msg_offset:
        logger.info(
            "cache_control has both msg_offset=%s and range(message) end=%s; legacy msg_offset wins.",
            msg_offset,
            derived_msg_offset,
        )
        derived_msg_offset = None
    return range_start, range_end, unit, derived_msg_offset


def _finalize_cache_control(
    cache_control: CacheControl,
    messages: list | None,
) -> CacheControl | None:
    """Shared msg_offset defaulting and bounds check for both cache_control sources.

    An unset ``msg_offset`` means no range narrowed the declaration, so it defaults to the
    whole conversation (``len(messages)``), matching the legacy contract. Ranges that cannot
    be interpreted are dropped upstream by ``_normalize_spec_range``, which therefore also
    falls back to this same whole-conversation default.
    """
    n = len(messages) if isinstance(messages, list) else None
    msg_offset = cache_control.msg_offset
    if msg_offset is None:
        if n is not None and n > 0:
            cache_control.msg_offset = n
    else:
        if msg_offset < 1:
            logger.warning(
                "cache_control.msg_offset=%s out of range [1, %s]; dropping cache_control.",
                msg_offset,
                (n) if n is not None else "len(messages)",
            )
            return None
        if n is not None and msg_offset > n:
            logger.warning(
                "cache_control.msg_offset=%s out of range [0, %s]; dropping cache_control.",
                msg_offset,
                n,
            )
            return None
    return cache_control


def _parse_cache_control(
    data: Any,
    messages: list | None = None,
) -> CacheControl | None:
    """Parse the legacy body ``cache_control`` (strictly ephemeral/ttl/msg_offset).

    The Agent-Hint spec fields (``pin`` type, ``ttl_s``, ``range``) are
    header-only protocol (X-Agent-Hint); their presence in the body is warned
    about and ignored so the body schema stays a pure legacy interface.
    """
    if not isinstance(data, dict):
        return None
    for spec_key in ("ttl_s", "range"):
        if spec_key in data:
            logger.warning(
                "cache_control.%s is a spec field and header-only (X-Agent-Hint); ignoring it in the body.",
                spec_key,
            )
    ttl_explicit = "ttl" in data
    data = {k: v for k, v in data.items() if k not in _CACHE_SERVER_ONLY_FIELDS}
    try:
        cache_control = CacheControl(**data)
    except Exception as e:
        logger.warning("Failed to parse cache_control: %s", e)
        return None
    cache_control.ttl_explicit = ttl_explicit
    if cache_control.type != _CACHE_TYPE_DEFAULT:
        logger.warning(
            "cache_control.type=%r in the request body is not supported (body is legacy ephemeral-only); "
            "dropping cache_control. Use the X-Agent-Hint header for spec cache_control.",
            cache_control.type,
        )
        return None
    return _finalize_cache_control(cache_control, messages)


def _parse_spec_cache_control(
    data: Any,
    messages: list | None = None,
) -> CacheControl | None:
    """Parse the spec-form ``cache_control`` carried by X-Agent-Hint.

    Accepts ``type`` pin/ephemeral, ``ttl_s`` (preferred over ``ttl``) and
    ``range{start,end,unit}`` (``unit=message``, the only protocol value, maps onto
    the legacy ``msg_offset`` translation; any other unit drops the range).
    """
    if isinstance(data, list):
        if not data:
            return None
        if len(data) > 1:
            logger.warning(
                "X-Agent-Hint carries %d cache_control entries; using the first (multi-entry not supported yet).",
                len(data),
            )
        data = data[0]
    if not isinstance(data, dict):
        logger.warning(
            "X-Agent-Hint cache_control is not a dict (got %s); ignoring.",
            type(data).__name__,
        )
        return None
    data = {k: v for k, v in data.items() if k not in _CACHE_SERVER_ONLY_FIELDS}
    ttl_s = data.pop("ttl_s", None)
    # Explicit retention means the client sent either alias; a bare ``type`` keeps the
    # default TTL but stays out of the lifecycle.
    ttl_explicit = ttl_s is not None or "ttl" in data
    if ttl_s is not None:
        # Spec-preferred alias wins over a coexisting legacy `ttl`.
        data["ttl"] = ttl_s
    range_data = data.pop("range", None)
    try:
        cache_control = CacheControl(**data)
    except Exception as e:
        logger.warning("Failed to parse cache_control: %s", e)
        return None
    cache_control.ttl_explicit = ttl_explicit

    range_start, range_end, range_unit, derived_msg_offset = _normalize_spec_range(range_data, cache_control.msg_offset)
    cache_control.range_start = range_start
    cache_control.range_end = range_end
    cache_control.range_unit = range_unit
    if derived_msg_offset is not None:
        cache_control.msg_offset = derived_msg_offset
    return _finalize_cache_control(cache_control, messages)


def _validate_edit_indices(
    edit: ContextEdit,
    messages: list | None,
    tools: list | None = None,
) -> bool:
    """
    Validate ContextEdit.start / ContextEdit.end:

    - start / end must be >= 0.
    - Bounds:
        * target="tools": [0, len(tools)].
        * otherwise: [0, len(messages)].
    - start < end when both are non-None; start >= end is invalid.

    Returns False (with WARNING) when any check fails; callers should drop
    the entry.
    """
    msg_count: int | None
    if isinstance(messages, list):
        msg_count = len(messages)
    else:
        msg_count = None

    # target="tools" validates indices against len(tools), not len(messages).
    if edit.target == "tools" and isinstance(tools, list):
        bound = len(tools)
        bound_name = "len(tools)"
        use_tools_bound = True
    else:
        bound = msg_count
        bound_name = "len(messages)"
        use_tools_bound = False

    start = edit.start
    end = edit.end
    dropped = False

    if start is not None and start < 0:
        logger.warning(
            "context_edit.start=%s less than 0; dropping edit (type=%s, target=%s).",
            start,
            edit.type,
            edit.target,
        )
        dropped = True
    if end is not None and end < 0:
        logger.warning(
            "context_edit.end=%s less than 0; dropping edit (type=%s, target=%s).",
            end,
            edit.type,
            edit.target,
        )
        dropped = True

    if bound is not None:
        if start is not None and start > bound:
            logger.warning(
                "context_edit.start=%s > %s=%s; dropping edit (type=%s, target=%s).",
                start,
                bound_name,
                bound,
                edit.type,
                edit.target,
            )
            dropped = True
        if end is not None and end > bound:
            logger.warning(
                "context_edit.end=%s > %s=%s; dropping edit (type=%s, target=%s).",
                end,
                bound_name,
                bound,
                edit.type,
                edit.target,
            )
            dropped = True

    if start is not None and end is not None and start >= end:
        logger.warning(
            "context_edit.start=%s >= end=%s; dropping edit (type=%s, target=%s).",
            start,
            end,
            edit.type,
            edit.target,
        )
        dropped = True

    # target="tools" with empty tools: bounds are meaningless; warn here for parity
    # with the offset translator, which also short-circuits.
    if use_tools_bound and bound == 0:
        logger.warning(
            "context_edit target='tools' but tools is empty; edit (type=%s) will be a no-op downstream.",
            edit.type,
        )

    return not dropped


def _parse_context_management(
    data: Any,
    messages: list | None = None,
    tools: list | None = None,
) -> ContextManagement | None:
    if not isinstance(data, dict):
        return None
    try:
        try:
            manage_request = parse_manage_request(data.get("manage_request", False))
        except TypeError:
            manage_request = False

        edits_raw = data.get("edits") or []
        edits: list[ContextEdit] = []
        for entry in edits_raw:
            if not isinstance(entry, dict):
                logger.warning(
                    "Ignoring non-dict context_management.edits entry: %r",
                    entry,
                )
                continue
            forged = sorted(k for k in entry if k in _EDIT_SERVER_ONLY_FIELDS)
            if forged:
                logger.warning(
                    "context_management.edits entry contains server-only "
                    "offset field(s) %s; dropping entire edit (type=%r) to "
                    "prevent client forgery.",
                    forged,
                    entry.get("type"),
                )
                continue
            try:
                edit = ContextEdit(**entry)
            except Exception as exc:
                logger.warning(
                    "Failed to parse context_management.edits entry: %s",
                    exc,
                )
                continue

            if not _validate_edit_indices(edit, messages, tools):
                continue
            edits.append(edit)

        if not manage_request and not edits:
            return None

        if manage_request and not edits:
            logger.warning(
                "context_management.manage_request=true with no usable edits; "
                "dropping context_management (set to None)."
            )
            return None
        return ContextManagement(manage_request=manage_request, edits=edits)
    except Exception as exc:
        logger.warning("Failed to parse context_management: %s", exc)
        return None


def _parse_session_control(data: Any) -> SessionControl | None:
    if not isinstance(data, dict):
        return None
    try:
        return SessionControl(type=data.get("type"))
    except ValidationError as exc:
        logger.warning("Failed to parse session_control: %s", exc)
        return None


class _SessionControlStatus(Enum):
    """Classification of raw agent_hint.session_control for the shared decision path."""

    ABSENT = auto()  # agent_hint is not a dict or carries no session_control key
    MALFORMED = auto()  # session_control is present but not a dict
    CONFLICT = auto()  # coexists with explicit context_management; context_management wins
    INVALID_TYPE = auto()  # type not in _SESSION_CONTROL_TYPES_ALLOWED
    START = auto()  # type == "start": keep the hint, inject nothing
    ACTIONABLE = auto()  # type in _SESSION_CONTROL_EDIT_TYPES: translate into a manage-request


def _resolve_session_control(
    agent_hint_data: Any,
) -> tuple[_SessionControlStatus, Any, str]:
    """Classify raw agent_hint.session_control in a single place.

    Single source of truth for both ``apply_session_control_autofill``
    (mutating translation) and ``session_control_implies_manage_request``
    (pure predicate), so dict checks, the context_management mutual-exclusion
    rule, and type normalization/validation cannot drift between the two
    call paths.

    Returns ``(status, raw_type, type_value)``:

    - ``raw_type`` — the original value as it appeared in the request
      (preserved for diagnostic logging under CONFLICT / INVALID_TYPE).
    - ``type_value`` — the normalized string used for white-list checks,
      routing decisions, and the INFO log of valid translations. Empty
      string when ``raw_type`` is missing/None.

    Mutual-exclusion rule: a raw context_management only conflicts with
    session_control when it constitutes a *valid* session-targeted manage
    request (i.e. ``_has_manage_request_session_edit`` holds). An invalid
    / empty / non-dict context_management does not block session_control;
    it will be dropped by ``_parse_context_management`` anyway, so falling
    through to the ACTIONABLE / START branches keeps the client intent
    intact instead of silently downgrading the request to plain inference.
    """
    if not isinstance(agent_hint_data, dict) or "session_control" not in agent_hint_data:
        return _SessionControlStatus.ABSENT, None, ""
    sc_data = agent_hint_data.get("session_control")
    if not isinstance(sc_data, dict):
        return _SessionControlStatus.MALFORMED, None, ""
    if "context_management" in agent_hint_data and _has_manage_request_session_edit(agent_hint_data):
        return _SessionControlStatus.CONFLICT, sc_data.get("type"), ""
    raw_type = sc_data.get("type")
    type_value = str(raw_type) if raw_type is not None else ""
    if type_value not in _SESSION_CONTROL_TYPES_ALLOWED:
        return _SessionControlStatus.INVALID_TYPE, raw_type, ""
    return (
        _SessionControlStatus.START if type_value == "start" else _SessionControlStatus.ACTIONABLE,
        raw_type,
        type_value,
    )


def apply_session_control_autofill(request_json: dict) -> None:
    """Translate agent_hint.session_control into agent_hint.context_management.

    Mutates ``request_json`` in place — the caller shares the same
    ``agent_hint`` sub-dict with ``req_data`` via a shallow copy, so the
    injected context_management is visible to every downstream consumer
    (ensure_minimum_messages_for_session_edits, parse_agent_hint,
    attach_block_offsets).

    Rules (classification itself is delegated to _resolve_session_control):

    - session_control and context_management are mutually exclusive; when both
      are present, context_management wins and session_control is dropped.
    - invalid / non-dict session_control is dropped.
    - pause / stop / compact / resume inject a session-targeted manage-request
      (offload / evict / evict / prefetch); 'start' only keeps the parsed hint.
    """
    if not isinstance(request_json, dict):
        return
    agent_hint = request_json.get("agent_hint")
    status, raw_type, type_value = _resolve_session_control(agent_hint)
    if status is _SessionControlStatus.ABSENT:
        return

    raw_session_id = agent_hint.get("session_id")

    if status is _SessionControlStatus.MALFORMED:
        logger.warning(
            "agent_hint.session_control=%r is not a dict; dropping session_control. session_id=%s",
            agent_hint.get("session_control"),
            raw_session_id,
        )
        agent_hint.pop("session_control", None)
        return

    if status is _SessionControlStatus.CONFLICT:
        logger.warning(
            "agent_hint contains both session_control(type=%r) and context_management; "
            "keeping context_management and dropping session_control. session_id=%s",
            raw_type,
            raw_session_id,
        )
        agent_hint.pop("session_control", None)
        return

    if status is _SessionControlStatus.INVALID_TYPE:
        logger.warning(
            "Unsupported session_control.type=%r; expected one of %s. Dropping session_control. session_id=%s",
            raw_type,
            sorted(_SESSION_CONTROL_TYPES_ALLOWED),
            raw_session_id,
        )
        agent_hint.pop("session_control", None)
        return

    logger.info(
        "session_control.type=%s session_id=%s (raw, resolved later by parse_agent_hint)",
        type_value,
        raw_session_id,
    )

    if status is _SessionControlStatus.START:
        return

    agent_hint["context_management"] = {
        "manage_request": True,
        "edits": [{"type": _SESSION_CONTROL_EDIT_TYPES[type_value], "target": "session"}],
    }


def session_control_implies_manage_request(agent_hint_data: Any) -> bool:
    """Return True when a raw agent_hint session_control implies a manage-request.

    Only pause / stop / compact / resume imply context management; 'start' does
    not. When a raw context_management block is also present it takes
    precedence (mutual exclusion), so this returns False.
    """
    return _resolve_session_control(agent_hint_data)[0] is _SessionControlStatus.ACTIONABLE


def _has_manage_request_session_edit(agent_hint_data: Any) -> bool:
    """Return True when raw context_management is a manage-request with a session-targeted edit.

    An edit is considered 'session-targeted' when its `target` field is either
    explicitly 'session' or absent (the V1.1 default is 'session' — see
    _EDIT_TARGET_DEFAULT). Malformed substructures are treated as 'no session
    edit' so that API validation keeps rejecting such bodies.
    """
    if not isinstance(agent_hint_data, dict):
        return False
    context_management = agent_hint_data.get("context_management")
    if not isinstance(context_management, dict):
        return False
    if not parse_manage_request(context_management.get("manage_request", False)):
        return False
    edits = context_management.get("edits")
    if not isinstance(edits, list):
        return False
    return any(isinstance(edit, dict) and edit.get("target", "session") == "session" for edit in edits)


def agent_hint_implies_manage_request(agent_hint_data: Any) -> bool:
    """Return True when a raw agent_hint qualifies the request as a manage-request.

    True when either the explicit context_management path holds (manage_request
    is true and at least one edit targets 'session') or the session_control
    path holds (pause / stop / compact / resume without a conflicting
    context_management).
    """
    return _has_manage_request_session_edit(agent_hint_data) or session_control_implies_manage_request(agent_hint_data)


def _parse_priority_control(data: dict) -> PriorityControl | None:
    priority_control = None
    if isinstance(data, dict):
        try:
            reason = data.get("reason")
            priority_control = PriorityControl(
                priority=data.get("priority"),
                reason=reason if isinstance(reason, str) else None,
            )
        except Exception as e:
            logger.warning("Failed to parse priority_control: %s", e)
    return priority_control


def apply_strict_priority_trust(
    agent_hint_info: "AgentHintInfo",
    headers: dict | None,
    strict_config: Any,
) -> bool:
    """Entry trust gate for Agent Hint `strict_priority` (WP-E2).

    A trusted claim (strict channel enabled + matching
    ``X-Strict-Priority-Token``) is kept and marked; anything else is
    STRIPPED (set to None) with a WARNING so no consumer can act on an
    untrusted client declaration. Returns True when a trusted strict
    priority is present after the gate.

    ``strict_config`` is the coordinator ``StrictPriorityConfig`` (duck-typed:
    ``enabled: bool``, ``tokens: list[str]``); None/absent disables the channel.
    """
    if agent_hint_info is None or agent_hint_info.strict_priority is None:
        return False
    from motor.coordinator.domain.agent_hint_wire import strict_priority_trusted

    enabled = bool(getattr(strict_config, "enabled", False))
    tokens = list(getattr(strict_config, "tokens", None) or [])
    if not strict_priority_trusted(headers, enabled=enabled, tokens=tokens):
        logger.warning(
            "strict_priority dropped (untrusted source): level=%s reason=%s",
            agent_hint_info.strict_priority.level,
            agent_hint_info.strict_priority.reason,
        )
        agent_hint_info.strict_priority = None
        agent_hint_info.strict_priority_trusted = False
        return False
    agent_hint_info.strict_priority_trusted = True
    return True


def _finite_int(value: Any, field: str, default: Any = None) -> Any:
    """Convert ``value`` to int defensively.

    Rejects non-finite floats (json ``1e999`` decodes to ``inf``; ``int(inf)``
    raises ``OverflowError``, which is not a ``ValueError``) and any conversion
    failure, warning and returning ``default`` instead — a malformed optional
    hint must never break request parsing.
    """
    if value is None:
        return default
    try:
        if isinstance(value, float) and not math.isfinite(value):
            logger.warning("Invalid %s=%r (non-finite); treating as unset.", field, value)
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        logger.warning("Invalid %s=%r; treating as unset.", field, value)
        return default


def _parse_priority_hint(data: Any) -> PriorityHint | None:
    """Parse spec-form ``{level, reason}``; None for absent/malformed input."""
    if not isinstance(data, dict):
        return None
    level = data.get("level")
    if level is not None:
        level = _finite_int(level, "priority.level")
    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        logger.warning("Invalid priority reason=%r; treating as unset.", reason)
        reason = None
    if level is None and reason is None:
        return None
    return PriorityHint(level=level, reason=reason)


def _parse_session_hint(data: Any) -> SessionHint | None:
    """Parse the nested spec ``session{}`` block; None for absent/invalid input."""
    if not isinstance(data, dict):
        return None
    if not data:
        return None
    derivation = None
    derivation_data = data.get("derivation")
    if isinstance(derivation_data, dict) and derivation_data:
        try:
            derivation = DerivationHint(**derivation_data)
        except Exception as e:
            logger.warning("Failed to parse session.derivation: %s", e)
    elif derivation_data is not None:
        logger.warning("session.derivation=%r is not a dict; dropping derivation.", derivation_data)

    def _optional_str(key: str) -> str | None:
        value = data.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            logger.warning("Invalid session.%s=%r; treating as unset.", key, value)
            return None
        return value

    try:
        return SessionHint(
            session_id=_optional_str("session_id"),
            parent_session_id=_optional_str("parent_session_id"),
            derivation=derivation,
            agent_type=_optional_str("agent_type"),
            call_index=data.get("call_index"),
            epoch=data.get("epoch"),
        )
    except Exception as e:
        logger.warning("Failed to parse session hint: %s", e)
        return None


def _parse_agent_status(data: Any) -> AgentStatusHint | None:
    """Parse spec ``agent_status``; None for absent/invalid input (never raises)."""
    if not isinstance(data, dict) or not data:
        return None
    tools_data = data.get("tools")
    tools: list[AgentStatusTool] = []
    if isinstance(tools_data, list):
        for entry in tools_data:
            if not isinstance(entry, dict):
                logger.warning("Ignoring non-dict agent_status.tools entry: %r", entry)
                continue
            try:
                tools.append(
                    AgentStatusTool(
                        name=entry.get("name"),
                        call_id=entry.get("call_id"),
                        child_session_id=entry.get("child_session_id"),
                    )
                )
            except Exception as e:
                logger.warning("Failed to parse agent_status.tools entry: %s", e)
    elif tools_data is not None:
        logger.warning("agent_status.tools=%r is not a list; dropping tools.", tools_data)
    try:
        return AgentStatusHint(
            state=data.get("state"),
            state_since_ms=data.get("state_since_ms"),
            reason=data.get("reason"),
            tools=tools,
        )
    except Exception as e:
        logger.warning("Failed to parse agent_status: %s", e)
        return None


def _parse_prediction(data: Any) -> PredictionHint | None:
    """Parse spec ``prediction``; None for absent/invalid input (never raises)."""
    if not isinstance(data, dict) or not data:
        return None
    next_action = None
    next_action_data = data.get("next_action")
    if isinstance(next_action_data, dict):
        tools: list[PredictionTool] = []
        tools_data = next_action_data.get("tools")
        if isinstance(tools_data, list):
            for entry in tools_data:
                if not isinstance(entry, dict):
                    logger.warning("Ignoring non-dict prediction.next_action.tools entry: %r", entry)
                    continue
                expected_duration_s = entry.get("expected_duration_s")
                if expected_duration_s is not None:
                    expected_duration_s = max(
                        0, _finite_int(expected_duration_s, "prediction.next_action.tools[].expected_duration_s") or 0
                    )
                try:
                    tools.append(PredictionTool(name=entry.get("name"), expected_duration_s=expected_duration_s))
                except ValidationError as exc:
                    logger.warning("Invalid prediction.next_action.tools entry %r; dropping the entry: %s", entry, exc)
        elif tools_data is not None:
            logger.warning("prediction.next_action.tools=%r is not a list; dropping tools.", tools_data)
        try:
            next_action = PredictionNextAction(
                state=next_action_data.get("state"),
                prob=next_action_data.get("prob"),
                tools=tools,
            )
        except Exception as e:
            logger.warning("Failed to parse prediction.next_action: %s", e)
    elif next_action_data is not None:
        logger.warning("prediction.next_action=%r is not a dict; dropping next_action.", next_action_data)
    try:
        prediction = PredictionHint(
            remaining_calls=data.get("remaining_calls"),
            output_tokens=data.get("output_tokens"),
            interval_ms=data.get("interval_ms"),
            latency_ms=data.get("latency_ms"),
            remaining_time_ms=data.get("remaining_time_ms"),
            expected_resume_s=data.get("expected_resume_s"),
            next_action=next_action,
        )
    except Exception as e:
        logger.warning("Failed to parse prediction: %s", e)
        return None
    if prediction.model_dump(exclude_none=True) == {}:
        # Every supplied value was invalid/unset: collapse to None instead of
        # keeping an empty shell the scheduler would have to reason about.
        return None
    return prediction


def _is_valid_session_id(value: Any) -> bool:
    """Valid session id: must be a non-empty string. None / empty / non-string all invalid."""
    return isinstance(value, str) and value != ""


def _resolve_session_ids(
    agent_hint_data: Any,
    headers: dict | None = None,
    nested: "SessionHint | None" = None,
) -> tuple[str | None, str | None]:
    """
    Resolve and normalize session_id / parent_session_id.

    Per-field priority:
      1. The flat field in agent_hint (request body).
      2. The nested spec ``session{}`` block (X-Agent-Hint only).
      3. Otherwise fall back to x-session-id / x-parent-session-id header
         (Starlette normalizes header keys to lowercase).

    The body-first order matches the wire merge (body wins): a body session id
    must not be overridden by the header payload of the same request.

    Complement rule (single-agent clients typically send only one):
      - Only session_id valid         -> parent_session_id := session_id.
      - Only parent_session_id valid  -> session_id := parent_session_id.
      - Both valid                    -> keep as-is (parent/child hierarchy is
                                         the client's decision).
      - Both invalid                  -> normalize to None; do not generate IDs.
                                         If the client attempted to send them
                                         (body key present, nested session
                                         present or headers present), emit a
                                         WARNING.

    Returns:
        (session_id, parent_session_id): both non-empty strings, or both None.
    """
    # Non-dict agent_hint (e.g. str/list): treat as empty dict to keep .get safe.
    if not isinstance(agent_hint_data, dict):
        agent_hint_data = {}

    nested_session_id = nested.session_id if nested is not None else None
    nested_parent_session_id = nested.parent_session_id if nested is not None else None
    session_id = agent_hint_data.get("session_id")
    parent_session_id = agent_hint_data.get("parent_session_id")
    # Distinguish "client didn't send" from "client sent but invalid"; only the
    # latter (or present headers) should warn — avoids log spam on plain
    # requests without agent_hint.
    body_has_any_session_id = (
        "session_id" in agent_hint_data
        or "parent_session_id" in agent_hint_data
        or (nested is not None and (nested.session_id is not None or nested.parent_session_id is not None))
    )

    # Fall back through the nested header block then x-session-id headers when
    # the body's value is invalid. The body flat value (when valid) always wins
    # over the nested header block — same body-wins rule as the wire merge.
    if not _is_valid_session_id(session_id):
        if nested_session_id is not None:
            session_id = nested_session_id
        elif headers:
            session_id = headers.get(HEADER_SESSION_ID)
            if not _is_valid_session_id(session_id):
                session_id = None
        else:
            session_id = None

    if not _is_valid_session_id(parent_session_id):
        if nested_parent_session_id is not None:
            parent_session_id = nested_parent_session_id
        elif headers:
            parent_session_id = headers.get(HEADER_PARENT_SESSION_ID)
            if not _is_valid_session_id(parent_session_id):
                parent_session_id = None
        else:
            parent_session_id = None

    # Apply complement rule to fill in any missing field.
    session_valid = _is_valid_session_id(session_id)
    parent_valid = _is_valid_session_id(parent_session_id)
    if session_valid and not parent_valid:
        logger.warning("parent_session_id is invalid(missing/empty/non-string), set parent_session_id = session_id")
        parent_session_id = session_id
    elif parent_valid and not session_valid:
        logger.warning("session_id is invalid(missing/empty/non-string), set session_id = parent_session_id")
        session_id = parent_session_id
    elif not session_valid and not parent_valid:
        # Neither field has a usable value: normalize to None (overwrites dirty
        # values such as empty strings or non-strings).
        if body_has_any_session_id or headers:
            logger.warning(
                "session_id and parent_session_id are both missing/empty/non-string; passthrough (both will be None)"
            )
        session_id = None
        parent_session_id = None

    return session_id, parent_session_id


def parse_agent_hint(
    request_json: dict,
    headers: dict | None = None,
    header_hint: dict | None = None,
) -> AgentHintInfo:
    """Parse the request's agent hint into :class:`AgentHintInfo`.

    Two input channels exist with a strict protocol boundary:

    - ``header_hint`` — the decoded ``X-Agent-Hint`` payload (spec protocol,
      produced by :func:`apply_agent_hint_header`). Spec-form blocks (nested
      ``session``, ``agent_status``, ``prediction``, ``strict_priority``,
      spec ``cache_control``) are read from here ONLY.
    - ``request_json["agent_hint"]`` — the legacy body block; its schema is
      frozen (flat session ids, ephemeral cache_control, context/session
      control, latency/priority control).

    Per-field session-id priority: header nested ``session{}`` > body flat
    fields > ``x-session-id`` style headers.
    """
    agent_hint_data = request_json.get("agent_hint", {}) if isinstance(request_json, dict) else {}
    if not isinstance(agent_hint_data, dict):
        # agent_hint exists but is not a mapping (e.g. str/list) — treat as empty.
        logger.warning(
            "agent_hint is not a dict (got %s); falling back to defaults",
            type(agent_hint_data).__name__,
        )
        agent_hint_data = {}

    messages = request_json.get("messages") if isinstance(request_json, dict) else None
    tools = request_json.get("tools") if isinstance(request_json, dict) else None

    strict_priority = None
    agent_status = None
    prediction = None
    spec_cache_control = None
    nested_session = None
    if header_hint is not None:
        # Spec-form blocks travel ONLY in the X-Agent-Hint header (the body
        # agent_hint block stays a pure legacy interface).
        nested_session = _parse_session_hint(header_hint.get("session"))
        agent_status = _parse_agent_status(header_hint.get("agent_status"))
        prediction = _parse_prediction(header_hint.get("prediction"))
        strict_priority = _parse_priority_hint(header_hint.get("strict_priority"))
        spec_cache_control = _parse_spec_cache_control(
            header_hint.get("cache_control"),
            messages=messages,
        )

    session_id, parent_session_id = _resolve_session_ids(agent_hint_data, headers, nested_session)

    cache_control = _parse_cache_control(
        agent_hint_data.get("cache_control"),
        messages=messages,
    )
    if cache_control is None and spec_cache_control is not None:
        cache_control = spec_cache_control

    context_management = _parse_context_management(
        agent_hint_data.get("context_management"),
        messages=messages,
        tools=tools,
    )

    session_control = _parse_session_control(agent_hint_data.get("session_control"))

    latency_control = None
    lc_data = agent_hint_data.get("latency_control")
    if isinstance(lc_data, dict):
        try:
            latency_control = LatencyControl(latency_sensitivity=lc_data.get("latency_sensitivity"))
        except Exception as e:
            logger.warning("Failed to parse latency_control: %s", e)

    priority_control = _parse_priority_control(agent_hint_data.get("priority_control"))

    raw_extra = {}
    for key, value in agent_hint_data.items():
        if key not in _AGENT_HINT_KNOWN_FIELDS:
            raw_extra[key] = value

    return AgentHintInfo(
        session_id=session_id,
        parent_session_id=parent_session_id,
        session_hint=nested_session,
        cache_control=cache_control,
        context_management=context_management,
        session_control=session_control,
        latency_control=latency_control,
        priority_control=priority_control,
        strict_priority=strict_priority,
        agent_status=agent_status,
        prediction=prediction,
        raw_extra=raw_extra,
    )


_DEFAULT_SYSTEM_MESSAGE: dict[str, str] = {"role": "system", "content": "context management messages"}
_DEFAULT_USER_MESSAGE: dict[str, str] = {"role": "user", "content": "context management messages"}


def ensure_minimum_messages_for_session_edits(
    request_json: dict,
    req_data: dict,
) -> None:
    agent_hint_data = request_json.get("agent_hint", {}) if isinstance(request_json, dict) else {}
    if not _has_manage_request_session_edit(agent_hint_data):
        return

    messages = request_json.get("messages")
    if messages is None:
        new_list = [_DEFAULT_SYSTEM_MESSAGE, _DEFAULT_USER_MESSAGE]
        request_json["messages"] = new_list
        req_data["messages"] = new_list
        logger.warning(
            "Injecting default system and user message into empty/missing messages "
            "list for manage_request=true session-targeted edit; "
            "prevents apply_chat_template out-of-bounds crash downstream."
        )
        return
    if isinstance(messages, list) and len(messages) == 0:
        messages.append(_DEFAULT_SYSTEM_MESSAGE)
        messages.append(_DEFAULT_USER_MESSAGE)
        logger.warning(
            "Injecting default system and user message into empty messages list "
            "for manage_request=true session-targeted edit; "
            "prevents apply_chat_template out-of-bounds crash downstream."
        )
        return
