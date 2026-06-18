# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from enum import Enum
from pydantic import BaseModel, Field

from motor.common.logger import get_logger
from motor.common.resources.instance import Instance, ParallelConfig
from motor.common.resources.endpoint import Endpoint, DeviceInfo, EndpointStatus

logger = get_logger(__name__)


class ServerInfo(BaseModel):
    server_id: str = Field(..., description="Host IP address")
    container_ip: str = Field(..., description="Container IP address")
    device: list[DeviceInfo] = Field(..., description="List of DeviceInfo")


class Ranktable(BaseModel):
    """
    Instance level ranktable, it is unified between different infer engine
    """

    version: str = Field(..., description="")
    status: str = Field(..., description="")
    server_count: str = Field(..., description="")
    server_list: list[ServerInfo] = Field(..., description="List of ServerInfo")


class RegisterMsg(BaseModel):
    """
    Registration message format sent from NodeManager to controller.
    """

    job_name: str = Field(..., description="Instance job name")
    model_name: str = Field(..., description="Instance model name")
    engine_type: str | None = Field(default=None, description="Inference engine family")
    dispatch_capabilities: list[str] = Field(
        default_factory=list,
        description="Supported Motor dispatch plans for this instance",
    )
    role: str = Field(..., description="Instance role")
    pod_ip: str = Field(..., description="Pod IP address")
    business_port: list[str] = Field(..., description="Business port for all endpoints managed by this nm")
    mgmt_port: list[str] = Field(..., description="Management port for all endpoints managed by this nm")
    nm_port: str = Field(..., description="Node manager communication port")
    parallel_config: ParallelConfig = Field(..., description="Parallel configuration")
    enable_multi_endpoints: bool = Field(default=True, description="Whether to enable multi-endpoints mode")
    device_num: int = Field(..., description="Number of visible devices in the container")
    ranktable: Ranktable | None = Field(default=None, description="Ranktable managed by this nm")
    nnodes: int = Field(default=1, description="PCP cross-node count, from engine_config")
    is_master: bool = Field(
        default=False,
        description="Whether this node hosts the DP0 engine (snapshot master node)",
    )


class StartCmdMsg(BaseModel):
    """
    Start command message format sent from controller to NodeManager.
    This msg brings the necessary information .e.g instance's ranktable
    and instance id and role for NodeManager to start the instance.
    """

    job_name: str = Field(..., description="Instance job name")
    role: str = Field(..., description="Instance role")
    instance_id: int = Field(..., description="Instance id")
    endpoints: list[Endpoint] = Field(..., description="endpoints that managed by nm")
    master_dp_ip: str = Field(..., description="Master data parallel node IP address")
    ranktable: Ranktable | None = Field(default=None, description="Ranktable of the instance")
    d2d_peer_ips: list[str] | None = Field(
        default=None, description="IP addresses of ready peer instances for D2D weight transfer"
    )
    node_rank: int = Field(default=0, description="Node rank assigned by Controller (registration order)")


class ReregisterMsg(BaseModel):
    """
    Re-register message format sent from NodeManager to controller.
    It only occured when controller restarts and NodeManager needs to
    re-register to controller.
    """

    job_name: str = Field(..., description="Instance job name")
    model_name: str = Field(..., description="Instance model name")
    engine_type: str | None = Field(default=None, description="Inference engine family")
    dispatch_capabilities: list[str] = Field(
        default_factory=list,
        description="Supported Motor dispatch plans for this instance",
    )
    instance_id: int = Field(..., description="Instance id")
    role: str = Field(..., description="Instance role")
    pod_ip: str = Field(..., description="Pod IP address")
    nm_port: str = Field(..., description="Node manager communication port")
    parallel_config: ParallelConfig = Field(..., description="Parallel configuration")
    enable_multi_endpoints: bool = Field(default=True, description="Whether to enable multi-endpoints mode")
    device_num: int = Field(default=0, description="Number of visible devices in the container")
    endpoints: list[Endpoint] = Field(..., description="endpoints that managed by nm")
    nnodes: int = Field(default=1, description="PCP cross-node count, from engine_config")
    node_rank: int = Field(default=0, description="PCP node rank assigned by Controller")


class HeartbeatMsg(BaseModel):
    """
    Heartbeat message format sent from NodeManager to controller.
    """

    job_name: str = Field(..., description="Instance job name")
    ins_id: int = Field(..., description="Instance id")
    ip: str = Field(..., description="Pod IP address")
    status: dict[int, EndpointStatus] = Field(..., description="Endpoints status list")


class TerminateInstanceMsg(BaseModel):
    """
    Heartbeat message format sent from NodeManager to controller.
    """

    instance_id: int = Field(..., description="Instance id")
    reason: str = Field(..., description="The reason for terminating the instance")


class EventType(str, Enum):
    """
    Event types for instance events, currently include add, delete, and set.
    And used by EventPusher to notify the coordinator.
    """

    ADD = "add"
    DEL = "del"
    SET = "set"
    PAUSE = "pause"
    RESUME = "resume"

    def __repr__(self) -> str:
        return str.__repr__(self.value)  # return the value of the enum


class InsEventMsg(BaseModel):
    """
    Message format for instance events to be sent to the coordinator.
    Add and delete events carry a list of instances, while set events
    carry the full list of instances for the coordinator to update its state.
    """

    event: EventType = Field(..., description="event type: add, del, set")
    instances: list[Instance] = Field(..., description="instances for coordinator")
