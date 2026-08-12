# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of MulanPSL2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for message and tool block-offset translation."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from motor.coordinator.domain.agent_hint import CacheControl, ContextEdit, ContextManagement, parse_agent_hint
from motor.coordinator.domain.block_offset_translator import (
    Dsv4BlockOffsetCalculator,
    StandardBlockOffsetCalculator,
    _align_block_for_op,
    attach_block_offsets,
    compute_block_offset,
    compute_edit_block_offset,
    get_block_offset_calculator,
)


class MarkerTokenizer:
    """Small tokenizer double exposing the standard marker contract."""

    unk_token_id = -1

    def convert_tokens_to_ids(self, token):
        return {"<|im_start|>": 7}.get(token, -1)

    def encode(self, text, add_special_tokens=False):
        if text == "<|im_start|>":
            return [7]
        return list(range(len(text)))

    def apply_chat_template(self, messages, **kwargs):
        if kwargs.get("tokenize"):
            return [7, 11, 12, 7, 21, 7, 31]
        return "prefix"


class Dsv4Tokenizer:
    """Tokenizer double whose class name exercises DSV4 calculator selection."""

    unk_token_id = -1

    def convert_tokens_to_ids(self, token):
        return {"<｜User｜>": 1, "<｜Assistant｜>": 2, "<｜end▁of▁sentence｜>": 3}.get(token, -1)

    def encode(self, text, add_special_tokens=False):
        if text == "<｜User｜>":
            return [1]
        if text == "<｜Assistant｜>":
            return [2]
        if text == "<｜end▁of▁sentence｜>":
            return [3]
        return list(range(len(text)))

    def apply_chat_template(self, messages, **kwargs):
        if kwargs.get("tokenize"):
            return [1, 10, 2, 20, 3, 1, 30]
        return ""


Dsv4Tokenizer.__name__ = "DSV4Tokenizer"


def test_get_block_offset_calculator_selects_model_family():
    """DSV4 wrappers use the marker calculator while ordinary tokenizers use standard logic."""
    assert isinstance(get_block_offset_calculator(Dsv4Tokenizer()), Dsv4BlockOffsetCalculator)
    assert isinstance(get_block_offset_calculator(MarkerTokenizer()), StandardBlockOffsetCalculator)


@pytest.mark.parametrize(
    "token_idx, block_size, policy, boundary, expected",
    [
        (9, 8, "positive", "start", 1),
        (9, 8, "negative", "start", 2),
        (9, 8, "negative", "end", 0),
        (7, 8, "negative", "end", 0),
        (9, 8, "unknown", "start", 1),
        (9, 0, "positive", "start", 0),
    ],
)
def test_align_block_for_operation_policies(token_idx, block_size, policy, boundary, expected):
    """Alignment policy controls whether partial edge blocks are retained or removed."""
    assert _align_block_for_op(token_idx, block_size, policy, boundary) == expected


def test_compute_block_offset_uses_one_based_message_offset():
    """The first message offset maps to the first token in the supplied offset table."""
    result = compute_block_offset(
        messages=[{}, {}],
        tools=None,
        msg_offset=2,
        block_size=8,
        tokenizer=None,
        messages_table=[5, 17],
    )

    assert result == (2, 1, 17)


def test_compute_edit_block_offset_messages_uses_full_range_sentinels():
    """Message edits use token zero for an open range and the selected end message for its end."""
    result = compute_edit_block_offset(
        edit_type="offload",
        messages=[{}, {}, {}],
        tools=None,
        start=None,
        end=2,
        block_size=8,
        tokenizer=None,
        messages_table=[5, 17, 25],
    )

    assert result == ((0, 0, 0), (2, 1, 17))


def test_compute_edit_block_offset_tools_supports_negative_alignment():
    """Tool ranges shrink to complete blocks for evict operations."""
    result = compute_edit_block_offset(
        edit_type="evict",
        messages=[{}],
        tools=[{"function": {"name": "a"}}, {"function": {"name": "b"}}],
        start=1,
        end=2,
        block_size=8,
        tokenizer=None,
        target="tools",
        tools_table=[3, 16, 23],
    )

    assert result == ((2, 0, 16), (2, 7, 23))


def test_compute_edit_block_offset_returns_none_for_session_target():
    """Session-level edits are represented by the whole-session operation, not coordinates."""
    assert compute_edit_block_offset("offload", [{}], None, 1, 2, 8, None, target="session", messages_table=[5]) == (
        None,
        None,
    )


