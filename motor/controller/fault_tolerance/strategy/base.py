# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Strategy base class shared by all fault recovery strategies."""

import threading
from abc import ABC, abstractmethod


class StrategyBase(ABC):
    """Strategy base class"""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.name = self.__class__.__name__
        self._is_finished = False
        self._is_failed = False
        self._lock = threading.Lock()

    @abstractmethod
    def execute(self, instance_id: int):
        """
        Execute the strategy with the instance id.
        """
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError

    def is_finished(self) -> bool:
        with self._lock:
            return self._is_finished

    def mark_failed(self) -> None:
        """Mark the strategy run as failed (recovery did not complete).

        Any recovery strategy that finishes without restoring health calls
        this; the strategy center then escalates to the fallback strategy
        (EngineRelaunchStrategy) on the next round.
        """
        with self._lock:
            self._is_failed = True

    def is_failed(self) -> bool:
        with self._lock:
            return self._is_failed
