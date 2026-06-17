# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""ConfigMap Parser - parses ConfigMap configuration data"""

import json
from typing import Any

from motor.common.logger import get_logger
from motor.controller.fault_tolerance.fault_types import (
    HardwareFaultType,
    FaultInfo,
    OriginFaultLevel,
    map_fault_level,
    map_fault_type,
)

logger = get_logger(__name__)


def _parse_json_string(json_str: str) -> dict | None:
    """Safely parse JSON string"""
    if not json_str or not isinstance(json_str, str):
        return None

    try:
        cleaned_str = json_str.strip()
        return json.loads(cleaned_str)
    except json.JSONDecodeError as e:
        logger.error("JSON parsing failed: %s", e)
        return None
    except Exception as e:
        logger.error("Error parsing JSON string: %s", e)
        return None


def is_configmap_valid(config_data: dict) -> bool:
    """Check if the data is valid format (DeviceInfoCfg, SwitchInfoCfg, ManuallySeparateNPU)"""
    if not config_data:
        return False

    # Check if it contains the expected configuration keys
    expected_keys = {"DeviceInfoCfg", "SwitchInfoCfg", "ManuallySeparateNPU"}
    config_keys = set(config_data.keys())

    # Check if any of the expected keys are present (intersection)
    return bool(expected_keys & config_keys)


def _parse_device_fault_code(fault_code_str: str) -> int:
    """Parse device fault code from hex string."""
    try:
        if fault_code_str.startswith("0x"):
            return int(fault_code_str, 16)
        else:
            return int(fault_code_str, 16)  # Assume hex format
    except ValueError:
        return 0x1001  # Default device fault code


def _create_device_fault_info(fault_type_str: str, npu_name: str, fault_level_str: str, fault_code: int) -> FaultInfo:
    """Create FaultInfo object for device fault."""
    # Map fault type string to enum
    fault_type = map_fault_type(fault_type_str)

    # Get original fault level
    try:
        origin_fault_level = OriginFaultLevel(fault_level_str)
    except ValueError:
        origin_fault_level = OriginFaultLevel.NOT_HANDLE_FAULT
    # Map fault level string to enum
    fault_level = map_fault_level(origin_fault_level)

    return FaultInfo(
        fault_type=fault_type,
        npu_name=npu_name,
        fault_code=fault_code,
        fault_level=fault_level,
        origin_fault_level=origin_fault_level,
    )


def _process_single_device_fault(fault_device: dict) -> FaultInfo | None:
    """Process a single device fault entry, returns FaultInfo object or None if failed"""
    try:
        fault_type_str = fault_device.get("fault_type", "")
        npu_name = fault_device.get("npu_name", "")
        fault_level_str = fault_device.get("fault_level", "")
        fault_code_str = fault_device.get("fault_code", "")

        # Convert fault code from hex string to int
        fault_code = _parse_device_fault_code(fault_code_str)
        # Create fault info object
        fault_info = _create_device_fault_info(fault_type_str, npu_name, fault_level_str, fault_code)
        logger.debug("Added fault device: %s, level: %s, code: 0x%x", npu_name, fault_info.fault_level, fault_code)

        return fault_info

    except Exception as e:
        logger.error("Error processing fault device %s: %s", fault_device, e)
        return None


