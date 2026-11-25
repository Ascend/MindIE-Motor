# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Shared fast-recovery barriers and fault-specific completion policies."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from motor.common.resources import Endpoint, InsStatus, NodeManagerInfo
from motor.controller.fault_tolerance.strategy.engine_ft.fast_recovery import (
    HbmUceRecoveryStrategy,
    GenericFastRecoveryStrategy,
    TokenReinferenceStrategy,
    FastRecoveryState,
    EngineFastRecoveryStrategy,
)


def _proxy_query(query):
    def read(node, endpoint_ids, timeout):
        payload = query(node)
        return {
            entry["engine_id"]: (entry.get("engines") or [{}])[0] or {}
            for entry in payload.get("engines", [])
            if isinstance(entry, dict)
        }

    return read


def _command(call):
    return {"instruction": call.args[2], "params": call.args[3], "request_id": call.args[4]}


@pytest.fixture
def recovery():
    policy_evidence_check = EngineFastRecoveryStrategy._policy_evidence_valid
    nodes = [NodeManagerInfo(pod_ip="10.0.0.1", port="8080"), NodeManagerInfo(pod_ip="10.0.0.2", port="8080")]
    instance = Mock(id=7, status=InsStatus.ACTIVE)
    instance.get_node_managers.return_value = nodes
    instance.get_all_endpoints.return_value = [
        Endpoint(id=i, ip=node.pod_ip, business_port="8000") for i, node in enumerate(nodes)
    ]
    with (
        patch.object(
            EngineFastRecoveryStrategy,
            "_resolve_config",
            side_effect=lambda key, default: 0.03 if "timeout" in key else 0.001,
        ),
        patch.object(EngineFastRecoveryStrategy, "_get_instance_manager") as im,
        patch.object(EngineFastRecoveryStrategy, "_policy_evidence_valid", return_value=True),
        patch.object(EngineFastRecoveryStrategy, "_recovery_eligible", return_value=True),
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.fast_recovery.ServingOverlay.publish",
            return_value=True,
        ),
        patch.object(HbmUceRecoveryStrategy, "_pending_hbm_uce_faults", return_value=[]) as hbm_faults,
        patch(
            "motor.controller.fault_tolerance.strategy.engine_ft.fast_recovery.ServingOverlay.pause_for_recovery",
            return_value=True,
        ) as pause,
        patch("motor.controller.fault_tolerance.strategy.engine_ft.fast_recovery.NodeManagerApiClient") as client,
        patch("motor.controller.fault_tolerance.strategy.engine_ft.fast_recovery.get_ft_runtime_store") as runtime,
    ):
        runtime.return_value.is_reconfiguration_dispatched.return_value = False
        runtime.return_value.get.return_value = None
        im.return_value.get_instance.return_value = instance
        client.apply_engine_ft_instructions.return_value = True
        strategy = GenericFastRecoveryStrategy()
        state = SimpleNamespace(strategy=strategy, nodes=nodes, instance=instance, client=client, pause=pause)
        state.policy_evidence_check = policy_evidence_check
        state.hbm_faults = hbm_faults
        state.runtime = runtime.return_value
        state.outcomes = ["healthy"]
        state.polls = []

        def query(node):
            rank = nodes.index(node)
            attempt = state.strategy.context.attempt
            status = state.outcomes[min(attempt - 1, len(state.outcomes) - 1)] if attempt else "unhealthy"
            entry = {"id": rank, "status": status}
            if attempt:
                entry["last_ft_request_id"] = state.strategy.context.request_id
                if status == "failed":
                    entry.update(status="unhealthy", ft_state="failed", ft_error="OOM")
                if status == "recovering":
                    entry.update(status="unhealthy", ft_state="recovering")
            state.polls.append((attempt, rank, status))
            return {"engines": [{"engine_id": rank, "engines": [entry]}]}

        state.query = query
        client.query_engine_ft_entries.side_effect = _proxy_query(query)
        yield state


