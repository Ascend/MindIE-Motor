#!/usr/bin/env python3
# coding=utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from enum import Enum


class NodeHealthStatus(Enum):
    UNHEALTHY = False
    HEALTHY = True


class NodeRunningStatus(str, Enum):
    NORMAL = "normal"
    ABNORMAL = "abnormal"
    PAUSE = "pause"
    READY = "ready"
    INIT = "init"
    BUSY = "busy"


class ControllerCmd(str, Enum):
    PAUSE_ENGINE = 'PAUSE_ENGINE'
    REINIT_NPU = 'REINIT_NPU'
    START_ENGINE = 'START_ENGINE'
    STOP_ENGINE = 'STOP_ENGINE'
    PAUSE_ENGINE_ROCE = 'PAUSE_ENGINE_ROCE'


class EngineCmd(Enum):
    PAUSE_ENGINE = 0
    REINIT_NPU = 1
    START_ENGINE = 2
    PAUSE_ENGINE_ROCE = 3


class ServiceStatus(Enum):
    SERVICE_READY = 0
    SERVICE_NORMAL = 1
    SERVICE_ABNORMAL = 2
    SERVICE_PAUSE = 3
    SERVICE_INIT = 4
    SERVICE_BUSY = 5


class ControllerReply(Enum):
    SEND_CONTROLLER_ALARM_SUCCESS = 0
    SEND_CONTROLLER_ALARM_UNREACHEABLE = 1
