# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Unit tests for vLLM dispatch-profile classification."""

from motor.common.resources.dispatch import (
    DispatchProfile,
    classify_vllm_dispatch_profile,
    supports_vllm_decode_colocation,
)


def test_classify_vllm_dispatch_profile_layerwise():
    config = {"kv_transfer_config": {"kv_connector": "MooncakeLayerwiseConnector"}}
    assert classify_vllm_dispatch_profile(config) == DispatchProfile.TRIGGER


def test_classify_vllm_dispatch_profile_handoff():
    config = {"kv_transfer_config": {"kv_connector": "MooncakeHybridConnector"}}
    assert classify_vllm_dispatch_profile(config) == DispatchProfile.HANDOFF


def test_classify_vllm_dispatch_profile_unknown_connector():
    config = {"kv_transfer_config": {"kv_connector": "UnknownConnector"}}
    assert classify_vllm_dispatch_profile(config) == DispatchProfile.UNKNOWN


def test_decode_colocation_requires_recognized_connector_not_explicit_profile():
    """A coordination profile alone must not claim bare-request support."""
    recognized = {"kv_transfer_config": {"kv_connector": "MooncakeConnectorV1"}}
    unknown = {
        "dispatch_profile": "handoff",
        "kv_transfer_config": {"kv_connector": "CustomConnector"},
    }

    assert supports_vllm_decode_colocation(recognized) is True
    assert supports_vllm_decode_colocation(unknown) is False
