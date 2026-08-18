# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Tests for the EngineRelaunchStrategy two-phase fallback recovery."""

from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest

from motor.common.resources.instance import InsStatus, NodeManagerInfo
from motor.controller.fault_tolerance.strategy.engine_relaunch import (
    EngineRelaunchStrategy,
    RelaunchState,
)

# pylint: disable=redefined-outer-name


def _make_instance(node_managers, instance_id=1, status=InsStatus.INACTIVE):
    instance = Mock()
    instance.id = instance_id
    instance.status = status
    instance.get_node_managers.return_value = node_managers
    return instance


@pytest.fixture
def nm_a():
    return NodeManagerInfo(pod_ip="10.0.0.1", port="8080")


@pytest.fixture
def nm_b():
    return NodeManagerInfo(pod_ip="10.0.0.2", port="8080")


@contextmanager
def _apply_patch():
    """Mock InstanceManager, the client and the FaultManager config singleton."""
    with (
        patch("motor.controller.core.instance_manager.InstanceManager") as mock_im_cls,
        patch("motor.controller.fault_tolerance.strategy.engine_relaunch.NodeManagerApiClient") as mock_client,
        patch("motor.controller.fault_tolerance.fault_manager.FaultManager") as mock_fm_cls,
    ):
        ft_config = Mock()
        ft_config.engine_relaunch_complete_timeout_sec = 600
        ft_config.engine_relaunch_poll_interval_sec = 0.01
        ft_config.engine_relaunch_dispatch_retries = 3
        ft_config.engine_relaunch_nm_unreachable_threshold = 3
        mock_fm_cls.return_value.config.fault_tolerance_config = ft_config
        mock_client.restart_engine.return_value = True
        yield mock_im_cls, mock_client


def _new_strategy() -> EngineRelaunchStrategy:
    strategy = EngineRelaunchStrategy()
    strategy.engine_relaunch_poll_interval_sec = 0.01
    return strategy


def test_execute_dispatch_and_poll_until_all_normal(nm_a, nm_b):
    """Every NodeManager gets the restart command; polling continues until all recover."""
    instance = _make_instance([nm_a, nm_b])
    with _apply_patch() as (mock_im_cls, mock_client):
        mock_im_cls.return_value.get_instance.return_value = instance
        mock_client.query_status.side_effect = [
            {"status": False},  # probe nm_a
            {"status": False},  # probe nm_b
            {"status": False},  # poll round 1: nm_a loading
            {"status": False},  # poll round 1: nm_b loading
            {"status": True},  # poll round 2: nm_a ready
            {"status": True},  # poll round 2: nm_b ready
        ]

        strategy = _new_strategy()
        strategy.execute(1)

    assert strategy.is_finished()
    assert strategy.context.current_state == RelaunchState.SUCCESS
    restart_calls = [
        c for c in mock_client.restart_engine.call_args_list if c.kwargs.get("action", "restart") == "restart"
    ]
    assert len(restart_calls) == 2
    for call in restart_calls:
        assert call.kwargs["instance_id"] == 1
    assert mock_client.query_status.call_count >= 6


@pytest.mark.parametrize(
    "status_script, expect_dispatched, expect_aborted",
    [
        # probe fails -> nothing dispatched -> Phase 2 aborts it
        ([RuntimeError("unreachable")], False, True),
        # poll fails after dispatch -> already relaunching -> freeze kept, no abort
        ([{"status": False}, RuntimeError("gone")], True, False),
    ],
    ids=["probe_fails", "poll_fails"],
)
def test_execute_unreachable_nm_escalates_to_container_restart(nm_a, status_script, expect_dispatched, expect_aborted):
    """An unreachable NodeManager escalates to Phase 2.

    Phase 2 only aborts the NodeManagers that were NOT dispatched: one that
    already accepted the restart is relaunching in place and keeps its
    suicide freeze (the deadline provides the fallback).
    """
    instance = _make_instance([nm_a])
    with _apply_patch() as (mock_im_cls, mock_client):
        mock_im_cls.return_value.get_instance.return_value = instance
        mock_client.query_status.side_effect = status_script

        strategy = _new_strategy()
        strategy.engine_relaunch_nm_unreachable_threshold = 3
        strategy.execute(1)

    assert strategy.context.current_state == RelaunchState.FAILED
    assert strategy.is_failed()
    restart_calls = [
        c for c in mock_client.restart_engine.call_args_list if c.kwargs.get("action", "restart") == "restart"
    ]
    assert bool(restart_calls) is expect_dispatched
    abort_calls = [c for c in mock_client.restart_engine.call_args_list if c.kwargs.get("action") == "abort"]
    assert bool(abort_calls) is expect_aborted


