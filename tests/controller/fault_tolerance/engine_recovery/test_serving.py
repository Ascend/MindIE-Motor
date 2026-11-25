# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for the immutable Coordinator serving overlay."""

import threading
from unittest.mock import patch

import pytest

from motor.common.resources import EventType, Instance, ReadOnlyInstance
from motor.common.resources.endpoint import Endpoint, EndpointStatus
from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase, FtRuntime, get_ft_runtime_store
from motor.controller.fault_tolerance.engine_recovery.serving import ServingOverlay


@pytest.fixture(autouse=True)
def clear_runtime_store():
    get_ft_runtime_store().clear()
    yield
    get_ft_runtime_store().clear()


def _instance() -> Instance:
    instance = Instance(job_name="decode-0", model_name="model", id=7, role="decode")
    instance.add_endpoints(
        "192.0.2.1",
        {0: Endpoint(id=0, ip="192.0.2.1", business_port="8000")},
    )
    instance.add_endpoints(
        "192.0.2.2",
        {1: Endpoint(id=1, ip="192.0.2.2", business_port="8000")},
    )
    return instance


@patch(
    "motor.controller.fault_tolerance.engine_recovery.serving.CoordinatorApiClient.send_instance_refresh",
    return_value=True,
)
def test_recovery_pause_survives_set_and_resumes_without_mutating_topology(mock_refresh):
    instance = _instance()
    original = ServingOverlay.fingerprint([instance])
    assert ServingOverlay.pause_for_recovery(ReadOnlyInstance(instance))
    event = mock_refresh.call_args.args[0]
    assert event.event == EventType.SET
    assert event.instances[0].recovery_paused
    projected = ServingOverlay.project_many([ReadOnlyInstance(instance)])
    assert projected[0].recovery_paused
    assert ServingOverlay.fingerprint(projected) != original
    assert not instance.recovery_paused
    assert ReadOnlyInstance(projected[0]).to_instance().recovery_paused
    get_ft_runtime_store().resume_after_fault_clear(instance.id)
    assert ServingOverlay.publish(instance)
    assert not ServingOverlay.project(instance).recovery_paused
    # A later abnormal heartbeat must pause even after the previous task completed.
    assert get_ft_runtime_store().get(instance.id)["stage"] == "completed"
    instance.recovery_paused = True
    assert ServingOverlay.project(instance).recovery_paused
    instance.recovery_paused = False
    assert not ServingOverlay.project(instance).recovery_paused


@pytest.mark.parametrize(
    "phase,preserve_streams",
    [(FtPhase.RELAUNCHING, False), (FtPhase.RELAUNCHING, True), (FtPhase.RECONFIGURING, True)],
)
def test_whole_instance_recovery_never_keeps_streams(phase, preserve_streams):
    instance = _instance()
    get_ft_runtime_store().transition(instance.id, phase=phase, preserve_recovery_streams=preserve_streams)
    assert ServingOverlay.project(ReadOnlyInstance(instance)) is None


def test_scaled_down_view_filters_dead_rank_without_mutating_authoritative_topology():
    instance = _instance()
    get_ft_runtime_store().put(FtRuntime(instance_id=7, phase=FtPhase.SCALED_DOWN_RUNNING, dead_committed=[1]))

    projected = ServingOverlay.project(instance)

    assert [endpoint.id for endpoint in projected.get_all_endpoints(True)] == [0]
    assert sorted(endpoint.id for endpoint in instance.get_all_endpoints(True)) == [
        0,
        1,
    ]
    assert list(projected.endpoints) == ["192.0.2.1"]


@patch("motor.controller.fault_tolerance.engine_recovery.serving.CoordinatorApiClient.send_instance_refresh")
def test_withdraw_uses_full_del_and_publish_uses_filtered_set(mock_refresh):
    mock_refresh.return_value = True
    instance = _instance()

    assert ServingOverlay.withdraw(instance) is True
    get_ft_runtime_store().put(FtRuntime(instance_id=7, phase=FtPhase.SCALED_DOWN_RUNNING, dead_committed=[1]))
    assert ServingOverlay.publish(instance) is True

    withdraw_msg = mock_refresh.call_args_list[0].args[0]
    publish_msg = mock_refresh.call_args_list[1].args[0]
    assert withdraw_msg.event == EventType.DEL
    assert sorted(endpoint.id for endpoint in withdraw_msg.instances[0].get_all_endpoints(True)) == [0, 1]
    assert publish_msg.event == EventType.SET
    assert [endpoint.id for endpoint in publish_msg.instances[0].get_all_endpoints(True)] == [0]


