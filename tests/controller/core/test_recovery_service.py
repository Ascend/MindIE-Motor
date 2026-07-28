# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

from unittest.mock import MagicMock, patch

from motor.common.alarm.enums import Category, Cleared
from motor.common.alarm.precision_issue_alarm import PRECISION_ISSUE_ALARM_ID, PrecisionIssueAlarm
from motor.common.alarm.record import Record
from motor.controller.core.recovery_service import (
    complete_precision_pd_group_recovery,
    terminate_instance_for_recovery,
    terminate_pd_group,
)


@patch("motor.controller.core.recovery_service.NodeManagerApiClient")
@patch("motor.controller.core.recovery_service.InstanceManager")
def test_recovery_returns_false_when_instance_missing(mock_im_cls, mock_nm) -> None:
    im = MagicMock()
    mock_im_cls.return_value = im
    im.get_instance.return_value = None
    assert terminate_instance_for_recovery(42, "reason") is False
    im.separate_instance.assert_not_called()
    mock_nm.stop.assert_not_called()


@patch("motor.controller.core.recovery_service.NodeManagerApiClient")
@patch("motor.controller.core.recovery_service.InstanceManager")
def test_recovery_separates_then_stops_node_managers(mock_im_cls, mock_nm) -> None:
    im = MagicMock()
    mock_im_cls.return_value = im
    instance = MagicMock()
    im.get_instance.return_value = instance
    nm = MagicMock()
    instance.get_node_managers.return_value = [nm]
    mock_nm.stop.return_value = True

    assert terminate_instance_for_recovery(7, "probe failed") is True
    im.separate_instance.assert_called_once_with(7)
    mock_nm.stop.assert_called_once_with(nm)


@patch("motor.controller.core.recovery_service.CoordinatorApiClient")
@patch("motor.controller.core.recovery_service.terminate_instance_for_recovery")
def test_complete_recovery_clears_active_alarm_and_notifies_coordinator(
    mock_terminate,
    mock_coord,
) -> None:
    mock_terminate.return_value = True
    mock_coord.notify_precision_alarm_cleared.return_value = True

    alarm = PrecisionIssueAlarm.model_construct(
        alarm_id=PRECISION_ISSUE_ALARM_ID,
        category=Category.ALARM,
        cleared=Cleared.NO,
        instance_id="2",
        p_instance_id="1",
        moi="service name=Coordinator, service ip=1.1.1.1, pId=1, instanceId=2",
    )
    observability = MagicMock()
    observability.alarm_store.find_active_precision_alarm.return_value = alarm

    outcome = complete_precision_pd_group_recovery(
        p_instance_id=1,
        d_instance_id=2,
        source="ccae_manual",
        observability=observability,
        terminate=False,
        report_to_om=True,
    )

    assert outcome.cleared is True
    assert outcome.terminated is True
    assert outcome.moi == alarm.moi
    assert outcome.scheduler_state_cleared is True
    observability.add_alarms.assert_called_once()
    assert len(observability.add_alarms.call_args[0][0]) == 2
    mock_coord.notify_precision_alarm_cleared.assert_called_once_with(1, 2)


@patch("motor.controller.core.recovery_service.CoordinatorApiClient")
def test_complete_recovery_reports_coordinator_clear_failure(mock_coord) -> None:
    mock_coord.notify_precision_alarm_cleared.return_value = False

    alarm = PrecisionIssueAlarm.model_construct(
        alarm_id=PRECISION_ISSUE_ALARM_ID,
        category=Category.ALARM,
        cleared=Cleared.NO,
        instance_id="2",
        p_instance_id="1",
        moi="service name=Coordinator, service ip=1.1.1.1, pId=1, instanceId=2",
    )
    observability = MagicMock()
    observability.alarm_store.find_active_precision_alarm.return_value = alarm

    outcome = complete_precision_pd_group_recovery(
        p_instance_id=1,
        d_instance_id=2,
        source="ccae_manual",
        observability=observability,
        terminate=False,
        report_to_om=True,
    )

    assert outcome.cleared is True
    assert outcome.scheduler_state_cleared is False
    mock_coord.notify_precision_alarm_cleared.assert_called_once_with(1, 2)


@patch("motor.controller.core.recovery_service.terminate_instance_for_recovery")
def test_terminate_pd_group_requires_all_targets_ok(mock_terminate) -> None:
    mock_terminate.side_effect = [True, False]
    assert terminate_pd_group(p_instance_id=1, d_instance_id=2, reason="test") is False
    assert mock_terminate.call_count == 2


@patch("motor.controller.core.recovery_service.CoordinatorApiClient")
def test_complete_recovery_skips_coordinator_when_active_record_not_alarm(mock_coord) -> None:
    observability = MagicMock()
    observability.alarm_store.find_active_precision_alarm.return_value = Record.model_construct(
        alarm_id=PRECISION_ISSUE_ALARM_ID,
        category=Category.ALARM,
        cleared=Cleared.NO,
        instance_id="2",
        p_instance_id="1",
    )

    outcome = complete_precision_pd_group_recovery(
        p_instance_id=1,
        d_instance_id=2,
        source="ccae_manual",
        observability=observability,
        terminate=False,
        report_to_om=True,
    )

    assert outcome.cleared is False
    assert outcome.scheduler_state_cleared is False
    observability.add_alarms.assert_not_called()
    mock_coord.notify_precision_alarm_cleared.assert_not_called()


@patch("motor.controller.core.recovery_service.CoordinatorApiClient")
@patch("motor.controller.core.recovery_service.terminate_instance_for_recovery")
def test_complete_recovery_skips_clear_when_terminate_fails(
    mock_terminate,
    mock_coord,
) -> None:
    mock_terminate.return_value = False
    observability = MagicMock()

    outcome = complete_precision_pd_group_recovery(
        p_instance_id=1,
        d_instance_id=2,
        source="auto_recovery",
        observability=observability,
        terminate=True,
    )

    assert outcome.cleared is False
    assert outcome.terminated is False
    observability.add_alarm.assert_not_called()
    mock_coord.notify_precision_alarm_cleared.assert_not_called()
