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
import threading
import copy
from enum import Enum
from types import MappingProxyType
from typing import Optional
from pydantic import BaseModel, Field
from motor.common.logger import get_logger
from motor.common.resources.endpoint import Endpoint, EndpointStatus, Workload
from motor.common.alarm.server_exception_event import ServerExceptionEvent, ServerExceptionReason

logger = get_logger(__name__)

CLEAR_INSTANCE_TIMEOUT = 300
# Default heartbeat timeout for ACTIVE instances (seconds).
# Can be overridden by passing ``active_timeout`` to ``is_all_endpoints_alive()``.
DEFAULT_ACTIVE_HEARTBEAT_TIMEOUT = 10


class InsStatus(str, Enum):
    INITIAL = "initial"
    INACTIVE = "inactive"
    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"

    def __repr__(self) -> str:
        return str.__repr__(self.value)  # Use value representation for serialization


class PDRole(str, Enum):
    ROLE_E = "encode"
    ROLE_P = "prefill"
    ROLE_D = "decode"
    ROLE_U = "union"

    def __repr__(self) -> str:
        return str.__repr__(self.value)

    @classmethod
    def _missing_(cls, value):
        # Accept historical hybrid literal.
        if isinstance(value, str) and value == "both":
            return cls.ROLE_U
        return super()._missing_(value)


# Instance condition event
class InsConditionEvent(str, Enum):
    INSTANCE_INIT = "instance_init"
    INSTANCE_HEARTBEAT_TIMEOUT = "instance_heartbeat_timeout"
    INSTANCE_NORMAL = "instance_normal"
    INSTANCE_ABNORMAL = "instance_abnormal"
    INSTANCE_PAUSED = "instance_paused"
    INSTANCE_RESUMED = "instance_resumed"

    def __repr__(self) -> str:
        return str.__repr__(self.value)


class NodeManagerInfo(BaseModel):
    pod_ip: str = Field(..., description="Node manager pod ip")
    port: str = Field(..., description="Node manager port")
    device_num: int = Field(default=0, description="Number of devices in this node")


class ParallelConfig(BaseModel):
    dp_size: int = Field(default=1, description="Data parallel size")
    pcp_size: int = Field(default=1, description="Prefill context parallel size")
    tp_size: int = Field(default=1, description="Tensor parallel size")
    ep_size: int = Field(default=1, description="Expert parallel size")
    pp_size: int = Field(default=1, description="Pipeline parallel size")
    world_size: int = Field(default=0, description="World size: dp * pcp_size * tp * pp")
    local_world_size: int = Field(default=0, description="Local world size: pcp * tp * pp (no dp)")

    def __init__(
        self,
        dp_size: int = None,
        pcp_size: int = None,
        tp_size: int = None,
        ep_size: int = None,
        pp_size: int = None,
        world_size: int = None,
        local_world_size: int = None,
        **kwargs,
    ) -> None:
        dp_val = dp_size if dp_size is not None else 1
        pcp_val = pcp_size if pcp_size is not None else 1
        tp_val = tp_size if tp_size is not None else 1
        pp_val = pp_size if pp_size is not None else 1
        world_size_val = world_size if world_size is not None else 0
        local_world_size_val = local_world_size if local_world_size is not None else 0

        if world_size_val == 0:
            world_size_val = dp_val * pcp_val * tp_val * pp_val
        if local_world_size_val == 0:
            local_world_size_val = pcp_val * tp_val * pp_val

        enable_ep = kwargs.get('enable_ep', False)
        if enable_ep:
            ep_val = world_size_val
        else:
            ep_val = ep_size if ep_size is not None else 1

        super().__init__(
            dp_size=dp_val,
            pcp_size=pcp_val,
            tp_size=tp_val,
            ep_size=ep_val,
            pp_size=pp_val,
            world_size=world_size_val,
            local_world_size=local_world_size_val,
        )
        logger.debug(
            "ParallelConfig initialized with dp:%d, pcp:%d, tp:%d, ep:%d, pp:%d, "
            "world_size:%d, local_world_size:%d, enable_ep:%s",
            dp_val,
            pcp_val,
            tp_val,
            ep_val,
            pp_val,
            world_size_val,
            local_world_size_val,
            enable_ep,
        )


