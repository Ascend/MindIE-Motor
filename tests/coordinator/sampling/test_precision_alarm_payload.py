# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Verify alarm payload includes both P and D instance IDs."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from motor.config.coordinator import CoordinatorConfig, TokenSamplingConfig
from motor.config.tls_config import TLSConfig
from motor.coordinator.domain import InstanceReadiness
from motor.coordinator.domain.request_manager import RequestManager
from motor.coordinator.fault_tolerance.precision import build_precision_reporter
from motor.coordinator.fault_tolerance.precision.checker import CheckResult, PrecisionChecker
from motor.coordinator.fault_tolerance.precision.sample_controller import DecodeSample
from motor.coordinator.fault_tolerance.probe.chat_probe import ProbeOutcome


class _StubNeverIssueChecker(PrecisionChecker):
    """Returns False always — precision threshold never breached."""

    def __init__(self, results: list[bool] | None = None) -> None:
        self._results = results or [True]

    async def check(self, *args: object, **kwargs: object) -> CheckResult:
        return CheckResult(has_issue=bool(self._results.pop(0)) if self._results else False)


def _build_rep(cfg: TokenSamplingConfig):
    sched = AsyncMock()
    sched.has_required_instances = AsyncMock(return_value=InstanceReadiness.REQUIRED_MET)
    return build_precision_reporter(
        cfg,
        TLSConfig(),
        config=CoordinatorConfig(),
        scheduler=sched,
        request_manager=RequestManager(CoordinatorConfig()),
        checker=_StubNeverIssueChecker(),
    )


def _make_sample(*, p_id: int | None = None, d_id: int = 2, req_id: str = "req") -> DecodeSample:
    return DecodeSample(
        p_instance_id=p_id,
        d_instance_id=d_id,
        prompt_token_ids=[101, 102],
        output_token_ids=[201, 202, 203],
        logprobs=[-0.1, -0.2, -0.3],
        req_id=req_id,
        extra={"d_infer_base_url": "http://127.0.0.1:9", "model": "test-model"},
    )


class TestAlarmPayloadPDInstanceIds:
    @pytest.mark.asyncio
    async def test_alarm_payload_contains_both_p_and_d_instance_ids(self) -> None:
        cfg = TokenSamplingConfig(
            precision_check_enabled=True,
            precision_issue_threshold=1,
            probe_max_attempts=1,
            probe_timeout_seconds=1.0,
        )
        rep = _build_rep(cfg)
        with patch.object(
            rep._action._probe,
            "run",
            new_callable=AsyncMock,
            return_value=ProbeOutcome(failures=0),
        ):
            with patch(
                "motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms"
            ) as mock_report:
                await rep.handle(_make_sample(p_id=1, d_id=2, req_id="r1"))
                for _ in range(100):
                    if mock_report.call_count:
                        break
                    await asyncio.sleep(0.01)

                assert mock_report.call_count == 1
                payload = mock_report.call_args[0][0]
                assert payload["instance_id"] == "2"
                assert payload["p_instance_id"] == "1"
                assert payload["alarm_name"] == "Precision anomaly alarm"
                assert payload["native_me_dn"] == "test-model"
                from motor.common.alarm.precision_issue_alarm import PRECISION_ISSUE_ALARM_ID

                assert PRECISION_ISSUE_ALARM_ID in payload["alarm_id"]

    @pytest.mark.asyncio
    async def test_alarm_payload_p_instance_id_empty_when_none(self) -> None:
        cfg = TokenSamplingConfig(
            precision_check_enabled=True,
            precision_issue_threshold=1,
            probe_max_attempts=1,
            probe_timeout_seconds=1.0,
        )
        rep = _build_rep(cfg)
        with patch.object(
            rep._action._probe,
            "run",
            new_callable=AsyncMock,
            return_value=ProbeOutcome(failures=0),
        ):
            with patch(
                "motor.coordinator.api_client.controller_api_client.ControllerApiClient.report_alarms"
            ) as mock_report:
                await rep.handle(_make_sample(p_id=None, d_id=99, req_id="r2"))
                for _ in range(100):
                    if mock_report.call_count:
                        break
                    await asyncio.sleep(0.01)

                assert mock_report.call_count == 1
                payload = mock_report.call_args[0][0]
                assert payload["instance_id"] == "99"
                assert payload["p_instance_id"] == ""
