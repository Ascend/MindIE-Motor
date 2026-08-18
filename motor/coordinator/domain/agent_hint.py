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
_CACHE_TYPE_ALLOWED = "ephemeral"
_CACHE_TTL_DEFAULT = 300
_CACHE_TTL_MIN = 60
_CACHE_TTL_MAX = 3600
_CACHE_MSG_OFFSET_DEFAULT = None
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
    """KV cache control."""

    type: str = Field(default="ephemeral", description="Cache type; only 'ephemeral' is supported.")
    ttl: int = Field(default=_CACHE_TTL_DEFAULT, description="Cache TTL in seconds; clamped to [60, 3600].")
    msg_offset: int | None = Field(
        default=_CACHE_MSG_OFFSET_DEFAULT,
        description=(
            "1-based message index at which cache_control takes effect; None means apply to the whole conversation."
        ),
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
            return _CACHE_TYPE_ALLOWED
        value = str(value)
        if value != _CACHE_TYPE_ALLOWED:
            logger.warning(
                "Invalid cache_control.type=%r; dropping cache_control (only %r is supported).",
                value,
                _CACHE_TYPE_ALLOWED,
            )
            raise ValueError(f"unsupported cache_control.type: {value!r}")
        return value

    @field_validator("ttl", mode="before")
    @classmethod
    def _validate_ttl(cls, value: Any) -> int:
        try:
            value = int(value)
        except (TypeError, ValueError):
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
        try:
            if value is None:
                return None
            value = int(value)
        except (TypeError, ValueError):
            logger.warning("Invalid context_edit.start=%s; using default %s", value, None)
            return None
        if value < 0:
            logger.warning("context_edit.start=%s less than %s", value, 0)
        return value

    @field_validator("end", mode="before")
    @classmethod
    def _validate_end(cls, value: Any) -> int | None:
        try:
            if value is None:
                return None
            value = int(value)
        except (TypeError, ValueError):
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
    # More fields may be added in future versions.


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
    raw_extra: dict | None = Field(
        default_factory=dict, description="Pass-through dict for unrecognized extension fields in agent_hint."
    )


def _parse_cache_control(
    data: Any,
    messages: list | None = None,
) -> CacheControl | None:
    if not isinstance(data, dict):
        return None
    data = {k: v for k, v in data.items() if k not in _CACHE_SERVER_ONLY_FIELDS}
    try:
        cache_control = CacheControl(**data)
    except Exception as e:
        logger.warning("Failed to parse cache_control: %s", e)
        return None

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
            priority_control = PriorityControl(priority=data.get("priority"))
        except Exception as e:
            logger.warning("Failed to parse priority_control: %s", e)
    return priority_control


def _is_valid_session_id(value: Any) -> bool:
    """Valid session id: must be a non-empty string. None / empty / non-string all invalid."""
    return isinstance(value, str) and value != ""


def _resolve_session_ids(
    agent_hint_data: Any,
    headers: dict | None = None,
) -> tuple[str | None, str | None]:
    """
    Resolve and normalize session_id / parent_session_id.

    Per-field priority:
      1. The field in agent_hint (request body).
      2. Otherwise fall back to x-session-id / x-parent-session-id header
         (Starlette normalizes header keys to lowercase).

    Complement rule (single-agent clients typically send only one):
      - Only session_id valid         -> parent_session_id := session_id.
      - Only parent_session_id valid  -> session_id := parent_session_id.
      - Both valid                    -> keep as-is (parent/child hierarchy is
                                         the client's decision).
      - Both invalid                  -> normalize to None; do not generate IDs.
                                         If the client attempted to send them
                                         (body key present or headers present),
                                         emit a WARNING.

    Returns:
        (session_id, parent_session_id): both non-empty strings, or both None.
    """
    # Non-dict agent_hint (e.g. str/list): treat as empty dict to keep .get safe.
    if not isinstance(agent_hint_data, dict):
        agent_hint_data = {}

    session_id = agent_hint_data.get("session_id")
    parent_session_id = agent_hint_data.get("parent_session_id")
    # Distinguish "client didn't send" from "client sent but invalid"; only the
    # latter (or present headers) should warn — avoids log spam on plain
    # requests without agent_hint.
    body_has_any_session_id = "session_id" in agent_hint_data or "parent_session_id" in agent_hint_data

    # Fall back to headers when a field's body value is invalid.
    if headers:
        if not _is_valid_session_id(session_id):
            session_id = headers.get(HEADER_SESSION_ID)
        if not _is_valid_session_id(parent_session_id):
            parent_session_id = headers.get(HEADER_PARENT_SESSION_ID)

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
) -> AgentHintInfo:
    agent_hint_data = request_json.get("agent_hint", {}) if isinstance(request_json, dict) else {}
    if not isinstance(agent_hint_data, dict):
        # agent_hint exists but is not a mapping (e.g. str/list) — treat as empty.
        logger.warning(
            "agent_hint is not a dict (got %s); falling back to defaults",
            type(agent_hint_data).__name__,
        )
        agent_hint_data = {}

    session_id, parent_session_id = _resolve_session_ids(agent_hint_data, headers)

    messages = request_json.get("messages") if isinstance(request_json, dict) else None
    tools = request_json.get("tools") if isinstance(request_json, dict) else None

    cache_control = _parse_cache_control(
        agent_hint_data.get("cache_control"),
        messages=messages,
    )

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
        cache_control=cache_control,
        context_management=context_management,
        session_control=session_control,
        latency_control=latency_control,
        priority_control=priority_control,
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
