# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""Engine fast recovery and DP scale-down strategies."""

import concurrent.futures
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from motor.controller.api_client.node_manager_api_client import NodeManagerApiClient
from motor.common.logger import get_logger
from motor.controller.fault_tolerance.dp_scale_down import (
    FtPhase,
    FtRuntime,
    GateSnapshot,
    ScaleDownContext,
    TopologyResolver,
    get_ft_runtime_store,
)
from motor.controller.fault_tolerance.pod_lifecycle import PodLifecycle
from motor.controller.fault_tolerance.serving_overlay import ServingOverlay
from motor.controller.fault_tolerance.strategy.base import StrategyBase

logger = get_logger(__name__)


@dataclass(frozen=True)
class _NodeManagerFtGroup:
    node_manager: Any
    local_endpoint_ids: tuple[int, ...]
    survivor_endpoint_ids: tuple[int, ...]


def _get_instance(instance_id: int):
    from motor.controller.core.instance_manager import InstanceManager

    return InstanceManager().get_instance(instance_id)


class _EngineFtStrategyBase(StrategyBase):
    def _finish(self, failed: bool, error: str | None = None) -> None:
        if failed:
            self.mark_failed()
            if error:
                logger.error("%s failed: %s", self.name, error)
        with self._lock:
            self._is_finished = True

    def stop(self) -> None:
        self.event.set()

    @property
    def ft_config(self) -> Any:
        if self._controller_config is None:
            raise RuntimeError("strategy configuration is not bound")
        return self._controller_config.fault_tolerance_config


