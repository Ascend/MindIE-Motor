# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import pytest

from motor.common.alarm import precision_issue_alarm as precision_module
from motor.common.alarm import record as record_module


ALARM_ENVIRONMENT_VARIABLES = (
    "ENGINE_TYPE",
    "MINDX_SERVER_IP",
    "POD_IP",
    "SERVICE_ID",
    "sys_id",
)

EXPECTED_FORMAT_KEYS = frozenset(
    {
        "category",
        "cleared",
        "clearCategory",
        "occurUtc",
        "occurTime",
        "nativeMeDn",
        "originSystem",
        "originSystemName",
        "originSystemType",
        "location",
        "moi",
        "eventType",
        "alarmId",
        "alarmName",
        "severity",
        "probableCause",
        "reasonId",
        "serviceAffectedType",
        "additionalInformation",
        "instanceId",
        "pInstanceId",
    }
)


@pytest.fixture(autouse=True)
def isolate_alarm_environment(monkeypatch):
    """Keep process environment from leaking into alarm records."""
    for variable in ALARM_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(precision_module, "_POD_IP", None)


@pytest.fixture()
def fixed_alarm_time(monkeypatch):
    """Use deterministic UTC and local timestamps without sleeping."""
    timestamps = {"occur_utc": 1_721_473_200_123, "occur_time": 1_721_502_000_456}
    monkeypatch.setattr(record_module, "get_utc_time_in_millisec", lambda: timestamps["occur_utc"])
    monkeypatch.setattr(record_module, "get_local_time_in_millisec", lambda: timestamps["occur_time"])
    return timestamps


@pytest.fixture()
def expected_format_keys():
    """Return the single shared Record.format key contract."""
    return EXPECTED_FORMAT_KEYS
