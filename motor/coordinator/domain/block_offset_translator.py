# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Translate ``CacheControl.msg_offset`` and ``ContextEdit.start/end`` from
message-level indices into PagedAttention block coordinates.
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any, Callable
from motor.common.logger import get_logger
from motor.coordinator.api_client.conductor_api_client import ConductorApiClient

logger = get_logger(__name__)

_EDIT_TYPE_OFFSET_COMPUTERS: dict[str, Callable] = {}


# DSV4 (vLLM DeepseekV4Tokenizer) chat template uses 3 special tokens as
# message-boundary markers:
#   <|User|>            : start of user/developer/tool-merged messages.
#   <|Assistant|>       : transition at end of user-like messages (also the
#                          logical start of an assistant message).
#   <|end of sentence|> : actual end of an assistant message.
# Note: DSV4 user/system/developer messages carry NO eos; only assistant does.
# Prefix boundaries are therefore tracked along two separate rails: the
# "role-start marker" rail and the "eos" rail.
_DSV4_USER_MARKER: str = "<｜User｜>"
_DSV4_ASSISTANT_MARKER: str = "<｜Assistant｜>"
_DSV4_EOS_MARKER: str = "<｜end▁of▁sentence｜>"

_CHAT_MARKER_CANDIDATES = (
    "<|im_start|>",  # Qwen / Qwen2 / Qwen3
    "<|begin_of_text|>",  # Llama-3
    "<|user|>",  # Some chat-tuned models
    "<|sep|>",  # Mistral / Mixtral
)


def _is_dsv4_tokenizer(tokenizer: Any) -> bool:
    """Detect vLLM's DeepSeekV4 tokenizer wrapper.

    vLLM's deepseek_v4.py:80 sets ``_DeepseekV4Tokenizer.__name__ =
    f"DSV4{...}"`` as a stable contract. But ``from_pretrained`` wraps the
    class again via ``get_cached_tokenizer``, which renames it to
    ``f"Cached{...}"`` — so the leaf class becomes ``CachedDSV4TokenizersBackend``
    and a leaf-only check would miss. We walk the whole MRO to stay robust
    against extra wrapping layers.
    """
    if tokenizer is None:
        return False
    try:
        mro = type(tokenizer).__mro__
    except Exception:  # noqa: BLE001 — defensive: type() shouldn't raise
        return False
    return any((getattr(cls, "__name__", "") or "").startswith("DSV4") for cls in mro)


def _try_get_token_id(tokenizer: Any, tok_str: str) -> int | None:
    """Pure helper for the DSV4 markers.

    Returns the single-token id for ``tok_str``, or None if it is not a
    single token (or tokenization fails).
    """
    if tokenizer is None:
        return None
    try:
        tid = tokenizer.convert_tokens_to_ids(tok_str)
    except Exception:  # noqa: BLE001 — tokenizer API quirks
        return None
    if tid is None or getattr(tokenizer, "unk_token_id", None) == tid:
        return None
    try:
        enc = tokenizer.encode(tok_str, add_special_tokens=False)
    except Exception:  # noqa: BLE001
        return None
    return tid if (len(enc) == 1 and enc[0] == tid) else None


# ---------------------------------------------------------------------------
# Model-family block-offset calculators
# ---------------------------------------------------------------------------
#
# Each model family has its own chat-template rendering, so the prefix / tools
# offset algorithm is per-family. Encapsulated as ``BaseBlockOffsetCalculator``
# subclasses; adding a new model means one subclass + one factory line.
# ``attach_block_offsets`` only calls ``preprocess / compute_messages /
# compute_tools`` and is agnostic to model differences.
#
# Design choice: a single calculator (covering preprocess + messages + tools)
# rather than two split calculators keeps "preprocess once" locked inside one
# object, avoiding skip-or-duplicate inconsistency when prefix and tools would
# otherwise run preprocessing independently.


class BaseBlockOffsetCalculator(ABC):
    """Block-offset calculator for a model family.

    Owns its full chat-template logic (messages-to-token and tools-to-token
    offsets) inside the class, so callers only need to invoke the three
    public methods: ``preprocess / compute_messages / compute_tools``.
    """

    @abstractmethod
    def preprocess(self, messages, tools):
        """Model-family preprocessing of messages/tools. Default is identity;
        DSV4 flattens multipart content and sorts tool results. Returns
        ``(messages, tools)``.
        """
        ...

    @abstractmethod
    def compute_messages(self, messages, tools, tokenizer):
        """Returns a monotonic non-decreasing list of length ``len(messages)``,
        or ``None`` on failure (caller falls back to per-call partial tokenize).
        """
        ...

    @abstractmethod
    def compute_tools(self, messages, tools, tokenizer):
        """Returns a list of length ``len(tools) + 1`` (each tool's start
        plus a trailing sentinel), or ``None`` on failure.
        """
        ...


