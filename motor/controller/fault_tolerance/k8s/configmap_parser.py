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
    parse_fault_code,
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
    """Parse device fault code from hex string.

    Some ConfigMap producers emit comma-separated fault codes (e.g.
    ``"8F180E00,110001024"``).  We take the first code that parses
    successfully; if none do we fall back to the default.
    """
    if not fault_code_str or not isinstance(fault_code_str, str):
        return 0x1001

    # Handle comma-separated fault codes — take the first valid one
    codes = [c.strip() for c in fault_code_str.split(",") if c.strip()]
    for code in codes:
        parsed = parse_fault_code(code)
        if parsed is not None:
            return parsed

    return 0x1001  # Default device fault code


def _normalize_fault_level_string(raw_level: str) -> str:
    """Normalize fault-level strings from both DeviceInfoCfg and SwitchInfoCfg
    into a form recognised by :class:`OriginFaultLevel`.

    MindCluster 26.0.0+ uses shortened SwitchInfoCfg ``FaultLevel`` values
    (``"NotHandle"``, ``"Separate"``) that need mapping to the longer
    ``OriginFaultLevel`` member names.
    """
    if not raw_level or not isinstance(raw_level, str):
        return "NotHandleFault"

    # Already a standard OriginFaultLevel value
    known = {e.value for e in OriginFaultLevel}
    if raw_level in known:
        return raw_level

    # MindCluster 26.0.0+ SwitchInfoCfg shortened forms
    _switch_fault_level_map = {
        "NotHandle": OriginFaultLevel.NOT_HANDLE_FAULT.value,
        "Separate": OriginFaultLevel.SEPARATE_NPU.value,
    }
    mapped = _switch_fault_level_map.get(raw_level)
    if mapped:
        return mapped

    logger.debug("Unknown fault level string '%s', falling back to NotHandleFault", raw_level)
    return OriginFaultLevel.NOT_HANDLE_FAULT.value


def _create_device_fault_info(fault_type_str: str, npu_name: str, fault_level_str: str, fault_code: int) -> FaultInfo:
    """Create FaultInfo object for device fault."""
    # Map fault type string to enum
    fault_type = map_fault_type(fault_type_str)

    # Get original fault level (normalize shortened SwitchInfoCfg forms first)
    normalized_level = _normalize_fault_level_string(fault_level_str)
    try:
        origin_fault_level = OriginFaultLevel(normalized_level)
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


def _process_single_device_fault(fault_device: dict) -> list[FaultInfo]:
    """保留同一卡的逐码证据，供快恢原因分类和清码检查使用。

    旧逻辑仅取首个有效码：例如普通告警排在 1801 前，会隐藏 HBM UCE；
    直接复用整卡等级又可能把低等级码提升为隔离故障。优先使用逐码等级，
    缺失时兼容回退到整卡等级；同一卡内重复的数值码只保留一份。
    这里不判断主故障与连带故障，也不逐码启动恢复：下游按实例统一规划。
    快恢规划中 HBM UCE 优先于 token 重推；伴随的 L1/L2 码不会触发严重硬件故障拦截。
    因此需保留每个码自己的级别，不能把整卡级别传播给所有伴随码。
    """
    try:
        fault_type_str = fault_device.get("fault_type", "")
        npu_name = fault_device.get("npu_name", "")
        card_level = fault_device.get("fault_level", "")
        raw_codes = fault_device.get("fault_code", "")
        codes = []
        if isinstance(raw_codes, str):
            codes = [parse_fault_code(code) for code in raw_codes.split(",")]
        codes = list(dict.fromkeys(code for code in codes if code is not None)) or [0x1001]
        raw_details = fault_device.get("fault_time_and_level_map", {})
        details = (
            {
                parse_fault_code(code): detail
                for code, detail in raw_details.items()
                if isinstance(detail, dict) and parse_fault_code(code) is not None
            }
            if isinstance(raw_details, dict)
            else {}
        )
        faults = []
        for code in codes:
            level = details.get(code, {}).get("fault_level")
            if not isinstance(level, str) or not level.strip():
                level = card_level
            faults.append(_create_device_fault_info(fault_type_str, npu_name, level, code))
        return faults
    except Exception as e:
        logger.error("Error processing fault device %s: %s", fault_device, e)
        return []


