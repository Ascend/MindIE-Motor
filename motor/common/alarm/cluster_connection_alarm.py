# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import os
import enum

from pydantic import Field

from motor.common.alarm.alarm import Alarm
from motor.common.alarm.enums import (
    EventType,
    ServiceAffectedType,
    Severity,
)


class ClusterConnectionReason(enum.IntEnum):
    REGISTER_FAILED = 1
    FAULT_SUBSCRIBE_FAILED = 3
    CONNECTION_INTERRUPTED = 4


class ClusterConnectionAlarm(Alarm):
    """Cluster connection alarm."""

    event_type: EventType = Field(default=EventType.STATE_CHANGE)
    alarm_id: str = Field(default="0xFC001006")
    alarm_name: str = Field(default="Cluster Connection Exception Alarm")
    severity: Severity = Field(default=Severity.CRITICAL)
    probable_cause: str = Field(
        default=(
            "1: Cluster service connection failed; 3: Subscription to fault messages failed; 4: Connection interrupted"
        )
    )
    service_affected_type: ServiceAffectedType = Field(default=ServiceAffectedType.YES)

    def __init__(self, reason_id: ClusterConnectionReason, is_clear: bool = False):
        super().__init__()
        self.reason_id = reason_id.value
        if is_clear:
            self.clear()
        self.update_time()
        pod_ip = os.getenv("POD_IP", "")
        cluster_ip = os.getenv("MINDX_SERVER_IP", "")
        service_location = f"service name=Controller, service ip={pod_ip}"
        if cluster_ip:
            service_location += f", cluster ip={cluster_ip}"
        self.location = service_location
        self.moi = service_location
        self.additional_information = service_location
