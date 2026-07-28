# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Precision issue: chat probe then report alarm to controller."""

from __future__ import annotations

from motor.common.alarm.precision_issue_alarm import (
    build_precision_issue_alarm,
    build_precision_issue_clear_alarm,
)
from motor.common.logger import get_logger
from motor.coordinator.fault_tolerance.alarm.base import AlarmAction, AlarmContext, AlarmReportOutcome
from motor.coordinator.fault_tolerance.probe.chat_probe import ChatProbe

logger = get_logger(__name__)


def _precision_cleared_by_controller(outcome: dict) -> bool:
    return bool(outcome.get("precision_alarm_cleared") or outcome.get("precision_alarm_cleared_by_auto_recovery"))


class PrecisionAlarm(AlarmAction):
    def __init__(
        self,
        probe: ChatProbe,
        *,
        probe_max_attempts: int,
        probe_timeout_seconds: float,
    ) -> None:
        self._probe = probe
        self._probe_max_attempts = probe_max_attempts
        self._probe_timeout_seconds = probe_timeout_seconds

    async def execute(self, ctx: AlarmContext) -> AlarmReportOutcome:
        extra = ctx.extra or {}
        model = extra.get("model") or ""

        if ctx.action == "clear":
            if not ctx.alarm_moi:
                logger.error(
                    "PrecisionAlarm: clear requested without alarm_moi pd_group=(%s,%s)",
                    ctx.p_instance_id,
                    ctx.d_instance_id,
                )
                return AlarmReportOutcome(success=False)
            payload = build_precision_issue_clear_alarm(
                p_instance_id=ctx.p_instance_id,
                d_instance_id=ctx.d_instance_id,
                alarm_moi=ctx.alarm_moi,
                model_id=model,
            )
            logger.info(
                "PrecisionAlarm: reporting CLEAR alarm_id=%s moi=%s",
                payload.get("alarm_id"),
                payload.get("moi"),
            )
            from motor.coordinator.api_client.controller_api_client import ControllerApiClient

            outcome = ControllerApiClient.report_alarms(payload)
            return AlarmReportOutcome(
                success=outcome.get("ok", False),
                moi=str(payload.get("moi") or ctx.alarm_moi),
                auto_recovery_cleared=_precision_cleared_by_controller(outcome),
            )

        logger.info(
            "PrecisionAlarm: probe+alarm pd_group=(%s,%s) model=%s (router pipeline)",
            ctx.p_instance_id,
            ctx.d_instance_id,
            model,
        )
        outcome_probe = await self._probe.run(
            p_instance_id=ctx.p_instance_id,
            d_instance_id=ctx.d_instance_id,
            model=model,
            max_attempts=self._probe_max_attempts,
            timeout_seconds=self._probe_timeout_seconds,
        )
        logger.info(
            "PrecisionAlarm: probe done pd_group=(%s,%s) failures=%s",
            ctx.p_instance_id,
            ctx.d_instance_id,
            outcome_probe.failures,
        )
        payload = build_precision_issue_alarm(
            p_instance_id=ctx.p_instance_id,
            d_instance_id=ctx.d_instance_id,
            precision_issue_count=ctx.issue_count,
            probe_failure_count=outcome_probe.failures,
            model_id=model,
        )
        logger.info(
            "PrecisionAlarm: reporting alarm_id=%s instance_id=%s p_instance_id=%s moi=%s",
            payload["alarm_id"],
            payload["instance_id"],
            payload["p_instance_id"],
            payload.get("moi"),
        )
        from motor.coordinator.api_client.controller_api_client import ControllerApiClient

        outcome = ControllerApiClient.report_alarms(payload)
        return AlarmReportOutcome(
            success=outcome.get("ok", False),
            moi=str(payload.get("moi") or ""),
            auto_recovery_cleared=_precision_cleared_by_controller(outcome),
        )
