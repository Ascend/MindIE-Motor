# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for DP scale-down admission, execution, and NodeManager routing."""

from types import SimpleNamespace
import itertools
from unittest.mock import MagicMock, patch
import threading
import time

import pytest

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import FtCapabilitySnapshot, NodeManagerInfo, ParallelConfig
from motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.topology import FaultNormalizer
from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase, FtRuntime, get_ft_runtime_store
from motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.context import ScaleDownContext
from motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling import DpScaleDownStrategy
from motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy import _engine_http_url
from motor.controller.fault_tolerance.strategy.engine_ft.fast_recovery import EngineFastRecoveryStrategy


def _config(**overrides):
    values = {
        "request_timeout_sec": 1,
        "poll_interval_sec": 0.001,
        "execution_deadline_sec": 1,
    }
    values.update(overrides)
    return SimpleNamespace(
        enable_fault_tolerance=True,
        fault_collection_timeout_seconds=1,
        enable_dp_scale_down=True,
        dp_scale_down_config=SimpleNamespace(**values),
    )


def _endpoints(count=2):
    return [Endpoint(id=rank, ip="192.0.2.%d" % (rank + 1), business_port="8000") for rank in range(count)]


def _instance(endpoints):
    instance = MagicMock()
    instance.get_all_endpoints.return_value = tuple(endpoints)
    instance.parallel_config = ParallelConfig(dp_size=len(endpoints), ep_size=len(endpoints))
    instance.ft_capability = FtCapabilitySnapshot(
        enabled=True,
        scale_down_supported=True,
        external_lb=True,
    )
    instance.endpoints = {}
    for endpoint in endpoints:
        instance.endpoints.setdefault(endpoint.ip, {})[endpoint.id] = endpoint
    instance.get_node_managers.return_value = [
        NodeManagerInfo(pod_ip=ip, port="9000") for ip in sorted({endpoint.ip for endpoint in endpoints})
    ]
    return instance


def _execute(
    *,
    instance_id=1,
    endpoints=None,
    candidates=(1,),
    statuses=None,
    config=None,
    publish=True,
    pod_stop=True,
    initial_runtime=None,
    guard_error=None,
    apply_error=None,
    withdraw=True,
    context=None,
    context_provider=None,
    finalize_error=None,
):
    endpoints = endpoints or _endpoints()
    survivor_ids = [endpoint.id for endpoint in endpoints if endpoint.id not in candidates]
    statuses = statuses or [
        {rank: {"id": rank, "status": "unhealthy"} for rank in survivor_ids},
        {rank: {"id": rank, "status": "healthy"} for rank in survivor_ids},
    ]
    store = get_ft_runtime_store()
    store.clear()
    if initial_runtime is not None:
        store.put(initial_runtime)
    instance = _instance(endpoints)
    strategy = DpScaleDownStrategy()
    strategy.bind_config(SimpleNamespace(fault_tolerance_config=config or _config()))
    strategy.bind_context(context or ScaleDownContext(pending_removed_ranks=tuple(candidates)))
    strategy.context_provider = context_provider
    with (
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy._get_instance",
            return_value=instance,
        ),
        patch.object(
            DpScaleDownStrategy,
            "_query_via_node_managers",
            side_effect=statuses,
        ) as query,
        patch.object(DpScaleDownStrategy, "_apply_via_node_managers", side_effect=apply_error) as apply,
        patch.object(DpScaleDownStrategy, "_guard_node_managers", side_effect=guard_error) as guard,
        patch.object(DpScaleDownStrategy, "_finalize_node_managers", side_effect=finalize_error) as finalize,
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.ServingOverlay.pause_for_recovery",
            return_value=withdraw,
        ) as withdraw_call,
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.ServingOverlay.publish",
            return_value=publish,
        ),
        patch(
            "motor.controller.fault_tolerance.pod_lifecycle.NodeManagerApiClient.stop",
            return_value=pod_stop,
        ) as stop,
    ):
        strategy.execute(instance_id)
    return SimpleNamespace(
        strategy=strategy,
        runtime=store.get(instance_id),
        query=query,
        apply=apply,
        guard=guard,
        finalize=finalize,
        withdraw=withdraw_call,
        stop=stop,
        instance=instance,
    )


def test_strategy_requires_bound_config_and_context():
    with pytest.raises(RuntimeError, match="not bound"):
        DpScaleDownStrategy().execute(1)

    strategy = DpScaleDownStrategy()
    strategy.bind_config(SimpleNamespace(fault_tolerance_config=_config()))
    with pytest.raises(RuntimeError, match="fault context is not bound"):
        strategy.execute(1)