class Instance(BaseModel):
    """
    instance is a group of endpoints, it can be prefill or decode
    """

    job_name: str = Field(..., description="Instance job name")
    model_name: str = Field(..., description="Instance model name")
    engine_type: str | None = Field(default=None, description="Inference engine family, e.g. vllm or sglang")
    dispatch_capabilities: list[str] = Field(
        default_factory=list,
        description="Supported Motor dispatch plans for this instance",
    )
    id: int = Field(..., description="Instance ID")
    role: str = Field(..., description="Instance role")
    status: InsStatus = Field(default=InsStatus.INITIAL, description="Instance status")
    parallel_config: ParallelConfig | None = Field(None, description="Parallel configuration")
    enable_multi_endpoints: bool = Field(default=True, description="Whether to enable multi-endpoints mode")
    node_managers: list[NodeManagerInfo] = Field(default_factory=list, description="List of node manager info")
    endpoints: dict[str, dict[int, Endpoint]] = Field(
        default_factory=dict, description="Mapping of endpoints by pod IP"
    )
    gathered_workload: Workload = Field(
        default_factory=Workload, description="Gathered workload of all endpoints in the instance"
    )

    def __init__(self, **data) -> None:
        super().__init__(**data)
        self._lock = threading.RLock()  # Use RLock to allow reentrant locking
        # Cache for get_all_endpoints() to avoid repeated tuple creation.
        # Version counter tracks structural changes (add/del); O(1) check vs O(n) hashing.
        self._endpoints_version: int = 0  # Incremented when endpoints structure changes
        self._cached_endpoints_tuple: Optional[tuple[Endpoint, ...]] = None
        self._cached_endpoints_version: Optional[int] = None  # Version at cache time

    def add_node_mgr(self, pod_ip: str, port: str, device_num: int = 0) -> None:
        if pod_ip is None or port is None:
            logger.warning("Invalid pod_ip: %s or port: %s", pod_ip, port)
            return

        node_mgr_info = NodeManagerInfo(pod_ip=pod_ip, port=port, device_num=device_num)
        with self._lock:
            if node_mgr_info not in self.node_managers:
                self.node_managers.append(node_mgr_info)
                logger.info("Add node manager %s:%s to instance:%s", pod_ip, port, self.job_name)
            else:
                logger.info("Node manager %s:%s already in instance:%s", pod_ip, port, self.job_name)

    def del_node_mgr(self, pod_ip: str, port: str) -> None:
        if pod_ip is None or port is None:
            logger.warning("Invalid pod_ip: %s or port: %s", pod_ip, port)
            return

        with self._lock:
            for i, nm in enumerate(self.node_managers):
                if nm.pod_ip == pod_ip and nm.port == port:
                    self.node_managers.pop(i)
                    logger.info("Del node manager %s:%s from instance:%s", pod_ip, port, self.job_name)
                    return
            logger.info("Node manager %s:%s not in instance:%s", pod_ip, port, self.job_name)

    def has_node_mgr(self, pod_ip: str) -> bool:
        if pod_ip is None:
            return False
        with self._lock:
            for nm in self.node_managers:
                if nm.pod_ip == pod_ip:
                    return True
            return False

    def add_endpoints(self, pod_ip: str, endpoints: dict[int, Endpoint]) -> None:
        if endpoints is None or not isinstance(endpoints, dict):
            logger.warning("Invalid endpoints for pod_ip: %s", pod_ip)
            return

        new_endpoint_num = len(endpoints.values())
        current_endpoint_num = self.get_endpoints_num()

        with self._lock:
            old_endpoint_num = len(self.endpoints.get(pod_ip, {}))
            self.endpoints[pod_ip] = endpoints
            actual_added_num = new_endpoint_num - old_endpoint_num
            # Bump version when endpoints structure changes
            self._endpoints_version += 1

        expected_count = self._get_expected_endpoint_count()
        total_endpoints = current_endpoint_num + actual_added_num
        logger.info(
            "Add endpoints for pod_ip:%s, added endpoints number is %d, total endpoint number is %d/%d",
            pod_ip,
            actual_added_num,
            total_endpoints,
            expected_count,
        )

    def del_endpoints(self, pod_ip: str):
        current_endpoint_num = self.get_endpoints_num()
        with self._lock:
            if pod_ip in self.endpoints:
                del_endpoint_num = len(self.endpoints[pod_ip])
                del self.endpoints[pod_ip]
                # Bump version when endpoints structure changes
                self._endpoints_version += 1
            else:
                del_endpoint_num = 0
                logger.warning("Pod_ip:%s not found in instance:%s", pod_ip, self.job_name)

        expected_count = self._get_expected_endpoint_count()
        remaining_endpoints = current_endpoint_num - del_endpoint_num
        logger.info(
            "Del endpoints for pod_ip:%s, deleted endpoints number is %d, total endpoint number is %d/%d",
            pod_ip,
            del_endpoint_num,
            remaining_endpoints,
            expected_count,
        )

    def is_endpoints_enough(self) -> bool:
        """Return True if the instance has enough endpoints or node managers.
        When enable_multi_endpoints is True:
            Check if the number of non-headless endpoints equals dp_size.
        When enable_multi_endpoints is False:
            Check if the number of node managers equals expected node count.
            Expected node count = world_size / device_num_per_node

        Returns:
            bool: Whether this instance has enough endpoints/node managers and is ready.
        """
        with self._lock:
            if self.parallel_config is None:
                return False

            if self.enable_multi_endpoints:
                if self.endpoints is None:
                    return False

                dp_size = self.parallel_config.dp_size
                total_endpoints = self.get_endpoints_num()
                logger.debug("total endpoint size: %d dp size: %d", total_endpoints, dp_size)
                if total_endpoints == dp_size:
                    logger.info("Instance %d has enough endpoints now, endpoint number is %d", self.id, total_endpoints)
                    return True
                return False
            else:
                expected_node_count = self._get_expected_endpoint_count()
                if expected_node_count <= 0:
                    return False

                actual_node_count = len(self.node_managers)
                logger.debug(
                    "world_size: %d, expected_node_count: %d, actual_node_count: %d",
                    self.parallel_config.world_size,
                    expected_node_count,
                    actual_node_count,
                )
                if actual_node_count == expected_node_count:
                    logger.info(
                        "Instance %d has enough node managers now, node manager number is %d",
                        self.id,
                        actual_node_count,
                    )
                    return True
                return False

    def is_all_endpoints_alive(self, active_timeout: float | None = None) -> bool:
        timestamp = time.time()

        if self.status == InsStatus.ACTIVE:
            timeout = active_timeout if active_timeout is not None else DEFAULT_ACTIVE_HEARTBEAT_TIMEOUT
        else:
            timeout = CLEAR_INSTANCE_TIMEOUT

        dead_endpoints: dict[str, list[int]] = {}  # pod_ip -> [endpoint_id]
        with self._lock:
            for pod_endpoints in self.endpoints.values():
                for endpoint in pod_endpoints.values():
                    if not endpoint.is_alive(timestamp, timeout):
                        if endpoint.ip not in dead_endpoints:
                            dead_endpoints[endpoint.ip] = []
                        dead_endpoints[endpoint.ip].append(endpoint.id)

            if dead_endpoints and len(dead_endpoints) > 0:
                for endpoint_ip, endpoint_ids in dead_endpoints.items():
                    from motor.controller.observability.observability import Observability

                    event = ServerExceptionEvent(
                        endpoint_ip=endpoint_ip,
                        endpoint_ids=endpoint_ids,
                        reason_id=ServerExceptionReason.HEARTBEAT_TIMEOUT,
                    )
                    Observability().add_alarm(event)
                logger.warning(
                    "Instance %s(id:%d)'s endpoints %s have heartbeat timeout", self.job_name, self.id, dead_endpoints
                )
                return False
            return True

    def is_all_endpoints_ready(self) -> bool:
        with self._lock:
            for pod_endpoints in self.endpoints.values():
                for endpoint in pod_endpoints.values():
                    if endpoint.status != EndpointStatus.NORMAL:
                        return False
            return True

    def is_have_one_endpoint_abnormal(self) -> bool:
        abnormal_endpoints: dict[str, list[int]] = {}  # pod_ip -> [endpoint_id]
        with self._lock:
            for pod_endpoints in self.endpoints.values():
                for endpoint in pod_endpoints.values():
                    if endpoint.status == EndpointStatus.ABNORMAL:
                        if endpoint.ip not in abnormal_endpoints:
                            abnormal_endpoints[endpoint.ip] = []
                        abnormal_endpoints[endpoint.ip].append(endpoint.id)

            if abnormal_endpoints and len(abnormal_endpoints) > 0:
                for endpoint_ip, endpoint_ids in abnormal_endpoints.items():
                    from motor.controller.observability.observability import Observability

                    event = ServerExceptionEvent(
                        endpoint_ip=endpoint_ip,
                        endpoint_ids=endpoint_ids,
                        reason_id=ServerExceptionReason.ENDPOINT_ABNORMAL,
                    )
                    Observability().add_alarm(event)
                logger.warning(
                    "Instance %s(id:%d)'s endpoints %s have ABNORMAL status", self.job_name, self.id, abnormal_endpoints
                )
                return True
            return False

    def is_all_endpoints_paused(self) -> bool:
        with self._lock:
            if not self.endpoints:
                return False
            for pod_endpoints in self.endpoints.values():
                for endpoint in pod_endpoints.values():
                    if endpoint.status != EndpointStatus.PAUSED:
                        return False
            return True

    def is_any_endpoint_paused(self) -> bool:
        """Check if at least one endpoint is PAUSED (partial PreStop scenario)."""
        with self._lock:
            for pod_endpoints in self.endpoints.values():
                for endpoint in pod_endpoints.values():
                    if endpoint.status == EndpointStatus.PAUSED:
                        return True
            return False

    def is_ip_in_endpoints(self, ip: str) -> bool:
        with self._lock:
            return ip in self.endpoints

    def update_heartbeat(self, ip: str, timestamp: float, status: dict[int, EndpointStatus]) -> bool:
        with self._lock:
            if ip in self.endpoints:
                if len(self.endpoints[ip]) != len(status):
                    logger.error(
                        "Heartbeat status size %s is not equal to endpoints size %s for pod_ip %s in instance %s",
                        len(status),
                        len(self.endpoints[ip]),
                        ip,
                        self.job_name,
                    )
                    return False
                for endpoint in self.endpoints[ip].values():
                    endpoint.hb_timestamp = timestamp
                    endpoint.status = status[endpoint.id]
                logger.debug("Updated heartbeat for pod_ip %s in instance %s", ip, self.job_name)
                return True
            else:
                logger.error("Instance %s not found endpoints for pod_ip %s", self.id, ip)
                return False

    def get_endpoints_num(self) -> int:
        with self._lock:
            if self.endpoints is not None:
                return sum(len(pod_endpoints) for pod_endpoints in self.endpoints.values())
            return 0

    def get_endpoints(self, ip: str) -> MappingProxyType[int, Endpoint]:
        """Get endpoints by pod(server) ip"""
        with self._lock:
            return MappingProxyType(self.endpoints.get(ip, {}))

    def invalidate_endpoints_cache(self) -> None:
        """Invalidate the cached endpoints tuple (e.g. after headless flag changes)."""
        with self._lock:
            self._endpoints_version += 1

    def get_all_endpoints(self, include_headless: bool = False) -> tuple[Endpoint, ...]:
        """Return a tuple of all endpoints, with versioned caching.

        When enable_multi_endpoints is True:
            Return all endpoints.
        When enable_multi_endpoints is False:
            Return only endpoints with id=0 (one endpoint per pod).

        Headless endpoints are excluded by default. Set include_headless=True to
        include them (cache is skipped in this case).

        Cache is invalidated when endpoints structure changes (add/del) or when
        invalidate_endpoints_cache() is called (e.g. headless flag change).
        """
        with self._lock:
            if not include_headless:
                if (
                    self._cached_endpoints_tuple is not None
                    and self._cached_endpoints_version == self._endpoints_version
                ):
                    return self._cached_endpoints_tuple

            eps = []
            for pod_endpoints in self.endpoints.values():
                for endpoint in pod_endpoints.values():
                    if not include_headless and endpoint.headless:
                        continue
                    if self.enable_multi_endpoints:
                        eps.append(endpoint)
                    else:
                        if endpoint.id == 0:
                            eps.append(endpoint)

            if not include_headless:
                self._cached_endpoints_tuple = tuple(eps)
                self._cached_endpoints_version = self._endpoints_version
            return tuple(eps)

    def get_node_managers_num(self) -> int:
        with self._lock:
            return len(self.node_managers)

    def get_node_managers(self) -> list[NodeManagerInfo]:
        with self._lock:
            return self.node_managers.copy()

    def update_instance_status(self, status: InsStatus) -> None:
        with self._lock:
            self.status = status
        logger.info("Instance %s(id:%s) status updated to %s", self.job_name, self.id, status)

    def _get_expected_endpoint_count(self) -> int:
        """Get expected endpoint count based on enable_multi_endpoints flag.
        When enable_multi_endpoints is True:
            Return dp_size (expected endpoint count).
        When enable_multi_endpoints is False:
            Return expected node count (world_size / device_num_per_node).
        """
        if self.parallel_config is None:
            logger.warning("parallel_config is None")
            return 0

        if self.enable_multi_endpoints:
            return self.parallel_config.dp_size
        else:
            if not self.node_managers or len(self.node_managers) == 0:
                logger.warning("node_managers is empty")
                return 0
            device_num_per_node = self.node_managers[0].device_num
            if device_num_per_node <= 0:
                logger.warning("device_num_per_node is %d", device_num_per_node)
                return 0
            return (self.parallel_config.world_size + device_num_per_node - 1) // device_num_per_node


