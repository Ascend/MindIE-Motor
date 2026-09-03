# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Validation tests for native POST /v1/responses requests."""

import pytest
from fastapi import HTTPException

from motor.coordinator.api_server.inference_server import _validate_responses_request


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"model": "qwen3"},
        {"model": "qwen3", "input": ""},
        {"model": "qwen3", "input": {}},
        {"model": "qwen3", "input": []},
    ],
)
def test_invalid_required_fields_are_rejected(body):
    with pytest.raises(HTTPException) as exc_info:
        _validate_responses_request(body)

    assert exc_info.value.status_code == 400


@pytest.mark.parametrize(
    "input_value",
    [
        "Hello",
        [{"role": "user", "content": "Hello"}],
        [{"type": "message", "role": "user", "content": "Hello"}],
        [{"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}],
    ],
)
def test_text_inputs_are_accepted(input_value):
    _validate_responses_request({"model": "qwen3", "input": input_value})


def test_developer_message_input_is_accepted():
    _validate_responses_request(
        {
            "model": "qwen3",
            "input": [{"role": "developer", "content": "Be concise"}],
        }
    )


@pytest.mark.parametrize(
    "input_item",
    [
        {"type": "function_call_output", "call_id": "call-1", "output": "42"},
        {"type": "reasoning", "id": "reasoning-1", "summary": []},
        {
            "type": "file_search_call",
            "id": "file-search-1",
            "queries": ["Motor Responses API"],
            "status": "completed",
        },
    ],
)
def test_typed_non_message_input_items_are_deferred_to_engine_validation(input_item):
    """Coordinator must not apply Chat message fields to native Responses items."""
    _validate_responses_request({"model": "qwen3", "input": [input_item]})


@pytest.mark.parametrize(
    ("input_value", "expected_detail"),
    [
        ([1], "Invalid input item at index 0: must be an object"),
        ([{}], "Invalid input item at index 0: missing type or message fields"),
        (
            [{"type": 1}],
            "Invalid input item type at index 0: must be a non-empty string",
        ),
        (
            [{"type": "message", "role": "user"}],
            "Invalid Responses message at index 0: missing role or content",
        ),
    ],
)
def test_invalid_input_items_have_responses_specific_errors(input_value, expected_detail):
    with pytest.raises(HTTPException) as exc_info:
        _validate_responses_request({"model": "qwen3", "input": input_value})

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == expected_detail