def test_transaction_reuses_one_node_manager_fanout_executor():
    result = _execute()

    executor = result.guard.call_args.args[-1]
    assert all(call.args[-1] is executor for call in result.query.call_args_list)
    assert result.apply.call_args.kwargs["executor"] is executor
    assert result.finalize.call_args.args[-1] is executor


@pytest.mark.parametrize(
    "faults,expected",
    [
        (
            [
                SimpleNamespace(
                    engine_id=0,
                    engine_status=2,
                    additional_info={"ft_state": "recovering", "mask": [True]},
                ),
                SimpleNamespace(engine_id=1, engine_status=1, additional_info={}),
                SimpleNamespace(engine_id=2, engine_status=2, additional_info={"ft_state": "failed"}),
            ],
            {1},
        ),
        ([SimpleNamespace(engine_id=1, engine_status=2, additional_info={"ft_state": "recovering"})], set()),
    ],
)
def test_fault_normalizer_uses_only_dead_engine_rank(faults, expected):
    assert FaultNormalizer.software_dp_ranks(faults) == expected


@pytest.mark.parametrize(
    "initial_status",
    [
        {"id": 0, "status": "healthy"},
        {"id": 0, "status": "unhealthy", "ft_state": "recovering"},
    ],
)
def test_non_ready_survivor_is_polled_before_single_apply(initial_status):
    result = _execute(
        statuses=[
            {0: initial_status},
            {0: {"id": 0, "status": "unhealthy"}},
            {0: {"id": 0, "status": "healthy"}},
        ]
    )

    assert result.query.call_count == 3
    result.apply.assert_called_once()
    assert result.runtime["phase"] == FtPhase.SCALED_DOWN_RUNNING.value


def test_healthy_survivor_wait_timeout_falls_back_before_apply():
    with patch(
        "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.time.monotonic",
        side_effect=itertools.chain([0, 0, 0, 2], itertools.repeat(2)),
    ):
        result = _execute(
            statuses=[{0: {"id": 0, "status": "healthy"}}],
            config=_config(execution_deadline_sec=1),
        )

    assert result.runtime["phase"] == FtPhase.WAITING_ENGINE_FAULT.value
    assert result.runtime["last_error"] == "surviving engine did not become apply-ready before deadline"
    result.guard.assert_called_once()
    assert result.finalize.call_args.args[3] is False
    result.apply.assert_not_called()


@pytest.mark.parametrize(
    "status",
    [
        {"id": 0, "status": "unhealthy", "ft_state": "failed"},
        {"id": 0, "status": "unknown"},
    ],
)
def test_ineligible_survivor_fails_before_apply(status):
    result = _execute(statuses=[{0: status}])

    assert result.runtime["phase"] == FtPhase.WAITING_ENGINE_FAULT.value
    result.apply.assert_not_called()


@pytest.mark.parametrize(
    ("late_dead_ranks", "expected_removed"),
    [({3}, [2, 3]), ({0, 1, 3}, None)],
)
@pytest.mark.parametrize("reported_by", ["query", "context"])
def test_late_dead_survivors_join_removal_set_or_fallback(late_dead_ranks, expected_removed, reported_by):
    endpoints = _endpoints(4)
    initial_survivors = {0, 1, 3}
    remaining_survivors = initial_survivors - late_dead_ranks
    statuses = [
        {
            rank: {
                "id": rank,
                "status": "dead" if rank in late_dead_ranks else "unhealthy",
            }
            for rank in initial_survivors
        },
        {rank: {"id": rank, "status": "healthy"} for rank in remaining_survivors},
    ]

    result = _execute(
        endpoints=endpoints,
        candidates=(2,),
        statuses=statuses,
        context_provider=(
            (lambda: ScaleDownContext(pending_removed_ranks=tuple({2} | late_dead_ranks)))
            if reported_by == "context"
            else None
        ),
    )

    if expected_removed is None:
        assert result.runtime["phase"] == FtPhase.WAITING_ENGINE_FAULT.value
        result.apply.assert_not_called()
        assert result.finalize.call_args.args[1] == {0, 1, 2, 3}
        assert result.finalize.call_args.args[3] is False
    else:
        assert result.apply.call_args.args[1]["removed_dp_ranks"] == expected_removed
        applied_groups = result.apply.call_args.args[0]
        assert {rank for group in applied_groups for rank in group.survivor_endpoint_ids} == remaining_survivors
        assert result.runtime["dead_committed"] == expected_removed


def test_success_uses_fault_candidates_not_observational_mask():
    result = _execute(
        statuses=[
            {0: {"id": 0, "status": "unhealthy", "mask": [False, True]}},
            {0: {"id": 0, "status": "healthy", "mask": [False, True]}},
        ]
    )
    assert result.apply.call_args.args[1] == {
        "removed_dp_ranks": [1],
        "dp_master_ip": "192.0.2.1",
        "dp_master_rank": 0,
    }
    assert result.runtime["dead_committed"] == [1]
    result.guard.assert_called_once()
    result.finalize.assert_called_once()
    assert result.finalize.call_args.args[2:5] == (result.runtime["request_id"], True, 0)