def test_attach_block_offsets_populates_model_and_request_coordinates():
    """Attached coordinates are written to both parsed models and the outbound request."""
    edit = ContextEdit(type="offload", start=1, end=2, target="messages")
    hint = SimpleNamespace(
        session_id="s",
        cache_control=CacheControl(msg_offset=2),
        context_management=ContextManagement(manage_request=True, edits=[edit]),
    )
    req_info = SimpleNamespace(
        agent_hint_info=hint,
        req_data={
            "agent_hint": {
                "cache_control": {"msg_offset": 2},
                "context_management": {"edits": [{"type": "offload", "start": 1, "end": 2}]},
            }
        },
    )
    config = SimpleNamespace(scheduler_config=SimpleNamespace(kv_conductor_config=SimpleNamespace(block_size=8)))

    with (
        patch("motor.coordinator.domain.block_offset_translator.ConductorApiClient.coordinator_config", config),
        patch("motor.coordinator.domain.block_offset_translator.get_block_offset_calculator") as get_calculator,
    ):
        calculator = get_calculator.return_value
        calculator.preprocess.return_value = ([{}, {}], None)
        calculator.compute_messages.return_value = [5, 17]
        calculator.compute_tools.return_value = None
        attach_block_offsets(req_info, [{}, {}], None, tokenizer=object())

    assert hint.cache_control.token_offset == 17
    assert hint.cache_control.block_offset == 2
    assert edit.start_token == 5
    assert edit.end_token == 17
    assert req_info.req_data["agent_hint"]["cache_control"]["token_offset"] == 17
    assert req_info.req_data["agent_hint"]["context_management"]["edits"][0]["end_token"] == 17


