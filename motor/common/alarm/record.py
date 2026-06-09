# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import time
from datetime import datetime, timezone
import os
from pydantic import BaseModel, Field

from motor.common.alarm.enums import Category, ClearCategory, Cleared, EventType, ServiceAffectedType, Severity
from motor.common.logger import get_logger


logger = get_logger(__name__)


def get_utc_time_in_millisec() -> int:
    """Get current UTC timestamp in milliseconds.

    ``time.time()`` already returns the number of seconds since the epoch
    in UTC.  We multiply by 1000 and cast to ``int`` to get
    milliseconds.
    """
    return int(time.time() * 1000)


def get_local_time_in_millisec() -> int:
    """Get current local timestamp in milliseconds.

    The epoch (1970‑01‑01 00:00:00 UTC) is a fixed point, so converting a
    naive ``datetime.now()`` to a timestamp produces the same numeric value
    as ``time.time()``.  This function is kept separate for semantic
    clarity; callers that need the local wall-clock time may use this name.
    """
    # ``datetime.now()`` returns a naive datetime in local time; calling
    # ``timestamp()`` on it interprets it as local and returns seconds since
    # the epoch.  No try/except is needed since this operation cannot fail
    # under normal circumstances.
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class Record(BaseModel):
    """Alarm/event record model for OM (Operation & Maintenance)."""

    category: Category = Field(default=Category.ALARM)
    cleared: Cleared = Field(default=Cleared.NO)
    clear_category: ClearCategory = Field(default=ClearCategory.AUTO)
    occur_utc: int = Field(default=0)
    occur_time: int = Field(default=0)
    native_me_dn: str = Field(default_factory=lambda: os.getenv("SERVICE_ID", "Unknown"))
    origin_system: str = Field(default_factory=lambda: os.getenv("ENGINE_TYPE", "Unknown"))
    origin_system_name: str = Field(default_factory=lambda: os.getenv("ENGINE_TYPE", "Unknown"))
    origin_system_type: str = Field(default_factory=lambda: os.getenv("ENGINE_TYPE", "Unknown"))
    location: str = Field(default="")
    moi: str = Field(default="")
    event_type: EventType = Field(default=EventType.COMMUNICATION)
    alarm_id: str = Field(default="")
    alarm_name: str = Field(default="")
    severity: Severity = Field(default=Severity.CRITICAL)
    probable_cause: str = Field(default="")
    reason_id: int = Field(default=0)
    service_affected_type: ServiceAffectedType = Field(default=ServiceAffectedType.NO)
    additional_information: str = Field(default="")
    instance_id: str = Field(default="")
    p_instance_id: str = Field(default="")

    def update_time(self):
        """Update the occur_utc and occur_time to current time."""
        self.occur_utc = get_utc_time_in_millisec()
        self.occur_time = get_local_time_in_millisec()

    def format(self):
        """Format the record for output, e.g., to a dictionary or JSON."""
        return {
            "category": self.category.value,
            "cleared": self.cleared.value,
            "clearCategory": self.clear_category.value,
            "occurUtc": self.occur_utc,
            "occurTime": self.occur_time,
            "nativeMeDn": self.native_me_dn,
            "originSystem": self.origin_system,
            "originSystemName": self.origin_system_name,
            "originSystemType": self.origin_system_type,
            "location": self.location,
            "moi": self.moi,
            "eventType": self.event_type.value,
            "alarmId": self.alarm_id,
            "alarmName": self.alarm_name,
            "severity": self.severity.value,
            "probableCause": self.probable_cause,
            "reasonId": self.reason_id,
            "serviceAffectedType": self.service_affected_type.value,
            "additionalInformation": f"{self.additional_information}, pod id={self.native_me_dn}",
            "instanceId": self.instance_id,
            "pInstanceId": self.p_instance_id,
        }
