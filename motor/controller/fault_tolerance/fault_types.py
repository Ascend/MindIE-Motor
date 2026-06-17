# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""
Fault tolerance type definitions: enums, data models, and mapping utilities.

This module defines fault level/fault_type/category enumerations, data models
(NodeMetadata, InstanceMetadata, FaultInfo), and mapping utilities for the
fault tolerance management subsystem.
"""

import threading
import time
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class FaultCategory(str, Enum):
    """Fault category to distinguish hardware and software faults."""

    HARDWARE = "hardware"
    SOFTWARE = "software"


class SpecialFaultCode(int, Enum):
    NODE_REBOOT = 0x0000001
    ENGINE_DEAD = 0x1000001
    ENGINE_UNHEALTHY = 0x1000002


class NodeStatus(str, Enum):
    READY = "READY"
    NOT_READY = "NOT_READY"


class HardwareFaultType(str, Enum):
    """Fault type enumeration"""

    CARD_UNHEALTHY = "CardUnhealthy"  # Card fault
    CARD_NETWORK_UNHEALTHY = "CardNetworkUnhealthy"  # chip network fault
    NODE_UNHEALTHY = "NodeUnhealthy"  # Node fault


class OriginFaultLevel(str, Enum):
    """Original fault level enumeration for mapping fault type strings"""

    NOT_HANDLE_FAULT = "NotHandleFault"
    RESTART_REQUEST = "RestartRequest"
    RESTART_BUSINESS = "RestartBusiness"
    FREE_RESTART_NPU = "FreeRestartNPU"
    RESTART_NPU = "RestartNPU"
    SEPARATE_NPU = "SeparateNPU"
    PRE_SEPARATE_NPU = "PreSeparateNPU"


class FaultLevel(int, Enum):
    """Fault level enumeration with severity levels from 0 to 6.

    Higher values indicate more severe faults requiring more aggressive
    recovery strategies.
    """

    HEALTHY = 0  # Healthy state, no faults
    L1 = 1  # Level 1 faults that don't require handling
    L2 = 2  # Level 2 faults that can be self-healed
    L3 = 3  # Level 3 faults that cannot be handled automatically
    L4 = 4  # Level 4 faults requiring severe isolation actions
    L5 = 5  # Level 5 faults requiring NPU restart
    L6 = 6  # Level 6 faults requiring NPU separation


class FaultInfo(BaseModel):
    """Unified fault information for both hardware and software faults."""

    # --- Common fields ---
    fault_category: FaultCategory = Field(default=FaultCategory.HARDWARE, description="Fault category")
    fault_level: FaultLevel = Field(default=FaultLevel.L1, description="Fault level, L1, L2, L3, L4, L5, L6")
    fault_code: int = Field(default=0x0, description="Fault code")

    # --- Hardware-specific ---
    fault_type: HardwareFaultType | None = Field(default=None, description="Fault type (hardware only)")
    npu_name: str = Field(default="", description="Faulty chip name, empty for node faults")
    origin_fault_level: OriginFaultLevel | None = Field(
        default=None, description="Original fault level (hardware only)"
    )

    # --- Software-specific ---
    exception_type: str | None = Field(default=None, description="Exception class name (software only)")
    exception_message: str | None = Field(default=None, description="Exception message (software only)")
    engine_id: int | None = Field(default=None, description="Engine ID (software only)")
    engine_status: int | None = Field(default=None, description="EngineStatusType value (software only)")
    timestamp: str | None = Field(default=None, description="Fault timestamp")
    additional_info: dict | None = Field(default=None, description="Additional fault info (software only)")

    @classmethod
    def from_exception(
        cls,
        exception: Exception,
        engine_id: int,
        engine_status: int,
        additional_info: dict | None = None,
    ) -> "FaultInfo":
        """Create a software FaultInfo from an exception."""
        fault_level = cls._map_engine_status_to_fault_level(engine_status)
        fault_code = cls._engine_status_to_fault_code(engine_status)
        local_time = time.localtime(time.time())
        return cls(
            fault_category=FaultCategory.SOFTWARE,
            fault_level=fault_level,
            fault_code=fault_code,
            exception_type=type(exception).__name__,
            exception_message=str(exception),
            engine_id=engine_id,
            engine_status=int(engine_status),
            timestamp=time.strftime("%H:%M:%S", local_time),
            additional_info=additional_info or {},
        )

    @staticmethod
    def _map_engine_status_to_fault_level(engine_status: int) -> FaultLevel:
        """Map EngineStatusType to FaultLevel. All engine issues map to L2."""
        # EngineStatusType: HEALTHY=0, DEAD=1, UNHEALTHY=2
        # DEAD and UNHEALTHY both map to L2; strategy dispatch happens by fault_code at L2
        if engine_status in (1, 2):  # DEAD or UNHEALTHY
            return FaultLevel.L2
        return FaultLevel.L1

    @staticmethod
    def _engine_status_to_fault_code(engine_status: int) -> int:
        """Map EngineStatusType to a fault code."""
        if engine_status == 1:
            return int(SpecialFaultCode.ENGINE_DEAD)
        elif engine_status == 2:
            return int(SpecialFaultCode.ENGINE_UNHEALTHY)
        return 0x0


def map_fault_type(fault_type_str: str) -> HardwareFaultType:
    """Map fault type string to HardwareFaultType enum.

    Maps fault type strings from configuration to corresponding HardwareFaultType
    enumeration values based on predefined mapping rules.

    Args:
        fault_type_str: Fault type string from configuration data

    Returns:
        HardwareFaultType: Mapped fault type enum value, defaults to NODE_UNHEALTHY for unknown types

    Mapping rules:
    - CardUnhealthy -> HardwareFaultType.CARD_UNHEALTHY
    - CardNetworkUnhealthy -> HardwareFaultType.CARD_NETWORK_UNHEALTHY
    - Others -> HardwareFaultType.NODE_UNHEALTHY
    """
    fault_type_mapping = {
        "CardUnhealthy": HardwareFaultType.CARD_UNHEALTHY,
        "CardNetworkUnhealthy": HardwareFaultType.CARD_NETWORK_UNHEALTHY,
    }

    return fault_type_mapping.get(fault_type_str, HardwareFaultType.NODE_UNHEALTHY)


def map_fault_level(fault_level_str: str) -> FaultLevel:
    """Map fault level string to FaultLevel enum.

    Maps fault type strings from configuration to corresponding FaultLevel
    enumeration values based on predefined mapping rules.

    Args:
        fault_level_str: Fault level string from configuration data

    Returns:
        FaultLevel: Mapped fault level enum value, defaults to HEALTHY for unknown types

    Mapping rules:
    - L1: OriginFaultLevel.NOT_HANDLE_FAULT
    - L2: OriginFaultLevel.RESTART_REQUEST
    - L3: OriginFaultLevel.RESTART_BUSINESS
    - L4: OriginFaultLevel.FREE_RESTART_NPU
    - L5: OriginFaultLevel.RESTART_NPU
    - L6: OriginFaultLevel.SEPARATE_NPU, OriginFaultLevel.PRE_SEPARATE_NPU
    """
    fault_level_mapping = {
        OriginFaultLevel.NOT_HANDLE_FAULT: FaultLevel.L1,
        OriginFaultLevel.RESTART_REQUEST: FaultLevel.L2,
        OriginFaultLevel.RESTART_BUSINESS: FaultLevel.L3,
        OriginFaultLevel.FREE_RESTART_NPU: FaultLevel.L4,
        OriginFaultLevel.RESTART_NPU: FaultLevel.L5,
        OriginFaultLevel.SEPARATE_NPU: FaultLevel.L6,
        OriginFaultLevel.PRE_SEPARATE_NPU: FaultLevel.L6,
    }

    return fault_level_mapping.get(fault_level_str, FaultLevel.HEALTHY)


class NodeMetadata(BaseModel):
    """
    Each node metadata represents a physical node in the cluster.
    A single physical node may host multiple instances (e.g., Prefill and Decode
    in a 2P1D deployment), so instance tracking uses sets/maps keyed by instance_id.

    We don't determine the node's status, we just use this
    node's device configmap info to update the node's status.
    And the `hardware_fault_infos` is used to record the device faults
    of the node, if there is no device fault, it will be an empty dict.
    """

    node_name: str = Field(..., description="Kubernetes node name")
    instance_ids: set[int] = Field(default_factory=set, description="Instance IDs running on this node")
    instance_pod_ips: dict[int, str] = Field(
        default_factory=dict, description="Per-instance pod IP mapping (instance_id -> pod_ip)"
    )
    instance_job_names: dict[int, str] = Field(
        default_factory=dict, description="Per-instance job name mapping (instance_id -> job_name)"
    )
    node_status: NodeStatus = Field(default=NodeStatus.READY, description="Node status")
    hardware_fault_infos: dict[int, FaultInfo] = Field(
        default_factory=dict, description="Hardware fault information dictionary keyed by fault_code"
    )
    software_fault_infos: dict[int, FaultInfo] = Field(
        default_factory=dict, description="Software fault information dictionary keyed by fault_code"
    )


class InstanceMetadata(BaseModel):
    """
    Instance metadata for fault tolerance management.

    When an instance's nodes are faulty, we need to trigger
    the recovery function, we record the current strategy,
    strategy level and fault code. if the instance is healthy,
    we should try to stop the strategy.

    strategy_fault_level records the fault level of the currently running strategy,
    used for downgrade prevention and fault cleanup after strategy completion.
    """

    instance_id: int = Field(..., description="Instance ID")
    fault_level: FaultLevel = Field(default=FaultLevel.HEALTHY, description="Current instance fault level")
    fault_code: int = Field(default=0x0, description="Fault code that trigger the current strategy")
    strategy_fault_level: FaultLevel = Field(
        default=FaultLevel.HEALTHY, description="Fault level of the currently running strategy"
    )

    # Non-serializable fields (excluded from serialization)
    lock: Any = Field(default=None, exclude=True)
    # StrategyBase instance, using Any to avoid requiring arbitrary_types_allowed
    strategy: Any = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def init_lock(self):
        """Initialize lock if not provided"""
        if self.lock is None:
            self.lock = threading.RLock()
        return self

    def model_dump(self, **kwargs) -> dict:
        """Override model_dump to exclude non-serializable fields"""
        return super().model_dump(exclude={"lock", "strategy"}, **kwargs)