def test_attach_block_offsets_uses_parsed_pydantic_hint():
    """Regression: hints built via parse_agent_hint() must populate offsets even though
    manage_request lives at context_management.manage_request, not at the top level
    on AgentHintInfo. The SimpleNamespace-based positive test alone cannot catch this.
    """
    request_json = {
        "messages": [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        "agent_hint": {
            "session_id": "s",
            "cache_control": {"msg_offset": 2},
            "context_management": {
                "manage_request": True,
                "edits": [{"type": "offload", "start": 1, "end": 2}],
            },
        },
    }
    hint = parse_agent_hint(request_json)

    # Schema guards: manage_request must NOT be hoisted to AgentHintInfo.
    assert not hasattr(hint, "manage_request")
    assert hint.context_management is not None
    assert hint.context_management.manage_request is True

    req_info = SimpleNamespace(
        agent_hint_info=hint,
        req_data={
            "agent_hint": {
                "cache_control": {"msg_offset": 2},
                "context_management": {
                    "manage_request": True,
                    "edits": [{"type": "offload", "start": 1, "end": 2}],
                },
            }
        },
    )
    config = SimpleNamespace(scheduler_config=SimpleNamespace(kv_conductor_config=SimpleNamespace(block_size=8)))

    with (
        patch("motor.coordinator.domain.block_offset_translator.ConductorApiClient.coordinator_config", config),
        patch("motor.coordinator.domain.block_offset_translator.get_block_offset_calculator") as get_calculator,
    ):
        calculator = get_calculator.return_value
        calculator.preprocess.return_value = ([{}, {}], None)
        calculator.compute_messages.return_value = [5, 17]
        calculator.compute_tools.return_value = None
        attach_block_offsets(req_info, [{}, {}], None, tokenizer=object())

    # Real Pydantic path now reaches the offset-computation branch.
    assert hint.cache_control is not None
    assert hint.cache_control.token_offset == 17
    assert hint.cache_control.block_offset == 2
    assert req_info.req_data["agent_hint"]["cache_control"]["token_offset"] == 17


def test_attach_block_offsets_preserves_edit_semantics_after_partial_filter():
    """Partial edit filtering at parse time must not strip type/target/start/end
    from surviving edits when attach_block_offsets rebuilds the list.

    Regression: the rebuild path previously produced dicts with only block_* fields,
    silently dropping offload/prefetch/evict type and target — downstream could no
    longer dispatch the edit or know its scope.
    """
    # 2 messages; second edit has end > len(messages) so _validate_edit_indices drops it.
    request_json = {
        "messages": [{"role": "user"}, {"role": "assistant"}],
        "agent_hint": {
            "session_id": "s",
            "context_management": {
                "manage_request": True,
                "edits": [
                    {"type": "offload", "start": 0, "end": 1, "target": "messages"},
                    {"type": "evict", "start": 0, "end": 99, "target": "messages"},
                    {"type": "prefetch", "start": 0, "end": 2, "target": "messages"},
                ],
            },
        },
    }
    hint = parse_agent_hint(request_json)
    assert hint.context_management is not None
    assert len(hint.context_management.edits) == 2  # Parser kept 2 of 3.

    # Simulate the dispatch path: req_data carries the RAW (unfiltered) request body
    # while cm.edits is filtered. Length mismatch triggers the rebuild branch.
    raw_edits = [dict(e) for e in request_json["agent_hint"]["context_management"]["edits"]]
    req_info = SimpleNamespace(
        agent_hint_info=hint,
        req_data={
            "agent_hint": {
                "context_management": {
                    "manage_request": True,
                    "edits": raw_edits,
                }
            }
        },
    )
    config = SimpleNamespace(scheduler_config=SimpleNamespace(kv_conductor_config=SimpleNamespace(block_size=8)))

    with (
        patch("motor.coordinator.domain.block_offset_translator.ConductorApiClient.coordinator_config", config),
        patch("motor.coordinator.domain.block_offset_translator.get_block_offset_calculator") as get_calculator,
    ):
        calculator = get_calculator.return_value
        calculator.preprocess.return_value = ([{}, {}], None)
        calculator.compute_messages.return_value = [5, 17]
        calculator.compute_tools.return_value = None
        attach_block_offsets(req_info, [{}, {}], None, tokenizer=object())

    out_edits = req_info.req_data["agent_hint"]["context_management"]["edits"]
    # Length aligned to surviving edits, NOT the original raw list.
    assert len(out_edits) == 2
    # Semantic fields preserved on each surviving edit (the regression surface).
    assert [(e["type"], e["target"], e["start"], e["end"]) for e in out_edits] == [
        ("offload", "messages", 0, 1),
        ("prefetch", "messages", 0, 2),
    ]
    # Block offsets still populated.
    assert all("block_start" in e and "end_token" in e for e in out_edits)


def test_attach_block_offsets_skips_non_management_hint():
    """Non-management requests are left untouched and do not require tokenizer access."""
    edit = ContextEdit(type="offload", start=0, end=1)
    req_info = SimpleNamespace(
        agent_hint_info=SimpleNamespace(
            session_id="s", manage_request=False, cache_control=None, context_management=ContextManagement(edits=[edit])
        ),
        req_data={},
    )

    attach_block_offsets(req_info, [{}], None)

    assert edit.block_start is None


def test_dsv4_message_offsets_follow_role_markers():
    """DSV4 message boundaries use user, assistant, and EOS markers rather than generic markers."""
    calculator = Dsv4BlockOffsetCalculator()

    assert calculator.compute_messages([{"role": "user"}, {"role": "assistant"}], [], Dsv4Tokenizer()) == [2, 5]


def test_standard_message_offsets_fail_open_without_marker():
    """A tokenizer without a recognized chat marker yields no offset table."""
    tokenizer = SimpleNamespace(
        convert_tokens_to_ids=lambda _: -1, unk_token_id=-1, encode=lambda *_args, **_kwargs: []
    )

    assert StandardBlockOffsetCalculator().compute_messages([{}], None, tokenizer) is None


def test_invalid_block_size_does_not_compute_offsets():
    """Non-positive block sizes are rejected before any division occurs."""
    assert compute_block_offset([{}], None, 1, 0, None, [1]) is None
    assert compute_edit_block_offset("offload", [{}], None, 0, 1, -1, None, messages_table=[1]) is None


@pytest.mark.parametrize("target", ["tools", "session"])
def test_edit_target_without_required_data_returns_empty_coordinates(target):
    """Missing target data fails open instead of creating forged coordinates."""
    assert compute_edit_block_offset("offload", [{}], None, 0, 1, 8, None, target=target, messages_table=[1]) == (
        None,
        None,
    )


@pytest.mark.parametrize("edit_type", ["unknown", "compact"])
def test_unknown_edit_type_is_ignored(edit_type):
    """Unregistered edit types do not invoke an arbitrary offset computer."""
    assert compute_edit_block_offset(edit_type, [{}], None, 0, 1, 8, None, messages_table=[1]) == (None, None)


@pytest.mark.parametrize("tokenizer", [None, SimpleNamespace()])
def test_dsv4_tools_offsets_fail_open_without_usable_render(tokenizer):
    """Tool translation returns no table when the tokenizer cannot render a complete prompt."""
    assert Dsv4BlockOffsetCalculator().compute_tools([{}], [{"function": {"name": "tool"}}], tokenizer) is None