class DpScaleDownStrategy(_EngineFtStrategyBase):
    """Apply one cumulative DP rank removal round to every surviving engine."""

    @property
    def scale_down_context(self) -> ScaleDownContext:
        if not isinstance(self._strategy_context, ScaleDownContext):
            raise RuntimeError("scale-down fault context is not bound")
        return self._strategy_context

    def execute(self, instance_id: int) -> None:
        config = self.ft_config
        scale_down_config = config.dp_scale_down_config
        context = self.scale_down_context
        store = get_ft_runtime_store()
        previous_runtime = store.get_or_create(instance_id)
        context_ranks = set(context.pending_removed_ranks)
        if context_ranks and context_ranks.issubset(set(previous_runtime.dead_committed)):
            if previous_runtime.phase == FtPhase.SCALED_DOWN_RUNNING:
                self._finish(False)
                return
            self._finish(True, "duplicate scale-down fault arrived outside scaled-down running state")
            return
        request_id = "scale-down-%s" % uuid.uuid4().hex
        runtime = store.transition(
            instance_id,
            phase=FtPhase.DECIDING,
            request_id=request_id,
            fallback_strategy=context.fallback_strategy,
            last_error=None,
        )

        instance = _get_instance(instance_id)
        if instance is None:
            self._fail(runtime, "instance not found")
            return
        gate = self._gate(config, instance)
        runtime = store.transition(instance_id, gate=gate)
        if not gate.allowed:
            self._fail(runtime, "scale-down gate rejected: %s" % ",".join(gate.reason_codes))
            return
        try:
            original_dp_ranks = TopologyResolver.original_dp_ranks(instance)
        except ValueError as e:
            self._fail(runtime, str(e))
            return
        runtime = store.transition(instance_id, original_dp_ranks=original_dp_ranks)

        pending_candidates = context_ranks
        invalid_candidates = pending_candidates - set(original_dp_ranks)
        if invalid_candidates:
            self._fail(
                runtime,
                "fault context contains invalid DP ranks: %s" % sorted(invalid_candidates),
            )
            return
        dead_ranks = set(runtime.dead_committed) | pending_candidates
        pending = sorted(dead_ranks - set(runtime.dead_committed))
        endpoints = list(instance.get_all_endpoints(include_headless=False))
        survivors = [ep for ep in endpoints if ep.id not in dead_ranks]
        try:
            groups = self._build_node_manager_groups(instance, dead_ranks)
        except RuntimeError as e:
            self._fail(runtime, str(e))
            return
        survivor_groups = [group for group in groups if group.survivor_endpoint_ids]
        runtime = store.transition(instance_id, pending_removed_ranks=pending)
        if not survivors:
            self._fail(runtime, "no surviving rank")
            return
        if not pending:
            self._fail(runtime, "no new removable rank")
            return
        committed = False
        timeout = scale_down_config.request_timeout_sec
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(groups))
        try:
            try:
                self._guard_node_managers(groups, request_id, self._guard_lease_sec(config), timeout, executor)
            except Exception as e:
                self._fail(runtime, "failed to guard NodeManager FT transaction: %s" % e)
                return
            ready_plan = self._wait_until_apply_ready(runtime, instance, dead_ranks, groups, config, executor)
            if ready_plan is None:
                return
            runtime, groups, survivor_groups, survivors = ready_plan
            try:
                withdrawn = ServingOverlay.withdraw(instance)
            except Exception as e:
                self._fail(runtime, "failed to confirm serving withdrawal before scale-down: %s" % e)
                return
            if not withdrawn:
                self._fail(runtime, "failed to confirm serving withdrawal before scale-down")
                return
            runtime = store.transition(instance_id, phase=FtPhase.SCALING_DOWN, serving_published=False)
            # vLLM elects the minimum surviving rank as the new DP master. The
            # DP store port remains a NodeManager-local engine detail.
            new_master = min(survivors, key=lambda ep: ep.id)
            params = {
                "removed_dp_ranks": sorted(dead_ranks),
                "dp_master_ip": new_master.ip,
                "dp_master_rank": new_master.id,
            }
            try:
                self._apply_via_node_managers(survivor_groups, params, request_id, timeout, executor=executor)
            except Exception as e:
                self._fail(runtime, str(e))
                return

            deadline = time.monotonic() + scale_down_config.execution_deadline_sec
            while not self.event.is_set() and time.monotonic() < deadline:
                try:
                    survivor_statuses = self._query_via_node_managers(survivor_groups, timeout, executor)
                except Exception as e:
                    self._fail(runtime, "scale-down status query failed: %s" % e)
                    return
                runtime = store.transition(instance_id, engine_statuses=survivor_statuses)
                outcome, failed_status = self._scale_down_outcome(survivor_statuses)
                if outcome == "failed":
                    self._fail(
                        runtime,
                        self._status_failure("surviving engine reported scale-down failure", failed_status),
                    )
                    return
                if outcome == "succeeded":
                    try:
                        self._finalize_node_managers(
                            groups, dead_ranks, request_id, True, new_master.id, timeout, executor
                        )
                    except Exception as e:
                        self._fail(runtime, "failed to commit NodeManager FT transaction: %s" % e)
                        return
                    committed = True
                    runtime = store.transition(
                        instance_id,
                        dead_committed=sorted(dead_ranks),
                        pending_removed_ranks=[],
                        phase=FtPhase.POST_SCALE_CLEANUP,
                    )
                    if config.enable_dp_scale_up:
                        PodLifecycle.stop_recyclable_pods(
                            instance,
                            TopologyResolver.recyclable_pods(instance, dead_ranks),
                        )
                    store.transition(instance_id, phase=FtPhase.SCALED_DOWN_RUNNING)
                    try:
                        published = ServingOverlay.publish(instance)
                    except Exception as e:
                        published = False
                        publication_error = "ServingOverlay publication pending: %s" % e
                    else:
                        publication_error = None if published else "ServingOverlay publication pending"
                    store.transition(
                        instance_id,
                        serving_published=published,
                        last_error=publication_error,
                    )
                    log_args = (
                        self.name,
                        instance_id,
                        request_id,
                        sorted(dead_ranks),
                        sorted(endpoint.id for endpoint in survivors),
                        new_master.id,
                    )
                    if published:
                        logger.info(
                            "%s succeeded: instance_id=%d, request_id=%s, removed_dp_ranks=%s, "
                            "surviving_dp_ranks=%s, new_master_rank=%d",
                            *log_args,
                        )
                    else:
                        logger.warning(
                            "%s committed but serving publication is pending: instance_id=%d, request_id=%s, "
                            "removed_dp_ranks=%s, surviving_dp_ranks=%s, new_master_rank=%d",
                            *log_args,
                        )
                    self._finish(False)
                    return
                self.event.wait(scale_down_config.poll_interval_sec)
            self._fail(runtime, "DP scale-down recovery deadline exceeded")
        finally:
            try:
                if not committed:
                    self._finalize_node_managers(groups, dead_ranks, request_id, False, None, timeout, executor)
            finally:
                executor.shutdown(wait=True)

    def _wait_until_apply_ready(
        self,
        runtime: FtRuntime,
        instance: Any,
        dead_ranks: set[int],
        groups: list[_NodeManagerFtGroup],
        config: Any,
        executor: concurrent.futures.ThreadPoolExecutor,
    ) -> (
        tuple[
            FtRuntime,
            list[_NodeManagerFtGroup],
            list[_NodeManagerFtGroup],
            list[Any],
        ]
        | None
    ):
        store = get_ft_runtime_store()
        scale_down_config = config.dp_scale_down_config
        deadline = time.monotonic() + scale_down_config.execution_deadline_sec
        while not self.event.is_set() and time.monotonic() < deadline:
            survivor_groups = [group for group in groups if group.survivor_endpoint_ids]
            try:
                statuses = self._query_via_node_managers(
                    survivor_groups, scale_down_config.request_timeout_sec, executor
                )
            except Exception as e:
                self._fail(runtime, "pre-scale-down status query failed: %s" % e)
                return None
            runtime = store.transition(runtime.instance_id, engine_statuses=statuses)
            newly_dead = {
                rank for rank, status in statuses.items() if status.get("status") == "dead" and rank not in dead_ranks
            }
            if newly_dead:
                dead_ranks.update(newly_dead)
                runtime = store.transition(
                    runtime.instance_id,
                    pending_removed_ranks=sorted(dead_ranks - set(runtime.dead_committed)),
                )
            survivors = [
                endpoint
                for endpoint in instance.get_all_endpoints(include_headless=False)
                if endpoint.id not in dead_ranks
            ]
            if not survivors:
                self._fail(runtime, "no surviving rank")
                return None
            if newly_dead:
                try:
                    groups = self._build_node_manager_groups(instance, dead_ranks)
                except RuntimeError as e:
                    self._fail(runtime, str(e))
                    return None
                survivor_groups = [group for group in groups if group.survivor_endpoint_ids]
                logger.info(
                    "Instance %d adds newly DEAD ranks %s before scale-down apply",
                    runtime.instance_id,
                    sorted(newly_dead),
                )
            survivor_statuses = {rank: status for rank, status in statuses.items() if rank not in dead_ranks}
            readiness, failed_status = self._apply_readiness(survivor_statuses)
            if readiness == "failed":
                self._fail(
                    runtime,
                    self._status_failure("surviving engine is not eligible for scale-down", failed_status),
                )
                return None
            # A failed collective makes this a domain-level fault.  vLLM
            # accepts FT commands only while the engine is unhealthy, so all
            # surviving ranks must first observe the fault.
            if readiness == "ready":
                return runtime, groups, survivor_groups, survivors
            runtime = store.transition(runtime.instance_id, phase=FtPhase.WAITING_ENGINE_FAULT)
            self.event.wait(scale_down_config.poll_interval_sec)
        self._fail(runtime, "surviving engine did not become apply-ready before deadline")
        return None

    @staticmethod
    def _build_node_manager_groups(instance, dead_ranks: set[int]) -> list[_NodeManagerFtGroup]:
        managers = instance.get_node_managers()
        if not managers:
            raise RuntimeError("instance has no NodeManager topology")
        manager_by_ip = {node_manager.pod_ip: node_manager for node_manager in managers}
        if len(manager_by_ip) != len(managers):
            raise RuntimeError("instance contains duplicate NodeManager addresses")
        local_ids = {pod_ip: set() for pod_ip in manager_by_ip}
        survivor_ids = {pod_ip: set() for pod_ip in manager_by_ip}
        for endpoint in instance.get_all_endpoints(include_headless=True):
            if endpoint.ip not in local_ids:
                raise RuntimeError("no NodeManager found for endpoint rank %s at %s" % (endpoint.id, endpoint.ip))
            local_ids[endpoint.ip].add(endpoint.id)
        for endpoint in instance.get_all_endpoints(include_headless=False):
            if endpoint.id not in dead_ranks:
                survivor_ids[endpoint.ip].add(endpoint.id)
        empty_managers = sorted(pod_ip for pod_ip, endpoint_ids in local_ids.items() if not endpoint_ids)
        if empty_managers:
            raise RuntimeError("NodeManager topology has no local endpoints: %s" % empty_managers)
        return [
            _NodeManagerFtGroup(
                node_manager=manager_by_ip[pod_ip],
                local_endpoint_ids=tuple(sorted(local_ids[pod_ip])),
                survivor_endpoint_ids=tuple(sorted(survivor_ids[pod_ip])),
            )
            for pod_ip in manager_by_ip
        ]

    @staticmethod
    def _query_via_node_managers(groups, timeout, executor=None):
        result = {}
        expected_ids = set()
        for group in groups:
            expected_ids.update(group.survivor_endpoint_ids)
        requests = DpScaleDownStrategy._fanout_node_managers(
            groups,
            lambda group: NodeManagerApiClient.query_engine_ft_entries(
                group.node_manager,
                list(group.survivor_endpoint_ids),
                timeout,
            ),
            executor,
        )
        for _, future in requests:
            result.update(future.result())
        if set(result) != expected_ids:
            raise RuntimeError(
                "NodeManager FT status set mismatch: expected=%s, actual=%s" % (sorted(expected_ids), sorted(result))
            )
        return result

    @staticmethod
    def _apply_via_node_managers(groups, params, request_id, timeout, instruction="scale_down", executor=None):
        requests = DpScaleDownStrategy._fanout_node_managers(
            groups,
            lambda group: NodeManagerApiClient.apply_engine_ft_instructions(
                group.node_manager,
                list(group.survivor_endpoint_ids),
                instruction,
                params,
                request_id,
                timeout,
            ),
            executor,
        )
        for _, future in requests:
            future.result()

    @staticmethod
    def _guard_node_managers(groups, request_id, lease_sec, timeout, executor=None):
        requests = DpScaleDownStrategy._fanout_node_managers(
            groups,
            lambda group: NodeManagerApiClient.guard_engine_ft(
                group.node_manager,
                request_id,
                lease_sec,
                timeout,
            ),
            executor,
        )
        for _, future in requests:
            future.result()

    @staticmethod
    def _finalize_node_managers(groups, dead_ranks, request_id, commit, dp_master_rank, timeout, executor=None):
        first_error = None
        requests = DpScaleDownStrategy._fanout_node_managers(
            groups,
            lambda group: NodeManagerApiClient.finalize_engine_ft(
                group.node_manager,
                request_id,
                [endpoint_id for endpoint_id in group.local_endpoint_ids if endpoint_id in dead_ranks],
                commit,
                dp_master_rank,
                timeout,
            ),
            executor,
        )
        for group, future in requests:
            try:
                future.result()
            except Exception as error:
                logger.error(
                    "Failed to %s NodeManager FT transaction at %s: %s",
                    "commit" if commit else "abort",
                    group.node_manager.pod_ip,
                    error,
                )
                if first_error is None:
                    first_error = error
        if first_error is not None and commit:
            raise first_error

    @staticmethod
    def _fanout_node_managers(
        groups: list[_NodeManagerFtGroup],
        operation: Callable[[_NodeManagerFtGroup], Any],
        executor: concurrent.futures.ThreadPoolExecutor | None = None,
    ) -> list[tuple[_NodeManagerFtGroup, concurrent.futures.Future]]:
        owns_executor = executor is None
        if executor is None:
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(groups))
        try:
            requests = [(group, executor.submit(operation, group)) for group in groups]
            concurrent.futures.wait(future for _, future in requests)
        finally:
            if owns_executor:
                executor.shutdown(wait=True)
        return requests

    @staticmethod
    def _guard_lease_sec(config) -> float:
        scale_down_config = config.dp_scale_down_config
        return (
            2 * scale_down_config.execution_deadline_sec
            + 4 * scale_down_config.request_timeout_sec
            + max(5.0, scale_down_config.poll_interval_sec)
        )

    @staticmethod
    def _apply_readiness(statuses: dict[int, dict]) -> tuple[str, dict | None]:
        """Classify whether survivors can accept an external scale-down."""
        return DpScaleDownStrategy._classify_statuses(
            statuses,
            success_states={("unhealthy", None)},
            waiting_states={("healthy", None), ("unhealthy", "recovering")},
            success_outcome="ready",
        )

    @staticmethod
    def _scale_down_outcome(statuses: dict[int, dict]) -> tuple[str, dict | None]:
        """Classify the asynchronous scale-down result from survivors."""
        return DpScaleDownStrategy._classify_statuses(
            statuses,
            success_states={("healthy", None)},
            waiting_states={("unhealthy", None), ("unhealthy", "recovering")},
            success_outcome="succeeded",
        )

    @staticmethod
    def _classify_statuses(
        statuses: dict[int, dict],
        success_states: set[tuple[str, str | None]],
        waiting_states: set[tuple[str, str | None]],
        success_outcome: str,
    ) -> tuple[str, dict | None]:
        if not statuses:
            return "failed", {"status": "missing"}
        waiting = False
        for status in statuses.values():
            state = (status.get("status"), status.get("ft_state"))
            if state in success_states:
                continue
            if state in waiting_states:
                waiting = True
                continue
            return "failed", status
        return ("waiting", None) if waiting else (success_outcome, None)

    @staticmethod
    def _status_failure(message: str, status: dict | None) -> str:
        if status is None:
            return message
        ft_error = status.get("ft_error")
        if ft_error:
            return "%s: %s" % (message, ft_error)
        return "%s: status=%s, ft_state=%s" % (
            message,
            status.get("status"),
            status.get("ft_state"),
        )

    @staticmethod
    def _gate(config, instance) -> GateSnapshot:
        reasons = []
        if not config.enable_dp_scale_down:
            reasons.append("FEATURE_DISABLED")
        capability = instance.ft_capability
        if not capability.enabled:
            reasons.append("ENGINE_FT_REQUIRED")
        if not capability.external_lb:
            reasons.append("EXTERNAL_LB_REQUIRED")
        if capability.auto_recovery:
            reasons.append("AUTO_RECOVERY_CONFLICT")
        if not capability.scale_down_supported:
            reasons.append("ENGINE_SCALE_DOWN_UNSUPPORTED")
        if not capability.eplb_enabled:
            reasons.append("EPLB_REQUIRED")
        if capability.num_redundant_experts <= 0:
            reasons.append("EXPERT_REDUNDANCY_REQUIRED")
        return GateSnapshot(allowed=not reasons, reason_codes=reasons)

    def _fail(self, runtime: FtRuntime, error: str) -> None:
        get_ft_runtime_store().transition(
            runtime.instance_id,
            phase=FtPhase.RECONFIGURING,
            reconfiguration_dispatched=False,
            last_error=error,
        )
        self._finish(True, error)
