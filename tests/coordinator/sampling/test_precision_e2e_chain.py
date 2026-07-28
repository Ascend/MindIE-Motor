# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""End-to-end verification: alarm payload contract and controller auto-recovery."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from motor.common.alarm.alarm import Alarm
from motor.common.alarm.deserialize import deserialize_incoming_record
from motor.common.alarm.enums import Category, Cleared
from motor.common.alarm.event import Event
from motor.common.alarm.precision_issue_alarm import (
    PRECISION_ISSUE_ALARM_ID,
    build_precision_issue_alarm,
    build_precision_issue_clear_alarm,
    build_precision_moi,
)
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
        assert alarm["alarm_name"] == "Precision Anomaly Alarm"
        assert alarm["native_me_dn"] == "qwen-7b"
        assert "pId=5" in alarm["moi"]
        assert "instanceId=10" in alarm["moi"]

    def test_build_with_none_p(self) -> None:
        alarm = build_precision_issue_alarm(
            p_instance_id=None,
            d_instance_id=10,
            precision_issue_count=3,
            probe_failure_count=1,
        )
        assert alarm["instance_id"] == "10"
        assert alarm["p_instance_id"] == ""
        assert "pId=" not in alarm["moi"]
        assert "instanceId=10" in alarm["moi"]

    def test_clear_reuses_exact_moi(self) -> None:
        moi = build_precision_moi(p_instance_id=1, d_instance_id=2, pod_ip="10.0.0.5")
        clear_alarm = build_precision_issue_clear_alarm(
            p_instance_id=1,
            d_instance_id=2,
            alarm_moi=moi,
        )
        assert clear_alarm["moi"] == moi
        assert clear_alarm["category"] == Category.CLEAR.value
        assert clear_alarm["cleared"] == Cleared.YES.value


