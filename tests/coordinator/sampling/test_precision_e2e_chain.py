# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""End-to-end verification: alarm payload contract and controller auto-recovery."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from motor.common.alarm.precision_issue_alarm import PRECISION_ISSUE_ALARM_ID, build_precision_issue_alarm
from motor.common.alarm.record import Record
from motor.config.controller import ControllerConfig


class TestBuildPrecisionIssueAlarm:
    def test_build_with_p_and_d(self) -> None:
        alarm = build_precision_issue_alarm(
            p_instance_id=5,
            d_instance_id=10,
            precision_issue_count=3,
            probe_failure_count=1,
            model_id="qwen-7b",
        )
        assert alarm["instance_id"] == "10"
        assert alarm["p_instance_id"] == "5"
        assert alarm["alarm_name"] == "Precision anomaly alarm"
        assert alarm["native_me_dn"] == "qwen-7b"

    def test_build_with_none_p(self) -> None:
        alarm = build_precision_issue_alarm(
            p_instance_id=None,
            d_instance_id=10,
            precision_issue_count=3,
            probe_failure_count=1,
        )
        assert alarm["instance_id"] == "10"
        assert alarm["p_instance_id"] == ""


class TestRecordFormat:
    def test_format_includes_instance_ids(self) -> None:
        record = Record(
            alarm_id=PRECISION_ISSUE_ALARM_ID,
            alarm_name="test",
            instance_id="42",
            p_instance_id="7",
            additional_information="test info",
        )
        fmt = record.format()
        assert fmt["instanceId"] == "42"
        assert fmt["pInstanceId"] == "7"

    def test_format_empty_instance_ids(self) -> None:
        record = Record(
            alarm_id=PRECISION_ISSUE_ALARM_ID,
            alarm_name="test",
            additional_information="test info",
        )
        fmt = record.format()
        assert fmt["instanceId"] == ""
        assert fmt["pInstanceId"] == ""


class TestControllerPrecisionAutoRecovery:
    @pytest.mark.asyncio
    async def test_terminates_both_p_and_d_on_precision_alarm(self) -> None:
        from motor.controller.api_server.controller_api import ControllerAPI

        cfg = ControllerConfig(precision_auto_recovery_enabled=True)
        api = ControllerAPI(cfg)

        record = Record(
            alarm_id=PRECISION_ISSUE_ALARM_ID,
            alarm_name="precision",
            instance_id="42",
            p_instance_id="7",
            additional_information="precision_issue_count=1",
        )

        with patch(
            "motor.controller.api_server.controller_api.terminate_instance_for_recovery", return_value=True
        ) as mock_term:
            await api._maybe_precision_auto_recover(record)

            d_calls = [c for c in mock_term.call_args_list if c[0][0] == 42]
            p_calls = [c for c in mock_term.call_args_list if c[0][0] == 7]
            assert len(d_calls) == 1
            assert len(p_calls) == 1

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self) -> None:
        from motor.controller.api_server.controller_api import ControllerAPI

        cfg = ControllerConfig(precision_auto_recovery_enabled=False)
        api = ControllerAPI(cfg)

        record = Record(
            alarm_id=PRECISION_ISSUE_ALARM_ID,
            alarm_name="precision",
            instance_id="42",
            p_instance_id="7",
        )

        with patch("motor.controller.api_server.controller_api.terminate_instance_for_recovery") as mock_term:
            await api._maybe_precision_auto_recover(record)
            mock_term.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_non_precision_alarm(self) -> None:
        from motor.controller.api_server.controller_api import ControllerAPI

        cfg = ControllerConfig(precision_auto_recovery_enabled=True)
        api = ControllerAPI(cfg)

        record = Record(alarm_id="OTHER_ALARM", alarm_name="other", instance_id="42")

        with patch("motor.controller.api_server.controller_api.terminate_instance_for_recovery") as mock_term:
            await api._maybe_precision_auto_recover(record)
            mock_term.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_ids_logged_not_crashed(self) -> None:
        from motor.controller.api_server.controller_api import ControllerAPI

        cfg = ControllerConfig(precision_auto_recovery_enabled=True)
        api = ControllerAPI(cfg)

        record = Record(
            alarm_id=PRECISION_ISSUE_ALARM_ID,
            alarm_name="precision",
            instance_id="not-an-int",
            p_instance_id="also-not-int",
        )

        with patch("motor.controller.api_server.controller_api.terminate_instance_for_recovery") as mock_term:
            await api._maybe_precision_auto_recover(record)
            mock_term.assert_not_called()
