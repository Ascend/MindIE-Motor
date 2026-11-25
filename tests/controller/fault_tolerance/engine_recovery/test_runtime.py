# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for DP scale-down runtime state and recovery priority."""

from unittest.mock import Mock

import pytest

from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase, FtRuntime, FtRuntimeStore
from motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.topology import FaultNormalizer, TopologyResolver
from motor.common.resources.endpoint import DeviceInfo, Endpoint
from motor.common.resources.instance import Instance, ParallelConfig


def _endpoint(rank: int, pod_ip: str, device_ids: tuple[int, ...] = ()) -> Endpoint:
    return Endpoint(
        id=rank,
        ip=pod_ip,
        business_port=str(8000 + rank),
        device_infos=[DeviceInfo(device_id=str(device_id), rank_id=str(device_id)) for device_id in device_ids],
    )


def _instance(dp_size: int, endpoints: list[Endpoint], role: str = "decode") -> Instance:
    instance = Instance(
        job_name=f"{role}-0",
        model_name="model",
        id=1,
        role=role,
        parallel_config=ParallelConfig(dp_size=dp_size),
    )
    endpoints_by_pod: dict[str, dict[int, Endpoint]] = {}
    for endpoint in endpoints:
        endpoints_by_pod.setdefault(endpoint.ip, {})[endpoint.id] = endpoint
    for pod_ip, pod_endpoints in endpoints_by_pod.items():
        instance.add_endpoints(pod_ip, pod_endpoints)
    return instance


def test_runtime_store_returns_defensive_copies():
    store = FtRuntimeStore()
    runtime = FtRuntime(instance_id=7, phase=FtPhase.SCALING_DOWN, pending_removed_ranks=[2])
    store.put(runtime)

    fetched = store.get(7)
    fetched["pending_removed_ranks"].append(3)
    listed = store.list()
    listed[0]["pending_removed_ranks"].append(4)

    assert store.get(7)["pending_removed_ranks"] == [2]


def test_runtime_transition_updates_only_selected_fields():
    store = FtRuntimeStore()
    store.put(FtRuntime(instance_id=7, dead_committed=[1], serving_published=True))

    runtime = store.transition(
        7,
        phase=FtPhase.SCALING_DOWN,
        serving_published=False,
    )

    assert runtime.dead_committed == [1]
    assert store.get(7)["phase"] == FtPhase.SCALING_DOWN.value
    assert store.get(7)["serving_published"] is False


@pytest.mark.parametrize("owner", ["current", "new", None])
def test_owner_cleanup_does_not_recreate_or_modify_obsolete_runtime(owner):
    store = FtRuntimeStore()
    if owner is not None:
        store.transition(7, recovery_id=owner)
    updated = store.transition_if_owned(7, expected_recovery_id="current", last_error="failure")
    store.settle_recovery(7, "current", failed=True)
    runtime = store.get(7)
    if owner == "current":
        assert updated is not None
        assert (runtime["stage"], runtime["last_error"]) == ("failed", "failure")
    elif owner is None:
        assert updated is None
        assert runtime is None
    else:
        assert updated is None
        assert (runtime["stage"], runtime["last_error"]) == ("idle", None)


@pytest.mark.parametrize(
    "dispatched,published,stage",
    [(False, True, "completed"), (True, False, "dispatched"), (False, False, "publication_pending")],
)
def test_settlement_uses_current_publication_state(dispatched, published, stage):
    store = FtRuntimeStore()
    store.transition(7, recovery_id="current", reconfiguration_dispatched=dispatched, serving_published=published)
    store.settle_recovery(7, "current", failed=False)
    assert store.get(7)["stage"] == stage


def test_runtime_persistence_contains_only_durable_fields_and_skips_observations():
    store = FtRuntimeStore()
    persist = Mock(return_value=True)
    store.set_persist_callback(persist)

    store.transition(7, stage="polling")
    persist.assert_not_called()
    store.transition(
        7,
        phase=FtPhase.SCALING_DOWN,
        original_dp_ranks=[0, 1],
        fallback_strategy="EngineRelaunchStrategy",
    )

    persist.assert_called_once()
    assert store.persistent_data()["7"] == {
        "instance_id": 7,
        "phase": FtPhase.SCALING_DOWN.value,
        "original_dp_ranks": [0, 1],
        "dead_committed": [],
        "fallback_strategy": "EngineRelaunchStrategy",
        "reconfiguration_dispatched": False,
    }
    saved = store.persistent_data()
    assert store.remove(7) is True
    assert store.get(7) is None
    assert store.persistent_data() == {}
    for create in (
        lambda: store.transition(7, phase=FtPhase.WAITING_ENGINE_FAULT),
        lambda: store.get_or_create(7),
        lambda: store.put(FtRuntime(instance_id=7)),
    ):
        with pytest.raises(RuntimeError, match="removed instance"):
            create()
    assert persist.call_count == 2
    assert store.restore_persistent_data(saved) == {}
    assert store.list() == []
    store.clear()
    assert store.restore_persistent_data(saved) == {7: "EngineRelaunchStrategy"}