def _normalize_device_list_value(value: Any) -> list:
    """Normalize a DeviceList field value to a list.

    Some ConfigMap producers serialize fault arrays as a JSON string inside the
    JSON value (e.g. ``"[{...},{...}]"``), while others emit a native JSON array.
    Non-JSON strings (e.g. comma-separated NPU names like
    ``"Ascend910-0,Ascend910-1"``) are skipped without attempting a parse.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        stripped = value.strip()
        # Only attempt JSON parsing for strings that look like JSON arrays/objects.
        # Comma-separated identifiers (e.g. "Ascend910-0,Ascend910-1") are not JSON
        # and would log a spurious ERROR from _parse_json_string on every watch event.
        if stripped.startswith(("[", "{")):
            parsed = _parse_json_string(stripped)
            if isinstance(parsed, list):
                return parsed
        logger.debug("DeviceList field is a string but not a JSON array, skipping: %s...", value[:100])
    return []


def _resolve_device_list_key(device_list: dict, *key_candidates: str) -> list:
    """Return the normalized value of the first key in *key_candidates* that
    exists (with a truthy value) in *device_list*, or an empty list.

    This bridges two naming conventions:
      - old: ``huawei.com/Ascend910-Fault`` / ``huawei.com/Ascend910-NetworkUnhealthy``
      - new: ``huawei.com/npu-Fault``     / ``huawei.com/npu-NetworkUnhealthy``
    """
    for key in key_candidates:
        raw = device_list.get(key)
        if raw:
            normalized = _normalize_device_list_value(raw)
            if normalized:
                logger.debug("Resolved DeviceList key %s → %d entries", key, len(normalized))
                return normalized
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

        # Process fault devices — try new key name first, then old
        fault_devices = _resolve_device_list_key(device_list, "huawei.com/npu-Fault", "huawei.com/Ascend910-Fault")
        if fault_devices:
            logger.debug("Found %s detailed fault devices", len(fault_devices))
            for fault_device in fault_devices:
                device_fault_infos.extend(_process_single_device_fault(fault_device))

        # Also process network-unhealthy devices (same serialization variants)
        network_unhealthy = _resolve_device_list_key(
            device_list, "huawei.com/npu-NetworkUnhealthy", "huawei.com/Ascend910-NetworkUnhealthy"
        )
        if network_unhealthy:
            logger.debug("Found %s network-unhealthy devices", len(network_unhealthy))
            for fault_device in network_unhealthy:
                device_fault_infos.extend(_process_single_device_fault(fault_device))

        logger.debug("Processed %d device fault infos", len(device_fault_infos))

    except Exception as e:
        logger.error("Error processing device info: %s", e)

    return device_fault_infos


def _parse_switch_fault_key(fault_key: str) -> tuple[int, int, int]:
    """Parse fault key and return (fault_code, switch_chip_id, switch_port_id).

    Supports two formats:
      - **old** (bracket): ``[0x2001,info]_1_2``  (fault_code part is bracketed)
      - **new** (MindCluster 26.0.0+): ``0x2001_1_2``  (plain hex, underscore-delimited)
    """
    fault_code = 0x2001  # Default switch fault code
    switch_chip_id = 0
    switch_port_id = 0

    if "_" not in fault_key:
        return fault_code, switch_chip_id, switch_port_id

    parts = fault_key.split("_")
    if len(parts) < 3:
        return fault_code, switch_chip_id, switch_port_id

    fault_code_part = parts[0]
    switch_chip_id = int(parts[1]) if parts[1].isdigit() else 0
    switch_port_id = int(parts[2]) if parts[2].isdigit() else 0

    # --- New format (MindCluster 26.0.0+): plain hex code, e.g. "0x2001" ---
    if not fault_code_part.startswith("["):
        try:
            fault_code = int(fault_code_part, 16)
            return fault_code, switch_chip_id, switch_port_id
        except ValueError:
            pass  # fall through to old-format attempt below

    # --- Old format: "[0x2001,info]", possibly with commas ---
    if fault_code_part.startswith("[") and fault_code_part.endswith("]"):
        code_info = fault_code_part[1:-1].split(",")[0].strip()
        try:
            fault_code = int(code_info, 16)
        except ValueError:
            fault_code = 0x2001

    return fault_code, switch_chip_id, switch_port_id


def _create_switch_fault_info(fault_level_mapped_str: str, fault_code: int) -> FaultInfo:
    """Create FaultInfo object for switch fault, returns FaultInfo object"""
    # Get original fault level (normalize shortened SwitchInfoCfg forms first)
    normalized_level = _normalize_fault_level_string(fault_level_mapped_str)
    try:
        origin_fault_level = OriginFaultLevel(normalized_level)
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
            # Extract rank number from NPU name.
            # Examples: "Ascend910-0", "Ascend910-1" (old), "npu-0", "npu-1" (Atlas 950)
            # General pattern: "<prefix>-<number>"
            if "-" in npu_name:
                try:
                    rank_str = npu_name.rsplit("-", 1)[-1]
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


DPU_INFO_CFG_KEY = "DpuInfoCfg"
# Object name is dpuinfo-<nodeName>. Its namespace is configmap_namespace.
DPU_CONFIGMAP_PREFIX = "dpuinfo-"

# MindCluster skips these levels for an unhealthy HCA. Controller skips them too.
_DPU_LEVELS_NOT_HANDLED = {
    OriginFaultLevel.SUB_HEALTH_FAULT.value,
    OriginFaultLevel.NOT_HANDLE_FAULT.value,
}


def _parse_dpu_fault_code(raw_code: str) -> int | None:
    """Parse a MindCluster DPU fault code.

    fault_code.json uses decimal strings such as 21000024. A 0x prefix is hexadecimal.
    Empty, zero, and unparseable values return None so they do not become storage key 0:node.
    """
    text = raw_code.strip() if isinstance(raw_code, str) else ""
    if not text:
        return None
    if text.lower().startswith("0x"):
        parsed = parse_fault_code(text)
    elif text.isdigit():
        parsed = int(text)
    else:
        parsed = parse_fault_code(text)
    if not parsed:
        return None
    return parsed


def _as_config_object(value: Any) -> dict | None:
    """Normalize an object field to a dict. A nested JSON string is parsed once more."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        parsed = _parse_json_string(value)
        if isinstance(parsed, dict):
            return parsed
    return None


