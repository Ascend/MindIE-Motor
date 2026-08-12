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
context management, latency control, and priority control — and exposes the
translator that converts ``CacheControl.msg_offset`` and
``ContextEdit.start/end`` from message-level indices into PagedAttention block
coordinates consumed by the scheduler.
"""

from typing import Any
from pydantic import BaseModel, Field, field_validator
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
    if not isinstance(agent_hint_data, dict):
        return
    cm_data = agent_hint_data.get("context_management")
    if not isinstance(cm_data, dict):
        return

    try:
        manage_request = parse_manage_request(cm_data.get("manage_request", False))
    except Exception:
        manage_request = False
    if not manage_request:
        return

    edits_raw = cm_data.get("edits") or []
    if not any(isinstance(e, dict) and e.get("target", "session") == "session" for e in edits_raw):
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
