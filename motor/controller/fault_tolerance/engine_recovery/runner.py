# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""One recovery task owns evidence collection, concrete execution and cleanup."""

import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

from motor.common.logger import get_logger
from motor.common.utils.net import format_address
from motor.controller.fault_tolerance.engine_recovery.contracts import (
    FailurePolicy,
    RecoveryAdvice,
    RecoveryContext,
    RecoveryPlan,
    RecoveryResult,
    SoftwareFaultEvidence,
)
from motor.controller.fault_tolerance.engine_recovery.runtime import get_ft_runtime_store
from motor.controller.fault_tolerance.engine_recovery.serving import ServingOverlay
from motor.controller.fault_tolerance.strategy.base import StrategyBase

logger = get_logger(__name__)


def probe_recovery_rank(
    instance: Any,
    expanded_ranks: set[int],
    timeout: float,
    prefer_rank: int | None = None,
) -> None:
    """POST one short completion at a single surviving rank.

    A killed rank's API server just hangs, so the request must land on a
    survivor: its DP step hits the dead peers and the completion comes back
    aborted, which starts native scale-down. Failure here does not abort
    recovery — ``_wait_native_scale_down`` is the gate.

    Exactly one POST: UNHEALTHY ``prefer_rank`` if given, otherwise the
    lowest rank on another Pod than ``expanded_ranks``, else the lowest
    remaining survivor.
    """
    import json as _json
    import urllib.error
    import urllib.request

    endpoints = list(iter_recovery_endpoints(instance))
    skip = {int(rank) for rank in expanded_ranks}
    target = None
    if prefer_rank is not None:
        for ep in endpoints:
            if int(ep.id) == int(prefer_rank):
                target = ep
                break
    if target is None:
        targets = [ep for ep in endpoints if int(ep.id) not in skip]
        if not targets:
            logger.error(
                "survivor probe has no endpoint outside killed ranks %s",
                sorted(skip),
            )
            return
        dead_ips = {ep.ip for ep in endpoints if int(ep.id) in skip}
        other_pod = [ep for ep in targets if ep.ip not in dead_ips]
        target = min(other_pod or targets, key=lambda ep: int(ep.id))
    model_name = getattr(instance, "model_name", None) or "model"
    body = _json.dumps(
        {
            "model": str(model_name),
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": False,
        }
    ).encode()
    url = "http://%s/v1/chat/completions" % format_address(target.ip, target.business_port)
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(0.01, float(timeout))) as resp:
            payload = resp.read()[:300]
            logger.info(
                "survivor probe rank=%s %s -> %s %r",
                target.id,
                url,
                resp.status,
                payload,
            )
    except urllib.error.HTTPError as e:
        logger.info(
            "survivor probe rank=%s %s -> HTTP %s",
            target.id,
            url,
            e.code,
        )
    except Exception as e:
        logger.warning(
            "survivor probe rank=%s %s failed: %s",
            target.id,
            url,
            e,
        )


def iter_recovery_endpoints(instance: Any) -> Iterator[Any]:
    """Yield every endpoint from ``instance.endpoints`` (incl. headless).

    Do not use ``get_all_endpoints()`` here: when ``enable_multi_endpoints``
    is false that helper only returns id==0 per Pod and hides ranks 8–15.
    """
    endpoints = getattr(instance, "endpoints", None)
    if not isinstance(endpoints, dict):
        return
    for pod_endpoints in endpoints.values():
        if not isinstance(pod_endpoints, dict):
            continue
        for endpoint in pod_endpoints.values():
            if getattr(endpoint, "id", None) is None:
                continue
            yield endpoint