def _normalize_device_list_value(value: Any) -> list:
    """Normalize a DeviceList field value to a list.

    Some ConfigMap producers serialize fault arrays as a JSON string inside the
    JSON value (e.g. ``"[{...},{...}]"``), while others emit a native JSON array.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        parsed = _parse_json_string(value)
        if isinstance(parsed, list):
            return parsed
        logger.debug("DeviceList field is a string but not a JSON array, skipping: %s...", value[:100])
    return []


def process_device_info(device_info_json: str) -> list[FaultInfo]:
    """Process info from DeviceInfoCfg JSON string and return device fault info list."""
    device_fault_infos = []

    # Parse JSON string first
    device_info = _parse_json_string(device_info_json)
    if not device_info:
        logger.warning("Failed to parse DeviceInfoCfg: %s", device_info_json)
        return []

    try:
        device_info_data = device_info.get("DeviceInfo", {})
        device_list = device_info_data.get("DeviceList", {})
        update_time = device_info.get("UpdateTime", 0)

        logger.debug("Processing DeviceInfo - UpdateTime: %s", update_time)

        # Process fault devices
        fault_devices = _normalize_device_list_value(device_list.get("huawei.com/Ascend910-Fault", []))
        if fault_devices:
            logger.debug("Found %s detailed fault devices", len(fault_devices))
            for fault_device in fault_devices:
                fault_info = _process_single_device_fault(fault_device)
                if fault_info:
                    device_fault_infos.append(fault_info)

        # Also process network-unhealthy devices (same serialization variants)
        network_unhealthy = _normalize_device_list_value(device_list.get("huawei.com/Ascend910-NetworkUnhealthy", []))
        if network_unhealthy:
            logger.debug("Found %s network-unhealthy devices", len(network_unhealthy))
            for fault_device in network_unhealthy:
                fault_info = _process_single_device_fault(fault_device)
                if fault_info:
                    device_fault_infos.append(fault_info)

        logger.debug("Processed %d device fault infos", len(device_fault_infos))

    except Exception as e:
        logger.error("Error processing device info: %s", e)

    return device_fault_infos


def _parse_switch_fault_key(fault_key: str) -> tuple[int, int, int]:
    """Parse fault key in format "[fault_code]_chip_id_port_id".
    Returns:
        Tuple of (fault_code, switch_chip_id, switch_port_id)
    """
    fault_code = 0x2001  # Default switch fault code
    switch_chip_id = 0
    switch_port_id = 0

    if "_" in fault_key:
        parts = fault_key.split("_")
        if len(parts) >= 3:
            fault_code_part = parts[0]
            switch_chip_id = int(parts[1]) if parts[1].isdigit() else 0
            switch_port_id = int(parts[2]) if parts[2].isdigit() else 0

            # Extract fault code from the bracketed part
            if fault_code_part.startswith("[") and fault_code_part.endswith("]"):
                code_info = fault_code_part[1:-1].split(",")[0].strip()
                if code_info.startswith("0x"):
                    try:
                        fault_code = int(code_info, 16)
                    except ValueError:
                        fault_code = 0x2001

    return fault_code, switch_chip_id, switch_port_id


def _create_switch_fault_info(fault_level_mapped_str: str, fault_code: int) -> FaultInfo:
    """Create FaultInfo object for switch fault, returns FaultInfo object"""
    # Get original fault level
    try:
        origin_fault_level = OriginFaultLevel(fault_level_mapped_str)
    except ValueError:
        origin_fault_level = OriginFaultLevel.NOT_HANDLE_FAULT
    fault_level_mapped = map_fault_level(origin_fault_level)

    # Create device fault info for switch fault
    return FaultInfo(
        fault_type=HardwareFaultType.NODE_UNHEALTHY,
        npu_name="",  # Empty for node/switch faults
        fault_code=fault_code,
        fault_level=fault_level_mapped,
        origin_fault_level=origin_fault_level,
    )


def _process_single_switch_fault(fault_key: str, fault_info_data: dict) -> FaultInfo | None:
    """Process a single switch fault mapping entry, returns FaultInfo object or None if failed"""
    try:
        fault_time = fault_info_data.get("fault_time", 0)
        fault_level_mapped_str = fault_info_data.get("fault_level", "NotHandle")

        logger.debug(
            "Processing switch fault - Key: %s, Time: %s, Level: %s", fault_key, fault_time, fault_level_mapped_str
        )

        # Parse fault key to extract fault code and location info
        fault_code, switch_chip_id, switch_port_id = _parse_switch_fault_key(fault_key)
        # Create fault info object
        fault_info = _create_switch_fault_info(fault_level_mapped_str, fault_code)
        logger.debug(
            "Added switch fault: chip=%s, port=%s, code=0x%x, level=%s",
            switch_chip_id,
            switch_port_id,
            fault_code,
            fault_info.fault_level,
        )

        return fault_info
    except Exception as e:
        logger.error("Error processing fault mapping %s: %s", fault_key, e)
        return None


def process_switch_info(switch_info_json: str) -> list[FaultInfo]:
    """Process info from SwitchInfoCfg JSON string"""
    device_fault_infos = []

    switch_info = _parse_json_string(switch_info_json)
    if not switch_info:
        logger.warning("Failed to parse SwitchInfoCfg: %s", switch_info_json)
        return []

    try:
        fault_level_str = switch_info.get("FaultLevel", "NotHandle")
        fault_level = map_fault_level(fault_level_str)
        update_time = switch_info.get("UpdateTime", 0)
        fault_time_level_map = switch_info.get("FaultTimeAndLevelMap", {})

        logger.debug(
            "Processing SwitchInfo - FaultLevel: %s (%s), UpdateTime: %s", fault_level_str, fault_level, update_time
        )

        # Process fault time and level mapping - this contains the actual fault information
        if fault_time_level_map:
            logger.debug("Processing %s fault time/level mappings", len(fault_time_level_map))
            for fault_key, fault_info_data in fault_time_level_map.items():
                fault_info = _process_single_switch_fault(fault_key, fault_info_data)
                if fault_info:
                    device_fault_infos.append(fault_info)

        logger.debug("Processed %d switch device fault infos", len(device_fault_infos))
    except Exception as e:
        logger.error("Error processing switch info: %s", e)

    return device_fault_infos


def process_manually_separate_npu(manually_separate_npu: str) -> list[int]:
    """Process manually separate NPU configuration"""
    separated_ranks = []

    try:
        if not manually_separate_npu.strip():
            logger.debug("Manually separate NPU configuration is empty")
            return separated_ranks

        logger.debug("Processing manually separate NPU: %s", manually_separate_npu)

        # Parse the configuration - assume it's a comma-separated list of NPU names
        npu_names = [name.strip() for name in manually_separate_npu.split(",") if name.strip()]

        for npu_name in npu_names:
            # Extract rank number from NPU name
            # Example: "Ascend910-0", "Ascend910-1", "Ascend910-2"
            if npu_name.startswith("Ascend910-"):
                try:
                    rank_str = npu_name.split("-")[-1]
                    rank = int(rank_str)
                    separated_ranks.append(rank)
                    logger.debug("Added NPU rank %d for manual separation", rank)
                except (ValueError, IndexError) as e:
                    logger.error("Failed to parse NPU rank from name %s: %s", npu_name, e)
            else:
                logger.warning("Unexpected NPU name format: %s", npu_name)

        logger.debug("Processed %d manually separated NPU ranks: %s", len(separated_ranks), separated_ranks)
    except Exception as e:
        logger.error("Error processing manually separate NPU: %s", e)

    return separated_ranks
