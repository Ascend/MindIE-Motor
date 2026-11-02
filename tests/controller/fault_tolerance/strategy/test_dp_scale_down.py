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
from unittest.mock import MagicMock, patch
import threading
import time

import pytest

from motor.common.resources.endpoint import Endpoint
from motor.common.resources.instance import FtCapabilitySnapshot, NodeManagerInfo, ParallelConfig
from motor.controller.fault_tolerance.dp_scale_down import (
    FaultNormalizer,
    FtPhase,
    FtRuntime,
    ScaleDownContext,
    get_ft_runtime_store,
)
from motor.controller.fault_tolerance.strategy.dp_scale_down import DpScaleDownStrategy, _engine_http_url
from motor.controller.fault_tolerance.strategy.fast_recovery import EngineFastRecoveryStrategy


def _config(**overrides):
    values = {
        "request_timeout_sec": 1,
        "poll_interval_sec": 0.001,
        "execution_deadline_sec": 1,
    }
    values.update(overrides)
    return SimpleNamespace(
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
        auto_recovery=False,
        fused_mc2_enabled=True,
        eplb_enabled=True,
        num_redundant_experts=8,
    )
    instance.endpoints = {endpoint.ip: {endpoint.id: endpoint} for endpoint in endpoints}
    instance.get_node_managers.return_value = [
        NodeManagerInfo(pod_ip=endpoint.ip, port="9000") for endpoint in endpoints
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
    strategy.bind_context(ScaleDownContext(pending_removed_ranks=tuple(candidates)))
    with (
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down._get_instance",
            return_value=instance,
        ),
        patch.object(
            DpScaleDownStrategy,
            "_query_via_node_managers",
            side_effect=statuses,
        ) as query,
        patch.object(DpScaleDownStrategy, "_apply_via_node_managers", side_effect=apply_error) as apply,
        patch.object(DpScaleDownStrategy, "_guard_node_managers", side_effect=guard_error) as guard,
        patch.object(DpScaleDownStrategy, "_finalize_node_managers") as finalize,
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down.ServingOverlay.withdraw",
            return_value=withdraw,
        ) as withdraw_call,
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down.ServingOverlay.publish",
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
        "motor.controller.fault_tolerance.strategy.dp_scale_down.time.monotonic",
        side_effect=[0, 0, 2],
    ):
        result = _execute(
            statuses=[{0: {"id": 0, "status": "healthy"}}],
            config=_config(execution_deadline_sec=1),
        )

    assert result.runtime["phase"] == FtPhase.RECONFIGURING.value
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

    assert result.runtime["phase"] == FtPhase.RECONFIGURING.value
    result.apply.assert_not_called()


@pytest.mark.parametrize(
    ("late_dead_ranks", "expected_removed"),
    [({3}, [2, 3]), ({0, 1, 3}, None)],
)
def test_late_dead_survivors_join_removal_set_or_fallback(late_dead_ranks, expected_removed):
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
    )

    if expected_removed is None:
        assert result.runtime["phase"] == FtPhase.RECONFIGURING.value
        result.apply.assert_not_called()
        assert result.finalize.call_args.args[1] == {0, 1, 2, 3}
        assert result.finalize.call_args.args[3] is False
    else:
        assert result.apply.call_args.args[1]["removed_dp_ranks"] == expected_removed
        applied_groups = result.apply.call_args.args[0]
        assert {rank for group in applied_groups for rank in group.survivor_endpoint_ids} == remaining_survivors
        assert result.runtime["dead_committed"] == expected_removed


def test_success_uses_fault_candidates_not_observational_mask():
    with patch("motor.controller.fault_tolerance.strategy.dp_scale_down.logger.info") as log_info:
        result = _execute(
            statuses=[
                {0: {"id": 0, "status": "unhealthy", "mask": [False, True]}},
                {0: {"id": 0, "status": "healthy", "mask": [False, True]}},
            ]
        )

    params = result.apply.call_args.args[1]
    assert params == {"removed_dp_ranks": [1], "dp_master_ip": "192.0.2.1", "dp_master_rank": 0}
    assert result.runtime["dead_committed"] == [1]
    result.guard.assert_called_once()
    result.finalize.assert_called_once()
    assert result.finalize.call_args.args[3] is True
    assert result.finalize.call_args.args[2] == result.runtime["request_id"]
    assert result.finalize.call_args.args[4] == 0
    # execute() also emits the logical-removed-ranks debug log, so assert the
    # success summary by content instead of by call count.
    log_info.assert_any_call(
        "%s succeeded: instance_id=%d, request_id=%s, removed_dp_ranks=%s, surviving_dp_ranks=%s, new_master_rank=%d",
        "DpScaleDownStrategy",
        1,
        result.runtime["request_id"],
        [1],
        [0],
        0,
    )


