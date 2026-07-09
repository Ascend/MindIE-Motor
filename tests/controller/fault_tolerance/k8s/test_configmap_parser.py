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
    _parse_device_fault_code,
    _normalize_fault_level_string,
    _normalize_device_list_value,
    _resolve_device_list_key,
    _parse_switch_fault_key,
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


def test_map_fault_level_manually_separate_npu_static():
    """ManuallySeparateNPU statically maps to L6 — no runtime downgrade.
    Unlike PreSeparateNPU, ManuallySeparateNPU is never downgraded to L2.
    """
    assert map_fault_level(OriginFaultLevel.MANUALLY_SEPARATE_NPU) == FaultLevel.L6


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


def test_process_device_info_with_manually_separate_npu_level():
    """Device info with 'ManuallySeparateNPU' level should statically parse to L6.
    Unlike PreSeparateNPU, ManuallySeparateNPU is never downgraded at runtime.
    """
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/Ascend910-Fault": [
                    {
                        "fault_type": "CardNetworkUnhealthy",
                        "npu_name": "Ascend910-0",
                        "fault_level": "ManuallySeparateNPU",
                        "fault_code": "0x00F1FEF6",
                    },
                ]
            }
        },
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 1
    # Static mapping: ManuallySeparateNPU → L6
    assert result[0].fault_level == FaultLevel.L6
    assert result[0].origin_fault_level == OriginFaultLevel.MANUALLY_SEPARATE_NPU
    assert result[0].fault_code == 0x00F1FEF6


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


# =============================================================================
# 7. Comma-separated fault code parsing tests
# =============================================================================


def test_parse_device_fault_code_single_hex():
    """Single hex fault code should be parsed as-is."""
    assert _parse_device_fault_code("0x1001") == 0x1001
    assert _parse_device_fault_code("80F38003") == 0x80F38003
    assert _parse_device_fault_code("110001024") == 0x110001024


def test_parse_device_fault_code_comma_separated():
    """Comma-separated fault codes — first valid one wins."""
    # "8F180E00,110001024" → parse "8F180E00" first, return it
    assert _parse_device_fault_code("8F180E00,110001024") == 0x8F180E00
    # "110001024" alone
    assert _parse_device_fault_code("110001024") == 0x110001024


def test_parse_device_fault_code_comma_separated_with_spaces():
    """Comma-separated codes with whitespace around them."""
    assert _parse_device_fault_code("8F180E00 , 110001024") == 0x8F180E00


def test_parse_device_fault_code_all_invalid():
    """If all codes in the comma list are invalid, return default."""
    assert _parse_device_fault_code("not_hex,also_bad") == 0x1001


def test_parse_device_fault_code_empty_and_none():
    """Empty or None input returns default fault code."""
    assert _parse_device_fault_code("") == 0x1001
    assert _parse_device_fault_code(None) == 0x1001


# =============================================================================
# 8. _resolve_device_list_key tests
# =============================================================================


def test_resolve_device_list_key_new_name():
    """Should resolve via the new key name (huawei.com/npu-Fault)."""
    device_list = {
        "huawei.com/npu-Fault": [
            {"fault_type": "CardUnhealthy", "npu_name": "npu-0", "fault_level": "RestartNPU", "fault_code": "0xB001"}
        ]
    }
    result = _resolve_device_list_key(device_list, "huawei.com/npu-Fault", "huawei.com/Ascend910-Fault")
    assert len(result) == 1
    assert result[0]["npu_name"] == "npu-0"


def test_resolve_device_list_key_old_name_fallback():
    """Should fall back to old key when new key is missing."""
    device_list = {
        "huawei.com/Ascend910-Fault": [
            {
                "fault_type": "CardUnhealthy",
                "npu_name": "Ascend910-0",
                "fault_level": "RestartNPU",
                "fault_code": "0xB001",
            }
        ]
    }
    result = _resolve_device_list_key(device_list, "huawei.com/npu-Fault", "huawei.com/Ascend910-Fault")
    assert len(result) == 1
    assert result[0]["npu_name"] == "Ascend910-0"