def test_runtime_restore_resumes_committed_view_and_fails_interrupted_attempt():
    store = FtRuntimeStore()
    interrupted = store.restore_persistent_data(
        {
            "7": {
                "phase": FtPhase.POST_SCALE_CLEANUP.value,
                "generation": 1,
                "original_dp_ranks": [0, 1],
                "dead_committed": [1],
            },
            "8": {
                "phase": FtPhase.SCALING_DOWN.value,
                "fallback_strategy": "ScaleP2DStrategy",
            },
            "9": {
                "phase": FtPhase.RECONFIGURING.value,
                "fallback_strategy": "InstanceReconfigurationStrategy",
                "reconfiguration_dispatched": True,
            },
            "10": {
                "phase": FtPhase.WARMING_UP.value,
                "original_dp_ranks": [0, 1],
                "dead_committed": [],
            },
            "bad": {"phase": "not-a-phase"},
        }
    )

    assert store.get(7)["phase"] == FtPhase.SCALED_DOWN_RUNNING.value
    assert "generation" not in store.get(7)
    assert store.get(7)["serving_published"] is False
    assert store.get(7)["stage"] == "publication_pending"
    store.mark_serving_published({7})
    assert store.get(7)["stage"] == "completed"
    assert store.get(8)["phase"] == FtPhase.RECONFIGURING.value
    assert store.get(8)["pending_removed_ranks"] == []
    assert store.get(9)["phase"] == FtPhase.RECONFIGURING.value
    assert store.get(9)["reconfiguration_dispatched"] is True
    assert store.get(10)["phase"] == FtPhase.RECONFIGURING.value
    assert store.get(10)["dead_committed"] == []
    assert store.get(10)["last_error"] == "controller restarted during DP scale-up; original fallback required"
    assert store.get("bad") is None
    assert interrupted == {8: "ScaleP2DStrategy", 10: "InstanceReconfigurationStrategy"}


def test_runtime_status_reports_effective_alive_ranks_and_serving_state():
    runtime = FtRuntime(
        instance_id=7,
        phase=FtPhase.SCALING_DOWN,
        original_dp_ranks=[0, 1, 2],
        dead_committed=[1],
        pending_removed_ranks=[2],
    )

    status = runtime.to_dict()

    assert status["alive_dp_ranks"] == [0]
    assert status["can_serve"] is False


def test_mark_serving_published_does_not_overwrite_newer_scaling_phase():
    store = FtRuntimeStore()
    store.put(
        FtRuntime(
            instance_id=7,
            phase=FtPhase.SCALING_DOWN,
            serving_published=False,
            last_error="ServingOverlay publication pending",
        )
    )

    store.mark_serving_published({7})

    status = store.get(7)
    assert status["phase"] == FtPhase.SCALING_DOWN.value
    assert status["serving_published"] is False
    assert status["last_error"] == "ServingOverlay publication pending"


def test_mark_serving_published_completes_normal_reconfiguration_republication():
    store = FtRuntimeStore()
    store.put(
        FtRuntime(
            instance_id=7,
            phase=FtPhase.NORMAL,
            serving_published=False,
        )
    )

    store.mark_serving_published({7})

    status = store.get(7)
    assert status["serving_published"] is True
    assert status["can_serve"] is True


def test_engine_relaunch_restores_full_dp_view():
    store = FtRuntimeStore()
    store.put(
        FtRuntime(
            instance_id=7,
            phase=FtPhase.RECONFIGURING,
            dead_committed=[1],
            pending_removed_ranks=[2],
            serving_published=False,
            last_error="scale-down failed",
        )
    )

    store.prepare_after_engine_relaunch(7)

    status = store.get(7)
    assert status["phase"] == FtPhase.NORMAL.value
    assert status["dead_committed"] == []
    assert status["pending_removed_ranks"] == []
    assert status["serving_published"] is False
    assert status["last_error"] is None


def test_fault_clear_restores_waiting_runtime_without_dropping_committed_ranks():
    store = FtRuntimeStore()
    store.put(
        FtRuntime(
            instance_id=7,
            phase=FtPhase.WAITING_ENGINE_FAULT,
            dead_committed=[1],
            pending_removed_ranks=[2],
            serving_published=True,
        )
    )

    restored = store.resume_after_fault_clear(7)

    assert restored is not None
    assert restored.phase == FtPhase.SCALED_DOWN_RUNNING
    assert restored.dead_committed == [1]
    assert restored.pending_removed_ranks == []
    assert restored.serving_published is False


def test_recyclable_pods_requires_every_local_dp_rank_to_be_committed():
    instance = _instance(
        3,
        [_endpoint(0, "192.0.2.1"), _endpoint(1, "192.0.2.1"), _endpoint(2, "192.0.2.2")],
    )

    assert TopologyResolver.recyclable_pods(instance, {0}) == []
    assert TopologyResolver.recyclable_pods(instance, {0, 1}) == ["192.0.2.1"]