class TestDeserializeIncomingRecord:
    def test_precision_alarm_deserializes_to_alarm(self) -> None:
        payload = build_precision_issue_alarm(
            p_instance_id=1,
            d_instance_id=2,
            precision_issue_count=1,
            probe_failure_count=0,
        )
        record = deserialize_incoming_record(payload)
        assert isinstance(record, Alarm)
        assert record.alarm_id == PRECISION_ISSUE_ALARM_ID
        assert record.category is Category.ALARM
        assert record.cleared is Cleared.NO
        assert record.format()["category"] == Category.ALARM.value

    def test_event_stays_event(self) -> None:
        payload = {
            "category": Category.EVENT.value,
            "alarm_id": "x",
            "alarm_name": "evt",
        }
        record = deserialize_incoming_record(payload)
        assert isinstance(record, Event)


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
        from motor.controller.core.recovery_service import PrecisionRecoveryOutcome

        cfg = ControllerConfig(precision_auto_recovery_enabled=True)
        api = ControllerAPI(cfg)
        api.enable_observability_api = True
        payload = build_precision_issue_alarm(
            p_instance_id=7,
            d_instance_id=42,
            precision_issue_count=1,
            probe_failure_count=0,
        )

        with patch(
            "motor.controller.api_server.controller_api.complete_precision_pd_group_recovery",
            return_value=PrecisionRecoveryOutcome(
                terminated=True,
                cleared=True,
                moi="moi",
                scheduler_state_cleared=True,
            ),
        ) as mock_complete:
            resp = await api._add_alarm(_FakeRequest(payload))
            assert resp["data"]["precision_alarm_cleared"] is True
            assert resp["data"]["scheduler_state_cleared"] is True
            mock_complete.assert_called_once()
            kwargs = mock_complete.call_args.kwargs
            assert kwargs["p_instance_id"] == 7
            assert kwargs["d_instance_id"] == 42
            assert kwargs["terminate"] is True
            assert kwargs["source"] == "auto_recovery"

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self) -> None:
        from unittest.mock import MagicMock

        from motor.controller.api_server.controller_api import ControllerAPI

        cfg = ControllerConfig(precision_auto_recovery_enabled=False)
        api = ControllerAPI(cfg)
        api.enable_observability_api = True
        api.observability = MagicMock()
        payload = build_precision_issue_alarm(
            p_instance_id=7,
            d_instance_id=42,
            precision_issue_count=1,
            probe_failure_count=0,
        )

        with patch(
            "motor.controller.api_server.controller_api.complete_precision_pd_group_recovery",
        ) as mock_complete:
            await api._add_alarm(_FakeRequest(payload))
            mock_complete.assert_not_called()
            api.observability.add_alarm.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_clear_category(self) -> None:
        from unittest.mock import MagicMock

        from motor.controller.api_server.controller_api import ControllerAPI

        cfg = ControllerConfig(precision_auto_recovery_enabled=True)
        api = ControllerAPI(cfg)
        api.enable_observability_api = True
        api.observability = MagicMock()
        payload = build_precision_issue_clear_alarm(
            p_instance_id=7,
            d_instance_id=42,
            alarm_moi="service name=Coordinator, service ip=1.1.1.1, pId=7, instanceId=42",
        )

        with patch(
            "motor.controller.api_server.controller_api.complete_precision_pd_group_recovery",
        ) as mock_complete:
            await api._add_alarm(_FakeRequest(payload))
            mock_complete.assert_not_called()
            api.observability.add_alarm.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_non_precision_alarm(self) -> None:
        from unittest.mock import MagicMock

        from motor.controller.api_server.controller_api import ControllerAPI

        cfg = ControllerConfig(precision_auto_recovery_enabled=True)
        api = ControllerAPI(cfg)
        api.enable_observability_api = True
        api.observability = MagicMock()

        with patch(
            "motor.controller.api_server.controller_api.complete_precision_pd_group_recovery",
        ) as mock_complete:
            await api._add_alarm(_FakeRequest({"alarm_id": "OTHER_ALARM", "alarm_name": "other"}))
            mock_complete.assert_not_called()
            api.observability.add_alarm.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_ids_logged_not_crashed(self) -> None:
        from unittest.mock import MagicMock

        from motor.controller.api_server.controller_api import ControllerAPI

        cfg = ControllerConfig(precision_auto_recovery_enabled=True)
        api = ControllerAPI(cfg)
        api.enable_observability_api = True
        api.observability = MagicMock()

        record = Record(
            alarm_id=PRECISION_ISSUE_ALARM_ID,
            alarm_name="precision",
            instance_id="not-an-int",
            p_instance_id="also-not-int",
            category=Category.ALARM,
            cleared=Cleared.NO,
        )

        with patch(
            "motor.controller.api_server.controller_api.complete_precision_pd_group_recovery",
        ) as mock_complete:
            await api._add_alarm(_FakeRequest(record.model_dump(mode="json")))
            mock_complete.assert_not_called()
            api.observability.add_alarm.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_alarm_auto_clears_on_successful_recovery(self) -> None:
        from unittest.mock import MagicMock

        from motor.controller.api_server.controller_api import ControllerAPI
        from motor.controller.core.recovery_service import PrecisionRecoveryOutcome

        cfg = ControllerConfig(precision_auto_recovery_enabled=True)
        api = ControllerAPI(cfg)
        api.enable_observability_api = True
        api.observability = MagicMock()

        payload = build_precision_issue_alarm(
            p_instance_id=1,
            d_instance_id=2,
            precision_issue_count=1,
            probe_failure_count=0,
        )

        with patch(
            "motor.controller.api_server.controller_api.complete_precision_pd_group_recovery",
            return_value=PrecisionRecoveryOutcome(
                terminated=True,
                cleared=True,
                moi="moi",
                scheduler_state_cleared=True,
            ),
        ) as mock_complete:
            resp = await api._add_alarm(_FakeRequest(payload))
            assert resp["data"]["precision_alarm_cleared_by_auto_recovery"] is True
            assert resp["data"]["precision_alarm_cleared"] is True
            assert resp["data"]["scheduler_state_cleared"] is True
            mock_complete.assert_called_once()
            assert mock_complete.call_args.kwargs["report_to_om"] is True

    @pytest.mark.asyncio
    async def test_terminate_instance_can_clear_precision_alarm(self) -> None:
        from motor.controller.api_server.controller_api import ControllerAPI
        from motor.controller.core.recovery_service import PrecisionRecoveryOutcome

        api = ControllerAPI(ControllerConfig())
        api.enable_observability_api = True

        with patch(
            "motor.controller.api_server.controller_api.complete_precision_pd_group_recovery",
            return_value=PrecisionRecoveryOutcome(
                terminated=True,
                cleared=True,
                moi="moi",
                scheduler_state_cleared=True,
            ),
        ) as mock_complete:
            resp = await api._terminate_instance(
                _FakeRequest(
                    {
                        "instance_id": 2,
                        "reason": "ccae_precision_control",
                        "p_instance_id": 1,
                        "precision_alarm_clear": True,
                    }
                )
            )

        assert resp["data"]["precision_alarm_cleared"] is True
        assert resp["data"]["terminated"] is True
        assert resp["data"]["scheduler_state_cleared"] is True
        assert resp["data"]["moi"] == "moi"
        mock_complete.assert_called_once()
        kwargs = mock_complete.call_args.kwargs
        assert kwargs["d_instance_id"] == 2
        assert kwargs["p_instance_id"] == 1
        assert kwargs["source"] == "ccae_manual"
        assert kwargs["terminate"] is True
        assert kwargs["reason"] == "ccae_precision_control"


class _FakeRequest:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self) -> dict:
        return self._body