def test_resolve_device_list_key_new_preferred_over_old():
    """When both keys exist, the new key should take precedence."""
    device_list = {
        "huawei.com/npu-Fault": [
            {"fault_type": "CardUnhealthy", "npu_name": "npu-0", "fault_level": "RestartNPU", "fault_code": "0xB001"}
        ],
        "huawei.com/Ascend910-Fault": [
            {
                "fault_type": "CardNetworkUnhealthy",
                "npu_name": "Ascend910-1",
                "fault_level": "RestartRequest",
                "fault_code": "0xA001",
            }
        ],
    }
    result = _resolve_device_list_key(device_list, "huawei.com/npu-Fault", "huawei.com/Ascend910-Fault")
    assert len(result) == 1
    assert result[0]["npu_name"] == "npu-0"  # new key wins


def test_resolve_device_list_key_nonexistent():
    """Returns empty list when no candidate key exists."""
    device_list = {"other_key": []}
    result = _resolve_device_list_key(device_list, "huawei.com/npu-Fault", "huawei.com/Ascend910-Fault")
    assert result == []


def test_resolve_device_list_key_json_string_format():
    """Should handle nested JSON-string format via _normalize_device_list_value."""
    fault_list = [
        {"fault_type": "CardUnhealthy", "npu_name": "npu-3", "fault_level": "SeparateNPU", "fault_code": "0xC001"}
    ]
    device_list = {"huawei.com/npu-Fault": json.dumps(fault_list)}
    result = _resolve_device_list_key(device_list, "huawei.com/npu-Fault", "huawei.com/Ascend910-Fault")
    assert len(result) == 1
    assert result[0]["npu_name"] == "npu-3"


def test_normalize_device_list_value_comma_separated_string():
    """Comma-separated NPU names (e.g. ``Ascend910-0,Ascend910-1``) are not JSON
    and must be silently skipped without logging an ERROR.  This is the format
    used by ``huawei.com/Ascend910-NetworkUnhealthy`` in real ConfigMaps.
    """
    comma_sep = "Ascend910-0,Ascend910-1,Ascend910-5"
    result = _normalize_device_list_value(comma_sep)
    assert result == []


def test_normalize_device_list_value_comma_separated_no_error_log():
    """A comma-separated string must NOT trigger a JSON parse error log.  Before
    the fix every heartbeat cycle (3 s) would emit an ERROR from inside
    ``_parse_json_string``.
    """
    with patch("motor.controller.fault_tolerance.k8s.configmap_parser.logger") as mock_logger:
        _normalize_device_list_value("Ascend910-0,Ascend910-1")
        # error() must never be called — the string was skipped before JSON parsing
        mock_logger.error.assert_not_called()


# =============================================================================
# 9. process_device_info with new naming convention (npu-*) tests
# =============================================================================


def test_process_device_info_with_new_key_naming():
    """process_device_info should parse fault devices using huawei.com/npu-Fault key."""
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/npu-Fault": [
                    {
                        "fault_type": "CardUnhealthy",
                        "npu_name": "npu-0",
                        "fault_level": "NotHandleFault",
                        "fault_code": "110001024",
                    },
                    {
                        "fault_type": "CardUnhealthy",
                        "npu_name": "npu-1",
                        "fault_level": "NotHandleFault",
                        "fault_code": "8F180E00,110001024",
                    },
                ]
            }
        },
        "UpdateTime": 1782697438,
        "SuperPodID": -1,
        "ServerIndex": 15,
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 2
    assert result[0].npu_name == "npu-0"
    assert result[0].fault_code == 0x110001024
    assert result[0].fault_level == FaultLevel.L1  # NotHandleFault → L1
    assert result[0].fault_type == HardwareFaultType.CARD_UNHEALTHY

    assert result[1].npu_name == "npu-1"
    assert result[1].fault_code == 0x8F180E00  # first of comma-separated
    assert result[1].fault_level == FaultLevel.L1
    assert result[1].fault_type == HardwareFaultType.CARD_UNHEALTHY


def test_process_device_info_with_new_network_unhealthy_key():
    """process_device_info should parse network-unhealthy devices using huawei.com/npu-NetworkUnhealthy key."""
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/npu-NetworkUnhealthy": [
                    {
                        "fault_type": "CardNetworkUnhealthy",
                        "npu_name": "npu-5",
                        "fault_level": "NotHandleFault",
                        "fault_code": "81078607",
                    },
                ]
            }
        },
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 1
    assert result[0].npu_name == "npu-5"
    assert result[0].fault_code == 0x81078607
    assert result[0].fault_type == HardwareFaultType.CARD_NETWORK_UNHEALTHY
    assert result[0].fault_level == FaultLevel.L1


