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

"""Precision fault detection — sampling controller, checker, reporter wiring."""

from __future__ import annotations

from typing import TYPE_CHECKING

from motor.coordinator.fault_tolerance.precision.checker import (
    CheckResult,
    MsprobeChecker,
    PrecisionChecker,
)
from motor.coordinator.fault_tolerance.precision.reporter import PrecisionReporter
from motor.coordinator.fault_tolerance.precision.sample_controller import (
    DecodeSample,
    PDGroupKey,
    SampleController,
)

if TYPE_CHECKING:
    from typing import Any

    from motor.config.coordinator import CoordinatorConfig, TokenSamplingConfig
    from motor.config.tls_config import TLSConfig
    from motor.coordinator.domain import SchedulingFacade
    from motor.coordinator.domain.request_manager import RequestManager

__all__ = [
    "CheckResult",
    "DecodeSample",
    "MsprobeChecker",
    "PDGroupKey",
    "PrecisionChecker",
    "PrecisionReporter",
    "SampleController",
    "build_precision_reporter",
]


def build_precision_reporter(
    token_sampling_config: TokenSamplingConfig,
    infer_tls_config: TLSConfig,
    *,
    config: CoordinatorConfig,
    scheduler: SchedulingFacade,
    request_manager: RequestManager,
    scheduler_client: Any | None = None,
    checker: PrecisionChecker | None = None,
) -> PrecisionReporter:
    """Wire checker + scheduler-global streak + probe/alarm action.

    Checker selection:
    - ``checker=`` argument: always wins (used by tests).
    - default (production): ``MsprobeChecker``. Importing msprobe happens
      lazily inside ``MsprobeChecker.check`` so unit tests / environments
      without msprobe can still load the package.

    W1B modules (PrecisionAlarm, InternalRouterProbe) are imported lazily
    inside this function so that the package-level import graph stays clean
    when only W1A types are needed by tests.
    """

    from motor.coordinator.fault_tolerance.alarm.precision_alarm import PrecisionAlarm
    from motor.coordinator.fault_tolerance.probe.router_probe import InternalRouterProbe

    del infer_tls_config  # probe uses router path; TLS from config.infer_tls_config
    if checker is not None:
        chk = checker
    else:
        chk = MsprobeChecker()
    probe = InternalRouterProbe(
        config=config,
        scheduler=scheduler,
        request_manager=request_manager,
    )
    action = PrecisionAlarm(
        probe=probe,
        probe_max_attempts=token_sampling_config.probe_max_attempts,
        probe_timeout_seconds=token_sampling_config.probe_timeout_seconds,
    )
    return PrecisionReporter(
        checker=chk,
        action=action,
        threshold=token_sampling_config.precision_issue_threshold,
        scheduler_client=scheduler_client,
    )
