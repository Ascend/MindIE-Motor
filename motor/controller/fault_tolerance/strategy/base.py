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
import time
from abc import ABC, abstractmethod
from typing import Any


from motor.controller.fault_tolerance.engine_recovery.contracts import (
    FailurePolicy,
    RecoveryAdvice,
    RecoveryContext,
    SoftwareFaultEvidence,
    RecoveryResult,
    legacy_advice,
)


class StrategyBase(ABC):
    """Strategy base class"""

    recovery_action = "RECOVER"
    failure_policy = FailurePolicy.NATIVE
    recovery_target = RecoveryAdvice.DEFAULT
    protect_relaunch = False
    runs_under_recovery_owner = False
    requires_recovery_preparation = False

    def __init__(self) -> None:
        self.event = threading.Event()
        self.name = self.__class__.__name__
        self._is_finished = False
        self._is_failed = False
        self._lock = threading.Lock()
        self._controller_config: Any = None
        self._strategy_context: RecoveryContext | None = None
        self.recovery_id = ""
        self.mutation_started = False
        self.cleanup_failed = False
        self.invalidated = False
        self.consumed_evidence: SoftwareFaultEvidence = {}

    def bind_config(self, config: Any) -> None:
        """Bind the Controller configuration owned by the strategy orchestrator."""
        self._controller_config = config

    def bind_context(self, context: RecoveryContext | None) -> None:
        """Bind the immutable fault context captured by the strategy orchestrator."""
        self._strategy_context = context

    @property
    def strategy_context(self) -> RecoveryContext | None:
        """Return the immutable context bound by the strategy orchestrator."""
        return self._strategy_context

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

    def mark_finished(self) -> None:
        """Record completion after the managed worker has returned from execution and cleanup."""
        with self._lock:
            self._is_finished = True

    def mark_failed(self) -> None:
        """Mark the strategy run as failed (recovery did not complete).

        Any recovery strategy that finishes without restoring health calls
        this; the strategy center then advances to the next configured fallback
        without repeating the same failed strategy.
        """
        with self._lock:
            self._is_failed = True

    def is_failed(self) -> bool:
        with self._lock:
            return self._is_failed

    @property
    def completed_strategy_name(self) -> str:
        """Concrete policy name used only for logs and history display."""
        return self.__class__.__name__

    @property
    def recovery_result(self) -> RecoveryResult:
        """Expose behavior without requiring the orchestrator to identify the concrete policy."""
        return RecoveryResult(
            failure_policy=self.failure_policy,
            advice=legacy_advice(getattr(self, "failure_fallback", "")),
            evaluate_without_fault=self.failure_policy in {FailurePolicy.PREPARATION, FailurePolicy.FAST_RECOVERY},
            protect_relaunch=self.protect_relaunch,
            scale_up_reconfiguration=self.failure_policy == FailurePolicy.IN_PLACE,
        )

    def evaluate_update(self, context: RecoveryContext) -> None:
        """Policies may cancel on invalidating evidence; native strategies keep their rules."""

    @staticmethod
    def _remaining_timeout(deadline: float, request_timeout: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("recovery deadline exceeded before request dispatch")
        return min(request_timeout, remaining)
