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
from collections.abc import Callable

from motor.common.resources import EventType, InsEventMsg, InsStatus, Instance, ReadOnlyInstance
from motor.controller.api_client.coordinator_api_client import CoordinatorApiClient
from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase, get_ft_runtime_store


class ServingOverlay:
    """Project authoritative Controller topology into a routable Coordinator view."""

    _WITHDRAWN_PHASES = {
        FtPhase.DECIDING.value,
        FtPhase.RELAUNCHING.value,
        FtPhase.WAITING_ENGINE_FAULT.value,
        FtPhase.SCALING_DOWN.value,
        FtPhase.POST_SCALE_CLEANUP.value,
        FtPhase.RECONFIGURING.value,
    }
    _PAUSED_PHASES = {
        FtPhase.WAITING_ENGINE_FAULT.value,
        FtPhase.FAST_RECOVERING.value,
        FtPhase.SCALING_DOWN.value,
        FtPhase.POST_SCALE_CLEANUP.value,
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
        if runtime is not None:
            snapshot.recovery_paused = snapshot.recovery_paused or runtime.get("recovery_paused", False)
        if runtime is None or (
            not runtime.get("recovery_paused")
            and runtime["phase"]
            in {
                FtPhase.DISABLED.value,
                FtPhase.NORMAL.value,
            }
        ):
            return None if snapshot.id in cls._held else snapshot
        if runtime["phase"] in cls._PAUSED_PHASES or runtime.get("recovery_paused"):
            snapshot.recovery_paused = True
            snapshot.status = InsStatus.ACTIVE
            return cls._without_dead_ranks(snapshot, set(runtime["dead_committed"]))
        if runtime["phase"] in cls._WITHDRAWN_PHASES:
            if runtime["phase"] == FtPhase.WAITING_ENGINE_FAULT.value and runtime["preserve_recovery_streams"]:
                snapshot.recovery_paused = True
                return cls._without_dead_ranks(snapshot, set(runtime["dead_committed"]))
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
    ) -> tuple:
        return tuple(
            sorted(
                (
                    instance.id,
                    instance.recovery_paused,
                    tuple(
                        sorted(
                            (endpoint.id, endpoint.ip, endpoint.business_port)
                            for endpoint in instance.get_all_endpoints(True)
                        )
                    ),
                )
                for instance in instances
            )
        )

    @staticmethod
    def publication_owners() -> dict[int, str]:
        """Capture ownership before projecting a view that will be sent asynchronously."""
        return {runtime["instance_id"]: runtime["recovery_id"] for runtime in get_ft_runtime_store().list()}

    @classmethod
    def mark_published(cls, instances: list[Instance], expected_owners: dict[int, str] | None = None) -> None:
        """Only acknowledge the current owner and topology, never an old projection."""
        from motor.controller.core.instance_manager import InstanceManager

        matched = set()
        for instance in instances:
            current = InstanceManager().get_instance(instance.id)
            projected = cls.project(current) if current is not None else None
            if projected is None or cls.fingerprint([projected]) != cls.fingerprint([instance]):
                continue
            runtime = get_ft_runtime_store().get(instance.id) or {}
            if expected_owners is not None and expected_owners.get(instance.id) != runtime.get("recovery_id"):
                continue
            if (
                instance.recovery_paused
                and runtime.get("stage") == "publication_pending"
                and runtime.get("phase") in {FtPhase.NORMAL.value, FtPhase.SCALED_DOWN_RUNNING.value}
            ):
                cls.publish(current)
            elif not instance.recovery_paused:
                matched.add(instance.id)
        get_ft_runtime_store().mark_serving_published(matched, expected_owners)

    @classmethod
    def pause_for_recovery(cls, instance: Instance | ReadOnlyInstance) -> bool:
        """Publish scheduling pause without changing the caller-owned recovery phase."""
        store = get_ft_runtime_store()
        runtime = store.get(instance.id) or {}
        owner = runtime.get("recovery_id")
        store.transition(instance.id, expected_recovery_id=owner or None, recovery_paused=True, serving_published=False)
        with cls._publish_lock:
            projected = cls.project(instance)
            if projected is None:
                return False
            return CoordinatorApiClient.send_instance_refresh(
                InsEventMsg(event=EventType.SET, instances=cls._restored_roster(projected))
            )

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
    def publish(
        cls,
        instance: Instance | ReadOnlyInstance,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        """Confirm the new topology while paused, then release only this recovery pause."""
        store = get_ft_runtime_store()
        with cls._publish_lock:
            source = instance.get_instance() if isinstance(instance, ReadOnlyInstance) else instance
            pause_token = source.recovery_pause_token
            runtime = store.get(instance.id) or {}
            owner = runtime.get("recovery_id")
            projected = cls.project(instance)
            if projected is None:
                return False
            expected = cls.fingerprint([projected])
            phase = runtime.get("phase")

            def current() -> bool:
                latest = store.get(instance.id) or {}
                candidate = cls.project(instance)
                return (
                    not (cancelled and cancelled())
                    and source.recovery_pause_token is pause_token
                    and latest.get("recovery_id") == owner
                    and latest.get("phase") == phase
                    and candidate is not None
                    and cls.fingerprint([candidate]) == expected
                )

            if not current():
                return False
            if runtime.get("recovery_paused"):
                paused = cls._copy(projected)
                paused.recovery_paused = True
                if (
                    not CoordinatorApiClient.send_instance_refresh(
                        InsEventMsg(event=EventType.SET, instances=cls._restored_roster(paused))
                    )
                    or not current()
                ):
                    return False
            projected.recovery_paused = False
            if (
                not CoordinatorApiClient.send_instance_refresh(
                    InsEventMsg(event=EventType.SET, instances=cls._restored_roster(projected))
                )
                or not current()
            ):
                return False

            def confirm() -> bool:
                if runtime:
                    return instance.id in store.mark_serving_published(
                        {instance.id}, {instance.id: owner}, release_pause=True
                    )
                return True

            return source.finish_recovery_pause(pause_token, confirm)

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
        for candidate in InstanceManager().get_instances({InsStatus.ACTIVE, InsStatus.INACTIVE}):
            if candidate.id == restored.id:
                continue
            projected = cls.project(candidate)
            if candidate.status == InsStatus.INACTIVE and (projected is None or not projected.recovery_paused):
                continue
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
