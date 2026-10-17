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

import threading

from motor.common.resources import EventType, InsEventMsg, InsStatus, Instance, ReadOnlyInstance
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
    # Instances withdrawn by user recovery while their FT phase is still NORMAL.
    # Without this, the full SET the Controller pushes when the emptied
    # Coordinator turns not-ready re-adds the instance a second later.
    _held: set[int] = set()
    # One SET at a time: construct roster → POST → commit NORMAL. Two overlapping
    # publish_restored() calls would otherwise SET a stale projection of the
    # instance that just restored.
    _publish_lock = threading.Lock()

    @classmethod
    def hold(cls, instance_id: int) -> None:
        cls._held.add(instance_id)

    @classmethod
    def release(cls, instance_id: int) -> None:
        cls._held.discard(instance_id)

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
            return None if snapshot.id in cls._held else snapshot
        if runtime["phase"] in cls._WITHDRAWN_PHASES:
            return None

        dead_ranks = set(runtime["dead_committed"])
        if runtime["phase"] == FtPhase.SCALED_DOWN_RUNNING.value:
            return cls._without_dead_ranks(snapshot, dead_ranks)
        if runtime["phase"] == FtPhase.WARMING_UP.value:
            # Periodic SET, heartbeat-loss SET, and not-ready SET all call
            # project(). Keep them on the survivor ranks recorded when warmup
            # started. Heartbeat may already mark a joiner NORMAL before FT is
            # healthy, so hide by rank id. publish_restored() is the only full
            # cut-over, and it does not route this instance through project().
            serving_ranks = {int(rank) for rank in runtime.get("serving_dp_ranks") or []}
            if serving_ranks:
                hidden = {endpoint.id for endpoint in snapshot.get_all_endpoints(True)} - serving_ranks
                return cls._without_dead_ranks(snapshot, hidden)
            return snapshot
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

    @classmethod
    def _restored_roster(cls, restored: Instance) -> list[Instance]:
        """Full serving roster for a SET refresh, with ``restored`` overriding its id.

        SET is a diff against the complete roster: any registered instance left
        out of the list is deleted, so every live instance must be enumerated.
        Other instances still go through ``project()`` (WARMING_UP stays on
        survivors) — a second scale-up waits on the cluster lock, so it is
        not cut over here.
        """
        from motor.controller.core.instance_manager import InstanceManager

        roster = {restored.id: restored}
        for candidate in InstanceManager().get_instances({InsStatus.ACTIVE}):
            if candidate.id == restored.id:
                continue
            projected = cls.project(candidate)
            if projected is not None:
                roster[projected.id] = projected
        return list(roster.values())

    @classmethod
    def publish_lock(cls) -> threading.Lock:
        """Shared SET lock for scale-up restore and EventPusher full refresh."""
        return cls._publish_lock

    @classmethod
    def publish_restored(cls, instance: Instance | ReadOnlyInstance) -> bool:
        """Second Coordinator sync: publish full topology after DP scale-up warmup.

        Callers hold the cluster scale-up lock so only one restore round runs.
        This method still takes ``_publish_lock`` so a periodic SET cannot
        interleave. After the Coordinator accepts the SET, every restored pod
        of this instance is on the roster and the phase returns to NORMAL.
        """
        store = get_ft_runtime_store()
        snapshot = cls._copy(instance)
        runtime = store.get(snapshot.id)
        if runtime is None or runtime["phase"] != FtPhase.WARMING_UP.value:
            return False
        with cls._publish_lock:
            runtime = store.get(snapshot.id)
            if runtime is None or runtime["phase"] != FtPhase.WARMING_UP.value:
                return False
            # Scale-up grows the endpoint set, which the Coordinator rejects on ADD
            # as an instance-id conflict. SET carries the whole roster instead.
            event = InsEventMsg(event=EventType.SET, instances=cls._restored_roster(snapshot))
            try:
                published = CoordinatorApiClient.send_instance_refresh(event)
            except Exception:
                published = False
            if not published:
                store.transition(
                    snapshot.id,
                    last_error="ServingOverlay restored publication pending",
                )
                return False
            store.transition(
                snapshot.id,
                phase=FtPhase.NORMAL,
                dead_committed=[],
                pending_removed_ranks=[],
                serving_dp_ranks=[],
                serving_published=True,
                last_error=None,
            )
            return True


def complete_dp_scale_up(instance: Instance | ReadOnlyInstance) -> bool:
    """Entry point for the warmup path to perform the second Coordinator sync."""
    return ServingOverlay.publish_restored(instance)