def test_withdraw_failure_falls_back_without_applying_scale_down():
    result = _execute(withdraw=False)

    result.withdraw.assert_called_once_with(result.instance)
    result.apply.assert_not_called()
    assert result.runtime["phase"] == FtPhase.RECONFIGURING.value
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
        ("eplb_enabled", False, "EPLB_REQUIRED"),
        ("num_redundant_experts", 0, "EXPERT_REDUNDANCY_REQUIRED"),
    ],
)
def test_gate_rejects_missing_official_engine_prerequisite(field, value, reason):
    instance = _instance(_endpoints())
    setattr(instance.ft_capability, field, value)

    gate = DpScaleDownStrategy._gate(_config(), instance)

    assert gate.allowed is False
    assert reason in gate.reason_codes


def test_gate_leaves_mc2_validation_to_engine():
    instance = _instance(_endpoints())
    instance.ft_capability.fused_mc2_enabled = False

    gate = DpScaleDownStrategy._gate(_config(), instance)

    assert gate.allowed is True
    assert "FUSED_MC2_REQUIRED" not in gate.reason_codes


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

    assert result.runtime["phase"] == FtPhase.RECONFIGURING.value
    assert result.finalize.call_args.args[3] is False
    assert result.apply.call_count == expected_applies


def test_software_scale_down_does_not_recycle_pods():
    """Fault-triggered scale-down stays scaled down. Only user recover recycles pods."""
    result = _execute(config=_config())

    assert result.runtime["phase"] == FtPhase.SCALED_DOWN_RUNNING.value
    assert result.stop.call_count == 0


def test_publication_failure_keeps_committed_runtime_hidden():
    with patch("motor.controller.fault_tolerance.strategy.dp_scale_down.logger.warning") as log_warning:
        result = _execute(publish=False)

    assert result.runtime["phase"] == FtPhase.SCALED_DOWN_RUNNING.value
    assert result.runtime["last_error"] == "ServingOverlay publication pending"
    assert result.runtime["can_serve"] is False
    log_warning.assert_called_once_with(
        "%s committed but serving publication is pending: instance_id=%d, request_id=%s, "
        "removed_dp_ranks=%s, surviving_dp_ranks=%s, new_master_rank=%d",
        "DpScaleDownStrategy",
        1,
        result.runtime["request_id"],
        [1],
        [0],
        0,
    )


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
        "motor.controller.fault_tolerance.strategy.dp_scale_down.NodeManagerApiClient.query_engine_ft_entries",
        side_effect=lambda _node_manager, endpoint_ids, _timeout: {endpoint_ids[0]: {"status": "unhealthy"}},
    ) as query:
        result = DpScaleDownStrategy._query_via_node_managers(survivor_groups, 1)
    assert set(result) == {0}
    query.assert_called_once()

    with patch(
        "motor.controller.fault_tolerance.strategy.dp_scale_down.NodeManagerApiClient.query_engine_ft_entries",
        return_value={1: {"status": "unhealthy"}},
    ):
        with pytest.raises(RuntimeError, match="status set mismatch"):
            DpScaleDownStrategy._query_via_node_managers(survivor_groups, 1)

    with patch(
        "motor.controller.fault_tolerance.strategy.dp_scale_down.NodeManagerApiClient.finalize_engine_ft"
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


def test_fast_recovery_retries_all_unhealthy_dp_ranks():
    strategy = EngineFastRecoveryStrategy()
    strategy.bind_config(SimpleNamespace(fault_tolerance_config=_config()))
    instance = _instance(_endpoints())
    groups = DpScaleDownStrategy._build_node_manager_groups(instance, set())
    with (
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down._get_instance",
            return_value=instance,
        ),
        patch.object(DpScaleDownStrategy, "_build_node_manager_groups", return_value=groups),
        patch.object(DpScaleDownStrategy, "_guard_node_managers") as guard,
        patch.object(
            DpScaleDownStrategy,
            "_query_via_node_managers",
            side_effect=[
                {0: {"status": "unhealthy"}, 1: {"status": "unhealthy"}},
                {0: {"status": "healthy"}, 1: {"status": "healthy"}},
            ],
        ),
        patch.object(DpScaleDownStrategy, "_apply_via_node_managers") as apply,
        patch.object(DpScaleDownStrategy, "_finalize_node_managers") as finalize,
    ):
        strategy.execute(5)

    assert strategy.is_finished()
    assert not strategy.is_failed()
    guard.assert_called_once()
    assert apply.call_args.kwargs["instruction"] == "retry"
    finalize.assert_called_once()


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