def _affected_npu_name(affected_npu: Any) -> str:
    """Turn AffectedNPU into an NPU name. Only a list of integer chip ids is accepted."""
    if not isinstance(affected_npu, list):
        return ""
    names = []
    for chip_id in affected_npu:
        try:
            names.append("Ascend910-%d" % int(chip_id))
        except (TypeError, ValueError):
            continue
    return ",".join(names)


def _dpu_detail_to_fault_info(detail: dict, npu_name: str) -> FaultInfo | None:
    """Convert one DPU fault detail. Only SeparateDPU is kept, at the SeparateNPU L6 mapping."""
    if not isinstance(detail, dict):
        return None
    level = detail.get("FaultLevel") or detail.get("fault_level") or ""
    raw_code = str(detail.get("FaultCode") or detail.get("fault_code") or "")
    if level in _DPU_LEVELS_NOT_HANDLED:
        logger.info(
            "Skip DPU fault because controller does not handle %s, code=%s, npu=%s",
            level,
            raw_code,
            npu_name or "node",
        )
        return None
    # DPU FaultLevel is the full OriginFaultLevel name, such as SeparateDPU.
    # SwitchInfoCfg short form "Separate" is SeparateNPU and is not accepted here.
    if level != OriginFaultLevel.SEPARATE_DPU.value:
        logger.info("Skip DPU fault level %s, only SeparateDPU is recovered, code=%s", level, raw_code)
        return None
    fault_code = _parse_dpu_fault_code(raw_code)
    if fault_code is None:
        logger.warning(
            "Skip SeparateDPU because fault code is missing or invalid, raw=%s, npu=%s",
            raw_code,
            npu_name or "node",
        )
        return None
    return _create_device_fault_info("CardUnhealthy", npu_name, level, fault_code)