def test_withdraw_failure_falls_back_without_applying_scale_down():
    result = _execute(withdraw=False)

    result.withdraw.assert_called_once_with(result.instance)
    result.apply.assert_not_called()
    assert result.runtime["phase"] == FtPhase.WAITING_ENGINE_FAULT.value
    assert result.strategy.is_failed()


@pytest.mark.parametrize(
    ("committed_ranks", "candidate", "expected_removed", "expected_master_rank", "expected_master_ip"),
    [
        ([], 0, [0], 1, "192.0.2.2"),
        ([0], 1, [1], 2, "192.0.2.3"),
    ],
    ids=["dp0-to-dp1", "dp1-to-dp2"],
)
def test_consecutive_master_scale_down_selects_rank_specific_store_port_input(
    committed_ranks,
    candidate,
    expected_removed,
    expected_master_rank,
    expected_master_ip,
):
    endpoints = _endpoints(3)
    survivors = [endpoint.id for endpoint in endpoints if endpoint.id not in {*committed_ranks, candidate}]
    initial_runtime = FtRuntime(
        instance_id=1,
        phase=FtPhase.SCALED_DOWN_RUNNING if committed_ranks else FtPhase.NORMAL,
        original_dp_ranks=[0, 1, 2],
        dead_committed=committed_ranks,
    )
    config = _config()
    config.enable_dp_scale_up = True
    result = _execute(
        endpoints=endpoints,
        candidates=(candidate,),
        initial_runtime=initial_runtime,
        config=config,
        statuses=[
            {rank: {"id": rank, "status": "unhealthy"} for rank in survivors},
            {rank: {"id": rank, "status": "healthy"} for rank in survivors},
        ],
    )

    params = result.apply.call_args.args[1]
    assert params["removed_dp_ranks"] == expected_removed
    assert params["dp_master_rank"] == expected_master_rank
    assert params["dp_master_ip"] == expected_master_ip
    assert result.finalize.call_args.args[4] == expected_master_rank


def test_second_scale_down_keeps_stored_ranks_when_scale_up_disabled():
    endpoints = _endpoints(3)
    initial_runtime = FtRuntime(
        instance_id=1,
        phase=FtPhase.SCALED_DOWN_RUNNING,
        original_dp_ranks=[0, 1, 2],
        dead_committed=[0],
    )
    result = _execute(
        endpoints=endpoints,
        candidates=(1,),
        initial_runtime=initial_runtime,
        statuses=[
            {2: {"id": 2, "status": "unhealthy"}},
            {2: {"id": 2, "status": "healthy"}},
        ],
    )

    assert result.apply.call_args.args[1]["removed_dp_ranks"] == [0, 1]


@pytest.mark.parametrize(
    "phase,serving_published,failed",
    [
        (FtPhase.SCALED_DOWN_RUNNING, False, False),
        (FtPhase.RECONFIGURING, False, True),
    ],
)
def test_duplicate_committed_fault_preserves_existing_runtime(phase, serving_published, failed):
    initial = FtRuntime(
        instance_id=1,
        phase=phase,
        dead_committed=[1],
        serving_published=serving_published,
    )
    result = _execute(candidates=(1,), initial_runtime=initial)

    assert result.strategy.is_failed() is failed
    assert result.runtime["phase"] == phase.value
    assert result.runtime["serving_published"] is serving_published
    result.query.assert_not_called()
    result.apply.assert_not_called()


def test_healthy_recovering_waits_only_when_scale_up_enabled():
    config = _config()
    config.enable_dp_scale_up = True
    result = _execute(
        config=config,
        statuses=[
            {0: {"id": 0, "status": "unhealthy"}},
            {0: {"id": 0, "status": "healthy", "ft_state": "recovering"}},
            {0: {"id": 0, "status": "healthy"}},
        ],
    )

    assert result.strategy.is_failed() is False
    assert result.query.call_count == 3
    assert result.runtime["phase"] == FtPhase.SCALED_DOWN_RUNNING.value
    assert result.finalize.call_args.args[3] is True


@pytest.mark.parametrize(
    "terminal",
    [
        {"id": 0, "status": "unhealthy", "ft_state": "failed", "ft_error": "no slots"},
        {"id": 0, "status": "dead"},
        {"id": 0, "status": "healthy", "ft_state": "recovering"},
        {"id": 0, "status": "unknown"},
    ],
)
def test_terminal_status_falls_back_without_apply_retry(terminal):
    result = _execute(
        statuses=[
            {0: {"id": 0, "status": "unhealthy"}},
            {0: terminal},
        ]
    )

    assert result.runtime["phase"] == FtPhase.RECONFIGURING.value
    result.apply.assert_called_once()
    assert result.finalize.call_args.args[3] is False