def test_run_problem_pod_recover_kills_waits_recycles_and_scales_up():
    """Kill → wait native scale-down → recycle empty shells → elastic scale-up."""
    endpoints = [
        Endpoint(id=rank, ip="192.0.2.1" if rank < 8 else "192.0.2.2", business_port="8000") for rank in range(16)
    ]
    nm_low = NodeManagerInfo(pod_ip="192.0.2.1", port="9000")
    nm_high = NodeManagerInfo(pod_ip="192.0.2.2", port="9000")
    instance = MagicMock()
    instance.get_all_endpoints.return_value = endpoints
    instance.get_node_managers.return_value = [nm_low, nm_high]
    instance.endpoints = {
        "192.0.2.1": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.1"},
        "192.0.2.2": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.2"},
    }

    strategy = DpScaleDownStrategy()
    strategy.bind_config(SimpleNamespace(fault_tolerance_config=_config()))
    get_ft_runtime_store().get_or_create(7)

    with (
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down._get_instance",
            return_value=instance,
        ),
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down.ServingOverlay.withdraw",
            return_value=True,
        ),
        patch.object(strategy, "_drain_inflight_requests", return_value=False),
        patch.object(strategy, "_probe_survivor_rank") as probe,
        patch.object(strategy, "_kill_problem_pod_workers") as kill,
        patch.object(strategy, "_wait_native_scale_down", return_value=True) as wait_sd,
        patch.object(strategy, "_wait_survivors_healthy_before_recycle", return_value=True) as wait_healthy,
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down.TopologyResolver.recyclable_pods",
            return_value=["192.0.2.2"],
        ),
        patch("motor.controller.fault_tolerance.strategy.dp_scale_down.PodLifecycle.stop_recyclable_pods") as stop_pods,
        patch.object(strategy, "_auto_scale_up", return_value=True) as scale_up,
        patch.object(strategy, "execute") as execute,
    ):
        get_ft_runtime_store().transition(
            7,
            phase=FtPhase.SCALED_DOWN_RUNNING,
            dead_committed=list(range(8, 16)),
        )
        ok = strategy.run_problem_pod_recover(
            7,
            list(range(16)),
            set(range(8, 16)),
            8,
            _config(),
        )

    assert ok is True
    kill.assert_called_once()
    probe.assert_called_once()
    assert probe.call_args.args[1] == set(range(8, 16))
    wait_sd.assert_called_once()
    wait_healthy.assert_called_once()
    stop_pods.assert_called_once()
    assert scale_up.call_count == 1
    assert scale_up.call_args.args[0] == 7
    assert scale_up.call_args.args[1] == list(range(16))
    execute.assert_not_called()
    assert hasattr(strategy, "_soft_inject_for_kill_shrink") is False