class EngineRecoveryStrategy(StrategyBase):
    """Prepare once, then execute a fast-recovery or shrink policy under the same owner."""

    recovery_action = "ENGINE_RECOVERY"
    failure_policy = FailurePolicy.PREPARATION
    requires_recovery_preparation = True

    def __init__(self) -> None:
        super().__init__()
        self.context_provider: Callable[[], RecoveryContext] = self._unbound_context
        self.evidence_provider: Callable[[], SoftwareFaultEvidence] = self._unbound_evidence
        self.plan_provider: Callable[[RecoveryContext], RecoveryPlan] = self._unbound_plan
        self.selected_strategy: StrategyBase | None = None
        self.failure_fallback = ""
        self.handoff_advice = RecoveryAdvice.DEFAULT

    @staticmethod
    def _unbound_context() -> RecoveryContext:
        raise RuntimeError("recovery evidence providers are not bound")

    @staticmethod
    def _unbound_evidence() -> SoftwareFaultEvidence:
        raise RuntimeError("recovery evidence providers are not bound")

    @staticmethod
    def _unbound_plan(_context: RecoveryContext) -> RecoveryPlan:
        raise RuntimeError("recovery planner provider is not bound")

    @property
    def completed_strategy_name(self) -> str:
        if self.selected_strategy is None:
            return self.__class__.__name__
        return self.selected_strategy.completed_strategy_name

    @property
    def recovery_result(self) -> RecoveryResult:
        if self.selected_strategy is not None:
            return self.selected_strategy.recovery_result
        return super().recovery_result.model_copy(update={"advice": self.handoff_advice})

    def stop(self) -> None:
        self.event.set()
        with self._lock:
            if self.selected_strategy is not None:
                self.selected_strategy.stop()

    def evaluate_update(self, context: RecoveryContext) -> None:
        with self._lock:
            if self.selected_strategy is not None:
                self.selected_strategy.evaluate_update(context)

    def execute(self, instance_id: int) -> None:
        from motor.controller.core.instance_manager import InstanceManager

        plan_provider = self.plan_provider
        store = get_ft_runtime_store()
        instance = InstanceManager().get_instance(instance_id)
        if instance is None or not ServingOverlay.pause_for_recovery(instance):
            self.mark_failed()
            return
        probed = False
        ft = self._controller_config.fault_tolerance_config
        collection_deadline = None
        hardware_deadline = None
        while not self.event.is_set():
            context = self.context_provider()
            if context.engine_fault_observed and collection_deadline is None:
                collection_deadline = context.readiness_deadline or (
                    time.monotonic() + ft.fault_collection_timeout_seconds
                )
            if context.hardware_fault_observed and hardware_deadline is None:
                hardware_deadline = context.hardware_correlation_deadline or (
                    time.monotonic() + ft.hardware_ft_correlation_window_sec
                )
            context = replace(
                context,
                readiness_deadline=collection_deadline,
                collection_timed_out=context.collection_timed_out
                or (collection_deadline is not None and time.monotonic() >= collection_deadline),
                hardware_wait_timed_out=context.hardware_wait_timed_out
                or (
                    context.hardware_fault_observed
                    and not context.engine_fault_observed
                    and hardware_deadline is not None
                    and time.monotonic() >= hardware_deadline
                ),
            )
            self.bind_context(context)
            plan = plan_provider(context)
            if plan.strategy is EngineRecoveryStrategy:
                # Expansion's existing temporary inference probe remains in common collection.
                if context.engine_fault_observed and not probed:
                    timeout = ft.recovery_request_timeout_sec
                    if context.readiness_deadline is not None:
                        timeout = self._remaining_timeout(context.readiness_deadline, timeout)
                    retired = set((store.get(instance_id) or {}).get("dead_committed") or [])
                    probe_recovery_rank(instance, set(context.pending_removed_ranks) | retired, timeout)
                    probed = True
                delay = ft.recovery_preparation_poll_interval_sec
                deadlines = [
                    d
                    for d in (context.readiness_deadline, context.hardware_correlation_deadline)
                    if d is not None and d > time.monotonic()
                ]
                if deadlines:
                    delay = min(delay, max(0.0, min(deadlines) - time.monotonic()))
                self.event.wait(delay)
                continue
            if plan.strategy is None:
                if context.engine_fault_observed or context.hardware_fault_observed:
                    self.mark_failed()
                    return
                store.resume_after_fault_clear(instance_id)
                self.consumed_evidence = {}
                if not ServingOverlay.publish(instance, cancelled=self.event.is_set):
                    self.mark_failed()
                return
            if not plan.strategy.runs_under_recovery_owner:
                # HANDOFF leaves evidence and pause intact. FM submits the next native task after return.
                self.failure_fallback = plan.strategy.__name__
                self.handoff_advice = plan.strategy.recovery_target
                self.mark_failed()
                return
            child = plan.strategy()
            child.bind_config(self._controller_config)
            child.bind_context(plan.context)
            child.recovery_id = self.recovery_id
            child.consumed_evidence = self.evidence_provider()
            if child.requires_recovery_preparation:
                child.context_provider = self.context_provider
                child.evidence_provider = self.evidence_provider
            with self._lock:
                self.selected_strategy = child
                if self.event.is_set():
                    child.stop()
            try:
                child.execute(instance_id)
            finally:
                self.bind_context(child.strategy_context)
                self.consumed_evidence = child.consumed_evidence
                self.mutation_started = child.mutation_started
                self.cleanup_failed = child.cleanup_failed
                self.invalidated = child.invalidated
                self.failure_fallback = getattr(child, "failure_fallback", "")
                failed = child.is_failed() or child.invalidated or self.event.is_set()
                if failed:
                    self.mark_failed()
            return
        self.mark_failed()