def test_execute_timeout_escalates_but_keeps_dispatched_freeze(nm_a):
    """Completion timeout escalates to Phase 2, but the dispatched NM keeps its freeze."""
    instance = _make_instance([nm_a])
    with _apply_patch() as (mock_im_cls, mock_client):
        mock_im_cls.return_value.get_instance.return_value = instance
        mock_client.query_status.return_value = {"status": False}

        strategy = _new_strategy()
        strategy.engine_relaunch_complete_timeout_sec = 0.02
        strategy.execute(1)

    assert strategy.context.current_state == RelaunchState.FAILED
    assert strategy.is_failed()
    abort_calls = [c for c in mock_client.restart_engine.call_args_list if c.kwargs.get("action") == "abort"]
    assert len(abort_calls) == 0
    assert strategy.context.dispatch_results.get(nm_a.pod_ip) is True


def test_execute_partial_dispatch_aborts_only_failed_nm(nm_a, nm_b):
    """Phase 2 aborts only the NM whose restart dispatch failed."""
    instance = _make_instance([nm_a, nm_b])
    with _apply_patch() as (mock_im_cls, mock_client):
        mock_im_cls.return_value.get_instance.return_value = instance
        mock_client.query_status.return_value = {"status": False}
        mock_client.restart_engine.side_effect = [True, False]  # nm_a ok, nm_b fails

        strategy = _new_strategy()
        strategy.engine_relaunch_complete_timeout_sec = 0.02
        strategy.engine_relaunch_dispatch_retries = 1
        strategy.execute(1)

    assert strategy.context.current_state == RelaunchState.FAILED
    assert strategy.context.dispatch_results == {nm_a.pod_ip: True, nm_b.pod_ip: False}
    abort_calls = [c for c in mock_client.restart_engine.call_args_list if c.kwargs.get("action") == "abort"]
    assert [c.args[0].pod_ip for c in abort_calls] == [nm_b.pod_ip]


def test_execute_instance_deleted_finishes_without_fallback(nm_a):
    """Instance disappearing mid-relaunch ends the strategy (delete flow takes over)."""
    instance = _make_instance([nm_a])

    def _instances():
        instance.status = InsStatus.DELETED
        return instance

    with _apply_patch() as (mock_im_cls, mock_client):
        mock_im_cls.return_value.get_instance.side_effect = [instance, _instances()]
        mock_client.query_status.return_value = {"status": False}

        strategy = _new_strategy()
        strategy.execute(1)

    assert strategy.context.current_state == RelaunchState.SUCCESS
    assert not strategy.is_failed()


def test_stop_sets_event_and_aborts(nm_a):
    """stop() interrupts the wait and unfreezes the NodeManagers."""
    instance = _make_instance([nm_a])
    with _apply_patch() as (mock_im_cls, mock_client):
        mock_im_cls.return_value.get_instance.return_value = instance
        mock_client.query_status.return_value = {"status": False}

        strategy = _new_strategy()
        strategy.context = Mock(instance_id=1, node_managers=[nm_a])
        strategy.stop()

        import time

        time.sleep(0.2)  # fire-and-forget abort thread
        abort_calls = [c for c in mock_client.restart_engine.call_args_list if c.kwargs.get("action") == "abort"]
        assert len(abort_calls) == 1