@patch("motor.controller.fault_tolerance.engine_recovery.serving.CoordinatorApiClient.send_instance_refresh")
def test_consecutive_scale_down_withdraws_previous_published_view(mock_refresh):
    mock_refresh.return_value = True
    instance = _instance()
    get_ft_runtime_store().put(FtRuntime(instance_id=7, phase=FtPhase.SCALING_DOWN, dead_committed=[1]))

    assert ServingOverlay.withdraw(instance) is True

    withdraw_msg = mock_refresh.call_args.args[0]
    assert [endpoint.id for endpoint in withdraw_msg.instances[0].get_all_endpoints(True)] == [0]
    assert sorted(endpoint.id for endpoint in instance.get_all_endpoints(True)) == [
        0,
        1,
    ]


def _warming_instance(
    instance_id: int = 11,
    survivor_ip: str = "10.0.0.2",
    joiner_ip: str = "10.0.0.3",
) -> Instance:
    """Survivor pod keeps ranks 0,1. Joiner pod occupies 2,3 and is already NORMAL."""
    instance = Instance(job_name="decode-%d" % instance_id, model_name="model", id=instance_id, role="decode")
    instance.add_endpoints(
        survivor_ip,
        {
            0: Endpoint(id=0, ip=survivor_ip, business_port="8000", status=EndpointStatus.NORMAL),
            1: Endpoint(id=1, ip=survivor_ip, business_port="8002", status=EndpointStatus.NORMAL),
        },
    )
    instance.add_endpoints(
        joiner_ip,
        {
            2: Endpoint(id=2, ip=joiner_ip, business_port="18000", status=EndpointStatus.NORMAL),
            3: Endpoint(id=3, ip=joiner_ip, business_port="18002", status=EndpointStatus.NORMAL),
        },
    )
    return instance


def _put_warming(instance_id: int) -> None:
    get_ft_runtime_store().put(
        FtRuntime(
            instance_id=instance_id,
            phase=FtPhase.WARMING_UP,
            original_dp_ranks=[0, 1, 2, 3],
            serving_dp_ranks=[0, 1],
        )
    )


def test_warming_up_view_keeps_survivor_ranks_when_joiner_status_is_normal():
    instance = _warming_instance()
    get_ft_runtime_store().put(
        FtRuntime(
            instance_id=11,
            phase=FtPhase.WARMING_UP,
            original_dp_ranks=[0, 1, 2, 3],
            serving_dp_ranks=[0, 1],
        )
    )

    projected = ServingOverlay.project(instance)

    assert projected is not None
    assert sorted(endpoint.id for endpoint in projected.get_all_endpoints(True)) == [0, 1]
    assert list(projected.endpoints) == ["10.0.0.2"]
    assert sorted(endpoint.id for endpoint in instance.get_all_endpoints(True)) == [0, 1, 2, 3]
    assert {endpoint.status for endpoint in instance.get_all_endpoints(True)} == {EndpointStatus.NORMAL}


@patch("motor.controller.core.instance_manager.InstanceManager.get_instances", return_value=[])
@patch("motor.controller.fault_tolerance.engine_recovery.serving.CoordinatorApiClient.send_instance_refresh")
def test_publish_restored_sends_full_roster_after_warmup(mock_refresh, _instances):
    mock_refresh.return_value = True
    instance = _warming_instance()
    get_ft_runtime_store().put(
        FtRuntime(
            instance_id=11,
            phase=FtPhase.WARMING_UP,
            original_dp_ranks=[0, 1, 2, 3],
            serving_dp_ranks=[0, 1],
        )
    )

    assert ServingOverlay.publish_restored(instance) is True

    event = mock_refresh.call_args.args[0]
    assert event.event == EventType.SET
    published = event.instances[0]
    assert sorted(endpoint.id for endpoint in published.get_all_endpoints(True)) == [0, 1, 2, 3]
    assert sorted(published.endpoints) == ["10.0.0.2", "10.0.0.3"]
    runtime = get_ft_runtime_store().get(11)
    assert runtime["phase"] == FtPhase.NORMAL.value
    assert runtime["serving_dp_ranks"] == []
    assert runtime["serving_published"] is True