def test_status_query_failure_falls_back_without_apply_retry():
    result = _execute(
        statuses=[
            {0: {"id": 0, "status": "unhealthy"}},
            RuntimeError("status unavailable"),
        ]
    )

    assert result.runtime["phase"] == FtPhase.RECONFIGURING.value
    assert result.runtime["last_error"] == "scale-down status query failed: status unavailable"
    result.apply.assert_called_once()
    assert result.finalize.call_args.args[3] is False


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("enabled", False, "ENGINE_FT_REQUIRED"),
        ("external_lb", False, "EXTERNAL_LB_REQUIRED"),
        ("scale_down_supported", False, "ENGINE_SCALE_DOWN_UNSUPPORTED"),
    ],
)
def test_gate_rejects_missing_protocol_or_routing_capability(field, value, reason):
    instance = _instance(_endpoints())
    setattr(instance.ft_capability, field, value)

    gate = DpScaleDownStrategy._gate(_config(), instance)

    assert gate.allowed is False
    assert reason in gate.reason_codes


def test_gate_ignores_removed_flags_in_legacy_capability_snapshot():
    instance = _instance(_endpoints())
    removed = {"auto_recovery": True, "fused_mc2_enabled": True, "eplb_enabled": False, "num_redundant_experts": 0}
    instance.ft_capability = FtCapabilitySnapshot.model_validate({**instance.ft_capability.model_dump(), **removed})
    assert DpScaleDownStrategy._gate(_config(), instance).allowed
    assert not removed.keys() & instance.ft_capability.model_dump().keys()


@pytest.mark.parametrize(
    ("execute_kwargs", "expected_applies"),
    [
        ({"guard_error": RuntimeError("guard failed")}, 0),
        ({"apply_error": RuntimeError("apply unavailable")}, 1),
    ],
    ids=["guard", "apply"],
)
def test_transaction_stage_failure_aborts_without_retry(execute_kwargs, expected_applies):
    result = _execute(**execute_kwargs)

    assert result.runtime["phase"] == (FtPhase.RECONFIGURING if expected_applies else FtPhase.WAITING_ENGINE_FAULT)
    assert result.finalize.call_args.args[3] is False
    assert result.apply.call_count == expected_applies


@pytest.mark.parametrize("failure", ["exception", "deadline"])
def test_commit_failure_or_expired_ack_aborts_without_publishing(failure):
    clock = [1.0]

    def finalize(_groups, _dead, _request, commit, *_args):
        if commit:
            if failure == "exception":
                raise RuntimeError("commit unavailable")
            clock[0] = 10.0

    with patch(
        "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.time.monotonic",
        side_effect=lambda: clock[0],
    ):
        result = _execute(finalize_error=finalize)

    assert result.strategy.is_failed()
    assert result.runtime["phase"] == FtPhase.RECONFIGURING.value
    assert result.runtime["dead_committed"] == []
    assert [call.args[3] for call in result.finalize.call_args_list] == [True, False]


def test_post_commit_runtime_failure_does_not_abort_node_managers():
    original_transition = DpScaleDownStrategy._transition
    finalizations = []

    def finalize(_groups, _dead, _request, commit, *_args):
        finalizations.append(commit)

    def transition(strategy, instance_id, **changes):
        if changes.get("phase") == FtPhase.POST_SCALE_CLEANUP:
            raise RuntimeError("runtime unavailable after commit")
        return original_transition(strategy, instance_id, **changes)

    with patch.object(DpScaleDownStrategy, "_transition", new=transition):
        with pytest.raises(RuntimeError, match="runtime unavailable after commit"):
            _execute(finalize_error=finalize)
    assert finalizations == [True]


def test_software_scale_down_does_not_recycle_pods():
    """Fault-triggered scale-down stays scaled down. Only user recover recycles pods."""
    result = _execute(config=_config())

    assert result.runtime["phase"] == FtPhase.SCALED_DOWN_RUNNING.value
    assert result.stop.call_count == 0


def test_publication_failure_keeps_committed_runtime_hidden():
    result = _execute(publish=False)
    assert result.runtime["phase"] == FtPhase.SCALED_DOWN_RUNNING.value
    assert result.runtime["last_error"] == "ServingOverlay publication pending"
    assert result.runtime["can_serve"] is False


