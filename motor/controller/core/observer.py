# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

from abc import ABC, abstractmethod
from enum import Enum

from motor.common.resources import ReadOnlyInstance


class ObserverEvent(Enum):
    INSTANCE_INITIAL = 0
    INSTANCE_READY = 1
    INSTANCE_SEPARATED = 2
    INSTANCE_REMOVED = 3
    INSTANCE_PAUSED = 4
    INSTANCE_RESUMED = 5


class Observer(ABC):
    def __init__(self) -> None:
        pass

    @abstractmethod
    def update(self, instance: ReadOnlyInstance, event: ObserverEvent) -> None:
        pass
