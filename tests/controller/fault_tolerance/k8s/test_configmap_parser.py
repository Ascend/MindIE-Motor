# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Test cases are organized according to the following logical blocks:
1. ConfigMap validation
2. JSON parsing
3. Device info processing
4. Switch info processing
5. Manual NPU separation processing
"""

import json
from unittest.mock import patch

from motor.controller.fault_tolerance.k8s.configmap_parser import (
    is_configmap_valid,
    _parse_json_string,
    process_device_info,
    process_switch_info,
    process_manually_separate_npu,
)
from motor.controller.fault_tolerance.fault_types import (
    FaultLevel,
    HardwareFaultType,
    FaultInfo,
    OriginFaultLevel,
    map_fault_level,
)

# pylint: disable=redefined-outer-name


def test_is_configmap_valid_with_valid_config():
    """Test validation with valid configuration containing expected keys"""
    valid_configs = [
        {"DeviceInfoCfg": {}},
        {"SwitchInfoCfg": {}},
        {"ManuallySeparateNPU": ""},
        {"DeviceInfoCfg": {}, "SwitchInfoCfg": {}},
        {"DeviceInfoCfg": {}, "ManuallySeparateNPU": "test"},
    ]

    for config in valid_configs:
        assert is_configmap_valid(config) is True


def test_is_configmap_valid_with_invalid_config():
    """Test validation with invalid configuration"""
    invalid_configs = [
        {},
        None,
        {"OtherKey": "value"},
        {"DeviceInfoCfg_wrong": {}},
        {"deviceinfocfg": {}},  # Wrong case
        {"RandomKey1": {}, "RandomKey2": {}},
    ]

    for config in invalid_configs:
        assert is_configmap_valid(config) is False


def test_parse_json_string_valid():
    """Test parsing valid JSON strings"""
    test_cases = [
        ('{"key": "value"}', {"key": "value"}),
        ('{"number": 123}', {"number": 123}),
        ("[]", []),
        ('  {"key": "value"}  ', {"key": "value"}),  # With whitespace
        ("null", None),
    ]

    for json_str, expected in test_cases:
        result = _parse_json_string(json_str)
        assert result == expected


def test_parse_json_string_invalid():
    """Test parsing invalid JSON strings"""
    invalid_cases = [
        "",
        None,
        "not json",
        "{invalid json",
        '["unclosed array"',
        123,  # Non-string input
        [],  # Non-string input
    ]

    for invalid_input in invalid_cases:
        result = _parse_json_string(invalid_input)
        assert result is None


def test_parse_json_string_with_logger_error():
    """Test that logger.error is called for invalid JSON"""
    with patch("motor.controller.fault_tolerance.k8s.configmap_parser.logger") as mock_logger:
        _parse_json_string("{invalid json")
        mock_logger.error.assert_called()


def test_process_device_info_empty():
    """Test processing empty device info"""
    result = process_device_info("{}")
    assert not result


def test_process_device_info_with_fault_devices():
    """Test processing device info with fault devices"""
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/Ascend910-Fault": [
                    {
                        "fault_type": "CardUnhealthy",
                        "npu_name": "Ascend910-0",
                        "fault_level": "RestartBusiness",
                        "fault_code": "0x1001",
                    },
                    {
                        "fault_type": "CardNetworkUnhealthy",
                        "npu_name": "Ascend910-1",
                        "fault_level": "RestartRequest",
                        "fault_code": "0x1002",
                    },
                ]
            }
        },
        "UpdateTime": 1234567890,
        "SuperPodID": 1,
        "ServerIndex": 0,
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 2
    assert all(isinstance(item, FaultInfo) for item in result)

    # Check first fault device
    fault1 = result[0]
    assert fault1.fault_type == HardwareFaultType.CARD_UNHEALTHY
    assert fault1.npu_name == "Ascend910-0"
    assert fault1.fault_code == 0x1001
    assert fault1.fault_level == FaultLevel.L3

    # Check second fault device
    fault2 = result[1]
    assert fault2.fault_type == HardwareFaultType.CARD_NETWORK_UNHEALTHY
    assert fault2.npu_name == "Ascend910-1"
    assert fault2.fault_code == 0x1002
    assert fault2.fault_level == FaultLevel.L2


def test_process_device_info_with_fault_devices_json_string_format():
    """Test processing device info where huawei.com/Ascend910-Fault is a JSON string.

    Some ConfigMap producers serialize the fault array as a JSON-encoded string
    inside the JSON value (e.g. ``"[{...},{...}]"``) rather than a native array.
    """
    fault_list = [
        {
            "fault_type": "CardUnhealthy",
            "npu_name": "Ascend910-1",
            "fault_level": "RestartNPU",
            "fault_code": "80F38003",
        },
        {
            "fault_type": "CardUnhealthy",
            "npu_name": "Ascend910-2",
            "fault_level": "RestartNPU",
            "fault_code": "80F38003",
        },
    ]
    # Simulate the nested JSON-string format
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/Ascend910-Fault": json.dumps(fault_list),
            }
        },
        "UpdateTime": 1781141675,
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 2
    assert all(isinstance(item, FaultInfo) for item in result)
    # Both faults share the same code but come from different NPUs
    assert result[0].npu_name == "Ascend910-1"
    assert result[0].fault_code == 0x80F38003
    assert result[0].fault_level == FaultLevel.L5  # RestartNPU → L5
    assert result[1].npu_name == "Ascend910-2"
    assert result[1].fault_code == 0x80F38003
    assert result[1].fault_level == FaultLevel.L5


def test_process_device_info_network_unhealthy_json_string_format():
    """Test processing device info where huawei.com/Ascend910-NetworkUnhealthy is a JSON string."""
    fault_list = [
        {
            "fault_type": "CardNetworkUnhealthy",
            "npu_name": "Ascend910-5",
            "fault_level": "RestartRequest",
            "fault_code": "0xA001",
        },
    ]
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/Ascend910-NetworkUnhealthy": json.dumps(fault_list),
            }
        },
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 1
    assert result[0].fault_type == HardwareFaultType.CARD_NETWORK_UNHEALTHY
    assert result[0].npu_name == "Ascend910-5"
    assert result[0].fault_code == 0xA001
    assert result[0].fault_level == FaultLevel.L2  # RestartRequest → L2


def test_process_device_info_mixed_native_and_string_format():
    """Both fault and network-unhealthy fields as JSON strings simultaneously."""
    fault_list = [
        {
            "fault_type": "CardUnhealthy",
            "npu_name": "Ascend910-0",
            "fault_level": "SeparateNPU",
            "fault_code": "0xB001",
        },
    ]
    net_list = [
        {
            "fault_type": "CardNetworkUnhealthy",
            "npu_name": "Ascend910-3",
            "fault_level": "FreeRestartNPU",
            "fault_code": "0xC001",
        },
    ]
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/Ascend910-Fault": json.dumps(fault_list),
                "huawei.com/Ascend910-NetworkUnhealthy": json.dumps(net_list),
            }
        },
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 2
    assert result[0].npu_name == "Ascend910-0"
    assert result[0].fault_level == FaultLevel.L6  # SeparateNPU → L6
    assert result[1].npu_name == "Ascend910-3"
    assert result[1].fault_level == FaultLevel.L4  # FreeRestartNPU → L4


def test_process_device_info_empty_json_string():
    """Empty JSON string in DeviceList field should be handled gracefully."""
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/Ascend910-Fault": "",
            }
        },
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)
    assert not result


def test_process_device_info_with_invalid_fault_code():
    """Test processing device info with invalid fault code"""
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/Ascend910-Fault": [
                    {
                        "fault_type": "CardUnhealthy",
                        "npu_name": "Ascend910-0",
                        "fault_level": "L3",
                        "fault_code": "invalid_hex",
                    }
                ]
            }
        }
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 1
    assert result[0].fault_code == 0x1001  # Default fault code


def test_process_device_info_with_unknown_fault_type():
    """Test processing device info with unknown fault type"""
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/Ascend910-Fault": [
                    {
                        "fault_type": "UnknownType",
                        "npu_name": "Ascend910-0",
                        "fault_level": "L1",
                        "fault_code": "0x1001",
                    }
                ]
            }
        }
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 1
    assert result[0].fault_type == HardwareFaultType.NODE_UNHEALTHY  # Default type


def test_process_device_info_exception_handling():
    """Test exception handling in device info processing"""
    with patch("motor.controller.fault_tolerance.k8s.configmap_parser.logger"):
        # This should trigger an exception during processing
        device_info_dict = {
            "DeviceInfo": {
                "DeviceList": {
                    "huawei.com/Ascend910-Fault": [
                        {
                            "fault_type": "CardUnhealthy",
                            "npu_name": "Ascend910-0",
                            "fault_level": None,  # This might cause issues
                            "fault_code": "0x1001",
                        }
                    ]
                }
            }
        }
        device_info_json = json.dumps(device_info_dict)

        result = process_device_info(device_info_json)
        # Should still return results despite exception in individual processing
        assert isinstance(result, list)


def test_process_switch_info_empty():
    """Test processing empty switch info"""
    result = process_switch_info("{}")
    assert not result


def test_process_switch_info_with_fault_mappings():
    """Test processing switch info with fault time/level mappings"""
    switch_info_dict = {
        "FaultLevel": "L2",
        "UpdateTime": 1234567890,
        "NodeStatus": "Fault",
        "FaultTimeAndLevelMap": {
            "[0x2001,info]_1_2": {"fault_time": 1234567890, "fault_level": "L2"},
            "[0x2002,info]_3_4": {"fault_time": 1234567891, "fault_level": "L3"},
        },
    }

    switch_info_json = json.dumps(switch_info_dict)
    result = process_switch_info(switch_info_json)

    assert len(result) == 2
    assert all(isinstance(item, FaultInfo) for item in result)

    # Check that fault codes are properly extracted
    fault_codes = [fault.fault_code for fault in result]
    assert 0x2001 in fault_codes
    assert 0x2002 in fault_codes


def test_process_switch_info_with_invalid_key_format():
    """Test processing switch info with invalid fault key format"""
    switch_info_dict = {"FaultTimeAndLevelMap": {"invalid_key_format": {"fault_time": 1234567890, "fault_level": "L2"}}}

    switch_info_json = json.dumps(switch_info_dict)
    result = process_switch_info(switch_info_json)

    assert len(result) == 1
    assert result[0].fault_code == 0x2001  # Default fault code


def test_process_switch_info_with_malformed_hex_code():
    """Test processing switch info with malformed hex fault code"""
    switch_info_dict = {
        "FaultTimeAndLevelMap": {"[invalid_hex,info]_1_2": {"fault_time": 1234567890, "fault_level": "L2"}}
    }

    switch_info_json = json.dumps(switch_info_dict)
    result = process_switch_info(switch_info_json)
    assert len(result) == 1
    assert result[0].fault_code == 0x2001  # Default fault code


def test_process_manually_separate_npu_empty():
    """Test processing empty manual separation config"""
    result = process_manually_separate_npu("")
    assert not result
    result = process_manually_separate_npu("   ")
    assert not result


def test_process_manually_separate_npu_valid():
    """Test processing valid manual separation config"""
    config = "Ascend910-0,Ascend910-2,Ascend910-5"
    result = process_manually_separate_npu(config)
    expected = [0, 2, 5]
    assert result == expected


def test_process_manually_separate_npu_with_whitespace():
    """Test processing config with whitespace"""
    config = " Ascend910-0 , Ascend910-2 , Ascend910-5 "
    result = process_manually_separate_npu(config)
    expected = [0, 2, 5]
    assert result == expected


def test_process_manually_separate_npu_invalid_format():
    """Test processing config with invalid NPU name format"""
    config = "Ascend910-0,InvalidName,Ascend910-2"
    result = process_manually_separate_npu(config)
    expected = [0, 2]  # Invalid name should be skipped
    assert result == expected


def test_process_manually_separate_npu_invalid_rank_number():
    """Test processing config with invalid rank number"""
    config = "Ascend910-abc,Ascend910-1"
    result = process_manually_separate_npu(config)
    expected = [1]  # Invalid rank should be skipped
    assert result == expected


def test_process_manually_separate_npu_exception_handling():
    """Test exception handling in manual NPU separation processing"""
    with patch("motor.controller.fault_tolerance.k8s.configmap_parser.logger") as mock_logger:
        # Force an exception by passing None
        result = process_manually_separate_npu(None)
        assert not result
        mock_logger.error.assert_called()


# =============================================================================
# 6. SubHealthFault and PreSeparateNPU static mapping tests
# =============================================================================


def test_map_fault_level_sub_health_fault():
    """SubHealthFault should statically map to L1 (informational)."""
    assert map_fault_level(OriginFaultLevel.SUB_HEALTH_FAULT) == FaultLevel.L1


def test_map_fault_level_pre_separate_npu_static():
    """PreSeparateNPU statically maps to L6 — runtime downgrade to L2 is
    handled by FaultManager._handle_fault_info_update, not by the parser.
    """
    assert map_fault_level(OriginFaultLevel.PRE_SEPARATE_NPU) == FaultLevel.L6


def test_process_device_info_with_sub_health_fault_level():
    """Device info with 'SubHealthFault' level should parse to L1."""
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/Ascend910-Fault": [
                    {
                        "fault_type": "CardUnhealthy",
                        "npu_name": "Ascend910-0",
                        "fault_level": "SubHealthFault",
                        "fault_code": "0x80E01801",
                    },
                ]
            }
        },
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 1
    assert result[0].fault_level == FaultLevel.L1
    assert result[0].origin_fault_level == OriginFaultLevel.SUB_HEALTH_FAULT


def test_process_device_info_with_pre_separate_npu_level():
    """Device info with 'PreSeparateNPU' level should statically parse to L6
    (runtime downgrade to L2 is handled by FaultManager).
    """
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/Ascend910-Fault": [
                    {
                        "fault_type": "CardUnhealthy",
                        "npu_name": "Ascend910-3",
                        "fault_level": "PreSeparateNPU",
                        "fault_code": "0x00F1FEF5",
                    },
                ]
            }
        },
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 1
    # Static mapping: PreSeparateNPU → L6
    assert result[0].fault_level == FaultLevel.L6
    assert result[0].origin_fault_level == OriginFaultLevel.PRE_SEPARATE_NPU
    assert result[0].fault_code == 0x00F1FEF5


def test_process_switch_info_with_sub_health_fault_level():
    """Switch info with SubHealthFault level should map to L1."""
    switch_info_dict = {
        "FaultTimeAndLevelMap": {
            "[0x08520003,na,L2,na]_1_2": {
                "fault_time": 1234567890,
                "fault_level": "SubHealthFault",
            },
        },
    }
    switch_info_json = json.dumps(switch_info_dict)
    result = process_switch_info(switch_info_json)

    assert len(result) == 1
    assert result[0].fault_level == FaultLevel.L1
    assert result[0].origin_fault_level == OriginFaultLevel.SUB_HEALTH_FAULT


def test_map_fault_level_unknown_string_returns_healthy():
    """Unrecognized fault level string should default to HEALTHY (0)."""
    assert map_fault_level("NonExistentFaultLevel") == FaultLevel.HEALTHY
    assert map_fault_level("") == FaultLevel.HEALTHY
