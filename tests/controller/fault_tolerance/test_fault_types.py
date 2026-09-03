# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Unit tests for A2 linkdown NPU attribution helpers."""

from motor.common.resources.endpoint import DeviceInfo, Endpoint
from motor.controller.fault_tolerance.fault_types import (
    A2_PD_ISOLATION_FAULT_CODES,
    FaultCategory,
    FaultInfo,
    FaultLevel,
    HardwareFaultType,
    OriginFaultLevel,
    SpecialFaultCode,
    a2_linkdown_targets_instance,
    instance_requires_a2_linkdown_l6,
    is_800i_a2,
    is_a2_linkdown_pre_separate,
    parse_npu_chip_ids,
)


def _linkdown(npu_name: str) -> FaultInfo:
    return FaultInfo(
        fault_category=FaultCategory.HARDWARE,
        fault_type=HardwareFaultType.CARD_NETWORK_UNHEALTHY,
        npu_name=npu_name,
        fault_code=int(SpecialFaultCode.CARD_NETWORK_LINKDOWN),
        fault_level=FaultLevel.L6,
        origin_fault_level=OriginFaultLevel.PRE_SEPARATE_NPU,
    )


def test_parse_npu_chip_ids_accepts_ascend_and_npu_spellings():
    assert parse_npu_chip_ids("Ascend910-6") == {6}
    assert parse_npu_chip_ids("npu-4") == {4}
    assert parse_npu_chip_ids("npu0") == {0}
    assert parse_npu_chip_ids("Ascend910-0, Ascend910-1") == {0, 1}
    assert parse_npu_chip_ids("") == set()


def test_a2_linkdown_targets_only_owner_instance():
    prefill = type("Inst", (), {})()
    prefill.get_all_endpoints = lambda: (
        Endpoint(
            id=0,
            ip="10.0.0.1",
            business_port="10000",
            device_infos=[
                DeviceInfo(device_id="6", rank_id="0"),
                DeviceInfo(device_id="7", rank_id="1"),
            ],
        ),
    )
    decode = type("Inst", (), {})()
    decode.get_all_endpoints = lambda: (
        Endpoint(
            id=0,
            ip="10.0.0.2",
            business_port="10000",
            device_infos=[
                DeviceInfo(device_id="4", rank_id="0"),
                DeviceInfo(device_id="5", rank_id="1"),
            ],
        ),
    )
    fault = _linkdown("Ascend910-6")
    assert a2_linkdown_targets_instance(fault, prefill, "800I_A2") is True
    assert a2_linkdown_targets_instance(fault, decode, "800I_A2") is False


def test_a2_linkdown_fails_closed_without_device_list():
    """Colocated INITIAL instances without device_ids must not be treated as owners."""
    empty = type("Inst", (), {})()
    empty.get_all_endpoints = lambda: ()
    assert a2_linkdown_targets_instance(_linkdown("Ascend910-6"), empty, "800I_A2") is False
    # Non-A2 path is not attributed here; caller treats it as node-level.
    assert a2_linkdown_targets_instance(_linkdown("Ascend910-6"), empty, "800I_A3") is True


def test_is_800i_a2_accepts_underscore_and_hyphen():
    assert is_800i_a2("800I_A2") is True
    assert is_800i_a2("800I-A2") is True
    assert is_800i_a2("800I_A3") is False


def test_instance_requires_a2_linkdown_l6_for_pd_and_multi_pod_union():
    prefill = type("Inst", (), {"role": "prefill"})()
    decode = type("Inst", (), {"role": "decode"})()
    union_one = type("Inst", (), {"role": "union", "get_node_managers_num": lambda self: 1})()
    union_two = type("Inst", (), {"role": "union", "get_node_managers_num": lambda self: 2})()
    assert instance_requires_a2_linkdown_l6(prefill) is True
    assert instance_requires_a2_linkdown_l6(decode) is True
    assert instance_requires_a2_linkdown_l6(union_one) is False
    assert instance_requires_a2_linkdown_l6(union_two) is True


def test_a2_pd_isolation_set_currently_only_linkdown():
    assert A2_PD_ISOLATION_FAULT_CODES == frozenset({int(SpecialFaultCode.CARD_NETWORK_LINKDOWN)})


def test_is_a2_linkdown_pre_separate_requires_a2_and_isolation_code():
    linkdown = _linkdown("Ascend910-6")
    other = linkdown.model_copy(update={"fault_code": 0x00F1FEF5})
    assert is_a2_linkdown_pre_separate(linkdown, "800I_A2") is True
    assert is_a2_linkdown_pre_separate(linkdown, "800I_A3") is False
    assert is_a2_linkdown_pre_separate(other, "800I_A2") is False