@patch("motor.controller.core.instance_manager.InstanceManager.get_instances")
@patch("motor.controller.fault_tolerance.engine_recovery.serving.CoordinatorApiClient.send_instance_refresh")
def test_publish_restored_keeps_other_warming_instance_on_survivors(mock_refresh, mock_get_instances):
    """A cut-over SET must not advertise another instance's joiners.

    Cluster scale-up is serialized, so a second instance should not be in
    WARMING_UP. If one still is, project() keeps it on survivor ranks until
    its own publish_restored().
    """
    mock_refresh.return_value = True
    ready = _warming_instance(11, "10.0.0.2", "10.0.0.3")
    other = _warming_instance(12, "10.0.1.2", "10.0.1.3")
    mock_get_instances.return_value = [ready, other]
    _put_warming(11)
    _put_warming(12)

    assert ServingOverlay.publish_restored(ready) is True

    event = mock_refresh.call_args.args[0]
    assert event.event == EventType.SET
    by_id = {instance.id: instance for instance in event.instances}
    assert sorted(endpoint.id for endpoint in by_id[11].get_all_endpoints(True)) == [0, 1, 2, 3]
    assert sorted(endpoint.id for endpoint in by_id[12].get_all_endpoints(True)) == [0, 1]
    assert get_ft_runtime_store().get(11)["phase"] == FtPhase.NORMAL.value
    assert get_ft_runtime_store().get(12)["phase"] == FtPhase.WARMING_UP.value


@patch("motor.controller.core.instance_manager.InstanceManager.get_instances", return_value=[])
@patch("motor.controller.fault_tolerance.engine_recovery.serving.CoordinatorApiClient.send_instance_refresh")
def test_publish_restored_waits_for_in_flight_set(mock_refresh, _instances):
    """A second SET cannot construct its roster until the first POST returns."""
    order = []
    first_entered = threading.Event()
    release_first = threading.Event()

    def send(_event):
        order.append("send")
        first_entered.set()
        assert release_first.wait(timeout=2.0)
        return True

    mock_refresh.side_effect = send
    first = _warming_instance(11, "10.0.0.2", "10.0.0.3")
    second = _warming_instance(12, "10.0.1.2", "10.0.1.3")
    _put_warming(11)
    _put_warming(12)

    def run_second():
        ServingOverlay.publish_restored(second)
        order.append("second_done")

    worker = threading.Thread(target=lambda: ServingOverlay.publish_restored(first))
    blocked = threading.Thread(target=run_second)
    worker.start()
    blocked_started = False
    try:
        assert first_entered.wait(timeout=2.0)
        blocked.start()
        blocked_started = True
        blocked.join(timeout=0.2)
        assert blocked.is_alive()
        assert order == ["send"]
    finally:
        release_first.set()
        worker.join(timeout=2.0)
        if blocked_started:
            blocked.join(timeout=2.0)
    assert order == ["send", "send", "second_done"]
    assert get_ft_runtime_store().get(11)["phase"] == FtPhase.NORMAL.value
    assert get_ft_runtime_store().get(12)["phase"] == FtPhase.NORMAL.value


@pytest.mark.parametrize("phase", [FtPhase.WAITING_ENGINE_FAULT, FtPhase.FAST_RECOVERING, FtPhase.SCALING_DOWN])
def test_recovery_phases_preserve_instance_and_pause_new_scheduling(phase):
    instance = _instance()
    get_ft_runtime_store().transition(7, phase=phase, dead_committed=[1])
    projected = ServingOverlay.project(instance)
    assert projected.recovery_paused
    assert [ep.id for ep in projected.get_all_endpoints(True)] == [0]
    assert not instance.recovery_paused


@pytest.mark.parametrize("read_only", [False, True])
@patch("motor.controller.core.instance_manager.InstanceManager.get_instances", return_value=[])
def test_topology_update_is_confirmed_while_paused_before_unpause(_instances, read_only):
    instance = _instance()
    instance.recovery_paused = True
    store = get_ft_runtime_store()
    store.transition(
        7,
        recovery_id="owner",
        phase=FtPhase.SCALED_DOWN_RUNNING,
        dead_committed=[1],
        recovery_paused=True,
        serving_published=False,
        last_error="ServingOverlay publication pending; engine cleanup failed",
    )
    sent = []
    with patch(
        "motor.controller.fault_tolerance.engine_recovery.serving.CoordinatorApiClient.send_instance_refresh",
        side_effect=lambda event: sent.append(event) or True,
    ):
        assert ServingOverlay.publish(ReadOnlyInstance(instance) if read_only else instance)
    assert [event.instances[0].recovery_paused for event in sent] == [True, False]
    assert all([ep.id for ep in event.instances[0].get_all_endpoints(True)] == [0] for event in sent)
    assert store.get(7)["serving_published"]
    assert not store.get(7)["recovery_paused"]
    assert store.get(7)["stage"] == "completed"
    assert store.get(7)["last_error"] == "engine cleanup failed"
    assert not instance.recovery_paused
    assert not ServingOverlay.project(instance).recovery_paused