@pytest.mark.parametrize(
    "policy,params,clearance_calls",
    [
        (GenericFastRecoveryStrategy, {}, 0),
        (TokenReinferenceStrategy, {}, 0),
        (HbmUceRecoveryStrategy, {"abort": True}, 1),
    ],
)
def test_recovery_payload_and_clearance_follow_policy(recovery, policy, params, clearance_calls):
    recovery.strategy = policy()
    recovery.strategy.execute(7)
    assert recovery.strategy.context.current_state == FastRecoveryState.SUCCESS
    recovery.pause.assert_called_once_with(recovery.instance)
    payloads = [_command(call) for call in recovery.client.apply_engine_ft_instructions.call_args_list]
    assert len(payloads) == 2
    assert len({payload["request_id"] for payload in payloads}) == 1
    assert all(payload["instruction"] == "retry" for payload in payloads)
    assert all(payload["params"] == {"instance_id": 7, **params} for payload in payloads)
    assert recovery.hbm_faults.call_count == clearance_calls


@pytest.mark.parametrize("policy", [GenericFastRecoveryStrategy, TokenReinferenceStrategy])
@pytest.mark.parametrize("second,failed", [("healthy", False), ("failed", True)])
def test_recovery_escalates_once_after_every_rank_finishes_first_round(recovery, second, failed, policy):
    recovery.strategy = policy()
    recovery.outcomes = ["failed", second]
    recovery.strategy.execute(7)
    assert recovery.strategy.is_finished()
    assert recovery.strategy.is_failed() is failed
    payloads = [_command(call) for call in recovery.client.apply_engine_ft_instructions.call_args_list]
    assert len(payloads) == 4
    assert "abort" not in payloads[0]["params"]
    assert payloads[2]["params"]["abort"] is True
    assert payloads[0]["request_id"] == payloads[1]["request_id"]
    assert payloads[2]["request_id"] == payloads[3]["request_id"]
    assert payloads[0]["request_id"] != payloads[2]["request_id"]
    assert {(a, r) for a, r, _ in recovery.polls if a == 1} == {(1, 0), (1, 1)}
    recovery.hbm_faults.assert_not_called()
    if failed:
        assert recovery.strategy.failure_fallback == "EngineRelaunchStrategy"


@pytest.mark.parametrize("outcome", ["dead", "unknown", "recovering"])
def test_terminal_or_uncertain_round_never_dispatches_overlapping_recovery(recovery, outcome):
    recovery.outcomes = [outcome]
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    assert recovery.client.apply_engine_ft_instructions.call_count == 2


def test_partial_round_failure_waits_for_peer_before_second_round(recovery):
    recovery.outcomes = ["failed", "healthy"]
    calls = 0

    def query(node):
        nonlocal calls
        payload = recovery.query(node)
        if recovery.strategy.context.attempt == 1 and node == recovery.nodes[1]:
            calls += 1
            if calls == 1:
                payload["engines"][0]["engines"][0]["ft_state"] = "recovering"
        return payload

    recovery.client.query_engine_ft_entries.side_effect = _proxy_query(query)
    recovery.strategy.execute(7)
    assert not recovery.strategy.is_failed()
    assert calls == 2
    assert recovery.client.apply_engine_ft_instructions.call_count == 4


@pytest.mark.parametrize("stale_status", ["healthy", "failed"])
def test_stale_round_status_cannot_complete_or_fail_new_round(recovery, stale_status):
    recovery.outcomes = [stale_status]

    def query(node):
        payload = recovery.query(node)
        if recovery.strategy.context.attempt:
            payload["engines"][0]["engines"][0]["last_ft_request_id"] = "old-round"
        return payload

    recovery.client.query_engine_ft_entries.side_effect = _proxy_query(query)
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    assert recovery.client.apply_engine_ft_instructions.call_count == 2