def test_hardware_faults_map_device_to_motor_dp_endpoint_without_ep_layout():
    instance = _instance(2, [_endpoint(0, "192.0.2.1", (0,)), _endpoint(1, "192.0.2.1", (1,))])

    fault = type("HardwareFault", (), {"npu_name": "Ascend910-1"})()
    candidates, mapped_keys = FaultNormalizer.hardware_dp_ranks(
        instance,
        [("node-a:1000:Ascend910-1", "192.0.2.1", fault)],
    )

    assert candidates == {1}
    assert mapped_keys == {"node-a:1000:Ascend910-1"}


def test_comma_joined_hardware_fault_maps_every_named_device():
    instance = _instance(
        3,
        [
            _endpoint(0, "192.0.2.1", (0,)),
            _endpoint(1, "192.0.2.1", (1,)),
            _endpoint(2, "192.0.2.1", (2,)),
        ],
    )
    fault = type("HardwareFault", (), {"npu_name": "Ascend910-0, Ascend910-2"})()

    candidates, mapped_keys = FaultNormalizer.hardware_dp_ranks(
        instance,
        [("multi-card", "192.0.2.1", fault)],
    )

    assert candidates == {0, 2}
    assert mapped_keys == {"multi-card"}


def test_unrecognized_hardware_device_is_not_guessed_from_trailing_number():
    instance = _instance(1, [_endpoint(0, "192.0.2.1", (0,))])

    switch_fault = type("HardwareFault", (), {"npu_name": "switch0"})()
    candidates, mapped_keys = FaultNormalizer.hardware_dp_ranks(
        instance,
        [("node-a:2000:switch0", "192.0.2.1", switch_fault)],
    )

    assert candidates == set()
    assert mapped_keys == set()


def test_device_fault_only_affects_instance_that_owns_device():
    prefill = _instance(1, [_endpoint(0, "192.0.2.10", (0, 1))], role="prefill")
    fault = type("HardwareFault", (), {"npu_name": "Ascend910-15"})()

    assert not FaultNormalizer.hardware_fault_affects_instance(prefill, "192.0.2.10", fault)


def test_node_fault_affects_every_instance_on_node():
    instance = _instance(1, [_endpoint(0, "192.0.2.10")], role="prefill")
    fault = type("HardwareFault", (), {"npu_name": ""})()

    assert FaultNormalizer.hardware_fault_affects_instance(instance, "192.0.2.10", fault)


def test_stale_owner_cannot_commit_or_acknowledge_new_recovery():
    store = FtRuntimeStore()
    store.transition(1, recovery_id="old", phase=FtPhase.SCALED_DOWN_RUNNING, serving_published=False)
    store.transition(1, recovery_id="new", dead_committed=[1])
    with pytest.raises(RuntimeError, match="stale recovery owner"):
        store.transition(1, expected_recovery_id="old", dead_committed=[2])
    store.mark_serving_published({1}, {1: "old"})
    assert store.get(1)["dead_committed"] == [1]
    assert store.get(1)["serving_published"] is False
    store.mark_serving_published({1}, {1: "new"})
    assert store.get(1)["serving_published"] is True


def test_stale_owner_does_not_recreate_removed_runtime_or_resume_new_owner():
    store = FtRuntimeStore()
    with pytest.raises(RuntimeError, match="stale recovery owner"):
        store.transition(1, expected_recovery_id="removed", dead_committed=[1])
    assert store.get(1) is None
    store.transition(1, recovery_id="new", phase=FtPhase.DECIDING)
    store.prepare_after_engine_relaunch(1, "old")
    assert store.get(1)["phase"] == FtPhase.DECIDING
    with pytest.raises(RuntimeError, match="stale recovery owner"):
        store.mark_instance_reconfiguration_dispatched(1, "old")
    assert store.get(1)["reconfiguration_dispatched"] is False


def test_relaunch_phase_preserves_protection_and_restart_identity():
    from motor.controller.fault_tolerance.engine_recovery.runtime import get_ft_runtime_store, partial_loss_policy

    phase = FtPhase("RELAUNCHING")
    store = get_ft_runtime_store()
    store.set_persist_callback(None)
    store.clear()
    try:
        store.transition(7, phase=phase, action="RELAUNCH", serving_published=False)
        assert partial_loss_policy(7).protect_transaction
        saved = store.persistent_data()
        assert saved["7"]["action"] == "RELAUNCH"
        restored = FtRuntimeStore()
        assert restored.restore_persistent_data(saved) == {7: "InstanceReconfigurationStrategy"}
        assert "engine relaunch" in restored.get(7)["last_error"]
        store.prepare_after_engine_relaunch(7)
        assert store.get(7)["phase"] == FtPhase.NORMAL
    finally:
        store.clear()