def test_process_device_info_new_key_json_string_format():
    """New key with nested JSON-string format (the actual ConfigMap format)."""
    fault_list = [
        {
            "fault_type": "CardUnhealthy",
            "npu_name": "npu-0",
            "fault_level": "NotHandleFault",
            "fault_code": "110001024",
        },
        {
            "fault_type": "CardUnhealthy",
            "npu_name": "npu-1",
            "fault_level": "NotHandleFault",
            "fault_code": "8F180E00,110001024",
        },
        {
            "fault_type": "CardNetworkUnhealthy",
            "npu_name": "npu-5",
            "fault_level": "NotHandleFault",
            "fault_code": "81078607",
        },
    ]
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/npu": "",
                "huawei.com/npu-Fault": json.dumps(fault_list),
                "huawei.com/npu-NetworkUnhealthy": "",
                "huawei.com/npu-Recovering": "",
                "huawei.com/npu-Unhealthy": "",
            }
        },
        "UpdateTime": 1782697438,
        "SuperPodID": -1,
        "ServerIndex": 15,
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    # All 3 fault devices should be parsed from npu-Fault (nested JSON string)
    # npu-NetworkUnhealthy is empty string → skipped
    assert len(result) == 3
    npu_names = [f.npu_name for f in result]
    assert "npu-0" in npu_names
    assert "npu-1" in npu_names
    assert "npu-5" in npu_names

    # npu-1 has comma-separated fault code → first one wins
    npu1 = next(f for f in result if f.npu_name == "npu-1")
    assert npu1.fault_code == 0x8F180E00


def test_process_device_info_new_fault_and_old_network_unhealthy():
    """Mixed naming: new fault key + old network-unhealthy key → both parsed."""
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/npu-Fault": [
                    {
                        "fault_type": "CardUnhealthy",
                        "npu_name": "npu-0",
                        "fault_level": "SeparateNPU",
                        "fault_code": "0x00F1FEF5",
                    },
                ],
                "huawei.com/Ascend910-NetworkUnhealthy": [
                    {
                        "fault_type": "CardNetworkUnhealthy",
                        "npu_name": "Ascend910-3",
                        "fault_level": "FreeRestartNPU",
                        "fault_code": "0xC001",
                    },
                ],
            }
        },
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 2
    assert result[0].npu_name == "npu-0"
    assert result[0].fault_level == FaultLevel.L6  # SeparateNPU → L6
    assert result[1].npu_name == "Ascend910-3"
    assert result[1].fault_level == FaultLevel.L4  # FreeRestartNPU → L4


def test_process_device_info_old_keys_still_work():
    """Old key names (Ascend910-*) must still be parsed — backward compatibility."""
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
                ],
                "huawei.com/Ascend910-NetworkUnhealthy": [
                    {
                        "fault_type": "CardNetworkUnhealthy",
                        "npu_name": "Ascend910-5",
                        "fault_level": "RestartRequest",
                        "fault_code": "0xA001",
                    },
                ],
            }
        },
    }
    device_info_json = json.dumps(device_info_dict)
    result = process_device_info(device_info_json)

    assert len(result) == 2
    assert result[0].npu_name == "Ascend910-0"
    assert result[0].fault_level == FaultLevel.L3
    assert result[1].npu_name == "Ascend910-5"
    assert result[1].fault_type == HardwareFaultType.CARD_NETWORK_UNHEALTHY