@pytest.mark.parametrize("entry", [{}, {"engines": []}, {"engines": [None]}])
def test_missing_or_malformed_status_cannot_report_success(recovery, entry):
    def query(node):
        if not recovery.strategy.context.attempt:
            return recovery.query(node)
        return {"engines": [{"engine_id": recovery.nodes.index(node), **entry}]}

    recovery.client.query_engine_ft_entries.side_effect = _proxy_query(query)
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()


def test_fast_recovery_requires_pause_ack_before_query_or_apply(recovery):
    recovery.pause.return_value = False
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    recovery.client.query_engine_ft_entries.assert_not_called()
    recovery.client.apply_engine_ft_instructions.assert_not_called()


@pytest.mark.parametrize("role", ["prefill", "decode", "union"])
def test_every_role_pauses_without_motor_resume_switch(recovery, role):
    recovery.instance.role = role
    recovery.strategy.execute(7)
    recovery.pause.assert_called_once_with(recovery.instance)
    assert not recovery.strategy.is_failed()


def test_partial_dispatch_failure_does_not_repeat_an_unknown_execution(recovery):
    recovery.client.apply_engine_ft_instructions.side_effect = [None, RuntimeError("dispatch rejected")]
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    assert recovery.client.apply_engine_ft_instructions.call_count == 2


def test_live_admission_does_not_fall_back_to_stale_fault_reports(recovery):
    recovery.client.query_engine_ft_entries.return_value = {}
    recovery.client.query_engine_ft_entries.side_effect = None
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    recovery.client.apply_engine_ft_instructions.assert_not_called()


def test_fast_recovery_requires_all_registered_endpoints_unhealthy(recovery):
    def query(node):
        payload = recovery.query(node)
        if node == recovery.nodes[1]:
            payload["engines"][0]["engines"][0]["status"] = "healthy"
        return payload

    recovery.client.query_engine_ft_entries.side_effect = _proxy_query(query)
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    recovery.client.apply_engine_ft_instructions.assert_not_called()


def test_ineligible_instance_never_receives_fast_recovery(recovery):
    recovery.instance.status = InsStatus.INACTIVE
    recovery.strategy._recovery_eligible = Mock(return_value=False)
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    recovery.client.apply_engine_ft_instructions.assert_not_called()


def test_native_healthy_push_may_omit_round_metadata(recovery):
    def query(node):
        payload = recovery.query(node)
        if recovery.strategy.context.attempt:
            payload["engines"][0]["engines"][0].pop("last_ft_request_id")
        return payload

    recovery.client.query_engine_ft_entries.side_effect = _proxy_query(query)
    recovery.strategy.execute(7)
    assert not recovery.strategy.is_failed()


def test_admission_rejects_existing_recovery_without_another_collection_window(recovery):
    def query(node):
        result = recovery.query(node)
        result["engines"][0]["engines"][0]["ft_state"] = "recovering"
        return result

    recovery.client.query_engine_ft_entries.side_effect = _proxy_query(query)
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    recovery.client.apply_engine_ft_instructions.assert_not_called()
    assert recovery.client.query_engine_ft_entries.call_count == len(recovery.nodes)


def test_two_local_dp_endpoints_share_one_broadcast_per_recovery_round(recovery):
    """Exercise the deployed topology: two DP endpoints behind one NodeManager."""
    node = recovery.nodes[0]
    recovery.instance.get_node_managers.return_value = [node]
    recovery.instance.get_all_endpoints.return_value = [
        Endpoint(id=rank, ip=node.pod_ip, business_port=str(8000 + rank)) for rank in range(2)
    ]

    def query(_node):
        attempt = recovery.strategy.context.attempt
        engines = []
        for rank in range(2):
            engine = {"id": rank, "status": "healthy" if attempt == 2 else "unhealthy"}
            if attempt:
                engine.update(last_ft_request_id=recovery.strategy.context.request_id)
            if attempt == 1:
                engine.update(ft_state="failed", ft_error="recoverable failure")
            engines.append({"engine_id": rank, "engines": [engine]})
        return {"engines": engines}

    recovery.client.query_engine_ft_entries.side_effect = _proxy_query(query)
    recovery.strategy.execute(7)
    assert recovery.strategy.context.current_state == FastRecoveryState.SUCCESS
    calls = recovery.client.apply_engine_ft_instructions.call_args_list
    assert len(calls) == 2
    first, second = [_command(call) for call in calls]
    assert first["request_id"] != second["request_id"]
    assert "abort" not in first["params"]
    assert second["params"]["abort"] is True
    assert all(call.args[0] == node for call in calls)
    assert all(call.args[1] == [0, 1] for call in calls)