def test_node_manager_grouping_and_status_set_validation():
    endpoints = _endpoints(2)
    headless = Endpoint(id=1, ip="192.0.2.3", business_port="8000", headless=True)
    instance = _instance(endpoints)
    instance.get_all_endpoints.side_effect = lambda include_headless=False: tuple(
        endpoints + [headless] if include_headless else endpoints
    )
    instance.get_node_managers.return_value.append(NodeManagerInfo(pod_ip=headless.ip, port="9000"))
    groups = DpScaleDownStrategy._build_node_manager_groups(instance, {1})
    assert [group.local_endpoint_ids for group in groups] == [(0,), (1,), (1,)]
    assert [group.survivor_endpoint_ids for group in groups] == [(0,), (), ()]
    survivor_groups = [group for group in groups if group.survivor_endpoint_ids]

    with patch(
        "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.NodeManagerApiClient.query_engine_ft_entries",
        side_effect=lambda _node_manager, endpoint_ids, _timeout: {endpoint_ids[0]: {"status": "unhealthy"}},
    ) as query:
        result = DpScaleDownStrategy._query_via_node_managers(survivor_groups, 1)
    assert set(result) == {0}
    query.assert_called_once()

    with patch(
        "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.NodeManagerApiClient.query_engine_ft_entries",
        return_value={1: {"status": "unhealthy"}},
    ):
        with pytest.raises(RuntimeError, match="status set mismatch"):
            DpScaleDownStrategy._query_via_node_managers(survivor_groups, 1)

    with patch(
        "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.NodeManagerApiClient.finalize_engine_ft"
    ) as finalize:
        DpScaleDownStrategy._finalize_node_managers(groups, {1}, "request", True, 2, 1)
    retired_by_pod = sorted((call.args[0].pod_ip, call.args[2]) for call in finalize.call_args_list)
    assert retired_by_pod == [
        ("192.0.2.1", []),
        ("192.0.2.2", [1]),
        ("192.0.2.3", [1]),
    ]


def test_missing_node_manager_topology_fails_closed():
    instance = _instance(_endpoints())
    instance.get_node_managers.return_value = []

    with pytest.raises(RuntimeError, match="no NodeManager topology"):
        DpScaleDownStrategy._build_node_manager_groups(instance, {1})


def test_fast_recovery_without_config_hands_off():
    strategy = EngineFastRecoveryStrategy()
    strategy.execute(5)
    assert strategy.is_finished() is True
    assert strategy.is_failed() is True


def test_stopped_fast_recovery_placeholder_reaches_terminal_state():
    strategy = EngineFastRecoveryStrategy()
    strategy.stop()

    strategy.execute(5)

    assert strategy.is_finished() is True
    assert strategy.is_failed() is False


@pytest.mark.parametrize("dead", [set(range(8)), set(range(8, 16))], ids=["low-rank-pod", "high-rank-pod"])
def test_problem_pod_recovery_preserves_original_survivor_ranks(dead):
    instance = _scale_up_instance(8)
    strategy = DpScaleDownStrategy()
    strategy.bind_config(SimpleNamespace(fault_tolerance_config=_config()))
    get_ft_runtime_store().clear()
    get_ft_runtime_store().get_or_create(7)

    with (
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy._get_instance",
            return_value=instance,
        ),
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.ServingOverlay.withdraw",
            return_value=True,
        ),
        patch.object(strategy, "_drain_inflight_requests", return_value=False),
        patch.object(strategy, "_probe_survivor_rank") as probe,
        patch.object(strategy, "_kill_problem_pod_workers") as kill,
        patch.object(strategy, "_wait_native_scale_down", return_value=True) as wait_sd,
        patch.object(strategy, "_wait_survivors_healthy_before_recycle", return_value=True) as wait_healthy,
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.TopologyResolver.recyclable_pods",
            return_value=["192.0.2.1" if 0 in dead else "192.0.2.2"],
        ),
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.PodLifecycle.stop_recyclable_pods"
        ) as stop_pods,
        patch.object(strategy, "_auto_scale_up", return_value=True) as scale_up,
        patch.object(strategy, "execute") as execute,
    ):
        if 0 not in dead:
            get_ft_runtime_store().transition(7, phase=FtPhase.SCALED_DOWN_RUNNING, dead_committed=sorted(dead))
        ok = strategy.run_problem_pod_recover(
            7,
            list(range(16)),
            dead,
            8,
            _config(),
        )

    assert ok is True
    kill.assert_called_once()
    assert kill.call_args.args[1] == dead
    probe.assert_called_once()
    assert probe.call_args.args[1] == dead
    wait_sd.assert_called_once()
    wait_healthy.assert_called_once()
    stop_pods.assert_called_once()
    assert scale_up.call_count == 1
    assert scale_up.call_args.args[0] == 7
    assert scale_up.call_args.args[1] == list(range(16))
    assert scale_up.call_args.kwargs["survivor_rank_ids"] == set(range(16)) - dead
    execute.assert_not_called()
    assert hasattr(strategy, "_soft_inject_for_kill_shrink") is False