class Dsv4BlockOffsetCalculator(BaseBlockOffsetCalculator):
    """DSV4-specific calculator.

    Encapsulates the full DSV4 algorithm:
      - ``_try_build_messages_offset_table_via_marker_dsv4``:
        marker-scan fast path (<|User|> / <|Assistant|> / <|end of sentence|>).
      - ``_build_messages_offset_table_dsv4``:
        thin wrapper around the fast path. Returns ``None`` on failure —
        DSV4 tokenization is deterministic, so a missing marker means a
        template mismatch (no iterative fallback).
      - ``_build_tools_offset_table_dsv4``:
        single-pass tokenize + decode + name_marker + reverse ``{`` scan.
      - ``_resolve_tools_end_pos_dsv4``:
        tools-segment end sentinel (char position of the first ``<|User|>``);
        falls back to ``len(full_str)`` when it returns ``None``.
    """

    def preprocess(self, messages, tools):
        from motor.coordinator.scheduler.policy.utils import (
            preprocess_messages_for_dsv4,
        )

        return preprocess_messages_for_dsv4(messages, tools)

    def compute_messages(self, messages, tools, tokenizer):
        return self._build_messages_offset_table_dsv4(messages, tools, tokenizer)

    def compute_tools(self, messages, tools, tokenizer):
        return self._build_tools_offset_table_dsv4(messages, tools, tokenizer)

    def _build_messages_offset_table_dsv4(
        self,
        messages,
        tools,
        tokenizer,
    ) -> list[int] | None:
        """DSV4 messages offset table: marker-only fast path; returns ``None`` on failure."""
        fast = self._try_build_messages_offset_table_via_marker_dsv4(
            messages,
            tools,
            tokenizer,
        )
        if fast is not None:
            return fast
        logger.debug(
            "dsv4 marker-based messages table unavailable (N=%d)",
            len(messages or []),
        )
        return None

    def _try_build_messages_offset_table_via_marker_dsv4(
        self,
        messages,
        tools,
        tokenizer,
    ) -> list[int] | None:
        """DSV4 marker-scan fast path.

        Rules:
          - End of a user-like message (user/developer/tool/merged-tool) =
            the next role marker in the token stream (``<|User|>`` or
            ``<|Assistant|>``, whichever comes first). This handles both
            user→assistant (transition) and user→user (next ``<|User|>``).
          - End of an assistant message = the next ``<|end of sentence|>`` + 1.
          - When ``messages[0]`` is system, the system segment ends at the
            first ``<|User|>``.

        Returns ``None`` on any failure (missing marker, count mismatch,
        unknown role, tokenize error); callers decide what to do next.
        """
        if not messages or not isinstance(messages, list):
            return None

        eos_id = _try_get_token_id(tokenizer, _DSV4_EOS_MARKER)
        user_id = _try_get_token_id(tokenizer, _DSV4_USER_MARKER)
        asst_id = _try_get_token_id(tokenizer, _DSV4_ASSISTANT_MARKER)
        if eos_id is None or user_id is None or asst_id is None:
            return None

        # Single full tokenize (same shape as _apply_chat_template_dsv4).
        try:
            ids = tokenizer.apply_chat_template(
                messages,
                tools=tools,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=False,
            )
        except TypeError:
            try:
                ids = tokenizer.apply_chat_template(
                    messages,
                    tools=tools,
                    add_generation_prompt=True,
                    tokenize=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("dsv4 marker tokenize (no return_dict) failed: %s", exc)
                return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("dsv4 marker tokenize failed: %s", exc)
            return None

        if not isinstance(ids, list) or len(ids) == 0:
            return None

        # Positions for each of the 3 markers (single pass, O(L_total)).
        user_pos: list[int] = [i for i, t in enumerate(ids) if t == user_id]
        asst_pos: list[int] = [i for i, t in enumerate(ids) if t == asst_id]
        eos_pos: list[int] = [i for i, t in enumerate(ids) if t == eos_id]

        n = len(messages)
        has_system_at_start = n > 0 and isinstance(messages[0], dict) and messages[0].get("role") == "system"

        result: list[int] = []
        user_pos_idx = 1 if has_system_at_start else 0
        asst_pos_idx = 0
        eos_consumed = 0

        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                return None
            role = msg.get("role")
            if i == 0 and has_system_at_start:
                if not user_pos:
                    return None
                result.append(user_pos[0])
            elif role in ("user", "developer", "tool"):
                consumed = False
                if asst_pos_idx < len(asst_pos):
                    next_user = user_pos[user_pos_idx + 1] if user_pos_idx + 1 < len(user_pos) else None
                    if next_user is None or asst_pos[asst_pos_idx] < next_user:
                        result.append(asst_pos[asst_pos_idx])
                        asst_pos_idx += 1
                        consumed = True
                if not consumed:
                    if user_pos_idx + 1 < len(user_pos):
                        result.append(user_pos[user_pos_idx + 1])
                        consumed = True
                if not consumed:
                    return None
                user_pos_idx += 1
            elif role == "assistant":
                if eos_consumed >= len(eos_pos):
                    return None
                result.append(eos_pos[eos_consumed] + 1)
                eos_consumed += 1
            else:
                return None  # Unknown role: fall back to per-call prefix.

        if len(result) != n:
            return None
        return result

    def _build_tools_offset_table_dsv4(
        self,
        messages,
        tools,
        tokenizer,
    ) -> list[int] | None:
        """DSV4 tools table built from a single render.

        Instead of N calls of
        ``apply_chat_template(tools=[t_i])``, do a single
        ``apply_chat_template(tools=tools, tokenize=True)`` to obtain the full
        ids, ``decode`` to a string, then locate each tool's start via
        ``name_marker`` + a reverse ``{`` scan.
        """
        if tokenizer is None or not tools or not isinstance(tools, list):
            return None
        if not isinstance(messages, list):
            return None

        # 1. Single full tokenize.
        try:
            ids = tokenizer.apply_chat_template(
                messages,
                tools=tools,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=False,
            )
        except TypeError:
            try:
                ids = tokenizer.apply_chat_template(
                    messages,
                    tools=tools,
                    add_generation_prompt=True,
                    tokenize=True,
                )
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.debug("dsv4 tools table: tokenize failed: %s", exc)
                return None
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("dsv4 tools table: tokenize failed: %s", exc)
            return None

        if not isinstance(ids, list) or len(ids) == 0:
            return None

        # 2. Decode to string once.
        try:
            full_str = tokenizer.decode(ids)
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("dsv4 tools table: decode failed: %s", exc)
            return None
        if not isinstance(full_str, str) or not full_str:
            return None

        # 3. Per tool: name_marker + reverse '{' scan.
        char_positions: list[int] = []
        for tool in tools:
            if not isinstance(tool, dict):
                return None
            fn = tool.get("function") or {}
            if not isinstance(fn, dict):
                return None
            tool_name = str(fn.get("name") or "")
            if not tool_name:
                return None
            search_from = char_positions[-1] if char_positions else 0
            name_marker = f'"name": "{tool_name}"'
            nm_idx = full_str.find(name_marker, search_from)
            if nm_idx < 0:
                return None
            obj_open = full_str.rfind('{', search_from, nm_idx)
            if obj_open < 0:
                return None
            char_positions.append(obj_open)

        # 4. End of tools segment: reuse _resolve_tools_end_pos_dsv4.
        end_pos = self._resolve_tools_end_pos_dsv4(full_str, char_positions)
        if end_pos is None:
            end_pos = len(full_str)
        char_positions.append(end_pos)

        # 5. char → token.
        token_positions: list[int] = []
        try:
            for cp in char_positions:
                prefix_ids = tokenizer.encode(full_str[:cp], add_special_tokens=False)
                token_positions.append(len(prefix_ids))
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.debug("dsv4 tools table: prefix encode failed: %s", exc)
            return None

        # 6. Monotonicity check.
        for i in range(1, len(token_positions)):
            if token_positions[i] < token_positions[i - 1]:
                return None

        return token_positions

    def _resolve_tools_end_pos_dsv4(
        self,
        full_str,
        char_positions,
    ) -> int | None:
        """DSV4 tools-segment char end (first ``<|User|>``).

        Returns ``None`` (caller falls back to ``len(full_str)``) when:
          - ``<|User|>`` is not in ``full_str``.
          - ``<|User|>`` position <= last tool start (anomalous; conservative).
          - ``full_str.find`` raises.
        """
        if not char_positions:
            return None
        try:
            um = full_str.find(_DSV4_USER_MARKER)
        except Exception:  # noqa: BLE001 — defensive
            return None
        if um <= char_positions[-1]:
            return None
        return um


class StandardBlockOffsetCalculator(BaseBlockOffsetCalculator):
    """Generic calculator for Qwen / Llama-3 / Mistral.

    Encapsulates the standard algorithm:
      - ``_build_messages_offset_table``: chat-marker counting.
      - ``_detect_chat_marker_id``: first single-token marker id from
        ``_CHAT_MARKER_CANDIDATES``.
      - ``_build_tools_offset_table``: 1+N renders + char anchors +
        ``</tools>`` closer (original ``build_tools_offset_table`` body).
    """

    def preprocess(self, messages, tools):
        return messages, tools  # identity

    def compute_messages(self, messages, tools, tokenizer):
        return self._build_messages_offset_table(messages, tools, tokenizer)

    def compute_tools(self, messages, tools, tokenizer):
        return self._build_tools_offset_table(messages, tools, tokenizer)

    def _build_messages_offset_table(
        self,
        messages,
        tools,
        tokenizer,
    ) -> list[int] | None:
        """Generic chat-marker counting: cumulative token offsets per message index."""
        if tokenizer is None:
            return None
        if not messages or not isinstance(messages, list):
            return None

        marker_id = self._detect_chat_marker_id(tokenizer)
        if marker_id is None:
            logger.warning(
                "no chat-template marker token found in tokenizer vocab; "
                "messages offset table unavailable, callers must fall back"
            )
            return None

        try:
            ids = tokenizer.apply_chat_template(
                messages,
                tools=tools,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=False,
            )
        except TypeError:
            ids = tokenizer.apply_chat_template(
                messages,
                tools=tools,
                add_generation_prompt=True,
                tokenize=True,
            )
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                "build_messages_offset_table: full tokenization failed: %s",
                exc,
            )
            return None

        if not isinstance(ids, list) or len(ids) == 0:
            return None

        im_starts = [i for i, t in enumerate(ids) if t == marker_id]
        expected_with_gen = len(messages) + 1
        expected_no_gen = len(messages)
        if len(im_starts) == expected_with_gen:
            return [im_starts[i + 1] for i in range(len(messages))]
        if len(im_starts) == expected_no_gen:
            return [im_starts[i + 1] if i + 1 < len(im_starts) else len(ids) for i in range(len(messages))]
        logger.warning(
            "build_messages_offset_table: marker count %d unexpected "
            "(messages=%d, expected %d or %d); table unavailable",
            len(im_starts),
            len(messages),
            expected_with_gen,
            expected_no_gen,
        )
        return None

    def _detect_chat_marker_id(self, tokenizer) -> int | None:
        """Find the first single-token marker id in ``_CHAT_MARKER_CANDIDATES``."""
        if tokenizer is None:
            return None
        for tok in _CHAT_MARKER_CANDIDATES:
            try:
                tid = tokenizer.convert_tokens_to_ids(tok)
            except Exception:  # noqa: BLE001 — tokenizer API quirks
                continue
            if tid is not None and getattr(tokenizer, "unk_token_id", None) != tid:
                try:
                    enc = tokenizer.encode(tok, add_special_tokens=False)
                except Exception:
                    continue
                if len(enc) == 1 and enc[0] == tid:
                    return tid
        return None

    def _build_tools_offset_table(
        self,
        messages,
        tools,
        tokenizer,
    ) -> list[int] | None:
        """1+N renders + char anchors + ``</tools>`` closer.

        Original ``build_tools_offset_table`` body moved here verbatim — same
        semantics, same control flow. The trailing sentinel falls back to
        ``len(full_str)`` when no closer matches (DSV4 template, no
        ``</tools>`` wrapping).
        """
        if tokenizer is None:
            return None
        if not tools or not isinstance(tools, list):
            return None
        if not isinstance(messages, list):
            return None

        # 1. Render the full prompt as a string.
        try:
            full_str = tokenizer.apply_chat_template(
                messages,
                tools=tools,
                add_generation_prompt=True,
                tokenize=False,
                return_dict=False,
            )
        except TypeError:
            try:
                full_str = tokenizer.apply_chat_template(
                    messages,
                    tools=tools,
                    add_generation_prompt=True,
                    tokenize=False,
                )
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.warning(
                    "build_tools_offset_table: full tokenize failed: %s",
                    exc,
                )
                return None
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                "build_tools_offset_table: full tokenize failed: %s",
                exc,
            )
            return None

        if not isinstance(full_str, str) or not full_str:
            return None

        # 2. For each tool, locate its character position in full_str.
        char_positions: list[int] = []
        for i, tool in enumerate(tools):
            # 2a. Render this single tool to capture its exact rendered form.
            try:
                s_i = tokenizer.apply_chat_template(
                    messages,
                    tools=[tool],
                    add_generation_prompt=True,
                    tokenize=False,
                    return_dict=False,
                )
            except TypeError:
                try:
                    s_i = tokenizer.apply_chat_template(
                        messages,
                        tools=[tool],
                        add_generation_prompt=True,
                        tokenize=False,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "build_tools_offset_table: single-tool render for tool[%d] failed: %s",
                        i,
                        exc,
                    )
                    return None
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "build_tools_offset_table: single-tool render for tool[%d] failed: %s",
                    i,
                    exc,
                )
                return None

            if not isinstance(s_i, str) or not s_i:
                logger.warning(
                    "build_tools_offset_table: single-tool render for tool[%d] returned empty/non-string",
                    i,
                )
                return None

            # 2b. Locate the tool's name in s_i (unique anchor).
            tool_name = ""
            if isinstance(tool, dict):
                fn = tool.get("function") or {}
                if isinstance(fn, dict):
                    tool_name = str(fn.get("name") or "")
            if not tool_name:
                logger.warning(
                    "build_tools_offset_table: tool[%d] has no function.name; cannot anchor",
                    i,
                )
                return None
            name_marker = f'"name": "{tool_name}"'
            s_i_name_idx = s_i.find(name_marker)
            if s_i_name_idx < 0:
                logger.warning(
                    "build_tools_offset_table: name %r not found in single-tool render for tool[%d]",
                    tool_name,
                    i,
                )
                return None

            # 2c. Locate this tool's JSON opener in full_str by walking forward
            # through `{"type": "function"` occurrences and matching the next name.
            search_from = char_positions[-1] if char_positions else 0
            candidate_prefix = '{"type": "function"'
            idx = full_str.find(candidate_prefix, search_from)
            found = False
            while idx >= 0:
                next_name_idx = full_str.find('"name":', idx)
                if next_name_idx < 0:
                    break
                name_start = full_str.find('"', next_name_idx + len('"name":'))
                if name_start < 0:
                    break
                name_end = full_str.find('"', name_start + 1)
                if name_end < 0:
                    break
                found_name = full_str[name_start + 1 : name_end]
                if found_name == tool_name:
                    char_positions.append(idx)
                    found = True
                    break
                idx = full_str.find(candidate_prefix, idx + 1)
            if not found:
                # Fallback: chat template may not use {"type": "function" wrapper
                # (e.g., Llama-3 inlines tools differently). Use name_marker
                # itself as anchor and back-walk to the nearest `{`.
                nm = full_str.find(name_marker, search_from)
                if nm < 0:
                    logger.warning(
                        "build_tools_offset_table: tool[%d] (%s) not located in full_str",
                        i,
                        tool_name,
                    )
                    return None
                obj_open = full_str.rfind('{', search_from, nm)
                if obj_open < 0:
                    logger.warning(
                        "build_tools_offset_table: tool[%d] (%s) opener `{` not found before name",
                        i,
                        tool_name,
                    )
                    return None
                char_positions.append(obj_open)

        # 3. End-of-tools-segment char position: prefer `</tools>` closer if present.
        end_pos = len(full_str)
        for closer in ('</tools>', '</tools_section>'):
            idx = full_str.rfind(closer)
            if idx > char_positions[-1]:
                end_pos = idx
                break
        char_positions.append(end_pos)

        # 4. Convert char positions → token positions via prefix encodes.
        token_positions: list[int] = []
        try:
            for cp in char_positions:
                prefix_ids = tokenizer.encode(full_str[:cp], add_special_tokens=False)
                token_positions.append(len(prefix_ids))
        except Exception as exc:  # noqa: BLE001 — fail-open
            logger.warning(
                "build_tools_offset_table: encode prefix failed: %s",
                exc,
            )
            return None

        # 5. Sanity check: monotonic non-decreasing.
        for i in range(1, len(token_positions)):
            if token_positions[i] < token_positions[i - 1]:
                logger.warning(
                    "build_tools_offset_table: non-monotonic token position at index %d (%d < %d)",
                    i,
                    token_positions[i],
                    token_positions[i - 1],
                )
                return None

        return token_positions


