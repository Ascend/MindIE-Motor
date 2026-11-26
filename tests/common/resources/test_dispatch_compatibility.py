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

from collections import UserDict
from types import SimpleNamespace

import pytest

from motor.common.resources.dispatch import (
    DispatchPlan,
    DispatchProfile,
    classify_vllm_dispatch_profile,
    dispatch_capabilities_for_profile,
    is_decode_colocation_instance,
    supports_vllm_decode_colocation,
)


def test_classify_vllm_dispatch_profile_layerwise():
    config = {"kv_transfer_config": {"kv_connector": "MooncakeLayerwiseConnector"}}
    assert classify_vllm_dispatch_profile(config) == DispatchProfile.TRIGGER


def test_classify_vllm_dispatch_profile_handoff():
    config = {"kv_transfer_config": {"kv_connector": "MooncakeHybridConnector"}}
    assert classify_vllm_dispatch_profile(config) == DispatchProfile.HANDOFF


def test_classify_vllm_dispatch_profile_mooncake_v2_handoff_with_decode_colocation():
    # MooncakeConnectorV2 is the pull-based successor of V1 and keeps the handoff contract.
    config = {"kv_transfer_config": {"kv_connector": "MooncakeConnectorV2", "kv_role": "kv_producer"}}
    assert classify_vllm_dispatch_profile(config) == DispatchProfile.HANDOFF
    assert supports_vllm_decode_colocation(config) is True


def test_classify_vllm_dispatch_profile_ascend_multi_connector_follows_transport():
    # vLLM-Ascend wraps PD transport in AscendMultiConnector; classify by connectors[0].
    config = {
        "kv_transfer_config": {
            "kv_connector": "AscendMultiConnector",
            "kv_connector_extra_config": {
                "connectors": [
                    {"kv_connector": "MooncakeHybridConnector", "kv_role": "kv_producer"},
                    {"kv_connector": "AscendStoreConnector", "kv_role": "kv_producer"},
                ]
            },
        }
    }
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


@pytest.mark.parametrize(
    "profile,expected",
    [
        (DispatchProfile.HANDOFF, [DispatchPlan.PREFILL_HANDOFF_DECODE.value]),
        (DispatchProfile.TRIGGER, [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]),
        (DispatchProfile.BOOTSTRAP, [DispatchPlan.CONCURRENT_ENGINE_SYNC.value]),
        (DispatchProfile.UNKNOWN, []),
    ],
)
def test_dispatch_capabilities_follow_coordination_profile(profile: DispatchProfile, expected: list[str]) -> None:
    assert dispatch_capabilities_for_profile(profile) == expected


@pytest.mark.parametrize(
    "explicit,expected",
    [
        (" HANDOFF ", DispatchProfile.HANDOFF),
        ("trigger", DispatchProfile.TRIGGER),
        ("bootstrap", DispatchProfile.BOOTSTRAP),
        ("prefill_handoff_decode", DispatchProfile.HANDOFF),
        ("concurrent_engine_sync", DispatchProfile.TRIGGER),
        ("unknown", DispatchProfile.TRIGGER),
        (" ", DispatchProfile.TRIGGER),
    ],
)
def test_explicit_profiles_override_connector_except_unknown_or_blank(explicit: str, expected: DispatchProfile) -> None:
    config = {"kv_transfer_config": {"kv_connector": "MooncakeLayerwiseConnector"}}
    assert classify_vllm_dispatch_profile(config, explicit) is expected


@pytest.mark.parametrize("profile", ["unsupported", "decode_colocation", 42])
def test_invalid_profile_is_rejected(profile: object) -> None:
    with pytest.raises(ValueError, match="Unsupported dispatch_profile"):
        classify_vllm_dispatch_profile({"dispatch_profile": profile})


@pytest.mark.parametrize("factory", [dict, UserDict, SimpleNamespace])
def test_engine_config_accepts_mapping_getter_and_attributes(factory: type) -> None:
    config = factory(kv_transfer_config={"kv_connector": " NIXLConnector "})
    assert classify_vllm_dispatch_profile(config) is DispatchProfile.HANDOFF


@pytest.mark.parametrize("config", [None, {}, {"kv_transfer_config": None}, {"kv_transfer_config": []}])
def test_absent_or_malformed_transfer_config_has_no_capability(config: object) -> None:
    assert classify_vllm_dispatch_profile(config) is DispatchProfile.UNKNOWN
    assert supports_vllm_decode_colocation(config) is False


@pytest.mark.parametrize(
    "extra",
    [None, [], {}, {"connectors": {}}, {"connectors": []}, {"connectors": [{}]}, {"connectors": [None, {}]}],
)
def test_incomplete_multi_connector_fails_closed(extra: object) -> None:
    config = {"kv_transfer_config": {"kv_connector": "MultiConnector", "kv_connector_extra_config": extra}}
    assert classify_vllm_dispatch_profile(config) is DispatchProfile.UNKNOWN
    assert supports_vllm_decode_colocation(config) is False


@pytest.mark.parametrize(
    "connectors,expected",
    [
        ([{"kv_connector": "MooncakeLayerwiseConnector"}, {"kv_connector": "StoreConnector"}], DispatchProfile.TRIGGER),
        ([{"kv_connector": "StoreConnector"}, {"kv_connector": "MooncakeConnectorV1"}], DispatchProfile.UNKNOWN),
    ],
)
def test_multi_connector_uses_first_transport_only(connectors: list[dict], expected: DispatchProfile) -> None:
    config = {
        "kv_transfer_config": {
            "kv_connector": "MultiConnector",
            "kv_connector_extra_config": {"connectors": connectors},
        }
    }
    assert classify_vllm_dispatch_profile(config) is expected


@pytest.mark.parametrize(
    "engine,capabilities,expected",
    [
        (" vLLM ", ["decode_colocation"], True),
        ("sglang", ["decode_colocation"], False),
        ("vllm", ["prefill_handoff_decode"], False),
        ("vllm", [], False),
        ("vllm", None, False),
    ],
)
def test_decode_colocation_requires_engine_and_capability(
    engine: str, capabilities: list[str] | None, expected: bool
) -> None:
    instance = SimpleNamespace(engine_type=engine, dispatch_capabilities=capabilities)
    assert is_decode_colocation_instance(instance) is expected
