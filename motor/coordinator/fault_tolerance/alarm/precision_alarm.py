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

from motor.common.alarm.precision_issue_alarm import build_precision_issue_alarm
from motor.common.logger import get_logger
from motor.coordinator.fault_tolerance.alarm.base import AlarmAction, AlarmContext
from motor.coordinator.fault_tolerance.probe.chat_probe import ChatProbe

logger = get_logger(__name__)


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

    async def execute(self, ctx: AlarmContext) -> None:
        extra = ctx.extra or {}
        model = extra.get("model") or ""

        logger.info(
            "PrecisionAlarm: probe+alarm pd_group=(%s,%s) model=%s (router pipeline)",
            ctx.p_instance_id,
            ctx.d_instance_id,
            model,
        )
        outcome = await self._probe.run(
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
            outcome.failures,
        )
        payload = build_precision_issue_alarm(
            p_instance_id=ctx.p_instance_id,
            d_instance_id=ctx.d_instance_id,
            precision_issue_count=ctx.issue_count,
            probe_failure_count=outcome.failures,
            model_id=model,
        )
        logger.info(
            "PrecisionAlarm: reporting alarm_id=%s instance_id=%s p_instance_id=%s",
            payload["alarm_id"],
            payload["instance_id"],
            payload["p_instance_id"],
        )
        from motor.coordinator.api_client.controller_api_client import ControllerApiClient

        ControllerApiClient.report_alarms(payload)