def test_run_user_elastic_recover_keeps_original_survivor_ranks():
    """A low-rank problem Pod is killed; survivors keep original ids for scale-up."""
    endpoints = [
        Endpoint(id=rank, ip="192.0.2.1" if rank < 8 else "192.0.2.2", business_port="8000") for rank in range(16)
    ]
    instance = MagicMock()
    instance.get_all_endpoints.return_value = endpoints
    instance.get_node_managers.return_value = [
        NodeManagerInfo(pod_ip="192.0.2.1", port="9000"),
        NodeManagerInfo(pod_ip="192.0.2.2", port="9000"),
    ]
    instance.endpoints = {
        "192.0.2.1": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.1"},
        "192.0.2.2": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.2"},
    }

    strategy = DpScaleDownStrategy()
    strategy.bind_config(SimpleNamespace(fault_tolerance_config=_config()))
    get_ft_runtime_store().get_or_create(11)

    with (
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down._get_instance",
            return_value=instance,
        ),
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down.ServingOverlay.withdraw",
            return_value=True,
        ),
        patch.object(strategy, "_drain_inflight_requests", return_value=False),
        patch.object(strategy, "_probe_survivor_rank") as probe,
        patch.object(strategy, "_kill_problem_pod_workers") as kill,
        patch.object(strategy, "_wait_native_scale_down", return_value=True),
        patch.object(strategy, "_wait_survivors_healthy_before_recycle", return_value=True),
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down.TopologyResolver.recyclable_pods",
            return_value=["192.0.2.1"],
        ),
        patch("motor.controller.fault_tolerance.strategy.dp_scale_down.PodLifecycle.stop_recyclable_pods"),
        patch.object(strategy, "_auto_scale_up", return_value=True) as scale_up,
    ):
        ok = strategy.run_problem_pod_recover(
            11,
            list(range(16)),
            set(range(8)),
            8,
            _config(),
        )

    assert ok is True
    kill.assert_called_once()
    assert kill.call_args.args[1] == set(range(8))
    probe.assert_called_once()
    assert probe.call_args.args[1] == set(range(8))
    assert scale_up.call_args.kwargs["survivor_rank_ids"] == set(range(8, 16))


def test_recycle_empty_shells_scale_up_success_releases_strategy_slot():
    """The user-recover empty-shell path finishes the strategy on success."""
    endpoints = [
        Endpoint(id=rank, ip="192.0.2.1" if rank < 8 else "192.0.2.2", business_port="8000") for rank in range(16)
    ]
    instance = MagicMock()
    instance.get_all_endpoints.return_value = endpoints
    instance.get_node_managers.return_value = [
        NodeManagerInfo(pod_ip="192.0.2.1", port="9000"),
        NodeManagerInfo(pod_ip="192.0.2.2", port="9000"),
    ]
    instance.endpoints = {
        "192.0.2.1": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.1"},
        "192.0.2.2": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.2"},
    }

    strategy = DpScaleDownStrategy()
    strategy.bind_config(SimpleNamespace(fault_tolerance_config=_config()))
    store = get_ft_runtime_store()
    store.clear()
    store.get_or_create(9)

    with (
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down._get_instance",
            return_value=instance,
        ),
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down.TopologyResolver.recyclable_pods",
            return_value=["192.0.2.2"],
        ),
        patch("motor.controller.fault_tolerance.strategy.dp_scale_down.PodLifecycle.stop_recyclable_pods") as stop_pods,
        patch.object(strategy, "_wait_survivors_healthy_before_recycle", return_value=True) as wait_healthy,
        patch.object(strategy, "_auto_scale_up", return_value=True) as scale_up,
    ):
        ok = strategy._recycle_empty_shells_then_scale_up(
            9,
            list(range(16)),
            set(range(8, 16)),
            _config(),
            set(range(8)),
        )

    assert ok is True
    wait_healthy.assert_called_once()
    stop_pods.assert_called_once()
    scale_up.assert_called_once()
    assert scale_up.call_args.kwargs["survivor_rank_ids"] == set(range(8))
    # Success must release the FaultManager strategy slot.
    assert strategy.is_finished() is True
    assert strategy.is_failed() is False


def test_recycle_empty_shells_scale_up_failure_releases_strategy_slot():
    """A failed scale-up still finishes the strategy so the slot is freed."""
    endpoints = [
        Endpoint(id=rank, ip="192.0.2.1" if rank < 8 else "192.0.2.2", business_port="8000") for rank in range(16)
    ]
    instance = MagicMock()
    instance.get_all_endpoints.return_value = endpoints
    instance.get_node_managers.return_value = [
        NodeManagerInfo(pod_ip="192.0.2.1", port="9000"),
        NodeManagerInfo(pod_ip="192.0.2.2", port="9000"),
    ]
    instance.endpoints = {
        "192.0.2.1": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.1"},
        "192.0.2.2": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.2"},
    }

    strategy = DpScaleDownStrategy()
    strategy.bind_config(SimpleNamespace(fault_tolerance_config=_config()))
    store = get_ft_runtime_store()
    store.clear()
    store.get_or_create(9)

    with (
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down._get_instance",
            return_value=instance,
        ),
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down.TopologyResolver.recyclable_pods",
            return_value=["192.0.2.2"],
        ),
        patch("motor.controller.fault_tolerance.strategy.dp_scale_down.PodLifecycle.stop_recyclable_pods"),
        patch.object(strategy, "_wait_survivors_healthy_before_recycle", return_value=True),
        patch.object(strategy, "_auto_scale_up", return_value=False) as scale_up,
    ):
        ok = strategy._recycle_empty_shells_then_scale_up(
            9,
            list(range(16)),
            set(range(8, 16)),
            _config(),
            set(range(8)),
        )

    assert ok is False
    scale_up.assert_called_once()
    # Failure must also release the FaultManager strategy slot.
    assert strategy.is_finished() is True
    assert strategy.is_failed() is True
    runtime = store.get_or_create(9)
    assert runtime.last_error == "DP scale-up failed"


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