@pytest.mark.parametrize("success", [True, False])
def test_recycle_empty_shells_scale_up_releases_strategy_slot(success):
    instance = _scale_up_instance(8)
    strategy = DpScaleDownStrategy()
    strategy.bind_config(SimpleNamespace(fault_tolerance_config=_config()))
    store = get_ft_runtime_store()
    store.clear()
    store.get_or_create(9)

    with (
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy._get_instance",
            return_value=instance,
        ),
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.TopologyResolver.recyclable_pods",
            return_value=["192.0.2.2"],
        ),
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.PodLifecycle.stop_recyclable_pods"
        ) as stop_pods,
        patch.object(strategy, "_wait_survivors_healthy_before_recycle", return_value=True) as wait_healthy,
        patch.object(strategy, "_auto_scale_up", return_value=success) as scale_up,
    ):
        ok = strategy.run_scale_up_recovery(
            9,
            list(range(16)),
            set(range(8, 16)),
            _config(),
            set(range(8)),
        )

    assert ok is success
    wait_healthy.assert_called_once()
    stop_pods.assert_called_once()
    scale_up.assert_called_once()
    assert scale_up.call_args.kwargs["survivor_rank_ids"] == set(range(8))
    assert strategy.is_finished() is True
    assert strategy.is_failed() is not success
    if not success:
        assert store.get(9)["last_error"] == "DP scale-up failed"


def test_empty_shell_ports_follow_endpoint_business_and_bootstrap_ports():
    """A live engine on its real port blocks recycle; unused 10000/10002/9000 do not."""
    shell = [
        Endpoint(id=8, ip="192.0.2.2", business_port="18000", bootstrap_port=19000),
        Endpoint(id=9, ip="192.0.2.2", business_port="18002", bootstrap_port=19000),
    ]
    survivor = [Endpoint(id=0, ip="192.0.2.1", business_port="8000", bootstrap_port=8100)]
    instance = MagicMock()
    instance.endpoints = {
        "192.0.2.1": {ep.id: ep for ep in survivor},
        "192.0.2.2": {ep.id: ep for ep in shell},
    }
    probed: list[tuple[str, int]] = []

    def _connect(address, timeout):
        probed.append((address[0], int(address[1])))
        if address[1] == 18000:
            raise OSError("closed")
        if address[1] in (18002, 19000):
            return MagicMock()
        raise OSError("not an endpoint port")

    with patch("socket.create_connection", side_effect=_connect):
        closed, detail = DpScaleDownStrategy._empty_shell_ports_closed(instance, ["192.0.2.2"])

    assert closed is False
    assert "192.0.2.2:18002" in detail or "192.0.2.2:19000" in detail
    assert probed == [("192.0.2.2", 18000), ("192.0.2.2", 19000), ("192.0.2.2", 18002)]
    assert not any(port in (10000, 10002, 9000) for _, port in probed)

    def _all_closed(address, timeout):
        probed.append((address[0], int(address[1])))
        raise OSError("closed")

    probed.clear()
    with patch("socket.create_connection", side_effect=_all_closed):
        closed, detail = DpScaleDownStrategy._empty_shell_ports_closed(instance, ["192.0.2.2"])

    assert closed is True
    assert detail == "closed"
    assert ("192.0.2.1", 8000) not in probed


def test_scale_up_ready_sees_ranks_hidden_by_get_all_endpoints():
    """Presence uses the raw roster, not get_all_endpoints()'s id==0 filter."""
    endpoints = [
        Endpoint(id=rank, ip="192.0.2.1" if rank < 8 else "192.0.2.2", business_port="8000") for rank in range(16)
    ]
    instance = MagicMock()
    instance.get_all_endpoints.return_value = tuple(ep for ep in endpoints if ep.id == 0)
    instance.get_node_managers.return_value = [
        NodeManagerInfo(pod_ip="192.0.2.1", port="9000"),
        NodeManagerInfo(pod_ip="192.0.2.2", port="9000"),
    ]
    instance.endpoints = {
        "192.0.2.1": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.1"},
        "192.0.2.2": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.2"},
    }
    healthy = {rank: {"id": rank, "status": "healthy", "ft_state": "normal"} for rank in range(16)}
    group = MagicMock()
    group.survivor_endpoint_ids = list(range(16))
    strategy = DpScaleDownStrategy()

    with (
        patch.object(strategy, "_build_node_manager_groups", return_value=[group]) as build,
        patch.object(strategy, "_query_via_node_managers", return_value=healthy) as query,
    ):
        ready, statuses = strategy._scale_up_ready(instance, list(range(16)), 1.0)

    assert ready is True
    assert statuses == healthy
    build.assert_called_once()
    query.assert_called_once()
    instance.get_all_endpoints.assert_not_called()


