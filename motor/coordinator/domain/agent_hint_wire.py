# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Wire-format adapter for the AgentHint HTTP header.

Agent frameworks send hints out-of-band in the ``X-Agent-Hint`` header
(base64url-encoded UTF-8 JSON, padding optional), while the coordinator's
``parse_agent_hint`` reads the body's legacy ``agent_hint`` block. This module
bridges the two: it decodes the header, merges the legacy-representable fields
into ``request_json["agent_hint"]``, and returns the decoded payload so
``parse_agent_hint`` can consume the spec-form blocks (which must never enter
the request body — see ``parse_agent_hint``).

Merge rule: body fields always win; header values only fill gaps. Any
decode/merge problem is logged at WARNING and swallowed — a malformed hint
must never fail the request.
"""

import base64
import hmac
import json

from motor.common.logger import get_logger

logger = get_logger(__name__)

# Lowercase to match Starlette's header normalization; the exact-case variant
# is a fallback for plain dict headers from non-Starlette callers.
AGENT_HINT_WIRE_HEADER = "x-agent-hint"
_AGENT_HINT_WIRE_HEADER_ALT = "X-Agent-Hint"
STRICT_PRIORITY_TOKEN_HEADER = "x-strict-priority-token"  # nosec B105 -- header name, not a credential
_STRICT_PRIORITY_TOKEN_HEADER_ALT = "X-Strict-Priority-Token"  # nosec B105 -- header name, not a credential

# Upper bound before decoding; the formal AgentHint schema fits far below this.
_MAX_HEADER_VALUE_CHARS = 16384


def decode_agent_hint_header(value: str) -> dict | None:
    """Decode a base64url X-Agent-Hint value into its JSON object.

    Accepts values with or without base64 padding. Returns None (with a
    WARNING logged) for any malformed input; never raises.
    """
    if not isinstance(value, str):
        logger.warning("X-Agent-Hint header is not a string (got %s); ignoring", type(value).__name__)
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > _MAX_HEADER_VALUE_CHARS:
        logger.warning("X-Agent-Hint header too large (%s chars); ignoring", len(value))
        return None
    try:
        payload = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoded = json.loads(payload.decode("utf-8"))
    except ValueError as exc:  # binascii.Error / JSONDecodeError / UnicodeDecodeError all subclass ValueError
        logger.warning("Failed to decode X-Agent-Hint header: %s", exc)
        return None
    if not isinstance(decoded, dict):
        logger.warning("X-Agent-Hint payload is not a JSON object (got %s); ignoring", type(decoded).__name__)
        return None
    return decoded


def _as_int(value: object) -> int | None:
    """Coerce a wire value to int; bools, fractional floats and non-numbers are rejected."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _fill_if_absent(agent_hint: dict, key: str, value: object) -> None:
    """Body wins: only write when the key is absent or explicitly null."""
    if agent_hint.get(key) is None:
        agent_hint[key] = value


def _merge_session_fields(agent_hint: dict, decoded: dict) -> None:
    """Map header session.session_id / session.parent_session_id into the flat body schema."""
    session = decoded.get("session")
    if not isinstance(session, dict):
        return
    session_id = session.get("session_id")
    if isinstance(session_id, str) and session_id:
        _fill_if_absent(agent_hint, "session_id", session_id)
    parent_session_id = session.get("parent_session_id")
    if isinstance(parent_session_id, str) and parent_session_id:
        _fill_if_absent(agent_hint, "parent_session_id", parent_session_id)


def _merge_priority(agent_hint: dict, decoded: dict) -> None:
    """Map header priority.level into agent_hint.priority_control.priority."""
    priority = decoded.get("priority")
    if not isinstance(priority, dict):
        return
    level = _as_int(priority.get("level"))
    if level is None:
        return
    priority_control = agent_hint.get("priority_control")
    if not isinstance(priority_control, dict):
        priority_control = {}
        agent_hint["priority_control"] = priority_control
    if priority_control.get("priority") is None:
        priority_control["priority"] = level
    reason = priority.get("reason")
    if isinstance(reason, str) and priority_control.get("reason") is None:
        priority_control["reason"] = reason


def _merge_latency(agent_hint: dict, decoded: dict) -> None:
    """Map header prediction.latency_ms into agent_hint.latency_control.latency_sensitivity."""
    prediction = decoded.get("prediction")
    if not isinstance(prediction, dict):
        return
    latency_ms = _as_int(prediction.get("latency_ms"))
    if latency_ms is None:
        return
    latency_control = agent_hint.get("latency_control")
    if not isinstance(latency_control, dict):
        latency_control = {}
        agent_hint["latency_control"] = latency_control
    if latency_control.get("latency_sensitivity") is None:
        latency_control["latency_sensitivity"] = latency_ms


