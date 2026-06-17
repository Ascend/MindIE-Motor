# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import time
from enum import Enum
from pydantic import BaseModel, Field

from motor.common.logger import get_logger

HEARTBEAT_TIMEOUT = 5  # 5 seconds

logger = get_logger(__name__)


class Workload(BaseModel):
    """Workload information for load balancing"""

    active_kv_cache: float = Field(default=0, description="Active KV cache size")
    active_tokens: float = Field(default=0, description="Number of active requests")

    def __iadd__(self, other):
        if not isinstance(other, Workload):
            raise TypeError("Unsupported operand type(s) for +=: 'Workload' and %s", type(other).__name__)

        self.active_kv_cache += other.active_kv_cache
        self.active_tokens += other.active_tokens

        return self

    def calculate_workload_score(self, role: Enum | str | None) -> float:
        """
        Calculate workload score based on role.

        Args:
            role: PDRole enum or str ("prefill"/"decode"/"mix") indicating the role.

        Returns:
            float: Calculated workload score
        """
        if role is None:
            raise ValueError("role is required for calculate_workload_score")
        role_value = role.value if isinstance(role, Enum) else role
        if role_value == "prefill":
            return self.active_tokens + self.active_kv_cache * 0.3
        elif role_value == "decode":
            return self.active_tokens
        elif role_value == "encode":
            return self.active_tokens
        elif role_value in ("union", "both"):
            return self.active_tokens + self.active_kv_cache * 0.15
        else:
            raise ValueError(f"Invalid role value: {role_value}")


class WorkloadAction(Enum):
    ALLOCATION = 'Allocation'
    RELEASE_KV = 'Release_KV'
    RELEASE_TOKENS = 'Release_Tokens'


class DeviceInfo(BaseModel):
    device_ip: str | None = Field(default=None, description="IP address of the device, A5 will be None")
    device_id: str = Field(..., description="Local rank id")
    rank_id: str = Field(..., description="Global rank id")
    super_device_id: str | None = Field(default=None, description="Super device id, A5 will be None")


class EndpointStatus(str, Enum):
    INITIAL = "initial"
    NORMAL = "normal"
    ABNORMAL = "abnormal"
    PAUSED = "paused"
    WAIT2START = "wait2start"

    def __repr__(self) -> str:
        return str.__repr__(self.value)


class Endpoint(BaseModel):
    id: int = Field(..., description="Endpoint ID, it associated with data parallel rank id")
    ip: str = Field(..., description="IP address")
    business_port: str = Field(..., description="Business port")
    mgmt_port: str = Field(..., description="Management port")
    status: EndpointStatus = Field(default=EndpointStatus.INITIAL, description="Endpoint status")
    device_infos: list[DeviceInfo] = Field(default_factory=list, description="List of DeviceInfo")
    hb_timestamp: float = Field(default=0, description="Last heartbeat timestamp")
    workload: Workload = Field(default_factory=Workload, description="Current workload of the endpoint")
    headless: bool = Field(default=False, description="Whether this endpoint runs in headless mode (PCP slave node)")

    def __init__(
        self,
        id: int,
        ip: str,
        business_port: str,
        mgmt_port: str,
        status: EndpointStatus | None = None,
        device_infos: list[DeviceInfo] | None = None,
        hb_timestamp: float | None = None,
        workload: Workload | None = None,
        headless: bool = False,
    ) -> None:
        super().__init__(
            id=id,
            ip=ip,
            business_port=business_port,
            mgmt_port=mgmt_port,
            status=status if status is not None else EndpointStatus.INITIAL,
            device_infos=device_infos if device_infos is not None else [],
            hb_timestamp=hb_timestamp if hb_timestamp is not None else time.time(),
            workload=workload if workload is not None else Workload(),
            headless=headless,
        )
        logger.debug("Init endpoint with id:%s ip:%s business_port:%s mgmt_port:%s", id, ip, business_port, mgmt_port)

    def add_device(self, device_info: DeviceInfo) -> None:
        if device_info not in self.device_infos:
            self.device_infos.append(device_info)

    def del_device(self, device_info: DeviceInfo) -> None:
        if device_info in self.device_infos:
            self.device_infos.remove(device_info)

    def is_alive(self, timestamp: float, heartbeat_timeout: int = HEARTBEAT_TIMEOUT) -> bool:
        return (timestamp - self.hb_timestamp) <= heartbeat_timeout