def test_engine_http_calls_bracket_ipv6_literals():
    """Scale, status, resume, metrics and probe URLs keep the IPv6 host intact."""
    ep = Endpoint(id=0, ip="2001:db8::1", business_port="8000")
    assert _engine_http_url(ep, "/scale_elastic_ep") == "http://[2001:db8::1]:8000/scale_elastic_ep"
    instance = MagicMock()
    instance.model_name = "qwen3_235b"
    instance.endpoints = {ep.ip: {ep.id: ep}}
    instance.get_node_managers.return_value = [NodeManagerInfo(pod_ip=ep.ip, port="9000")]
    strategy = DpScaleDownStrategy()
    seen: list[str] = []

    def capture(req, timeout=None):
        seen.append(req.full_url if hasattr(req, "full_url") else req)
        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"phase":"paused","epoch":"e1","engines":[{"id":0,"dp_rank":0}]}'
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        return response

    with patch("urllib.request.urlopen", side_effect=capture):
        strategy._poll_is_scaling_status(ep, 1)
        strategy._resume_elastic_ep(instance, "e1", 1)
        strategy._probe_endpoint_ft_status(ep, 1)
        strategy._read_inflight_requests(ep)
        strategy._probe_survivor_rank(instance, set(), 1)
        dispatch = strategy._trigger_scale_elastic_ep(instance, target_dp_size=1, old_rank_ids={0}, timeout=1)
        strategy._drain_scale_elastic_ep(dispatch, timeout=2)

    assert seen == [
        "http://[2001:db8::1]:8000/is_scaling_elastic_ep",
        "http://[2001:db8::1]:8000/resume_elastic_ep",
        "http://[2001:db8::1]:8000/v1/fault_tolerance/status",
        "http://[2001:db8::1]:8000/metrics",
        "http://[2001:db8::1]:8000/v1/chat/completions",
        "http://[2001:db8::1]:8000/scale_elastic_ep",
    ]


def _scale_up_instance(ranks_per_pod=2):
    return _instance(
        [
            Endpoint(id=rank, ip="192.0.2.1" if rank < ranks_per_pod else "192.0.2.2", business_port="8000")
            for rank in range(2 * ranks_per_pod)
        ]
    )


def _dispatch(error=None):
    import concurrent.futures

    from motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy import _ScaleDispatch

    fut = concurrent.futures.Future()
    if error is None:
        fut.set_result("0 -> 200")
    else:
        fut.set_exception(error)
    return _ScaleDispatch(pool=MagicMock(), futures={fut: Endpoint(id=0, ip="192.0.2.1", business_port="8000")})


@pytest.mark.parametrize("failed_rank", [0, 2], ids=["survivor-aborts", "joiner-keeps-waiting"])
def test_scale_up_ready_loop_distinguishes_survivor_and_joiner_failure(failed_rank):
    strategy = DpScaleDownStrategy()
    store = get_ft_runtime_store()
    store.clear()
    store.get_or_create(1)
    instance = _scale_up_instance()
    started = time.monotonic()
    budget = 30.0 if failed_rank == 0 else 0.05
    statuses = {rank: {"status": "healthy"} for rank in (0, 1, 2)}
    statuses[failed_rank] = {"status": "healthy" if failed_rank == 0 else "failed", "ft_state": "failed"}
    with (
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy._get_instance",
            return_value=instance,
        ),
        patch.object(strategy, "_wait_elastic_preparing", return_value="epoch-1"),
        patch.object(strategy, "_wait_elastic_paused", return_value="epoch-1"),
        patch.object(strategy, "_resume_elastic_ep"),
        patch.object(strategy, "_scale_up_ready", return_value=(False, statuses)) as query,
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.ServingOverlay.publish_restored"
        ) as publish,
    ):
        assert not strategy._run_scale_up_gates(
            1, list(range(4)), {0, 1}, _config(), time.monotonic() + budget, 1.0, _dispatch(), store
        )
    publish.assert_not_called()
    if failed_rank == 0:
        assert time.monotonic() - started < 1.0
        query.assert_called_once()
    else:
        assert query.call_count > 1


def test_auto_scale_up_does_not_scale_when_joiners_absent():
    strategy = DpScaleDownStrategy()
    get_ft_runtime_store().clear()
    get_ft_runtime_store().get_or_create(1)
    with (
        patch.object(strategy, "_wait_joiner_ranks_present", return_value=False),
        patch.object(strategy, "_trigger_scale_elastic_ep") as trigger,
    ):
        ok = strategy._auto_scale_up(1, list(range(4)), _config(), survivor_rank_ids={0, 1})
    assert ok is False
    trigger.assert_not_called()