def _dpu_list_field(value: Any) -> list | None:
    """Normalize a DPU list field.

    Missing, blank, and [] mean the field has no entries.
    A present value that is not a list, and not a JSON array string, returns None.
    """
    if isinstance(value, str) and not value.strip():
        return []
    if value in (None, []):
        return []
    if isinstance(value, list):
        return value
    # Native lists are returned above. Only a JSON array string is parsed again.
    if isinstance(value, str) and value.strip().startswith("["):
        parsed = _parse_json_string(value)
        if isinstance(parsed, list):
            return parsed
    return None


def process_dpu_info(dpu_info_json: str) -> list[FaultInfo] | None:
    """Parse DpuInfoCfg from <configmap_namespace>/dpuinfo-<node>.

    None means the payload cannot be parsed; the caller keeps the existing DPU snapshot.
    An empty list means the payload parsed and has no DPU fault.
    A missing list field is no fault. A present field with the wrong shape is unparseable.
    """
    dpu_info = _parse_json_string(dpu_info_json)
    if not isinstance(dpu_info, dict):
        logger.warning("Failed to parse DpuInfoCfg: %s", dpu_info_json)
        return None

    raw_body = dpu_info.get("DPUInfo", {})
    dpu_body = _as_config_object(raw_body)
    if raw_body in ({}, None, ""):
        return []
    if dpu_body is None:
        logger.warning("DpuInfoCfg DPUInfo is not an object: %s", type(raw_body).__name__)
        return None

    fault_infos: list[FaultInfo] = []
    dpu_items = _dpu_list_field(dpu_body.get("DPUList"))
    if dpu_items is None:
        logger.warning("DpuInfoCfg DPUList is not a list: %s", type(dpu_body.get("DPUList")).__name__)
        return None
    for item in dpu_items:
        if not isinstance(item, dict):
            logger.warning("Skip DPU list item because it is not an object: %s", type(item).__name__)
            continue
        try:
            # DPUList needs one integer chip id. Missing or unparseable AffectedNPU is skipped.
            # Node-level DPU faults come from NodeEvent.
            affected_npu = item.get("AffectedNPU")
            npu_name = _affected_npu_name(affected_npu)
            if not npu_name:
                logger.warning("Skip DPU list item because AffectedNPU has no chip id: %s", affected_npu)
                continue
            fault_details = _dpu_list_field(item.get("FaultList"))
            if fault_details is None:
                logger.warning("DpuInfoCfg FaultList is not a list: %s", type(item.get("FaultList")).__name__)
                return None
            for detail in fault_details:
                fault_info = _dpu_detail_to_fault_info(detail, npu_name)
                if fault_info is not None:
                    fault_infos.append(fault_info)
        except Exception as e:
            logger.error("Error processing DPU list item: %s", e)

    raw_node_event = dpu_body.get("NodeEvent")
    if isinstance(raw_node_event, str) and not raw_node_event.strip():
        raw_node_event = ""
    if raw_node_event in (None, {}, ""):
        logger.debug("Processed %d DPU fault infos", len(fault_infos))
        return fault_infos
    node_event = _as_config_object(raw_node_event)
    if node_event is None:
        logger.warning("DpuInfoCfg NodeEvent is not an object: %s", type(dpu_body.get("NodeEvent")).__name__)
        return None
    node_faults = _dpu_list_field(node_event.get("FaultList"))
    if node_faults is None:
        logger.warning(
            "DpuInfoCfg NodeEvent FaultList is not a list: %s",
            type(node_event.get("FaultList")).__name__,
        )
        return None
    # Card drop has no AffectedNPU and stays a node-level fault (empty npu_name).
    try:
        for detail in node_faults:
            fault_info = _dpu_detail_to_fault_info(detail, "")
            if fault_info is not None:
                fault_infos.append(fault_info)
    except Exception as e:
        logger.error("Error processing DPU node event: %s", e)
    logger.debug("Processed %d DPU fault infos", len(fault_infos))
    return fault_infos