@pytest.mark.parametrize("peer_status", ["healthy", "missing", "stale_failure"])
def test_one_failed_rank_cannot_authorize_second_collective(recovery, peer_status):
    """Only unanimous current-round failure permits another collective."""
    recovery.outcomes = ["failed"]

    def query(node):
        result = recovery.query(node)
        if recovery.strategy.context.attempt and node == recovery.nodes[1]:
            if peer_status == "missing":
                return {"engines": []}
            engine = result["engines"][0]["engines"][0]
            if peer_status == "healthy":
                engine.update(status="healthy", ft_state="idle")
            else:
                engine["last_ft_request_id"] = "previous-round"
        return result

    recovery.client.query_engine_ft_entries.side_effect = _proxy_query(query)
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    assert recovery.client.apply_engine_ft_instructions.call_count == 2


def test_instance_isolation_during_first_round_prevents_second_dispatch(recovery):
    recovery.outcomes = ["failed"]

    def query(node):
        result = recovery.query(node)
        if recovery.strategy.context.attempt:
            recovery.strategy._recovery_eligible = Mock(return_value=False)
        return result

    recovery.client.query_engine_ft_entries.side_effect = _proxy_query(query)
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    assert recovery.client.apply_engine_ft_instructions.call_count == 2


@pytest.mark.parametrize("outcomes", [["healthy"], ["failed", "healthy"]])
def test_success_starts_later_fault_with_fresh_recovery_budget(recovery, outcomes):
    """A completed recovery must not escalate a later independent fault."""
    recovery.outcomes = outcomes
    recovery.strategy.execute(7)
    assert not recovery.strategy.is_failed()
    recovery.client.apply_engine_ft_instructions.reset_mock()

    recovery.outcomes = ["healthy"]
    recovery.strategy = GenericFastRecoveryStrategy()
    recovery.strategy.execute(7)
    assert not recovery.strategy.is_failed()
    assert recovery.strategy.context.attempt == 1
    payloads = [_command(call) for call in recovery.client.apply_engine_ft_instructions.call_args_list]
    assert len(payloads) == 2
    assert all("abort" not in payload["params"] for payload in payloads)


@pytest.mark.parametrize("excluded", ["retired", "headless"])
def test_fast_recovery_ignores_endpoints_outside_active_topology(recovery, excluded):
    if excluded == "retired":
        recovery.runtime.get.return_value = {"dead_committed": [1]}
    else:
        recovery.instance.get_all_endpoints.return_value[1].headless = True
    recovery.strategy.execute(7)
    assert not recovery.strategy.is_failed()
    recovery.client.apply_engine_ft_instructions.assert_called_once()
    assert all(call.args[0] == recovery.nodes[0] for call in recovery.client.query_engine_ft_entries.call_args_list)


def test_fast_recovery_ignores_extra_query_error_but_requires_active_rank(recovery):
    def query(node):
        payload = recovery.query(node)
        payload["engines"].append({"engine_id": 99, "engines": [], "query_error": "retired"})
        return payload

    recovery.client.query_engine_ft_entries.side_effect = _proxy_query(query)
    recovery.strategy.execute(7)
    assert not recovery.strategy.is_failed()