def test_auto_scale_up_runs_one_round_at_a_time():
    """A second instance waits until the in-flight scale-up round finishes."""
    get_ft_runtime_store().clear()
    get_ft_runtime_store().get_or_create(1)
    get_ft_runtime_store().get_or_create(2)
    entered = []
    hold = threading.Event()
    first_entered = threading.Event()

    def wait_joiners(instance_id, *_args, **_kwargs):
        entered.append(instance_id)
        first_entered.set()
        if instance_id == 1:
            assert hold.wait(timeout=2.0)
        return False

    first = DpScaleDownStrategy()
    second = DpScaleDownStrategy()
    with (
        patch.object(DpScaleDownStrategy, "_wait_joiner_ranks_present", side_effect=wait_joiners),
        patch.object(DpScaleDownStrategy, "_trigger_scale_elastic_ep") as trigger,
    ):
        worker = threading.Thread(
            target=lambda: first._auto_scale_up(1, list(range(4)), _config(), survivor_rank_ids={0, 1})
        )
        blocked = threading.Thread(
            target=lambda: second._auto_scale_up(2, list(range(4)), _config(), survivor_rank_ids={0, 1})
        )
        worker.start()
        blocked_started = False
        try:
            assert first_entered.wait(timeout=2.0)
            blocked.start()
            blocked_started = True
            blocked.join(timeout=0.2)
            assert blocked.is_alive()
            assert entered == [1]
        finally:
            hold.set()
            worker.join(timeout=2.0)
            if blocked_started:
                blocked.join(timeout=2.0)
    assert entered == [1, 2]
    trigger.assert_not_called()


def test_failed_abort_is_not_safe_for_relaunch():
    result = _execute(guard_error=RuntimeError("partial guard"), finalize_error=RuntimeError("abort failed"))
    assert result.strategy.cleanup_failed
    assert result.strategy.is_failed()
    assert not result.strategy.mutation_started


def test_guard_renews_same_owner_within_existing_node_manager_limit():
    strategy = DpScaleDownStrategy()
    config = _config(execution_deadline_sec=600)
    config.fault_collection_timeout_seconds = 600
    with (
        patch.object(strategy, "_guard_node_managers") as guard,
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.time.monotonic", return_value=0.0
        ) as now,
    ):
        strategy._renew_guard([], "owner", config, None)
        now.return_value = 101.0
        strategy._renew_guard([], "owner", config, None)
    assert guard.call_count == 2
    assert all(call.args[1:3] == ("owner", 300.0) for call in guard.call_args_list)


def test_apply_dispatch_uses_execution_budget_before_result_poll():
    clock = [0.0]

    def apply_slow(*args, **kwargs):
        clock[0] = 2.0

    with patch(
        "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.time.monotonic",
        side_effect=lambda: clock[0],
    ):
        result = _execute(apply_error=apply_slow)
    assert result.strategy.mutation_started
    assert result.strategy.is_failed()
    assert result.query.call_count == 1  # readiness only; dispatch used the execution budget
    result.apply.assert_called_once()
    assert result.runtime["phase"] == FtPhase.RECONFIGURING


def test_collected_snapshot_does_not_restart_readiness_budget():
    clock = [2.0]
    initial = ScaleDownContext(engine_fault_observed=True, readiness_deadline=1.0, pending_removed_ranks=(1,))

    with patch(
        "motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.strategy.time.monotonic",
        side_effect=lambda: clock[0],
    ):
        result = _execute(context=initial)
    assert result.strategy.is_failed()
    assert not result.strategy.mutation_started
    result.apply.assert_not_called()
    result.guard.assert_not_called()


def test_cancellation_during_guard_renewal_never_dispatches_apply():
    renewals = [0]

    def renew(strategy, *args, **kwargs):
        renewals[0] += 1
        if renewals[0] == 3:  # initial guard, readiness query, immediately before apply
            strategy.event.set()

    with patch.object(DpScaleDownStrategy, "_renew_guard", new=renew):
        result = _execute()
    assert result.strategy.is_failed()
    assert not result.strategy.mutation_started
    result.apply.assert_not_called()
    assert result.finalize.call_args.args[3] is False


def test_preparation_stays_waiting_until_apply_without_deciding_bounce():
    store = get_ft_runtime_store()
    with patch.object(store, "transition", wraps=store.transition) as transition:
        result = _execute(
            statuses=[
                {0: {"id": 0, "status": "healthy"}},
                {0: {"id": 0, "status": "unhealthy"}},
                {0: {"id": 0, "status": "healthy"}},
            ]
        )
    assert not result.strategy.is_failed()
    phases = [call.kwargs["phase"] for call in transition.call_args_list if "phase" in call.kwargs]
    assert FtPhase.DECIDING not in phases
    assert phases[: phases.index(FtPhase.SCALING_DOWN)]
    assert set(phases[: phases.index(FtPhase.SCALING_DOWN)]) == {FtPhase.WAITING_ENGINE_FAULT}
    result.apply.assert_called_once()
