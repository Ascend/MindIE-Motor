# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import asyncio
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from motor.common.logger import get_logger
from motor.common.logger.rate_limited_logger import RateLimitedLogger
from motor.common.resources.dispatch import has_compatible_dispatch_pair
from motor.common.resources.instance import Instance, PDRole, Workload, Endpoint
from motor.common.resources.http_msg_spec import EventType
from motor.config.coordinator import CoordinatorConfig
from motor.coordinator.domain.scheduling import InstanceReadiness, readiness_from_instances
from motor.coordinator.api_client.conductor_api_client import ConductorApiClient


TYPE_SCHEDULER = "schedule"
TYPE_MGMT = "mgmt"
TYPE_OBS = "obs"

logger = get_logger(__name__)
_rl = RateLimitedLogger(logger)


def _role_to_pdrole(role: PDRole | str) -> PDRole:
    """Normalize role to PDRole for use as _available_role_pools key (avoid str/enum key mismatch)."""
    return PDRole(role) if isinstance(role, str) else role


def _clamp_workload_floor(workload: Workload) -> bool:
    """Clamp negative workload fields to 0 in place. Returns True if any field was clamped."""
    floored = False
    if workload.active_tokens < 0:
        workload.active_tokens = 0.0
        floored = True
    if workload.active_kv_cache < 0:
        workload.active_kv_cache = 0.0
        floored = True
    return floored


def _rebuild_instance_workload(instance: Instance) -> bool:
    """Rebuild an instance workload from its endpoint ledgers and floor invalid values."""
    active_tokens = 0.0
    active_kv_cache = 0.0
    floored = False
    for pod_endpoints in (instance.endpoints or {}).values():
        for endpoint in (pod_endpoints or {}).values():
            if endpoint.workload is None:
                endpoint.workload = Workload()
            floored = _clamp_workload_floor(endpoint.workload) or floored
            active_tokens += endpoint.workload.active_tokens
            active_kv_cache += endpoint.workload.active_kv_cache
    instance.gathered_workload = Workload(
        active_tokens=active_tokens,
        active_kv_cache=active_kv_cache,
    )
    return floored