def test_recovery_waits_for_hbm_clearance_after_engines_healthy(recovery):
    recovery.strategy = HbmUceRecoveryStrategy()
    recovery.hbm_faults.side_effect = [["node-a:card-0"], ["node-a:card-0"], []]
    recovery.strategy.execute(7)
    assert recovery.strategy.context.current_state == FastRecoveryState.SUCCESS
    assert recovery.hbm_faults.call_count == 3
    assert recovery.client.apply_engine_ft_instructions.call_count == 2


def test_fast_recovery_does_not_treat_missing_hardware_evidence_as_clearance(recovery):
    recovery.strategy = HbmUceRecoveryStrategy()
    recovery.hbm_faults.side_effect = RuntimeError("Cannot verify HBM UCE clearance")
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    assert "Cannot verify" in recovery.strategy.context.last_error


def test_successful_recovery_checks_clearance_after_all_engines_healthy(recovery):
    recovery.strategy = HbmUceRecoveryStrategy()

    def check_clearance(instance_id):
        assert instance_id == 7
        assert recovery.client.apply_engine_ft_instructions.call_count == len(recovery.nodes)
        assert {(attempt, rank, status) for attempt, rank, status in recovery.polls if attempt} == {
            (1, 0, "healthy"),
            (1, 1, "healthy"),
        }
        return []

    recovery.strategy._pending_hbm_uce_faults.side_effect = check_clearance
    recovery.strategy.execute(7)
    assert recovery.strategy.context.current_state == FastRecoveryState.SUCCESS
    recovery.strategy._pending_hbm_uce_faults.assert_called_once_with(7)


@pytest.mark.parametrize("outcome", ["failed", "dead", "recovering"])
def test_hbm_failure_still_waits_for_clearance_before_relaunch(recovery, outcome):
    recovery.strategy = HbmUceRecoveryStrategy()
    recovery.outcomes = [outcome]
    recovery.hbm_faults.side_effect = [["1801"], []]
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    assert recovery.strategy.failure_fallback == "EngineRelaunchStrategy"
    assert recovery.hbm_faults.call_count == 2
    calls = recovery.client.apply_engine_ft_instructions.call_args_list
    assert len(calls) == 2
    assert all(_command(call)["params"]["abort"] is True for call in calls)


@pytest.mark.parametrize("outcome", ["healthy", "failed"])
def test_hbm_clearance_failure_always_requires_reconfiguration(recovery, outcome):
    recovery.strategy = HbmUceRecoveryStrategy()
    recovery.outcomes = [outcome]
    recovery.hbm_faults.return_value = ["1801"]
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    assert recovery.strategy.failure_fallback == "InstanceReconfigurationStrategy"
    assert "0x80e01801 clearance" in recovery.strategy.context.last_error
    assert recovery.client.apply_engine_ft_instructions.call_count == 2


@pytest.mark.parametrize("policy", [GenericFastRecoveryStrategy, TokenReinferenceStrategy, HbmUceRecoveryStrategy])
def test_late_incompatible_hardware_cannot_complete_policy(recovery, policy):
    recovery.strategy = policy()
    with patch.object(EngineFastRecoveryStrategy, "_policy_evidence_valid", side_effect=[True, False]):
        recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    assert recovery.strategy.failure_fallback == "InstanceReconfigurationStrategy"


