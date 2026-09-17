# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Coordinator serving view for instances under fault tolerance."""

from motor.common.resources import EventType, InsEventMsg, Instance, ReadOnlyInstance
from motor.controller.api_client.coordinator_api_client import CoordinatorApiClient
from motor.controller.fault_tolerance.dp_scale_down import FtPhase, get_ft_runtime_store


class ServingOverlay:
    """Project authoritative Controller topology into a routable Coordinator view."""

    _WITHDRAWN_PHASES = {
        FtPhase.DECIDING.value,
        FtPhase.WAITING_ENGINE_FAULT.value,
        FtPhase.SCALING_DOWN.value,
        FtPhase.POST_SCALE_CLEANUP.value,
        FtPhase.RECONFIGURING.value,
    }

    @staticmethod
    def _copy(instance: Instance | ReadOnlyInstance) -> Instance:
        if isinstance(instance, ReadOnlyInstance):
            return instance.to_instance()
        return ReadOnlyInstance(instance).to_instance()

    @staticmethod
    def _without_dead_ranks(snapshot: Instance, dead_ranks: set[int]) -> Instance | None:
        snapshot.endpoints = {
            pod_ip: {
                endpoint_key: endpoint
                for endpoint_key, endpoint in pod_endpoints.items()
                if endpoint.id not in dead_ranks
            }
            for pod_ip, pod_endpoints in snapshot.endpoints.items()
        }
        snapshot.endpoints = {
            pod_ip: pod_endpoints for pod_ip, pod_endpoints in snapshot.endpoints.items() if pod_endpoints
        }
        return snapshot if snapshot.endpoints else None

    @classmethod
    def project(cls, instance: Instance | ReadOnlyInstance) -> Instance | None:
        """Return a detached serving copy, or None while traffic must stay withdrawn."""
        snapshot = cls._copy(instance)
        runtime = get_ft_runtime_store().get(snapshot.id)
        if runtime is None or runtime["phase"] in {
            FtPhase.DISABLED.value,
            FtPhase.NORMAL.value,
        }:
            return snapshot
        if runtime["phase"] in cls._WITHDRAWN_PHASES:
            return None

        dead_ranks = set(runtime["dead_committed"])
        if runtime["phase"] == FtPhase.SCALED_DOWN_RUNNING.value:
            return cls._without_dead_ranks(snapshot, dead_ranks)
        return snapshot

    @classmethod
    def project_many(cls, instances: list[Instance | ReadOnlyInstance]) -> list[Instance]:
        projected = [cls.project(instance) for instance in instances]
        return [instance for instance in projected if instance is not None]

    @staticmethod
    def fingerprint(
        instances: list[Instance],
    ) -> tuple[tuple[int, tuple[int, ...]], ...]:
        return tuple(
            sorted(
                (
                    instance.id,
                    tuple(sorted(endpoint.id for endpoint in instance.get_all_endpoints(True))),
                )
                for instance in instances
            )
        )

    @staticmethod
    def mark_published(instances: list[Instance]) -> None:
        """Record successful Coordinator convergence for scaled-down instances."""
        get_ft_runtime_store().mark_serving_published({instance.id for instance in instances})

    @classmethod
    def withdraw(cls, instance: Instance | ReadOnlyInstance) -> bool:
        """Remove the complete authoritative instance before engine scale-down starts."""
        snapshot = cls._copy(instance)
        runtime = get_ft_runtime_store().get(snapshot.id)
        if runtime is not None:
            snapshot = cls._without_dead_ranks(snapshot, set(runtime["dead_committed"]))
        if snapshot is None:
            return False
        event = InsEventMsg(event=EventType.DEL, instances=[snapshot])
        return CoordinatorApiClient.send_instance_refresh(event)

    @classmethod
    def publish(cls, instance: Instance | ReadOnlyInstance) -> bool:
        """Publish the current routable projection after scale-down commits."""
        projected = cls.project(instance)
        if projected is None:
            return False
        event = InsEventMsg(event=EventType.ADD, instances=[projected])
        return CoordinatorApiClient.send_instance_refresh(event)