class UpdateInstanceMode(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"

    def __repr__(self) -> str:
        return str.__repr__(self.value)


class InstanceManager:
    """
    Available/unavailable instance pools; workload updates. Implements
    InstanceProvider. Created explicitly and injected; no singleton.
    """

    def __init__(self, config: CoordinatorConfig | None = None, typename: str = TYPE_SCHEDULER):
        if config is None:
            config = CoordinatorConfig()
        self._lock = asyncio.Lock()
        self.typename = typename
        self._workload_locks: dict[int, asyncio.Lock] = {}
        self._available_pool: dict[int, Instance] = {}
        self._unavailable_pool: dict[int, Instance] = {}
        self._paused_pool: dict[int, Instance] = {}

        self._encode_pool: dict[int, Instance] = {}
        self._prefill_pool: dict[int, Instance] = {}
        self._decode_pool: dict[int, Instance] = {}
        self._hybrid_pool: dict[int, Instance] = {}

        self._available_role_pools = {
            PDRole.ROLE_E: self._encode_pool,
            PDRole.ROLE_P: self._prefill_pool,
            PDRole.ROLE_D: self._decode_pool,
            PDRole.ROLE_U: self._hybrid_pool,
        }
        # instance_id -> {endpoint_id -> Endpoint} for update_instance_workload O(1) lookup
        self._endpoint_id_cache: dict[int, dict[int, Endpoint]] = {}
        logger.info("InstanceManager started.")

    def get_required_instances_status(self) -> InstanceReadiness:
        """Return readiness inferred from roles and compatible dispatch capabilities."""
        instances = (
            *self._encode_pool.values(),
            *self._prefill_pool.values(),
            *self._decode_pool.values(),
            *self._hybrid_pool.values(),
        )
        return readiness_from_instances(instances)

    def has_required_instances(self) -> bool:
        """True when the current instance topology can serve requests."""
        return self.get_required_instances_status().is_run()

    async def stop(self) -> None:
        """
        Stop instance_manager, delete all info.

        :returns:
        """
        async with self._lock:
            self._available_pool = {}
            self._unavailable_pool = {}
            self._paused_pool = {}
            self._encode_pool = {}
            self._prefill_pool = {}
            self._decode_pool = {}
            self._hybrid_pool = {}
            self._available_role_pools = {
                PDRole.ROLE_E: self._encode_pool,
                PDRole.ROLE_P: self._prefill_pool,
                PDRole.ROLE_D: self._decode_pool,
                PDRole.ROLE_U: self._hybrid_pool,
            }
            self._workload_locks.clear()
            self._endpoint_id_cache.clear()
        logger.info("InstanceManager stopped.")

    def get_available_instances(self, role: PDRole | None = None) -> Mapping[int, Instance]:
        """
        Return read-only view, zero-copy; caller must not mutate.
        role=None means all roles (for GET_AVAILABLE_INSTANCES without role or hybrid).
        """
        # no need to lock here, asynchrony is acceptable
        if role is None:
            merged = {
                **self._encode_pool,
                **self._prefill_pool,
                **self._decode_pool,
                **self._hybrid_pool,
            }
            return MappingProxyType(merged)
        instance_pool = self._available_role_pools.get(role)
        if instance_pool is None:
            logger.error("Unknown role: %s, while getting available instances", role)
            return MappingProxyType({})
        return MappingProxyType(instance_pool)

    async def get_all_instances(self) -> tuple[dict[int, Instance], dict[int, Instance]]:
        # Hold lock only for items() snapshot; build dict outside to shorten lock hold
        async with self._lock:
            avail_items = list(self._available_pool.items())
            unavail_items = list(self._unavailable_pool.items())
        return dict(avail_items), dict(unavail_items)

    def update_instance_workload_sync(
        self,
        instance_id: int,
        endpoint_id: int,
        workload_change: Workload,
    ) -> tuple[PDRole | None, Workload | None]:
        """Synchronously update workload and return the endpoint's new workload."""
        instance = self._available_pool.get(instance_id)
        if instance is None:
            logger.warning("Instance ID %s not found in available pool while updating workload", instance_id)
            return (None, None)
        ep_cache = self._endpoint_id_cache.get(instance_id)
        if ep_cache is None:
            ep_cache = {}
            for pod_eps in (instance.endpoints or {}).values():
                for ep in (pod_eps or {}).values():
                    ep_cache[ep.id] = ep
            self._endpoint_id_cache[instance_id] = ep_cache
        endpoint = ep_cache.get(endpoint_id)
        if endpoint is None:
            logger.warning(
                "Endpoint ID %s not found in instance ID %s while updating workload",
                endpoint_id,
                instance_id,
            )
            return (None, None)
        endpoint.workload += workload_change
        # Ledger floor: workload is an unbounded signed accumulator updated by ALLOCATION(+) /
        # RELEASE(-) deltas. A release that exceeds the endpoint's outstanding allocation (e.g. a
        # duplicated/late release, or one whose allocation was reset) would drive the counter
        # negative and make this endpoint a permanent minimum-score scheduling magnet. Clamp to
        # zero so it self-heals, and warn (rate-limited) so the reconciliation gap stays visible.
        if _clamp_workload_floor(endpoint.workload):
            # Over-release path (rare): rebuild the aggregate from the endpoint ledgers so a floored
            # endpoint can't hide positive load on its siblings. Independently flooring the aggregate
            # would mask that sibling load.
            _rebuild_instance_workload(instance)
            _rl.error_window(
                f"workload_floor:{instance_id}:{endpoint_id}",
                "Workload floored to 0 (release exceeded allocation, accounting gap) "
                f"instance_id={instance_id} endpoint_id={endpoint_id} "
                f"change=(tokens={workload_change.active_tokens},kv={workload_change.active_kv_cache})",
                window_sec=60,
                level="WARNING",
            )
        else:
            # Fast path: the endpoint ledger stayed non-negative, so the invariant
            # gathered_workload == sum(endpoint ledgers) still holds; maintain it in O(1) instead of
            # rescanning every endpoint of the instance on this hot path.
            instance.gathered_workload += workload_change
        logger.debug(
            "Updated workload instance_id=%s endpoint_id=%s",
            instance_id,
            endpoint_id,
        )
        role = _role_to_pdrole(instance.role) if instance.role else PDRole.ROLE_U
        return (role, endpoint.workload)

    async def update_instance_workload(self, instance_id: int, endpoint_id: int, workload_change: Workload) -> None:
        """Update workload of instance and its endpoint in pool (ids only). O(1) lookup via _endpoint_id_cache."""
        self.update_instance_workload_sync(instance_id, endpoint_id, workload_change)

    def get_endpoint_workload_sync(self, instance_id: int, endpoint_id: int) -> tuple[PDRole | None, Workload | None]:
        """
        Get role and workload for endpoint by instance_id and endpoint_id.
        Used by WorkloadSharedMemoryWriter.write_single_entry for incremental write.

        Returns:
            (role, workload): (instance.role, endpoint.workload) if found;
            (None, None) if instance or endpoint does not exist.
        """
        instance = self._available_pool.get(instance_id)
        if instance is None:
            return (None, None)
        ep_cache = self._endpoint_id_cache.get(instance_id)
        if ep_cache is None:
            ep_cache = {}
            for pod_eps in (instance.endpoints or {}).values():
                for ep in (pod_eps or {}).values():
                    ep_cache[ep.id] = ep
            self._endpoint_id_cache[instance_id] = ep_cache
        endpoint = ep_cache.get(endpoint_id)
        if endpoint is None:
            return (None, None)
        role = _role_to_pdrole(instance.role) if instance.role else PDRole.ROLE_U
        return (role, endpoint.workload)

    async def get_endpoint_workload(self, instance_id: int, endpoint_id: int) -> tuple[PDRole | None, Workload | None]:
        """
        Get role and workload for endpoint by instance_id and endpoint_id.
        Used by WorkloadSharedMemoryWriter.write_single_entry for incremental write.

        Returns:
            (role, workload): (instance.role, endpoint.workload) if found;
            (None, None) if instance or endpoint does not exist.
        """
        return self.get_endpoint_workload_sync(instance_id, endpoint_id)

    async def has_instance_endpoint(self, instance_id: int, endpoint_id: int) -> bool:
        """Check if (instance_id, endpoint_id) exists in available pool. For ALLOCATE_ONLY validation."""
        async with self._lock:
            instance = self._available_pool.get(instance_id)
            if instance is None:
                return False
            ep_cache = self._endpoint_id_cache.get(instance_id)
            if ep_cache is None:
                for pod_eps in (instance.endpoints or {}).values():
                    for ep in (pod_eps or {}).values():
                        if ep.id == endpoint_id:
                            return True
                return False
            return endpoint_id in ep_cache

    async def delete_unavailable_instance(self, instance_id: int) -> None:
        async with self._lock:
            if instance_id not in self._unavailable_pool:
                logger.warning("Instance ID %s not found in unavailable instance pool yet, cannot delete", instance_id)
                return

            del self._unavailable_pool[instance_id]
            logger.info("Deleted unavailable instance with ID %s successfully", instance_id)

    async def update_instance_state(self, instance_id: int, update_mode: UpdateInstanceMode) -> None:
        if update_mode == UpdateInstanceMode.AVAILABLE:
            async with self._lock:
                if instance_id not in self._unavailable_pool:
                    logger.warning(
                        "Instance ID %s not found in unavailable instance pool, cannot update to available",
                        instance_id,
                    )
                    return

                instance = self._unavailable_pool[instance_id]
                del self._unavailable_pool[instance_id]

                if not self._add_instance_to_available_pool(instance):
                    logger.error(
                        "Failed to add instance ID %s to available pool, while updating to available",
                        instance_id,
                    )
                    return

                logger.info("Instance ID %s updated to available successfully", instance_id)

        elif update_mode == UpdateInstanceMode.UNAVAILABLE:
            async with self._lock:
                instance = self._available_pool.get(instance_id)
                if instance is None:
                    logger.warning(
                        "Instance ID %s not found in available instance pool, cannot update to unavailable",
                        instance_id,
                    )
                    return

                if not self._delete_instance_from_available_pool(instance_id):
                    logger.warning(
                        "Failed to delete instance ID %s from available pool, while updating to unavailable",
                        instance_id,
                    )
                    return

                self._unavailable_pool[instance_id] = instance
                logger.info("Instance ID %s updated to unavailable successfully", instance_id)

    async def refresh_instances(self, event_type: EventType, instances: list[Instance]) -> bool:
        """Apply instance refresh; return True if pools were modified (for Scheduler notify)."""
        async with self._lock:
            # Log instance change summary: event type, count, and instance ids
            change_summary = [(inst.id, getattr(inst, "role", None)) for inst in instances]
            logger.info(
                "Refresh instances: event_type=%s, count=%d, instance_ids=%s",
                event_type,
                len(instances),
                change_summary,
            )
            if event_type == EventType.ADD:
                result = self._add_instances(instances)
                # The _register_kv_instance function is called in _add_instances.
            elif event_type == EventType.DEL:
                result = self._delete_instances(instances)
                self._register_kv_instance(instances, False)
            elif event_type == EventType.SET:
                result = self._apply_set_diff(instances)
                # The _register_kv_instance function is called in _add_instances.
            elif event_type == EventType.PAUSE:
                result = self._pause_instances(instances)
            elif event_type == EventType.RESUME:
                result = self._resume_instances(instances)
            else:
                logger.error("Unknown event type: %s, cannot refresh instances", event_type)
                result = False
            logger.info(
                "Refresh instances done: E=%d, P=%d, D=%d, U=%d",
                len(self._encode_pool),
                len(self._prefill_pool),
                len(self._decode_pool),
                len(self._hybrid_pool),
            )
            self._log_dispatch_capabilities()
            return result

    def _log_dispatch_capabilities(self) -> None:
        """Log per-instance dispatch_capabilities and flag an incompatible P/D pool.

        Readiness gates on a shared P/D dispatch capability; when P and D are both present
        but advertise no common capability the service stays not-ready with a vague
        instances_status=unknown. This makes the actual capabilities (empty vs mismatched) visible.
        """

        def _role(inst: Instance) -> str:
            role = getattr(inst, "role", None)
            return role.value if hasattr(role, "value") else str(role)

        summary = ", ".join(
            f"{inst.id}({_role(inst)})={list(getattr(inst, 'dispatch_capabilities', []) or [])}"
            for pool in (self._prefill_pool, self._decode_pool, self._encode_pool, self._hybrid_pool)
            for inst in pool.values()
        )
        logger.info("Instance dispatch_capabilities: %s", summary or "(none)")

        if self._prefill_pool and self._decode_pool:
            if not has_compatible_dispatch_pair(self._prefill_pool.values(), self._decode_pool.values()):
                if self._hybrid_pool:
                    logger.warning(
                        "P/D instances are online but advertise no shared dispatch capability; "
                        "requests will fall back to PDHybridRouter via union instances. "
                        "Check the engine kv_connector is recognized or set dispatch_profile explicitly. "
                        "capabilities: %s",
                        summary or "(none)",
                    )
                else:
                    logger.warning(
                        "P/D instances are online but advertise no shared dispatch capability "
                        "(readiness will report instances_status=unknown). Check the engine kv_connector "
                        "is recognized or set dispatch_profile explicitly. capabilities: %s",
                        summary or "(none)",
                    )

    def _find_available_pool(self, instance_id: int) -> dict[int, Instance] | None:
        # This is a private method that should only be called within locked contexts
        instance = self._available_pool.get(instance_id)
        if instance is None:
            return None
        return self._available_role_pools.get(_role_to_pdrole(instance.role))

    def _register_kv_instance(self, instances: list[Instance], is_register: bool = True) -> None:
        """Only the Mgmt process registers KV instances (Scheduler/Obs are mirrors)."""
        if self.typename != TYPE_MGMT:
            return

        if is_register:
            ConductorApiClient().register_kv_instance(instances)
        else:
            ConductorApiClient().unregister_kv_instance(instances)

    def _add_instances(self, instances: list[Instance]) -> bool:
        """Add instances to pool. Return True if at least one instance was actually added (pool modified)."""
        # This is a private method that should only be called within locked contexts
        modified = False
        instances_tmp = []
        for instance in instances:
            if instance.id in self._unavailable_pool:
                logger.warning(
                    "Instance ID %d (role: %s, job_name: %s) already exists in unavailable pool, "
                    "cannot add instance again",
                    instance.id,
                    instance.role,
                    instance.job_name,
                )
                continue
            if not self._add_instance_to_available_pool(instance):
                logger.warning(
                    "Failed to add instance ID %d (role: %s, job_name: %s) to available pool, while adding instance",
                    instance.id,
                    instance.role,
                    instance.job_name,
                )
                continue

            modified = True
            # Initialize workload info
            instance.gathered_workload = Workload()
            for pod_eps in (instance.endpoints or {}).values():
                for ep in (pod_eps or {}).values():
                    ep.workload = Workload()

            num_endpoints = sum(len(pod_eps) for pod_eps in (instance.endpoints or {}).values())
            logger.info(
                "Added instance ID %d (role: %s, job_name: %s) with %d endpoints to available pool successfully",
                instance.id,
                instance.role,
                instance.job_name,
                num_endpoints,
            )

            instances_tmp.append(instance)

        self._register_kv_instance(instances_tmp)
        return modified

    def _delete_instances(self, instances: list[Instance]) -> bool:
        """Delete instances from pool. Return True if at least one instance was actually deleted (pool modified)."""
        # This is a private method that should only be called within locked contexts
        modified = False
        for instance in instances:
            if instance.id in self._unavailable_pool:
                del self._unavailable_pool[instance.id]
                modified = True
                logger.info(
                    "Deleted instance ID %d (role: %s, job_name: %s) from unavailable pool successfully",
                    instance.id,
                    instance.role,
                    instance.job_name,
                )
                continue

            if instance.id in self._paused_pool:
                del self._paused_pool[instance.id]
                modified = True
                logger.info(
                    "Deleted instance ID %d (role: %s, job_name: %s) from paused pool successfully",
                    instance.id,
                    instance.role,
                    instance.job_name,
                )
                continue

            if self._delete_instance_from_available_pool(instance.id):
                modified = True
            else:
                logger.warning(
                    "Instance ID %d (role: %s, job_name: %s) not found in instance pool, cannot delete instance",
                    instance.id,
                    instance.role,
                    instance.job_name,
                )
                continue

            logger.info(
                "Deleted instance ID %d (role: %s, job_name: %s) from available pool successfully",
                instance.id,
                instance.role,
                instance.job_name,
            )
        return modified

    def _compute_set_diff(self, instances: list[Instance]) -> tuple[list[Instance], list[Instance]]:
        """Compute to_add and to_remove for SET: (ids in new not in current, ids in current not in new).
        Must be called within _lock.
        """
        current_ids = (
            set(self._available_pool.keys()) | set(self._unavailable_pool.keys()) | set(self._paused_pool.keys())
        )
        new_ids = {inst.id for inst in instances}
        to_remove_ids = current_ids - new_ids
        to_add_ids = new_ids - current_ids
        to_remove = []
        for iid in to_remove_ids:
            inst = self._available_pool.get(iid) or self._unavailable_pool.get(iid)
            if inst is not None:
                to_remove.append(inst)
        to_add = [inst for inst in instances if inst.id in to_add_ids]
        return (to_add, to_remove)

    def _apply_set_diff(self, instances: list[Instance]) -> bool:
        """Apply SET as diff: delete removed, add new; return True if any change."""
        to_add, to_remove = self._compute_set_diff(instances)
        if not to_remove and not to_add:
            logger.debug("SET: no diff, instance set unchanged")
            return False
        if to_remove:
            logger.info("SET: removing %d instance(s), adding %d", len(to_remove), len(to_add))
            self._delete_instances(to_remove)
        if to_add:
            self._add_instances(to_add)
        return True

    def _pause_instances(self, instances: list[Instance]) -> bool:
        """Move instances from available pool to paused pool.

        Paused instances are excluded from get_available_instances(),
        continue serving existing connections, and can be resumed
        back to available pool via RESUME event.
        """
        modified = False
        for instance in instances:
            if instance.id in self._paused_pool:
                logger.warning("Instance ID %d already in paused pool, skipping", instance.id)
                continue
            if instance.id not in self._available_pool:
                logger.warning("Instance ID %d not in available pool, cannot pause", instance.id)
                continue
            if not self._delete_instance_from_available_pool(instance.id):
                continue
            self._paused_pool[instance.id] = instance
            modified = True
            logger.info(
                "Instance ID %d (role: %s, job_name: %s) moved to paused pool",
                instance.id,
                instance.role,
                instance.job_name,
            )
        return modified

    def _resume_instances(self, instances: list[Instance]) -> bool:
        """Move instances from paused pool back to available pool.

        Resumed instances resume normal scheduling for new requests.
        """
        modified = False
        for instance in instances:
            if instance.id not in self._paused_pool:
                logger.warning("Instance ID %d not in paused pool, cannot resume", instance.id)
                continue
            del self._paused_pool[instance.id]
            if not self._add_instance_to_available_pool(instance):
                logger.error("Failed to add instance ID %d back to available pool", instance.id)
                continue
            modified = True
            logger.info(
                "Instance ID %d (role: %s, job_name: %s) resumed to available pool",
                instance.id,
                instance.role,
                instance.job_name,
            )
        return modified

    def _add_instance_to_available_pool(self, instance: Instance) -> bool:
        # This is a private method that should only be called within locked contexts
        update_pool = self._available_role_pools.get(_role_to_pdrole(instance.role))
        if update_pool is None:
            logger.error(
                "Unknown role for instance ID %d (role: %s, job_name: %s), cannot add instance",
                instance.id,
                instance.role,
                instance.job_name,
            )
            return False
        if instance.id in update_pool:
            logger.warning(
                "Instance ID %d (role: %s, job_name: %s) already exists in available pool, cannot add instance again",
                instance.id,
                instance.role,
                instance.job_name,
            )
            return False

        # Do not create HTTP client here: Instance is passed across processes; client is not serializable.
        # Router gets client from HTTPClientPool in API Server process.
        update_pool[instance.id] = instance
        self._available_pool[instance.id] = instance

        # Build endpoint_id -> Endpoint index for update_instance_workload O(1) lookup
        endpoint_cache: dict[int, Endpoint] = {}
        for pod_endpoints in (instance.endpoints or {}).values():
            for ep in (pod_endpoints or {}).values():
                endpoint_cache[ep.id] = ep
        self._endpoint_id_cache[instance.id] = endpoint_cache

        logger.debug(
            "Instance ID %d (role: %s, job_name: %s) added to available pool successfully",
            instance.id,
            instance.role,
            instance.job_name,
        )
        return True

    def _delete_instance_from_available_pool(self, instance_id: int) -> bool:
        # This is a private method that should only be called within locked contexts
        update_pool = self._find_available_pool(instance_id)
        if update_pool is None:
            logger.warning("Instance ID %s not found in available instance pool yet, cannot delete", instance_id)
            return False

        self._release_instance_resource(update_pool[instance_id])
        del update_pool[instance_id]
        del self._available_pool[instance_id]
        self._workload_locks.pop(instance_id, None)
        self._endpoint_id_cache.pop(instance_id, None)
        logger.debug("Instance ID %s deleted from available pool successfully", instance_id)
        return True

    def _release_instance_resource(self, instance: Instance):
        # HTTP client is managed by HTTPClientPool, not on Endpoint; nothing to close here
        pass
