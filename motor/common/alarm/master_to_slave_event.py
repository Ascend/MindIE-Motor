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
import os
from dataclasses import dataclass

from pydantic import Field

from motor.common.alarm.enums import (
    EventType,
    ServiceAffectedType,
    Severity,
)
from motor.common.alarm.event import Event


class MasterToSlaveComponent(enum.Enum):
    CONTROLLER = "Controller"
    COORDINATOR = "Coordinator"


class MasterToSlaveReason(enum.IntEnum):
    MASTER_COMPONENT_EXCEPTION = 1


@dataclass(frozen=True)
class MasterToSlaveEventConfig:
    alarm_id: str
    alarm_name: str
    probable_cause: str


MASTER_TO_SLAVE_EVENT_CONFIGS = {
    MasterToSlaveComponent.CONTROLLER: MasterToSlaveEventConfig(
        alarm_id="0xFC001000",
        alarm_name="Controller Master To Slave Alarm",
        probable_cause="1:failures caused the original main controller to malfunction",
    ),
    MasterToSlaveComponent.COORDINATOR: MasterToSlaveEventConfig(
        alarm_id="0xFC001008",
        alarm_name="Coordinator Master To Slave Alarm",
        probable_cause="1:failures caused the original main coordinator to malfunction",
    ),
}


class MasterToSlaveEvent(Event):
    """Master to slave event for controller and coordinator."""

    event_type: EventType = Field(default=EventType.STATE_CHANGE)
    severity: Severity = Field(default=Severity.WARNING)
    service_affected_type: ServiceAffectedType = Field(default=ServiceAffectedType.NO)

    def __init__(self, component: MasterToSlaveComponent, reason_id: MasterToSlaveReason):
        super().__init__()
        event_config = MASTER_TO_SLAVE_EVENT_CONFIGS[component]
        self.alarm_id = event_config.alarm_id
        self.alarm_name = event_config.alarm_name
        self.probable_cause = event_config.probable_cause
        self.reason_id = reason_id.value
        self.update_time()

        pod_ip = os.getenv("POD_IP", "")
        service_location = f"service name={component.value}, service ip={pod_ip}"
        self.location = service_location
        self.moi = service_location
        self.additional_information = service_location
