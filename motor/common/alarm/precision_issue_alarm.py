# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You may use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
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

_POD_IP: str | None = None


def _get_pod_ip() -> str:
    global _POD_IP
    if _POD_IP is None:
        _POD_IP = os.getenv("POD_IP", "")
    return _POD_IP


def build_precision_moi(
    *,
    p_instance_id: int | None,
    d_instance_id: int,
    pod_ip: str | None = None,
) -> str:
    """Build stable moi for a PD group (raise and clear must match exactly)."""
    ip = pod_ip if pod_ip is not None else _get_pod_ip()
    base = f"service name=Coordinator, service ip={ip}"
    if p_instance_id is not None:
        return f"{base}, pId={p_instance_id}, instanceId={d_instance_id}"
    return f"{base}, instanceId={d_instance_id}"


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
        alarm_moi: str | None = None,
    ):
        super().__init__()
        self.instance_id = str(d_instance_id)
        self.p_instance_id = str(p_instance_id) if p_instance_id is not None else ""
        pod_ip = os.getenv("POD_IP", "")
        location = f"service name=Coordinator, service ip={pod_ip}"
        self.location = location
        self.moi = (
            alarm_moi
            if alarm_moi is not None
            else build_precision_moi(
                p_instance_id=p_instance_id,
                d_instance_id=d_instance_id,
                pod_ip=pod_ip,
            )
        )
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


def build_precision_issue_clear_alarm(
    *,
    p_instance_id: int | None,
    d_instance_id: int,
    alarm_moi: str,
    precision_issue_count: int = 0,
    probe_failure_count: int = 0,
    model_id: str = "",
) -> dict:
    """Build a CLEAR payload reusing the exact moi from the original raise alarm."""
    alarm = PrecisionIssueAlarm(
        p_instance_id=p_instance_id,
        d_instance_id=d_instance_id,
        precision_issue_count=precision_issue_count,
        probe_failure_count=probe_failure_count,
        model_id=model_id,
        alarm_moi=alarm_moi,
    )
    alarm.clear()
    alarm.update_time()
    return alarm.model_dump(mode="json")
