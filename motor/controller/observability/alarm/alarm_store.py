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
import threading
from motor.common.alarm.record import Record
from motor.common.alarm.instance_exception_alarm import INSTANCE_EXCEPTION_ALARM_ID
from motor.common.alarm.coordinator_exception_alarm import COORDINATOR_EXCEPTION_ALARM_ID
from motor.common.logger import get_logger
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.alarm.enums import Cleared


logger = get_logger(__name__)


class AlarmStore(ThreadSafeSingleton):
    """Alarm manager, using thread-safe singleton pattern"""

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self._dict_lock = threading.Lock()
        self._alarms: dict[str, list] = {os.getenv("NORTH_PLATFORM", "").strip(): []}
        self._recoverable_alarms: dict[str, Record] = {}

    def add_alarm(self, record: Record) -> bool:
        try:
            with self._dict_lock:
                if (
                    record.alarm_id == INSTANCE_EXCEPTION_ALARM_ID 
                    or record.alarm_id == COORDINATOR_EXCEPTION_ALARM_ID
                ):
                    self._handle_instance_exception_alarm(record)
                else:
                    for value in self._alarms.values():
                        value.append(record)
                logger.debug("Current alarms: %s", self._alarms)

            return True

        except Exception as e:
            logger.error("Failed to add alarm to dict: %s", e)
            return False

    def get_alarms(self, source_id: str) -> list[list[dict]]:
        """Get all current alarms"""
        with self._dict_lock:
            result = [record.format() for record in self._alarms.get(source_id, [])]
            self._alarms[source_id] = []  # Clear alarms after fetching
            return [result] if result else []

    def _handle_instance_exception_alarm(self, record: Record) -> None:
        recovery_alarm_key = f"{record.alarm_id}_{record.instance_id}"
        logger.debug(
            "Handling instance exception alarm with key: %s, dict keys: %s, cleared status: %s",
            recovery_alarm_key,
            list(self._recoverable_alarms.keys()),
            record.cleared,
        )
        if record.cleared == Cleared.NO and recovery_alarm_key not in self._recoverable_alarms:
            for value in self._alarms.values():
                value.append(record)
            self._recoverable_alarms[recovery_alarm_key] = record
        if record.cleared == Cleared.YES and recovery_alarm_key in self._recoverable_alarms:
            for value in self._alarms.values():
                value.append(record)
            del self._recoverable_alarms[recovery_alarm_key]