def _scale_up_instance():
    endpoints = [
        Endpoint(id=rank, ip="192.0.2.1" if rank < 2 else "192.0.2.2", business_port="8000") for rank in range(4)
    ]
    instance = MagicMock()
    instance.get_node_managers.return_value = [
        NodeManagerInfo(pod_ip="192.0.2.1", port="9000"),
        NodeManagerInfo(pod_ip="192.0.2.2", port="9000"),
    ]
    instance.endpoints = {
        "192.0.2.1": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.1"},
        "192.0.2.2": {ep.id: ep for ep in endpoints if ep.ip == "192.0.2.2"},
    }
    return instance


def _dispatch(error=None):
    import concurrent.futures

    from motor.controller.fault_tolerance.strategy.dp_scale_down import _ScaleDispatch

    fut = concurrent.futures.Future()
    if error is None:
        fut.set_result("0 -> 200")
    else:
        fut.set_exception(error)
    return _ScaleDispatch(pool=MagicMock(), futures={fut: Endpoint(id=0, ip="192.0.2.1", business_port="8000")})


def test_scale_up_ready_loop_aborts_on_survivor_engine_failure():
    strategy = DpScaleDownStrategy()
    get_ft_runtime_store().clear()
    get_ft_runtime_store().get_or_create(1)
    instance = _scale_up_instance()
    started = time.monotonic()
    with (
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down._get_instance",
            return_value=instance,
        ),
        patch.object(strategy, "_wait_elastic_preparing", return_value="epoch-1"),
        patch.object(strategy, "_wait_elastic_paused", return_value="epoch-1"),
        patch.object(strategy, "_resume_elastic_ep"),
        patch.object(
            strategy,
            "_scale_up_ready",
            return_value=(False, {0: {"status": "healthy", "ft_state": "failed"}, 1: {"status": "healthy"}}),
        ),
        patch("motor.controller.fault_tolerance.strategy.dp_scale_down.ServingOverlay.publish_restored") as publish,
    ):
        ok = strategy._run_scale_up_gates(
            1,
            list(range(4)),
            {0, 1},
            _config(),
            time.monotonic() + 30.0,
            1.0,
            _dispatch(),
            get_ft_runtime_store(),
        )
    assert ok is False
    assert time.monotonic() - started < 1.0
    publish.assert_not_called()


def test_scale_up_ready_loop_waits_out_joiner_engine_failure():
    strategy = DpScaleDownStrategy()
    get_ft_runtime_store().clear()
    instance = _scale_up_instance()
    polls = {"count": 0}

    def joiner_failed(_instance, _ranks, _timeout):
        polls["count"] += 1
        return False, {
            2: {"status": "failed", "ft_state": "failed"},
            0: {"status": "healthy"},
            1: {"status": "healthy"},
        }

    with (
        patch(
            "motor.controller.fault_tolerance.strategy.dp_scale_down._get_instance",
            return_value=instance,
        ),
        patch.object(strategy, "_wait_elastic_preparing", return_value="epoch-1"),
        patch.object(strategy, "_wait_elastic_paused", return_value="epoch-1"),
        patch.object(strategy, "_resume_elastic_ep"),
        patch.object(strategy, "_scale_up_ready", side_effect=joiner_failed),
    ):
        ok = strategy._run_scale_up_gates(
            1,
            list(range(4)),
            {0, 1},
            _config(poll_interval_sec=0.001, execution_deadline_sec=0.05),
            time.monotonic() + 0.05,
            1.0,
            _dispatch(),
            get_ft_runtime_store(),
        )
    assert ok is False
    assert polls["count"] > 1


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