def extract_priority_from_hint_headers(headers: dict | None) -> int | None:
    """Extract the spec `priority.level` from X-Agent-Hint headers (int, or None).

    Middleware-side helper: the rate limiter reads the ASGI scope's headers only
    (the request body is not available there), so spec-form priority arrives
    exclusively through this path — consistent with the Phase 1 protocol
    boundary. Malformed payloads yield None (with the decode-side WARNING),
    never an exception.
    """
    if not isinstance(headers, dict):
        return None
    raw_value = headers.get(AGENT_HINT_WIRE_HEADER)
    if raw_value is None:
        raw_value = headers.get(_AGENT_HINT_WIRE_HEADER_ALT)
    if not isinstance(raw_value, str):
        return None
    decoded = decode_agent_hint_header(raw_value)
    if decoded is None:
        return None
    priority = decoded.get("priority")
    if not isinstance(priority, dict):
        return None
    return _as_int(priority.get("level"))


def apply_agent_hint_header(request_json: dict, headers: dict | None) -> dict | None:
    """Decode X-Agent-Hint from headers and merge its legacy-compatible fields into the body.

    Protocol boundary: the header is the ONLY transport for spec-form blocks
    (nested ``session``, ``agent_status``, ``prediction``, ``strict_priority``,
    spec ``cache_control``). This function therefore:

    1. merges only the legacy-representable fields into
       ``request_json["agent_hint"]`` (session ids, ``priority.level`` →
       ``priority_control``, ``prediction.latency_ms`` → ``latency_control``),
       so legacy consumers and the block-offset translation keep working —
       body fields win, the header only fills gaps; and
    2. returns the decoded header payload for ``parse_agent_hint`` (via the
       ``header_hint`` argument) so the spec blocks reach ``AgentHintInfo``
       without ever entering the request body.

    Any decode/merge problem is logged at WARNING and swallowed — a malformed
    hint must never fail the request (None is returned).
    """
    if not isinstance(request_json, dict) or not isinstance(headers, dict):
        return None
    raw_value = headers.get(AGENT_HINT_WIRE_HEADER)
    if raw_value is None:
        raw_value = headers.get(_AGENT_HINT_WIRE_HEADER_ALT)
    if raw_value is None:
        return None
    decoded = decode_agent_hint_header(raw_value)
    if decoded is None:
        return None
    agent_hint = request_json.get("agent_hint")
    if not isinstance(agent_hint, dict):
        if agent_hint is not None:
            logger.warning(
                "request agent_hint is not a dict (got %s); replacing from X-Agent-Hint",
                type(agent_hint).__name__,
            )
        agent_hint = {}
        request_json["agent_hint"] = agent_hint
    _merge_session_fields(agent_hint, decoded)
    _merge_priority(agent_hint, decoded)
    _merge_latency(agent_hint, decoded)
    return decoded


def extract_strict_priority_from_hint_headers(headers: dict | None) -> tuple[int, str | None] | None:
    """Spec `strict_priority.{level, reason}` from X-Agent-Hint (headers-only).

    Middleware-side helper mirroring ``extract_priority_from_hint_headers``:
    the body is never read on the ASGI path. Returns ``(level, reason)`` or
    None when the header is absent/malformed — a malformed hint never raises.
    """
    if not isinstance(headers, dict):
        return None
    raw_value = headers.get(AGENT_HINT_WIRE_HEADER)
    if raw_value is None:
        raw_value = headers.get(_AGENT_HINT_WIRE_HEADER_ALT)
    if not isinstance(raw_value, str):
        return None
    decoded = decode_agent_hint_header(raw_value)
    if decoded is None:
        return None
    strict = decoded.get("strict_priority")
    if not isinstance(strict, dict):
        return None
    level = _as_int(strict.get("level"))
    if level is None:
        return None
    reason = strict.get("reason")
    return level, reason if isinstance(reason, str) else None


def extract_strict_priority_token(headers: dict | None) -> str | None:
    """`X-Strict-Priority-Token` header value (str, or None)."""
    if not isinstance(headers, dict):
        return None
    token = headers.get(STRICT_PRIORITY_TOKEN_HEADER)
    if token is None:
        token = headers.get(_STRICT_PRIORITY_TOKEN_HEADER_ALT)
    if not isinstance(token, str):
        return None
    token = token.strip()
    return token or None


def _constant_time_token_match(token: str, tokens: list[str]) -> bool:
    """Constant-time membership check for one bearer token.

    ``token in set(tokens)`` short-circuits on the first differing byte and
    exits as soon as a match is found, leaking the shared prefix length and the
    match position through timing (review 194909399). ``hmac.compare_digest``
    compares in constant time and every configured token is evaluated (the
    result is accumulated, never short-circuited), so the comparison cost does
    not depend on the token value or its position in the list.
    """
    token_bytes = token.encode("utf-8")
    matched = False
    for candidate in tokens:
        if not isinstance(candidate, str):
            continue
        if hmac.compare_digest(token_bytes, candidate.encode("utf-8")):
            matched = True
    return matched


def strict_priority_trusted(headers: dict | None, *, enabled: bool, tokens: list[str]) -> bool:
    """Trust rule shared by the rate-limit middleware and the dispatch entry.

    True only when the strict channel is enabled, the token list is
    non-empty, and the request carries a matching
    ``X-Strict-Priority-Token``. With ``enabled`` off this is always False —
    the default-off convention keeps the baseline untouched.
    """
    if not enabled or not tokens:
        return False
    token = extract_strict_priority_token(headers)
    return token is not None and _constant_time_token_match(token, tokens)
