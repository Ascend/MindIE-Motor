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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from motor.common.logger import get_logger
from motor.common.resources.dispatch import DispatchPlan
from motor.common.resources.instance import InsStatus, Instance, ParallelConfig, PDRole
from motor.common.resources.endpoint import Endpoint, DeviceInfo, EndpointStatus
from motor.common.utils.net import split_address

logger = get_logger(__name__)

_EXTERNAL_ALLOWED_DISPATCH_PLANS = (
    DispatchPlan.PREFILL_HANDOFF_DECODE,
    DispatchPlan.CONCURRENT_ENGINE_SYNC,
)


def _require_external_dispatch_plan(value: DispatchPlan) -> DispatchPlan:
    if value not in _EXTERNAL_ALLOWED_DISPATCH_PLANS:
        allowed = ", ".join(plan.value for plan in _EXTERNAL_ALLOWED_DISPATCH_PLANS)
        raise ValueError(f"External Deployer dispatch_capabilities must be one of: {allowed}")
    return value


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
    bootstrap_port: int | None = Field(default=None, ge=1, le=65535, description="Native PD bootstrap port")
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
    p_instance_id: int | None = Field(default=None, description="Optional paired P instance id for PD-group recovery")
    precision_alarm_clear: bool = Field(
        default=False,
        description="Whether to clear active precision alarm after terminating the PD group",
    )


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

    @model_validator(mode="after")
    def validate_unique_instance_ids(self) -> "InsEventMsg":
        """Reject duplicate IDs before consumers convert the list into an ID-keyed mapping."""
        instance_ids = [instance.id for instance in self.instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("duplicate instance IDs in one request")
        return self


class ExternalEndpoint(BaseModel):
    """Minimal routable endpoint supplied by an External Deployer."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., ge=0, description="Data-parallel endpoint ID")
    address: str = Field(
        ..., min_length=1, description="Engine host:port, e.g. 192.168.1.20:8200 or [2001:db8::1]:8200"
    )

    def parsed_host_port(self) -> tuple[str, str]:
        host, port = split_address(self.address.strip())
        if not host:
            raise ValueError(f"address must contain a host, got {self.address!r}")
        if not port:
            raise ValueError(f"address must contain a port, got {self.address!r}")
        try:
            port_int = int(port)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"address port must be an integer, got {port!r}") from exc
        if not 1 <= port_int <= 65535:
            raise ValueError(f"address port must be in range 1-65535, got {port_int}")
        return host, str(port_int)


class ExternalInstance(BaseModel):
    """Minimal P/D instance supplied by an External Deployer."""

    model_config = ConfigDict(extra="forbid")

    id: int = Field(..., ge=1, description="Globally unique instance ID")
    role: PDRole = Field(..., description="Only prefill or decode is supported")
    endpoints: list[ExternalEndpoint] = Field(..., min_length=1, description="Routable engine endpoints")


class ExternalInsEventMsg(BaseModel):
    """Standalone Coordinator instance event containing only routing information."""

    model_config = ConfigDict(extra="forbid")

    event: EventType
    model_name: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "The single served model. Optional in single-model deployments; "
            "Coordinator resolves it from the first reachable engine /v1/models. "
            "Required when any engine advertises multiple models."
        ),
    )
    dispatch_capabilities: DispatchPlan = Field(
        default=DispatchPlan.PREFILL_HANDOFF_DECODE,
        description="P/D coordination plan; only prefill_handoff_decode or concurrent_engine_sync",
    )
    engine_type: str = Field(
        default="vllm", min_length=1, description="Inference engine family shared by all instances"
    )
    instances: list[ExternalInstance]

    @field_validator("dispatch_capabilities")
    @classmethod
    def reject_non_pd_dispatch_plans(cls, value: DispatchPlan) -> DispatchPlan:
        return _require_external_dispatch_plan(value)

    def to_internal(self, configured_model_name: str = "", resolved_model_name: str = "") -> InsEventMsg:
        if self.event not in (EventType.SET, EventType.ADD, EventType.DEL):
            raise ValueError("External Deployer only supports event='set', 'add', or 'del'")

        model_name = (self.model_name or resolved_model_name).strip()
        if not model_name:
            raise ValueError("model_name is required when it cannot be resolved from native engine /v1/models")
        if configured_model_name and model_name.casefold() != configured_model_name.casefold():
            raise ValueError(
                f"external model_name {model_name!r} does not match configured single model {configured_model_name!r}"
            )
        internal_model_name = configured_model_name or model_name

        dispatch_capability = _require_external_dispatch_plan(self.dispatch_capabilities).value
        engine_type = self.engine_type.strip().lower()
        if engine_type != "vllm":
            raise ValueError("External Deployer currently supports engine_type='vllm' only")

        instance_ids: set[int] = set()
        internal_instances: list[Instance] = []
        for external_instance in self.instances:
            if external_instance.id in instance_ids:
                raise ValueError(f"duplicate instance id: {external_instance.id}")
            instance_ids.add(external_instance.id)
            if external_instance.role not in (PDRole.ROLE_P, PDRole.ROLE_D):
                raise ValueError(f"unsupported external instance role: {external_instance.role.value}")

            endpoint_ids: set[int] = set()
            endpoint_addresses: set[tuple[str, str]] = set()
            endpoints_by_ip: dict[str, dict[int, Endpoint]] = {}
            for external_endpoint in external_instance.endpoints:
                if external_endpoint.id in endpoint_ids:
                    raise ValueError(f"duplicate endpoint id {external_endpoint.id} in instance {external_instance.id}")
                endpoint_ids.add(external_endpoint.id)
                host, business_port = external_endpoint.parsed_host_port()
                address_key = (host, business_port)
                if address_key in endpoint_addresses:
                    raise ValueError(
                        f"duplicate endpoint address {external_endpoint.address!r} in instance {external_instance.id}"
                    )
                endpoint_addresses.add(address_key)
                endpoint = Endpoint(
                    id=external_endpoint.id,
                    ip=host,
                    business_port=business_port,
                    status=EndpointStatus.NORMAL,
                )
                endpoints_by_ip.setdefault(endpoint.ip, {})[endpoint.id] = endpoint

            internal_instances.append(
                Instance(
                    job_name=f"external-{external_instance.role.value}-{external_instance.id}",
                    model_name=internal_model_name,
                    engine_type=engine_type,
                    dispatch_capabilities=[dispatch_capability],
                    id=external_instance.id,
                    role=external_instance.role.value,
                    status=InsStatus.ACTIVE,
                    parallel_config=ParallelConfig(dp_size=len(endpoint_ids)),
                    enable_multi_endpoints=True,
                    endpoints=endpoints_by_ip,
                )
            )

        return InsEventMsg(event=self.event, instances=internal_instances)