def get_block_offset_calculator(tokenizer) -> BaseBlockOffsetCalculator:
    """Pick the right calculator for the tokenizer type.

    Centralized here so adding a new model family touches only this function.
    """
    if _is_dsv4_tokenizer(tokenizer):
        return Dsv4BlockOffsetCalculator()
    return StandardBlockOffsetCalculator()


def _register_edit_type(name: str) -> Callable[[Callable], Callable]:
    """Decorator: register a per-edit-type offset computer.

    Usage::

        @_register_edit_type("compact")
        def _compact_offset(messages, tools, msg_idx, block_size, tokenizer):
            ...
    """

    def deco(fn: Callable) -> Callable:
        _EDIT_TYPE_OFFSET_COMPUTERS[name] = fn
        return fn

    return deco


def _prefix_diff_block_offset(
    messages: list,
    tools: list | None,
    msg_idx: int,
    block_size: int,
    tokenizer: Any,
    messages_table: list[int] | None = None,
) -> tuple[int, int, int] | None:
    if not messages or not isinstance(messages, list):
        return None
    if msg_idx < 0 or msg_idx >= len(messages):
        return None
    if messages_table is None or msg_idx >= len(messages_table):
        # No messages_table or out-of-range: cannot derive precise coords
        # without per-call partial tokenize. Return None so attach_block_offsets
        # falls back to its own per-call partial tokenization.
        return None

    token_idx = messages_table[msg_idx]
    if token_idx <= 0:
        return None

    return (token_idx // block_size, token_idx % block_size, token_idx)


def _align_block_for_op(
    token_idx: int,
    block_size: int,
    align_policy: str,
    boundary: str,
) -> int:
    """Align an absolute token offset ``token_idx`` to a block index.

    Args:
        token_idx: Absolute token offset (range start or end).
        block_size: PagedAttention block size; <= 0 is invalid.
        align_policy: Alignment policy (see below). Dispatch paths pass
            ``"positive"`` (see ``*_offset_computer``s) or the raw ``edit_type``
            string (tools branch of ``compute_edit_block_offset``).
        boundary: ``"start"`` / ``"end"`` — whether token_idx is the range's
            start or end. Only used by ``negative``; rounding direction
            differs for start vs end.

    Policy semantics:
      - ``negative`` (shrink): keep only full blocks inside [start, end];
        prefer under-shooting to touching out-of-range tokens.
          * start mid-block (intra != 0)              -> round up to next block.
          * end not at block tail (intra != size-1)   -> round down to prev block.
          * Exactly on a boundary -> no rounding; result clamped to >= 0.
      - ``positive`` (truncate): use ``token_idx // block_size`` — the block
        containing token_idx, which may include out-of-range edge tokens.
      - Unknown values: behave like ``positive`` (fail-open, no exception).

    Returns:
        Aligned block index (>= 0). Invalid block_size fails open to 0.
    """
    # Invalid block_size (<=0): skip division and fail-open to block 0.
    if block_size <= 0:
        return 0
    block = token_idx // block_size
    intra = token_idx % block_size
    match align_policy:
        case "negative":
            # Shrink: start rounds up, end rounds down — only full blocks.
            if boundary == "start":
                # intra == 0: start aligns with block head; that block is in range.
                aligned = block + (1 if intra != 0 else 0)
            elif boundary == "end":
                # intra == block_size-1: end aligns with block tail; that block is in range.
                aligned = block - (1 if intra != block_size - 1 else 0)
            else:
                # Invalid boundary: degenerate to truncation, no directional rounding.
                aligned = block
            # End rounding may yield -1 (token_idx mid-block 0); clamp to 0.
            return max(0, aligned)
        case "positive":
            # Truncation: take the block containing token_idx, regardless of boundary.
            return block
        case _:
            # Unknown policy == positive; safe for newly added unregistered edit_types.
            return block


@_register_edit_type("offload")
def _offload_offset_computer(
    messages: list,
    tools: list | None,
    msg_idx: int,
    block_size: int,
    tokenizer: Any,
    boundary: str,
    messages_table: list[int] | None = None,
) -> tuple[int, int, int] | None:
    bo = _prefix_diff_block_offset(
        messages,
        tools,
        msg_idx,
        block_size,
        tokenizer,
        messages_table=messages_table,
    )
    if bo is None:
        return None
    _, intra, token_idx = bo
    aligned = _align_block_for_op(token_idx, block_size, "positive", boundary)
    return (aligned, intra, token_idx)


@_register_edit_type("prefetch")
def _prefetch_offset_computer(
    messages: list,
    tools: list | None,
    msg_idx: int,
    block_size: int,
    tokenizer: Any,
    boundary: str,
    messages_table: list[int] | None = None,
) -> tuple[int, int, int] | None:
    bo = _prefix_diff_block_offset(
        messages,
        tools,
        msg_idx,
        block_size,
        tokenizer,
        messages_table=messages_table,
    )
    if bo is None:
        return None
    _, intra, token_idx = bo
    aligned = _align_block_for_op(token_idx, block_size, "positive", boundary)
    return (aligned, intra, token_idx)


@_register_edit_type("evict")
def _evict_offset_computer(
    messages: list,
    tools: list | None,
    msg_idx: int,
    block_size: int,
    tokenizer: Any,
    boundary: str,
    messages_table: list[int] | None = None,
) -> tuple[int, int, int] | None:
    bo = _prefix_diff_block_offset(
        messages,
        tools,
        msg_idx,
        block_size,
        tokenizer,
        messages_table=messages_table,
    )
    if bo is None:
        return None
    _, intra, token_idx = bo
    aligned = _align_block_for_op(token_idx, block_size, "positive", boundary)
    return (aligned, intra, token_idx)


def compute_block_offset(
    messages: list,
    tools: list | None,
    msg_offset: int | None,
    block_size: int,
    tokenizer: Any,
    messages_table: list[int] | None = None,
) -> tuple[int, int, int] | None:
    if not isinstance(block_size, int) or block_size <= 0:
        logger.warning("invalid block_size=%r; cannot compute block_offset", block_size)
        return None
    if not messages or not isinstance(messages, list):
        logger.warning("messages is empty or invalid; cannot compute block_offset")
        return None
    if messages_table is None:
        # No messages_table: cannot give precise coords. attach_block_offsets
        # already logged a WARNING upstream; just return None here.
        return None

    effective = msg_offset - 1  # msg_offset range [1, n]; TTL protects messages[0, msg_offset).
    if effective < 0 or effective >= len(messages_table):
        return None
    token_idx = messages_table[effective]

    if token_idx == 0:
        logger.warning(
            "token_idx == 0 for effective msg_offset=%d; tokenizer configuration suspect?",
            effective,
        )
        return None

    return (token_idx // block_size, token_idx % block_size, token_idx)


def compute_edit_block_offset(
    edit_type: str,
    messages: list,
    tools: list | None,
    start: int | None,
    end: int | None,
    block_size: int,
    tokenizer: Any,
    target: str = "messages",
    messages_table: list[int] | None = None,
    tools_table: list[int] | None = None,
) -> tuple[
    tuple[int, int, int] | None,
    tuple[int, int, int] | None,
]:
    if not isinstance(block_size, int) or block_size <= 0:
        logger.warning("invalid block_size=%r; cannot compute edit_block_offset", block_size)
        return None

    if target == "session":
        logger.info(
            "target='session' for edit_type=%r; overriding start/end to [None, None] and skipping offset computation",
            edit_type,
        )
        return (None, None)

    if target == "tools":
        if not tools or not isinstance(tools, list):
            logger.warning(
                "target='tools' but tools is empty/None; returning (None, None)",
            )
            return (None, None)
        if not (isinstance(tools_table, list) and len(tools_table) == len(tools) + 1):
            logger.warning(
                "target='tools' but tools_offset_table unavailable "
                "(tools_table=%r, len(tools)=%d); returning (None, None)",
                tools_table if isinstance(tools_table, list) else type(tools_table).__name__,
                len(tools),
            )
            return (None, None)

        n_tools = len(tools)
        eff_start = 0 if start is None else max(0, min(start, n_tools))
        eff_end = n_tools if end is None else max(0, min(end, n_tools))

        if start is not None and end is not None and start > end:
            logger.warning(
                "context_edit.start=%d > end=%d (target='tools'); not corrected",
                start,
                end,
            )

        # tools_table[i] = absolute token offset of tools[i]'s first token
        # in the FULL render stream.
        if eff_start == 0:
            ts = tools_table[0]
            bo_start = (ts // block_size, ts % block_size, ts)
        else:
            ts = tools_table[eff_start]
            aligned = _align_block_for_op(ts, block_size, edit_type, "start")
            bo_start = (aligned, ts % block_size, ts)

        te = tools_table[eff_end]
        aligned_e = _align_block_for_op(te, block_size, edit_type, "end")
        bo_end = (aligned_e, te % block_size, te)

        if eff_start == eff_end:
            # Empty range (parity with messages path): bo_end signals "no end offset".
            return (bo_start, None)
        return (bo_start, bo_end)

    if edit_type not in _EDIT_TYPE_OFFSET_COMPUTERS:
        logger.warning(
            "Unknown edit_type=%r; skipping offset conversion (registered: %s)",
            edit_type,
            sorted(_EDIT_TYPE_OFFSET_COMPUTERS.keys()),
        )
        return (None, None)

    computer = _EDIT_TYPE_OFFSET_COMPUTERS[edit_type]
    n = len(messages) if isinstance(messages, list) else 0

    if n == 0:
        return (None, None)

    eff_start = 0 if start is None else (max(0, min(start, n - 1)))
    eff_end = n if end is None else (max(0, min(end, n)))

    if start is not None and end is not None and start > end:
        logger.warning(
            "context_edit.start=%d > end=%d; not corrected (left to downstream)",
            start,
            end,
        )

    if eff_start == 0:
        start_target = -1  # sentinel: handled below
    else:
        start_target = eff_start - 1

    if start_target == -1:
        bo_start = (0, 0, 0)
    else:
        bo_start = computer(
            messages,
            tools,
            start_target,
            block_size,
            tokenizer,
            boundary="start",
            messages_table=messages_table,
        )
    bo_end = computer(
        messages,
        tools,
        eff_end - 1,
        block_size,
        tokenizer,
        boundary="end",
        messages_table=messages_table,
    )
    return (bo_start, bo_end)


def attach_block_offsets(req_info, messages, tools, tokenizer=None) -> None:
    """Fill server-side block coordinates on ``req_info``'s agent hint.

    ``tokenizer`` is injected by the caller (the scheduler layer owns
    ``TokenizerManager``) so this domain module never imports back into
    ``scheduler.policy``.
    """
    try:
        hint = getattr(req_info, "agent_hint_info", None)
        if hint is None or not getattr(hint, "session_id", None):
            return

        cm = getattr(hint, "context_management", None)
        manage_request = bool(getattr(cm, "manage_request", False)) if cm is not None else False
        if not manage_request:
            return

        is_target_session = False
        if cm is not None and cm.edits:
            agent_hint = req_info.req_data.get("agent_hint") or {}
            cm_dict = agent_hint.setdefault("context_management", {})
            edits_list = cm_dict.get("edits")

            if not isinstance(edits_list, list) or len(edits_list) != len(cm.edits):
                edits_list = [
                    {
                        "type": edit.type,
                        "target": edit.target,
                        "start": edit.start,
                        "end": edit.end,
                    }
                    for edit in cm.edits
                ]

            for i, edit in enumerate(cm.edits):
                if edit.target == "session":
                    is_target_session = True
                    break

        cc = getattr(hint, "cache_control", None)
        kv_conductor_config = ConductorApiClient.coordinator_config.scheduler_config.kv_conductor_config
        block_size = getattr(kv_conductor_config, "block_size", 128)
        if is_target_session and cc is None:
            messages_table = None
            tools_table = None
        else:
            logger.debug(
                "attach_block_offsets: msgs=%d tools=%d",
                len(messages or []),
                len(tools or []),
            )

            calc = get_block_offset_calculator(tokenizer)
            messages, tools = calc.preprocess(messages, tools)
            messages_table = calc.compute_messages(messages, tools, tokenizer)
            if messages_table is None:
                logger.warning(
                    "attach_block_offsets: messages offset table is unavailable; "
                    "target='messages' edits will fall back to (None, None)."
                )

            tools_table = calc.compute_tools(messages, tools, tokenizer)
            if tools_table is None and tools:
                logger.warning(
                    "attach_block_offsets: tools offset table unavailable; "
                    "target='tools' edits will fall back to (None, None).",
                )

        if cc is not None:
            bo = compute_block_offset(
                messages=messages,
                tools=tools,
                msg_offset=cc.msg_offset,
                block_size=block_size,
                tokenizer=tokenizer,
                messages_table=messages_table,
            )
            if bo is not None:
                block_idx, intra, token_idx = bo
                cc.block_offset = block_idx
                cc.intra_block_offset = intra
                cc.token_offset = token_idx

                agent_hint = req_info.req_data.get("agent_hint") or {}
                cc_dict = agent_hint.setdefault("cache_control", {})
                cc_dict["block_offset"] = block_idx
                cc_dict["intra_block_offset"] = intra
                cc_dict["token_offset"] = token_idx
                req_info.req_data["agent_hint"] = agent_hint

        if cm is not None and cm.edits:
            agent_hint = req_info.req_data.get("agent_hint") or {}
            cm_dict = agent_hint.setdefault("context_management", {})
            edits_list = cm_dict.get("edits")

            if not isinstance(edits_list, list) or len(edits_list) != len(cm.edits):
                edits_list = [
                    {
                        "type": edit.type,
                        "target": edit.target,
                        "start": edit.start,
                        "end": edit.end,
                    }
                    for edit in cm.edits
                ]

            for i, edit in enumerate(cm.edits):
                if edit.target == "session":
                    n = len(messages) if isinstance(messages, list) else 0
                    eff_end = max(0, n)
                    edit.start = 0
                    edit.end = eff_end
                    if i < len(edits_list) and isinstance(edits_list[i], dict):
                        edits_list[i]["start"] = 0
                        edits_list[i]["end"] = eff_end
                    continue

                bo_start, bo_end = compute_edit_block_offset(
                    edit_type=edit.type,
                    target=edit.target,
                    messages=messages,
                    tools=tools,
                    start=edit.start,
                    end=edit.end,
                    block_size=block_size,
                    tokenizer=tokenizer,
                    messages_table=messages_table,
                    tools_table=tools_table,
                )
                if bo_start is not None:
                    bs, is_, ts = bo_start
                    edit.block_start = bs
                    edit.block_intra_start = is_
                    edit.start_token = ts
                if bo_end is not None:
                    be, ie, te = bo_end
                    edit.block_end = be
                    edit.block_intra_end = ie
                    edit.end_token = te
                if bo_start is not None:
                    edits_list[i]["block_start"] = bo_start[0]
                    edits_list[i]["block_intra_start"] = bo_start[1]
                    edits_list[i]["start_token"] = bo_start[2]
                if bo_end is not None:
                    edits_list[i]["block_end"] = bo_end[0]
                    edits_list[i]["block_intra_end"] = bo_end[1]
                    edits_list[i]["end_token"] = bo_end[2]

            cm_dict["edits"] = edits_list
            req_info.req_data["agent_hint"] = agent_hint
    except Exception as exc:  # noqa: BLE001 — fail-open on hot path
        logger.warning("attach_block_offsets failed, skipping: %s", exc)
        return
