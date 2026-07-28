# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Deserialize incoming OM payloads into Record / Alarm / Event."""

from __future__ import annotations

from enum import IntEnum
from typing import TypeVar

from pydantic import BaseModel

from motor.common.alarm.alarm import Alarm
from motor.common.alarm.cluster_connection_alarm import ClusterConnectionAlarm
from motor.common.alarm.coordinator_exception_alarm import (
    COORDINATOR_EXCEPTION_ALARM_ID,
    CoordinatorExceptionAlarm,
)
from motor.common.alarm.enums import Category, ClearCategory, Cleared, EventType, ServiceAffectedType, Severity
from motor.common.alarm.event import Event
from motor.common.alarm.instance_exception_alarm import INSTANCE_EXCEPTION_ALARM_ID, InstanceExceptionAlarm
from motor.common.alarm.precision_issue_alarm import PRECISION_ISSUE_ALARM_ID, PrecisionIssueAlarm
from motor.common.alarm.record import Record

TModel = TypeVar("TModel", bound=BaseModel)

_ENUM_FIELDS: dict[str, type[IntEnum]] = {
    "category": Category,
    "cleared": Cleared,
    "clear_category": ClearCategory,
    "event_type": EventType,
    "severity": Severity,
    "service_affected_type": ServiceAffectedType,
}

_ALARM_ID_TO_CLS: dict[str, type[Alarm]] = {
    PRECISION_ISSUE_ALARM_ID: PrecisionIssueAlarm,
    INSTANCE_EXCEPTION_ALARM_ID: InstanceExceptionAlarm,
    COORDINATOR_EXCEPTION_ALARM_ID: CoordinatorExceptionAlarm,
    "0xFC001006": ClusterConnectionAlarm,
}


def _normalize_category(value: object) -> Category | None:
    if isinstance(value, Category):
        return value
    try:
        return Category(int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _construct_model(cls: type[TModel], body: dict) -> TModel:
    """Build a pydantic model without invoking custom ``__init__`` hooks.

    NOTE: ``model_construct`` bypasses all validators and default factories.
    Callers MUST ensure wire payload fields match the model's type annotations.
    For ``PrecisionIssueAlarm`` this means ``instance_id`` and ``p_instance_id``
    must already be strings (as produced by ``model_dump``).
    """
    fields = cls.model_fields.keys()
    payload = {key: body[key] for key in fields if key in body}
    for key, enum_cls in _ENUM_FIELDS.items():
        if key in payload and not isinstance(payload[key], enum_cls):
            try:
                payload[key] = enum_cls(payload[key])
            except (TypeError, ValueError):
                pass
    return cls.model_construct(**payload)


def deserialize_incoming_record(body: dict) -> Record:
    """Restore a wire payload to the appropriate Record subtype."""
    category = _normalize_category(body.get("category"))
    if category in (Category.ALARM, Category.CLEAR):
        alarm_id = str(body.get("alarm_id") or "")
        alarm_cls = _ALARM_ID_TO_CLS.get(alarm_id, Alarm)
        return _construct_model(alarm_cls, body)
    if category == Category.EVENT:
        return _construct_model(Event, body)
    return Record.model_validate(body)