class ReadOnlyInstance:
    """
    A read-only wrapper for Instance that prevents modifications.
    Observers can safely access instance data without risking accidental modifications.
    The wrapper can be deep copied if observers need their own mutable copy.
    """

    def __init__(self, instance: Instance) -> None:
        if not isinstance(instance, Instance):
            raise TypeError("ReadOnlyInstance can only wrap Instance objects")
        self._instance = instance

    def __getattr__(self, name: str):
        """Delegate attribute access to the wrapped instance for read-only properties."""
        # Block modification methods
        modification_methods = {
            'add_node_mgr',
            'del_node_mgr',
            'add_endpoints',
            'del_endpoints',
            'update_heartbeat',
            'update_instance_status',
        }

        if name in modification_methods:
            raise AttributeError(f"'{self.__class__.__name__}' object does not allow modification method '{name}'")

        # For other attributes/methods, delegate to the wrapped instance
        return getattr(self._instance, name)

    def __repr__(self) -> str:
        return f"ReadOnlyInstance({self._instance!r})"

    def __str__(self) -> str:
        return f"ReadOnlyInstance wrapping {self._instance}"

    def __deepcopy__(self, memo):
        """Support deep copying by creating a new instance with copied data."""
        # Create a new Instance with the same data, excluding the lock.
        # engine_type / dispatch_capabilities / enable_multi_endpoints must be carried over
        # (readiness and P/D pairing gate on dispatch_capabilities).
        copied_instance = Instance(
            job_name=self._instance.job_name,
            model_name=self._instance.model_name,
            engine_type=self._instance.engine_type,
            dispatch_capabilities=list(self._instance.dispatch_capabilities or []),
            id=self._instance.id,
            role=self._instance.role,
            enable_multi_endpoints=self._instance.enable_multi_endpoints,
        )
        # Copy status and other attributes
        copied_instance.status = self._instance.status
        copied_instance.parallel_config = copy.deepcopy(self._instance.parallel_config, memo)

        # Deep copy node managers
        copied_instance.node_managers = copy.deepcopy(self._instance.node_managers, memo)

        # Deep copy endpoints (this is the most complex part)
        copied_instance.endpoints = copy.deepcopy(self._instance.endpoints, memo)

        # Copy gathered workload
        copied_instance.gathered_workload = copy.deepcopy(self._instance.gathered_workload, memo)

        return ReadOnlyInstance(copied_instance)

    def get_instance(self) -> Instance:
        """Get the underlying Instance object.

        This method provides controlled access to the internal Instance
        for scenarios where the raw Instance is needed (e.g., serialization).
        The returned Instance should not be modified directly.
        """
        return self._instance

    def to_instance(self) -> Instance:
        """Create a deep copy of the underlying Instance.

        This method creates a new Instance object with the same data as the
        wrapped instance, ensuring that modifications to the returned Instance
        do not affect the original data.
        """
        # Create a new Instance with the same data, excluding the lock.
        # engine_type / dispatch_capabilities / enable_multi_endpoints must be carried over:
        # the controller pushes this copy to the coordinator, and readiness/pairing gate on
        # dispatch_capabilities — dropping it leaves the coordinator with empty capabilities.
        copied_instance = Instance(
            job_name=self._instance.job_name,
            model_name=self._instance.model_name,
            engine_type=self._instance.engine_type,
            dispatch_capabilities=list(self._instance.dispatch_capabilities or []),
            id=self._instance.id,
            role=self._instance.role,
            enable_multi_endpoints=self._instance.enable_multi_endpoints,
        )
        # Copy status and other attributes
        copied_instance.status = self._instance.status
        copied_instance.parallel_config = copy.deepcopy(self._instance.parallel_config)

        # Deep copy node managers
        copied_instance.node_managers = copy.deepcopy(self._instance.node_managers)

        # Deep copy endpoints (this is the most complex part)
        copied_instance.endpoints = copy.deepcopy(self._instance.endpoints)

        # Copy gathered workload
        copied_instance.gathered_workload = copy.deepcopy(self._instance.gathered_workload)

        return copied_instance
