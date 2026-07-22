# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of the Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Alarm payload builder for token-sampling precision issues (coordinator -> controller OM)."""

from __future__ import annotations

import os

from pydantic import Field

from motor.common.alarm.alarm import Alarm
from motor.common.alarm.enums import EventType, ServiceAffectedType, Severity

# OM alarm id for precision / probe pipeline (coordinator token sampling).
PRECISION_ISSUE_ALARM_ID = "0xFC001009"


class PrecisionIssueAlarm(Alarm):
    """Precision Anomaly Alarm for Coordinator token-sampling detection.

    Raised when a Decode instance repeatedly produces token-level output
    quality anomalies (repetition, gibberish, rare characters) confirmed
    by msprobe sampling and chat probe.
    """

    event_type: EventType = Field(default=EventType.PROCESSING_ERROR)
    alarm_id: str = Field(default=PRECISION_ISSUE_ALARM_ID)
    alarm_name: str = Field(default="Precision Anomaly Alarm")
    severity: Severity = Field(default=Severity.MAJOR)
    probable_cause: str = Field(default="1:Repeated token-level precision issues detected by sampling")
    service_affected_type: ServiceAffectedType = Field(default=ServiceAffectedType.YES)

    def __init__(
        self,
        *,
        p_instance_id: int | None,
        d_instance_id: int,
        precision_issue_count: int,
        probe_failure_count: int,
        model_id: str = "",
    ):
        super().__init__()
        self.instance_id = str(d_instance_id)
        self.p_instance_id = str(p_instance_id) if p_instance_id is not None else ""
        pod_ip = os.getenv("POD_IP", "")
        location = f"service name=Coordinator, service ip={pod_ip}"
        self.location = location
        self.moi = location
        self.native_me_dn = os.getenv("SERVICE_ID", "").strip() or os.getenv("sys_id", "").strip() or model_id.strip()
        self.additional_information = (
            f"precision_issue_count={precision_issue_count}, "
            f"probe_failure_count={probe_failure_count}, "
            f"p_instance_id={p_instance_id}, d_instance_id={d_instance_id}"
        )
        self.update_time()


def build_precision_issue_alarm(
    *,
    p_instance_id: int | None,
    d_instance_id: int,
    precision_issue_count: int,
    probe_failure_count: int,
    model_id: str = "",
) -> dict:
    """Return a dict suitable for ``ControllerApiClient.report_alarms``.

    The result is a JSON-serializable dict produced by
    ``PrecisionIssueAlarm.model_dump(mode="json")``.
    """
    alarm = PrecisionIssueAlarm(
        p_instance_id=p_instance_id,
        d_instance_id=d_instance_id,
        precision_issue_count=precision_issue_count,
        probe_failure_count=probe_failure_count,
        model_id=model_id,
    )
    return alarm.model_dump(mode="json")