@pytest.mark.parametrize("owner", ["", "owner"])
@patch("motor.controller.core.instance_manager.InstanceManager.get_instances", return_value=[])
def test_owner_change_at_publication_commit_preserves_new_recovery(_instances, owner):
    instance = _instance()
    instance.recovery_paused = True
    store = get_ft_runtime_store()
    store.transition(7, recovery_id=owner, phase=FtPhase.NORMAL, recovery_paused=True, serving_published=False)
    acknowledge = store.mark_serving_published
    new_runtime = {}

    def switch_owner(*args, **kwargs):
        store.transition(
            7,
            recovery_id="new-owner",
            stage="publication_pending",
            last_error="ServingOverlay publication pending; new recovery error",
        )
        new_runtime.update(store.get(7))
        return acknowledge(*args, **kwargs)

    with (
        patch.object(store, "mark_serving_published", side_effect=switch_owner),
        patch(
            "motor.controller.fault_tolerance.engine_recovery.serving.CoordinatorApiClient.send_instance_refresh",
            return_value=True,
        ),
    ):
        assert not ServingOverlay.publish(instance)
    assert store.get(7) == new_runtime
    assert instance.recovery_paused


@pytest.mark.parametrize("interrupt", ["topology-change", "cancelled", "http-failure", "new-pause", "late-pause"])
@patch("motor.controller.core.instance_manager.InstanceManager.get_instances", return_value=[])
def test_interrupted_publication_never_releases_recovery_pause(_instances, interrupt):
    instance = _instance()
    instance.recovery_paused = True
    store = get_ft_runtime_store()
    store.transition(
        7,
        recovery_id="owner",
        phase=FtPhase.SCALED_DOWN_RUNNING,
        recovery_paused=True,
        serving_published=interrupt == "cancelled",
    )
    cancelled = [False]

    def send(event):
        if interrupt == "http-failure":
            return False
        if interrupt == "topology-change":
            store.transition(7, dead_committed=[1])
        elif interrupt == "cancelled":
            cancelled[0] = True
        elif interrupt == "new-pause" and not event.instances[0].recovery_paused:
            instance.set_recovery_paused(True)
        return True

    finish = Instance.finish_recovery_pause

    def finish_after_pause(source, token, confirm):
        if interrupt == "late-pause":
            source.set_recovery_paused(True)
        return finish(source, token, confirm)

    with (
        patch(
            "motor.controller.fault_tolerance.engine_recovery.serving.CoordinatorApiClient.send_instance_refresh",
            side_effect=send,
        ) as refresh,
        patch.object(Instance, "finish_recovery_pause", new=finish_after_pause),
    ):
        assert not ServingOverlay.publish(instance, cancelled=lambda: cancelled[0])
    assert refresh.call_count == (2 if interrupt in {"new-pause", "late-pause"} else 1)
    assert instance.recovery_paused
    assert store.get(7)["recovery_paused"]
    if interrupt == "topology-change":
        assert not store.get(7)["serving_published"]


@patch("motor.controller.core.instance_manager.InstanceManager.get_instances", return_value=[])
def test_periodic_paused_ack_retries_pending_topology_release(_instances):
    instance = _instance()
    store = get_ft_runtime_store()
    store.transition(
        7,
        recovery_id="owner",
        phase=FtPhase.SCALED_DOWN_RUNNING,
        dead_committed=[1],
        recovery_paused=True,
        serving_published=False,
        stage="publication_pending",
    )
    pending = ServingOverlay.project(instance)
    with (
        patch("motor.controller.core.instance_manager.InstanceManager.get_instance", return_value=instance),
        patch(
            "motor.controller.fault_tolerance.engine_recovery.serving.CoordinatorApiClient.send_instance_refresh",
            return_value=True,
        ),
    ):
        ServingOverlay.mark_published([pending], {7: "owner"})
    assert not store.get(7)["recovery_paused"]
    assert store.get(7)["serving_published"]