def test_process_device_info_network_unhealthy_comma_separated():
    """Real ConfigMap format: huawei.com/Ascend910-NetworkUnhealthy stores NPU
    names as a comma-separated string (not JSON).  This must be silently skipped
    without triggering a JSON parse ERROR log — the fault devices are already
    captured via huawei.com/Ascend910-Fault.
    """
    fault_list_json = json.dumps(
        [
            {
                "fault_type": "CardNetworkUnhealthy",
                "npu_name": "Ascend910-0",
                "fault_level": "PreSeparateNPU",
                "fault_code": "81078603",
            },
            {
                "fault_type": "CardNetworkUnhealthy",
                "npu_name": "Ascend910-1",
                "fault_level": "PreSeparateNPU",
                "fault_code": "81078603",
            },
        ]
    )
    device_info_dict = {
        "DeviceInfo": {
            "DeviceList": {
                "huawei.com/Ascend910": "",
                "huawei.com/Ascend910-Fault": fault_list_json,
                "huawei.com/Ascend910-NetworkUnhealthy": "Ascend910-0,Ascend910-1",
                "huawei.com/Ascend910-Recovering": "",
                "huawei.com/Ascend910-Unhealthy": "",
            }
        },
        "UpdateTime": 1783483394,
    }
    device_info_json = json.dumps(device_info_dict)

    with patch("motor.controller.fault_tolerance.k8s.configmap_parser.logger") as mock_logger:
        result = process_device_info(device_info_json)
        # Fault devices from the JSON-string field should be parsed
        assert len(result) == 2
        assert result[0].npu_name == "Ascend910-0"
        assert result[1].npu_name == "Ascend910-1"
        # The comma-separated field must NOT trigger a JSON parse error
        error_calls = [call for call in mock_logger.error.call_args_list if "JSON parsing failed" in str(call)]
        assert len(error_calls) == 0


# =============================================================================
# 10. _normalize_fault_level_string tests (MindCluster 26.0.0+ SwitchInfoCfg values)
# =============================================================================


def test_normalize_fault_level_string_standard_values():
    """Standard OriginFaultLevel values should pass through unchanged."""
    assert _normalize_fault_level_string("NotHandleFault") == "NotHandleFault"
    assert _normalize_fault_level_string("SubHealthFault") == "SubHealthFault"
    assert _normalize_fault_level_string("RestartRequest") == "RestartRequest"
    assert _normalize_fault_level_string("RestartBusiness") == "RestartBusiness"
    assert _normalize_fault_level_string("FreeRestartNPU") == "FreeRestartNPU"
    assert _normalize_fault_level_string("RestartNPU") == "RestartNPU"
    assert _normalize_fault_level_string("SeparateNPU") == "SeparateNPU"
    assert _normalize_fault_level_string("PreSeparateNPU") == "PreSeparateNPU"


def test_normalize_fault_level_string_shortened_switch_values():
    """MindCluster 26.0.0+ SwitchInfoCfg shortened values."""
    # NotHandle → NotHandleFault (L1)
    assert _normalize_fault_level_string("NotHandle") == "NotHandleFault"
    # Separate → SeparateNPU (L6)
    assert _normalize_fault_level_string("Separate") == "SeparateNPU"


def test_normalize_fault_level_string_empty_and_unknown():
    """Empty or unknown values fall back to NotHandleFault."""
    assert _normalize_fault_level_string("") == "NotHandleFault"
    assert _normalize_fault_level_string("UnknownLevel") == "NotHandleFault"


# =============================================================================
# 11. _parse_switch_fault_key — new key format (MindCluster 26.0.0+)
# =============================================================================


def test_parse_switch_fault_key_old_bracket_format():
    """Old format: [0x2001,info]_1_2."""
    code, chip, port = _parse_switch_fault_key("[0x2001,info]_1_2")
    assert code == 0x2001
    assert chip == 1
    assert port == 2


def test_parse_switch_fault_key_new_plain_format():
    """New format (MindCluster 26.0.0+): 0x2001_1_2."""
    code, chip, port = _parse_switch_fault_key("0x2001_1_2")
    assert code == 0x2001
    assert chip == 1
    assert port == 2


def test_parse_switch_fault_key_new_format_non_hex_code():
    """New format with non-0x-prefixed hex code."""
    code, chip, port = _parse_switch_fault_key("80F38003_3_5")
    assert code == 0x80F38003
    assert chip == 3
    assert port == 5


def test_parse_switch_fault_key_new_format_invalid_code():
    """New format with invalid hex falls back to default."""
    code, chip, port = _parse_switch_fault_key("not_hex_1_2")
    assert code == 0x2001  # default


