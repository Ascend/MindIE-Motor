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

from motor.common.alarm.enums import (
    Category,
    ClearCategory,
    Cleared,
    EventType,
    ServiceAffectedType,
    Severity,
)
from motor.common.alarm.record import Record

# OM alarm id for precision / probe pipeline (coordinator token sampling).
PRECISION_ISSUE_ALARM_ID = "0xFC001107"


def build_precision_issue_alarm(
    *,
    p_instance_id: int | None,
    d_instance_id: int,
    precision_issue_count: int,
    probe_failure_count: int,
    model_id: str = "",
) -> dict:
    """Return a dict suitable for ``ControllerApiClient.report_alarms`` / ``Record(**body)``."""
    pod_ip = os.getenv("POD_IP", "")
    location = f"service name=Coordinator, service ip={pod_ip}"
    additional = (
        f"precision_issue_count={precision_issue_count}, "
        f"probe_failure_count={probe_failure_count}, "
        f"p_instance_id={p_instance_id}, d_instance_id={d_instance_id}"
    )
    alarm = Record(
        category=Category.ALARM,
        cleared=Cleared.NO,
        clear_category=ClearCategory.AUTO,
        native_me_dn=os.getenv("SERVICE_ID", "").strip() or os.getenv("sys_id", "").strip() or model_id.strip(),
        location=location,
        moi=location,
        event_type=EventType.PROCESSING_ERROR,
        alarm_id=PRECISION_ISSUE_ALARM_ID,
        alarm_name="Precision anomaly alarm",
        severity=Severity.MAJOR,
        probable_cause="1:Repeated token-level precision issues detected by sampling",
        reason_id=0,
        service_affected_type=ServiceAffectedType.YES,
        additional_information=additional,
        instance_id=str(d_instance_id),
        p_instance_id=str(p_instance_id) if p_instance_id is not None else "",
    )
    alarm.update_time()
    return alarm.model_dump(mode="json")
