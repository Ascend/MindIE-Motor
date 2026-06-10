# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import enum

from pydantic import Field

from motor.common.alarm.event import Event
from motor.common.alarm.enums import (
    EventType,
    ServiceAffectedType,
    Severity,
)


class ScaleP2DReason(enum.IntEnum):
    D_INSTANCE_RECOVERED_BY_SCALE_P2D = 1


class ScaleP2DEvent(Event):
    """ScaleP2D recovery event for one Decode instance."""

    event_type: EventType = Field(default=EventType.STATE_CHANGE)
    alarm_id: str = Field(default="0xFC001007")
    alarm_name: str = Field(default="Scale P To D Recovery Event")
    severity: Severity = Field(default=Severity.WARNING)
    probable_cause: str = Field(default="1:D instance recovered by ScaleP2D strategy;")
    service_affected_type: ServiceAffectedType = Field(default=ServiceAffectedType.NO)

    def __init__(
        self,
        reason_id: ScaleP2DReason,
        d_instance_id: int,
        d_instance_job_name: str,
        killed_p_instance_ids: list[int],
    ):
        super().__init__()
        self.reason_id = reason_id.value
        self.update_time()
        p_instance_ids_str = ",".join(map(str, killed_p_instance_ids))
        service_location = (
            f"service name=Controller, d instance id={d_instance_id}, "
            f"d instance job name={d_instance_job_name}, killed p instance ids=[{p_instance_ids_str}]"
        )
        self.instance_id = str(d_instance_id)
        self.location = service_location
        self.moi = service_location
        self.additional_information = service_location
