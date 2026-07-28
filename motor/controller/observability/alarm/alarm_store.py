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
from motor.common.alarm.precision_issue_alarm import PRECISION_ISSUE_ALARM_ID
from motor.common.logger import get_logger
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.alarm.enums import Category, Cleared


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
        self._active_precision_alarms: dict[str, Record] = {}

    def add_alarm(self, record: Record) -> bool:
        return self.add_alarms([record])

    def add_alarms(self, records: list[Record]) -> bool:
        try:
            with self._dict_lock:
                for record in records:
                    if record.alarm_id == PRECISION_ISSUE_ALARM_ID:
                        self._handle_precision_issue_alarm(record)
                    elif record.alarm_id in (INSTANCE_EXCEPTION_ALARM_ID, COORDINATOR_EXCEPTION_ALARM_ID):
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

    def find_active_precision_alarm(
        self,
        p_instance_id: int | None,
        d_instance_id: int,
    ) -> Record | None:
        """Return the active precision raise alarm for a PD group, if any."""
        with self._dict_lock:
            for record in self._active_precision_alarms.values():
                try:
                    d_id = int(record.instance_id) if record.instance_id else None
                except (TypeError, ValueError):
                    continue
                if d_id != d_instance_id:
                    continue
                try:
                    p_id = int(record.p_instance_id) if record.p_instance_id else None
                except (TypeError, ValueError):
                    p_id = None
                if p_instance_id is None and (p_id is None or p_id <= 0):
                    return record
                if p_instance_id is not None and p_id == p_instance_id:
                    return record
        return None

    def _precision_alarm_key(self, record: Record) -> str:
        return record.moi or f"{record.alarm_id}:{record.p_instance_id}:{record.instance_id}"

    def _handle_precision_issue_alarm(self, record: Record) -> None:
        key = self._precision_alarm_key(record)
        logger.debug(
            "Handling precision issue alarm key=%s cleared=%s category=%s",
            key,
            record.cleared,
            record.category,
        )
        if record.cleared == Cleared.NO and record.category == Category.ALARM:
            if key not in self._active_precision_alarms:
                for value in self._alarms.values():
                    value.append(record)
                self._active_precision_alarms[key] = record
            return
        if record.cleared == Cleared.YES or record.category == Category.CLEAR:
            for value in self._alarms.values():
                value.append(record)
            self._active_precision_alarms.pop(key, None)
            return
        for value in self._alarms.values():
            value.append(record)

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