@pytest.mark.parametrize(
    "policy,codes,severe,expected",
    [
        (GenericFastRecoveryStrategy, (), False, True),
        (GenericFastRecoveryStrategy, (123,), False, True),
        (GenericFastRecoveryStrategy, (0x08520003,), False, True),
        (TokenReinferenceStrategy, (0x08520003, 123), False, True),
        (HbmUceRecoveryStrategy, (0x80E01801, 0x08520003, 123), False, True),
        (HbmUceRecoveryStrategy, (0x08520003, 123), False, True),
        (GenericFastRecoveryStrategy, (123,), True, False),
        (GenericFastRecoveryStrategy, (0x80E01801,), False, True),
        (TokenReinferenceStrategy, (0x08520003,), False, True),
        (TokenReinferenceStrategy, (0x08520003, 0x80E01801), False, True),
        (HbmUceRecoveryStrategy, (), False, True),
        (HbmUceRecoveryStrategy, (0x80E01801,), True, False),
    ],
)
def test_policy_revalidates_current_hardware_codes(policy, codes, severe, expected):
    with (
        patch.object(EngineFastRecoveryStrategy, "_resolve_config", side_effect=lambda _, default: default),
        patch("motor.controller.fault_tolerance.fault_manager.FaultManager") as manager,
    ):
        manager.return_value.get_recovery_hardware_evidence.return_value = (codes, severe)
        assert policy()._policy_evidence_valid(7) is expected


@pytest.mark.parametrize("policy", [GenericFastRecoveryStrategy, TokenReinferenceStrategy])
@pytest.mark.parametrize("severe", [False, True])
def test_late_hbm_does_not_change_non_hbm_completion_policy(recovery, policy, severe):
    recovery.strategy = policy()
    with (
        patch.object(
            recovery.strategy,
            "_policy_evidence_valid",
            side_effect=lambda instance_id: recovery.policy_evidence_check(recovery.strategy, instance_id),
        ),
        patch("motor.controller.fault_tolerance.fault_manager.FaultManager") as manager,
    ):
        manager.return_value.get_recovery_hardware_evidence.side_effect = [
            ((0x08520003,), False),
            ((0x08520003, 0x80E01801), severe),
        ]
        recovery.strategy.execute(7)
    assert recovery.strategy.is_failed() is severe
    assert recovery.strategy.context.current_state == (
        FastRecoveryState.FAILED if severe else FastRecoveryState.SUCCESS
    )
    assert recovery.client.apply_engine_ft_instructions.call_count == len(recovery.nodes)
    recovery.hbm_faults.assert_not_called()
    if severe:
        assert recovery.strategy.failure_fallback == "InstanceReconfigurationStrategy"


def test_hbm_status_query_exception_still_checks_clearance(recovery):
    recovery.strategy = HbmUceRecoveryStrategy()
    with patch.object(recovery.strategy, "_wait_until_recovered", side_effect=RuntimeError("query failed")):
        recovery.strategy.execute(7)
    recovery.hbm_faults.assert_called_once_with(7)
    assert recovery.strategy.is_failed()
    assert recovery.strategy.failure_fallback == "EngineRelaunchStrategy"


def test_hbm_health_lost_during_clearance_cannot_report_success(recovery):
    recovery.strategy = HbmUceRecoveryStrategy()
    with patch.object(recovery.strategy, "_poll_recovery", side_effect=[("", 0), (None, 1)]):
        recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    assert recovery.strategy.failure_fallback == "EngineRelaunchStrategy"


def test_hbm_partial_dispatch_failure_still_checks_hardware_before_relaunch(recovery):
    recovery.strategy = HbmUceRecoveryStrategy()
    recovery.client.apply_engine_ft_instructions.side_effect = RuntimeError("dispatch rejected")
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    recovery.hbm_faults.assert_called_once_with(7)
    assert recovery.strategy.failure_fallback == "EngineRelaunchStrategy"


def test_common_recovery_cannot_execute_without_a_policy(recovery):
    recovery.strategy = EngineFastRecoveryStrategy()
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    recovery.client.apply_engine_ft_instructions.assert_not_called()


def test_old_recovery_owner_cannot_dispatch_to_new_attempt(recovery):
    recovery.strategy.recovery_id = "old"
    recovery.runtime.get.return_value = {"recovery_id": "new", "dead_committed": []}
    recovery.strategy.execute(7)
    assert recovery.strategy.is_failed()
    recovery.client.apply_engine_ft_instructions.assert_not_called()