def test_parse_switch_fault_key_no_underscores():
    """Malformed key with no underscores returns defaults."""
    code, chip, port = _parse_switch_fault_key("nounderscores")
    assert code == 0x2001
    assert chip == 0
    assert port == 0


# =============================================================================
# 12. process_switch_info with new FaultLevel and key format (MindCluster 26.0.0+)
# =============================================================================


def test_process_switch_info_new_fault_level_not_handle():
    """SwitchInfoCfg with FaultLevel='NotHandle' should map to L1."""
    switch_info_dict = {
        "FaultLevel": "NotHandle",
        "UpdateTime": 1234567890,
        "FaultTimeAndLevelMap": {
            "0x2001_1_2": {"fault_time": 1234567890, "fault_level": "NotHandle"},
        },
    }
    switch_info_json = json.dumps(switch_info_dict)
    result = process_switch_info(switch_info_json)

    assert len(result) == 1
    assert result[0].fault_code == 0x2001
    assert result[0].fault_level == FaultLevel.L1  # NotHandle → NotHandleFault → L1
    assert result[0].origin_fault_level == OriginFaultLevel.NOT_HANDLE_FAULT


def test_process_switch_info_new_fault_level_separate():
    """SwitchInfoCfg with FaultLevel='Separate' should map to L6."""
    switch_info_dict = {
        "FaultLevel": "Separate",
        "FaultTimeAndLevelMap": {
            "0xA001_0_3": {"fault_time": 1234567890, "fault_level": "Separate"},
        },
    }
    switch_info_json = json.dumps(switch_info_dict)
    result = process_switch_info(switch_info_json)

    assert len(result) == 1
    assert result[0].fault_code == 0xA001
    assert result[0].fault_level == FaultLevel.L6  # Separate → SeparateNPU → L6
    assert result[0].origin_fault_level == OriginFaultLevel.SEPARATE_NPU


def test_process_switch_info_new_key_format():
    """SwitchInfoCfg with new plain key format (no brackets)."""
    switch_info_dict = {
        "FaultTimeAndLevelMap": {
            "0x08520003_1_2": {"fault_time": 1234567890, "fault_level": "SubHealthFault"},
            "0x2002_3_4": {"fault_time": 1234567891, "fault_level": "RestartRequest"},
        },
    }
    switch_info_json = json.dumps(switch_info_dict)
    result = process_switch_info(switch_info_json)

    assert len(result) == 2
    codes = {f.fault_code for f in result}
    assert 0x08520003 in codes
    assert 0x2002 in codes

    f1 = next(f for f in result if f.fault_code == 0x08520003)
    assert f1.fault_level == FaultLevel.L1  # SubHealthFault → L1
    f2 = next(f for f in result if f.fault_code == 0x2002)
    assert f2.fault_level == FaultLevel.L2  # RestartRequest → L2


def test_process_switch_info_mixed_old_and_new_key_formats():
    """SwitchInfoCfg with both old bracket and new plain key formats."""
    switch_info_dict = {
        "FaultTimeAndLevelMap": {
            "[0x2001,info]_1_2": {"fault_time": 1234567890, "fault_level": "L2"},
            "0x2002_3_4": {"fault_time": 1234567891, "fault_level": "L3"},
        },
    }
    switch_info_json = json.dumps(switch_info_dict)
    result = process_switch_info(switch_info_json)

    assert len(result) == 2


# =============================================================================
# 13. process_manually_separate_npu — npu-N format (Atlas 950)
# =============================================================================


def test_process_manually_separate_npu_npu_format():
    """Atlas 950 uses 'npu-0,npu-1' format instead of 'Ascend910-0'."""
    config = "npu-0,npu-2,npu-5"
    result = process_manually_separate_npu(config)
    assert result == [0, 2, 5]


def test_process_manually_separate_npu_mixed_old_new_format():
    """Mixed Ascend910-N and npu-N format should both parse."""
    config = "Ascend910-0,npu-3,Ascend910-7"
    result = process_manually_separate_npu(config)
    assert result == [0, 3, 7]


def test_process_manually_separate_npu_ascend_format_still_works():
    """Old Ascend910-N format must still work."""
    config = "Ascend910-0,Ascend910-2,Ascend910-5"
    result = process_manually_separate_npu(config)
    assert result == [0, 2, 5]
