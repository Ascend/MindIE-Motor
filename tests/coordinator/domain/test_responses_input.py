# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Tests for the Responses text view used only by Motor scheduling."""

from motor.coordinator.domain.responses_input import responses_scheduling_messages


def test_string_input_and_instructions_become_scheduling_messages():
    request = {"instructions": "Be brief", "input": "Hello"}

    result = responses_scheduling_messages(request)

    assert result == [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "Hello"},
    ]
    assert request == {"instructions": "Be brief", "input": "Hello"}


def test_text_message_array_is_supported_for_scheduling():
    result = responses_scheduling_messages(
        {
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello"}],
                }
            ]
        }
    )

    assert result == [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}]


def test_developer_message_is_mapped_to_system_for_scheduling():
    result = responses_scheduling_messages({"input": [{"role": "developer", "content": "Be concise"}]})

    assert result == [{"role": "system", "content": "Be concise"}]


def test_multimodal_input_falls_back_instead_of_claiming_wrong_affinity():
    result = responses_scheduling_messages(
        {
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "https://example/image.png"}],
                }
            ]
        }
    )

    assert result is None
