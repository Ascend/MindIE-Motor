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
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from motor.controller.api_client.node_manager_api_client import NodeManagerApiClient
from motor.common.logger import get_logger
from motor.common.resources.instance import DEFAULT_ACTIVE_HEARTBEAT_TIMEOUT
from motor.common.utils.net import format_address
from motor.controller.fault_tolerance.engine_recovery.runtime import FtPhase, FtRuntime, get_ft_runtime_store
from motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.context import ScaleDownContext
from motor.controller.fault_tolerance.strategy.engine_ft.dp_scaling.topology import TopologyResolver
from motor.controller.fault_tolerance.pod_lifecycle import PodLifecycle
from motor.controller.fault_tolerance.engine_recovery.serving import ServingOverlay

# Planner loads concrete strategies only inside decision functions.
from motor.controller.fault_tolerance.engine_recovery.planner import (  # pylint: disable=cyclic-import
    scale_down_gate,
)
from motor.controller.fault_tolerance.engine_recovery.contracts import FailurePolicy, SoftwareFaultEvidence
from motor.controller.fault_tolerance.strategy.base import StrategyBase

logger = get_logger(__name__)


def _engine_http_url(ep: Any, path: str) -> str:
    """Build an engine HTTP URL. IPv6 literals are wrapped in brackets."""
    if not path.startswith("/"):
        path = "/%s" % path
    return "http://%s%s" % (format_address(ep.ip, ep.business_port), path)


# External requests already inside the engine are left to finish after serving
# withdrawal and before workers are killed. Killing peers while any DP rank still
# runs a forward leaves survivors' NPUs in an aicore error state.
_USER_RECOVER_INFLIGHT_DRAIN_TIMEOUT_SEC = 300.0
_USER_RECOVER_INFLIGHT_POLL_SEC = 1.0
_USER_RECOVER_INFLIGHT_METRICS_TIMEOUT_SEC = 3.0
# Consecutive all-idle polls required, so a request the Coordinator dispatched
# just before withdrawal cannot slip in between two reads.
_USER_RECOVER_INFLIGHT_IDLE_POLLS = 3
# After the last request finishes, DP ranks keep running dummy forwards until
# the next DP state sync (every 32 steps) ends the wave.
_USER_RECOVER_IDLE_SETTLE_SEC = 10.0
_INFLIGHT_METRICS = ("vllm:num_requests_running", "vllm:num_requests_waiting")
# Consecutive live-healthy survivor polls required before recycling empty shells.
_SURVIVOR_HEALTHY_STABLE_POLLS = 3


@dataclass(frozen=True)
class _NodeManagerFtGroup:
    node_manager: Any
    local_endpoint_ids: tuple[int, ...]
    survivor_endpoint_ids: tuple[int, ...]


@dataclass(frozen=True)
class _ScaleDispatch:
    """In-flight ``/scale_elastic_ep`` POSTs.

    The endpoint only answers once the whole transition has committed, and
    commit is driven by gates that run after the dispatch, so the responses
    must be collected at the very end instead of at send time.
    """

    pool: Any
    futures: dict


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

    recovery_action = "SCALE_DOWN"
    failure_policy = FailurePolicy.IN_PLACE
    runs_under_recovery_owner = True
    requires_recovery_preparation = True

    # One cluster-wide scale-up at a time. The round restores every dead pod
    # of the instance; success is one Coordinator SET of the full topology.
    # A second instance waits. Failure still whole-instance reconfigure.
    _SCALE_UP_LOCK = threading.Lock()

    def __init__(self) -> None:
        super().__init__()
        self.context_provider: Callable[[], ScaleDownContext] | None = None
        self.evidence_provider: Callable[[], SoftwareFaultEvidence] | None = None
        self.readiness_deadline = None
        self._guard_renew_at = 0.0

    @property
    def scale_down_context(self) -> ScaleDownContext:
        if not isinstance(self._strategy_context, ScaleDownContext):
            raise RuntimeError("scale-down fault context is not bound")
        return self._strategy_context

    def evaluate_update(self, context: ScaleDownContext) -> None:
        initial = self.strategy_context
        if not self.mutation_started or self.event.is_set() or not isinstance(initial, ScaleDownContext):
            return
        new_dead = set(context.pending_removed_ranks) - set(initial.pending_removed_ranks)
        new_keys = set(context.fault_keys) - set(initial.fault_keys)
        new_versions = bool(initial.hardware_evidence_versions) and (
            set(context.hardware_evidence_versions) - set(initial.hardware_evidence_versions)
        )
        if new_dead or new_keys or new_versions:
            self.invalidated = True
            self.stop()

    def execute(self, instance_id: int) -> None:
        config = self.ft_config
        scale_down_config = config.dp_scale_down_config
        context = self.scale_down_context
        store = get_ft_runtime_store()
        instance = _get_instance(instance_id)
        if instance is None:
            self._finish(True, "instance not found")
            return
        gate = self._gate(config, instance)
        if not gate.allowed:
            self._finish(True, "scale-down gate rejected: %s" % ",".join(gate.reason_codes))
            return
        self.readiness_deadline = context.readiness_deadline or (
            time.monotonic() + config.fault_collection_timeout_seconds
        )
        previous_runtime = store.get_or_create(instance_id)
        logger.info(
            "Instance %d recovery decision evidence: stage=scale_down_selected, recovery_id=%s, context=%s",
            instance_id,
            self.recovery_id,
            context,
        )
        context_ranks = set(context.pending_removed_ranks)
        if context_ranks and context_ranks.issubset(set(previous_runtime.dead_committed)):
            if previous_runtime.phase == FtPhase.SCALED_DOWN_RUNNING:
                self._finish(False)
                return
            self._finish(True, "duplicate scale-down fault arrived outside scaled-down running state")
            return
        request_id = "scale-down-%s" % uuid.uuid4().hex
        runtime = self._transition(
            instance_id,
            phase=FtPhase.WAITING_ENGINE_FAULT,
            request_id=request_id,
            fallback_strategy=context.fallback_strategy,
            last_error=None,
        )
        try:
            original_dp_ranks = TopologyResolver.original_dp_ranks(instance)
        except ValueError as e:
            self._fail(runtime, str(e))
            return
        runtime = self._transition(instance_id, original_dp_ranks=original_dp_ranks)

        pending_candidates = context_ranks
        invalid_candidates = pending_candidates - set(original_dp_ranks)
        if invalid_candidates:
            self._fail(
                runtime,
                "fault context contains invalid DP ranks: %s" % sorted(invalid_candidates),
            )
            return
        previous_dead = set(runtime.dead_committed)
        dead_ranks = previous_dead | pending_candidates
        pending = sorted(dead_ranks - previous_dead)
        endpoints = list(instance.get_all_endpoints(include_headless=False))
        survivors = [ep for ep in endpoints if ep.id not in dead_ranks]
        try:
            groups = self._build_node_manager_groups(instance, dead_ranks)
        except RuntimeError as e:
            self._fail(runtime, str(e))
            return
        survivor_groups = [group for group in groups if group.survivor_endpoint_ids]
        runtime = self._transition(instance_id, pending_removed_ranks=pending)
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
                self._renew_guard(groups, request_id, config, executor, deadline=self.readiness_deadline)
            except Exception as e:
                self._fail(runtime, "failed to guard NodeManager FT transaction: %s" % e)
                return
            ready_plan = self._wait_until_apply_ready(runtime, instance, dead_ranks, groups, config, executor)
            if ready_plan is None:
                return
            runtime, groups, survivor_groups, survivors = ready_plan
            self.bind_context(replace(self.scale_down_context, pending_removed_ranks=tuple(sorted(dead_ranks))))
            try:
                withdrawn = ServingOverlay.pause_for_recovery(instance)
            except Exception as e:
                self._fail(runtime, "failed to confirm serving withdrawal before scale-down: %s" % e)
                return
            if not withdrawn:
                self._fail(runtime, "failed to confirm serving withdrawal before scale-down")
                return
            # vLLM elects the minimum surviving rank as the new DP master. The
            # DP store port remains a NodeManager-local engine detail and still
            # uses the stored endpoint id. Identity-preserving engines keep
            # original DP ranks, so scale-down must send those stored ids.
            new_master = min(survivors, key=lambda ep: ep.id)
            scale_down_fields = {
                "phase": FtPhase.SCALING_DOWN,
                "serving_published": False,
            }
            if getattr(config, "enable_dp_scale_up", False):
                scale_down_fields["dp_master_rank"] = int(new_master.id)
                scale_down_fields["dp_master_ip"] = str(new_master.ip)
                # Incremental: only ranks newly dead since the last shrink.
                # Survivors keep original ids, so after rank 0 the next
                # shrink must send [1..15].
                removed_for_engine = sorted(int(x) for x in (dead_ranks - previous_dead))
            else:
                removed_for_engine = sorted(int(x) for x in dead_ranks)
            runtime = self._transition(instance_id, **scale_down_fields)
            logger.info(
                "Instance %d: engine scale-down removed_dp_ranks=%s stored_dead=%s",
                instance_id,
                removed_for_engine,
                sorted(dead_ranks),
            )
            params = {
                "removed_dp_ranks": removed_for_engine,
                "dp_master_ip": new_master.ip,
                "dp_master_rank": new_master.id,
            }
            deadline = self._apply_scale_down(runtime, dead_ranks, groups, survivor_groups, params, config, executor)
            if deadline is None:
                return
            completed_runtime = self._wait_scale_down_result(
                runtime, groups, survivor_groups, config, deadline, executor
            )
            if completed_runtime is None:
                return
            runtime = completed_runtime
            if not self._commit_scale_down(runtime, groups, dead_ranks, new_master.id, config, deadline, executor):
                return
            committed = True
            self._after_scale_down_commit(runtime, instance, context, original_dp_ranks, dead_ranks, new_master, config)
        finally:
            try:
                if not committed:
                    try:
                        self._finalize_node_managers(groups, dead_ranks, request_id, False, None, timeout, executor)
                    except Exception as error:
                        self.cleanup_failed = True
                        self._fail(runtime, "NodeManager cleanup failed: %s" % error)
            finally:
                executor.shutdown(wait=True)

    def _apply_scale_down(
        self,
        runtime: FtRuntime,
        dead_ranks: set[int],
        groups: list[_NodeManagerFtGroup],
        survivor_groups: list[_NodeManagerFtGroup],
        params: dict[str, Any],
        config: Any,
        executor: concurrent.futures.ThreadPoolExecutor,
    ) -> float | None:
        """Dispatch apply and return the shared execution deadline, or mark failure."""
        instance_id, request_id = runtime.instance_id, runtime.request_id
        scale_down_config = config.dp_scale_down_config
        timeout = scale_down_config.request_timeout_sec
        if self.event.is_set():
            self._fail(runtime, "scale-down cancelled before apply")
            return None
        if self.evidence_provider is not None:
            self.consumed_evidence = {
                key: observed
                for key, observed in self.evidence_provider().items()
                if observed[1].engine_status != 1 or observed[1].engine_id in dead_ranks
            }
        deadline = time.monotonic() + scale_down_config.execution_deadline_sec
        self._transition(instance_id, stage="applying")
        try:
            self._renew_guard(groups, request_id, config, executor, deadline=deadline)
            if self.event.is_set():
                self._fail(runtime, "scale-down cancelled during pre-apply guard renewal")
                return None
            apply_timeout = self._remaining_timeout(deadline, timeout)
            self.mutation_started = True
            self._apply_via_node_managers(survivor_groups, params, request_id, apply_timeout, executor=executor)
        except Exception as e:
            self._fail(runtime, str(e))
            return None
        return deadline

    def _wait_scale_down_result(
        self,
        runtime: FtRuntime,
        groups: list[_NodeManagerFtGroup],
        survivor_groups: list[_NodeManagerFtGroup],
        config: Any,
        deadline: float,
        executor: concurrent.futures.ThreadPoolExecutor,
    ) -> FtRuntime | None:
        """Poll engine completion under the same owner, guard and deadline."""
        instance_id, request_id = runtime.instance_id, runtime.request_id
        scale_down_config = config.dp_scale_down_config
        timeout = scale_down_config.request_timeout_sec
        self._transition(instance_id, stage="polling")
        while not self.event.is_set() and time.monotonic() < deadline:
            try:
                self._renew_guard(groups, request_id, config, executor, deadline=deadline)
                survivor_statuses = self._query_via_node_managers(
                    survivor_groups, self._remaining_timeout(deadline, timeout), executor
                )
            except Exception as e:
                self._fail(runtime, "scale-down status query failed: %s" % e)
                return None
            # Recheck ownership after the engine query returns.
            runtime = self._transition(instance_id)
            outcome, failed_status = self._scale_down_outcome(
                survivor_statuses,
                wait_for_store_migration=bool(getattr(config, "enable_dp_scale_up", False)),
            )
            if outcome == "failed":
                self._fail(
                    runtime,
                    self._status_failure("surviving engine reported scale-down failure", failed_status),
                )
                return None
            if outcome == "succeeded":
                return runtime
            self.event.wait(min(scale_down_config.poll_interval_sec, max(0.0, deadline - time.monotonic())))
        self._fail(runtime, "DP scale-down recovery deadline exceeded")
        return None

    def _commit_scale_down(
        self,
        runtime: FtRuntime,
        groups: list[_NodeManagerFtGroup],
        dead_ranks: set[int],
        new_master_rank: int,
        config: Any,
        deadline: float,
        executor: concurrent.futures.ThreadPoolExecutor,
    ) -> bool:
        """Commit NodeManagers and confirm cancellation/deadline before accepting it."""
        request_id = runtime.request_id
        timeout = config.dp_scale_down_config.request_timeout_sec
        if self.event.is_set() or time.monotonic() >= deadline:
            self._fail(runtime, "scale-down deadline or cancellation before commit")
            return False
        try:
            self._finalize_node_managers(
                groups,
                dead_ranks,
                request_id,
                True,
                new_master_rank,
                self._remaining_timeout(deadline, timeout),
                executor,
            )
        except Exception as e:
            self._fail(runtime, "failed to commit NodeManager FT transaction: %s" % e)
            return False
        if time.monotonic() > deadline or self.event.is_set():
            self._fail(runtime, "scale-down deadline or cancellation during commit")
            return False
        return True

    def _after_scale_down_commit(
        self,
        runtime: FtRuntime,
        instance: Any,
        context: ScaleDownContext,
        original_dp_ranks: list[int],
        dead_ranks: set[int],
        new_master: Any,
        config: Any,
    ) -> None:
        """Publish committed topology and continue user-requested recycle/scale-up."""
        instance_id, request_id = runtime.instance_id, runtime.request_id
        runtime = self._transition(
            instance_id,
            dead_committed=sorted(dead_ranks),
            pending_removed_ranks=[],
            phase=FtPhase.POST_SCALE_CLEANUP,
        )
        # User recover only: recycle empty shells then scale-up.
        # Fault-triggered scale-down stays scaled-down until user calls recover.
        do_recycle_and_up = getattr(context, "source", "") == "user"
        recyclable = TopologyResolver.recyclable_pods(instance, dead_ranks)
        survivors = set(int(x) for x in original_dp_ranks) - set(int(x) for x in dead_ranks)
        if do_recycle_and_up and recyclable:
            # Do not recycle on the first healthy tick: wait until
            # survivors stay *live*-healthy before stopping empty shells.
            if not self._wait_survivors_healthy_before_recycle(
                instance_id,
                survivors,
                config,
                empty_shell_ips=list(recyclable),
            ):
                self._fail(
                    runtime,
                    "survivors not stably healthy before empty-shell recycle",
                )
                return
            instance = _get_instance(instance_id)
            if instance is None:
                self._fail(runtime, "instance disappeared before recycle")
                return
            PodLifecycle.stop_recyclable_pods(instance, recyclable)
            self._transition(
                instance_id,
                dead_committed=[],
                pending_removed_ranks=[],
            )
        self._transition(instance_id, phase=FtPhase.SCALED_DOWN_RUNNING)
        try:
            published = ServingOverlay.publish(instance, cancelled=self.event.is_set)
        except Exception as e:
            published = False
            publication_error = "ServingOverlay publication pending: %s" % e
        else:
            publication_error = None if published else "ServingOverlay publication pending"
        self._transition(
            instance_id,
            serving_published=published,
            last_error=publication_error,
        )
        surviving_ids = sorted(set(int(x) for x in original_dp_ranks) - set(int(x) for x in dead_ranks))
        log_args = (
            self.name,
            instance_id,
            request_id,
            sorted(dead_ranks),
            surviving_ids,
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
        if do_recycle_and_up and recyclable:
            # WARMING_UP is entered inside _auto_scale_up (blocks reconfig).
            # A failure here is the same as a scale-down failure: mark
            # the strategy failed so the next round reconfigures.
            if not self._auto_scale_up(
                instance_id,
                original_dp_ranks,
                config,
                survivor_rank_ids=survivors,
            ):
                self._fail(runtime, "DP scale-up failed")
                return
        self._transition(instance_id, stage="completed" if published else "publication_pending")
        self._finish(False)

    def _wait_until_apply_ready(
        self,
        runtime: Any,
        instance: Any,
        dead_ranks: set[int],
        groups: list[Any],
        config: Any,
        executor: Any,
    ) -> (
        tuple[
            Any,
            list[Any],
            list[Any],
            list[Any],
        ]
        | None
    ):
        scale_down_config = config.dp_scale_down_config
        deadline = self.readiness_deadline
        self._transition(runtime.instance_id, stage="readiness")
        while not self.event.is_set() and time.monotonic() < deadline:
            if self.context_provider is not None:
                latest = self.context_provider()
                reported_dead = set(latest.pending_removed_ranks)
                if reported_dead - dead_ranks:
                    dead_ranks.update(reported_dead)
                    runtime = self._transition(
                        runtime.instance_id, pending_removed_ranks=sorted(dead_ranks - set(runtime.dead_committed))
                    )
                if latest.hardware_fault_observed and (
                    not latest.hardware_mapping_complete or set(latest.hardware_affected_ranks) - dead_ranks
                ):
                    self.invalidated = True
                    self._fail(runtime, "hardware evidence invalidated scale-down preparation")
                    return None
                self.bind_context(replace(latest, pending_removed_ranks=tuple(sorted(dead_ranks))))
                groups = self._build_node_manager_groups(instance, dead_ranks)
            survivor_groups = [group for group in groups if group.survivor_endpoint_ids]
            try:
                self._renew_guard(groups, runtime.request_id, config, executor, deadline=deadline)
                statuses = self._query_via_node_managers(
                    survivor_groups, self._remaining_timeout(deadline, scale_down_config.request_timeout_sec), executor
                )
            except Exception as e:
                self._fail(runtime, "pre-scale-down status query failed: %s" % e)
                return None
            # Recheck ownership after the engine query returns.
            runtime = self._transition(runtime.instance_id)
            newly_dead = {
                rank for rank, status in statuses.items() if status.get("status") == "dead" and rank not in dead_ranks
            }
            if newly_dead:
                dead_ranks.update(newly_dead)
                runtime = self._transition(
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
            runtime = self._transition(runtime.instance_id, phase=FtPhase.WAITING_ENGINE_FAULT)
            self.event.wait(min(scale_down_config.poll_interval_sec, max(0.0, deadline - time.monotonic())))
        self._fail(runtime, "surviving engine did not become apply-ready before deadline")
        return None

    def _trigger_scale_elastic_ep(
        self,
        instance: Any,
        target_dp_size: int,
        old_rank_ids: set[int],
        timeout: float,
        existing_dp_ranks: list[int] | None = None,
        data_parallel_master_rank: int | None = None,
    ) -> _ScaleDispatch:
        """Fan out ``/scale_elastic_ep`` to every old-rank endpoint (external_scale.py).

        Shrink and expand both require **all** ranks that participate in the
        current (pre-scale) world to receive the POST — not only survivors.
        Ranks being dropped on scale-down must still be included in
        ``old_rank_ids``. Joiners for scale-up are admitted by prepare once
        they start; they are listed in ``old_rank_ids`` only after they are
        registered endpoints.
        """
        import json as _json
        import urllib.request

        registered = {nm.pod_ip for nm in instance.get_node_managers()}
        old_ids = {int(x) for x in old_rank_ids}
        targets = [ep for ep in self._iter_raw_endpoints(instance) if int(ep.id) in old_ids and ep.ip in registered]
        if not targets:
            raise RuntimeError("no old-rank endpoints for scale_elastic_ep (wanted %s)" % sorted(old_ids))
        missing = old_ids - {int(ep.id) for ep in targets}
        if missing:
            logger.warning(
                "scale_elastic_ep: registered endpoints missing old ranks %s",
                sorted(missing),
            )

        payload = {
            "new_data_parallel_size": int(target_dp_size),
            "drain_timeout": int(max(30, min(timeout, 300))),
        }
        if existing_dp_ranks is not None:
            payload["existing_dp_ranks"] = [int(x) for x in existing_dp_ranks]
        if data_parallel_master_rank is not None:
            payload["data_parallel_master_rank"] = int(data_parallel_master_rank)
        body = _json.dumps(payload).encode()

        def _post(ep) -> str:
            url = _engine_http_url(ep, "/scale_elastic_ep")
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=max(180.0, float(timeout))) as resp:
                return f"{ep.id}@{ep.ip}:{ep.business_port} -> {resp.status} {resp.read()[:200]!r}"

        logger.info(
            "DP elastic /scale_elastic_ep new_data_parallel_size=%s existing_dp_ranks=%s "
            "data_parallel_master_rank=%s on old_ranks %s",
            target_dp_size,
            payload.get("existing_dp_ranks"),
            payload.get("data_parallel_master_rank"),
            sorted(ep.id for ep in targets),
        )
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(targets)),
            thread_name_prefix="eep-scale",
        )
        return _ScaleDispatch(pool=pool, futures={pool.submit(_post, ep): ep for ep in targets})

    @staticmethod
    def _scale_dispatch_error(dispatch: _ScaleDispatch) -> str | None:
        """Return the first already-failed POST, so gates can abort early."""
        for fut, ep in dispatch.futures.items():
            if not fut.done():
                continue
            try:
                exc = fut.exception(timeout=0)
            except Exception:
                continue
            if exc is not None:
                return f"{ep.id}@{ep.ip}:{ep.business_port}: {exc}"
        return None

    @staticmethod
    def _drain_scale_elastic_ep(dispatch: _ScaleDispatch, timeout: float) -> None:
        """Collect the deferred ``/scale_elastic_ep`` responses and release the pool."""
        errors = []
        try:
            for fut in concurrent.futures.as_completed(list(dispatch.futures), timeout=max(1.0, float(timeout))):
                ep = dispatch.futures[fut]
                try:
                    logger.info("scale_elastic_ep ok: %s", fut.result())
                except Exception as e:
                    errors.append(f"{ep.id}@{ep.ip}:{ep.business_port}: {e}")
        except Exception as e:
            logger.warning("scale_elastic_ep responses not all collected: %s", e)
        finally:
            dispatch.pool.shutdown(wait=False)
        if errors:
            logger.error("scale_elastic_ep reported errors: %s", " | ".join(errors))

    def _survivor_hard_fault(self, instance: Any, survivor_rank_ids: set[int]) -> str | None:
        """Reason to abort scale-up when a survivor pod has failed.

        Joiners are not inspected. A zero heartbeat timestamp has not been
        observed yet. Engine ``recovering`` is normal during elastic scale and
        is not decided here.
        """
        if instance is None:
            return "instance disappeared"
        survivors = {int(rank) for rank in survivor_rank_ids}
        if not survivors:
            return None
        registered = {nm.pod_ip for nm in instance.get_node_managers()}
        by_id: dict[int, Any] = {}
        for endpoint in self._iter_raw_endpoints(instance):
            if getattr(endpoint, "headless", False):
                continue
            by_id[int(endpoint.id)] = endpoint
        now = time.time()
        for rank in sorted(survivors):
            endpoint = by_id.get(rank)
            if endpoint is None or endpoint.ip not in registered:
                return "survivor rank %s node manager gone" % rank
            if endpoint.hb_timestamp > 0 and not endpoint.is_alive(now, DEFAULT_ACTIVE_HEARTBEAT_TIMEOUT):
                return "survivor rank %s heartbeat timeout" % rank
        return None

    @staticmethod
    def _survivor_engine_failed(statuses: dict, survivor_rank_ids: set[int]) -> str | None:
        """Engine ``failed``/``dead`` on a survivor. ``recovering`` keeps waiting."""
        survivors = {int(rank) for rank in survivor_rank_ids}
        for rank in sorted(survivors):
            status = statuses.get(rank) or {}
            if not isinstance(status, dict):
                continue
            engine_status = str(status.get("status") or "")
            if engine_status in ("failed", "dead"):
                return "survivor rank %s engine status %s" % (rank, engine_status)
            if status.get("ft_state") == "failed":
                return "survivor rank %s ft_state failed" % rank
        return None

    def _roster_ranks_present(self, instance: Any, wanted_ranks: list[int]) -> tuple[bool, list[int], list[int]]:
        """Return (all_present, missing_ranks, not_normal_ranks) from live roster.

        Presence = joiner registered + hb. NORMAL is post-commit / often post-scale.
        ``not_normal`` is logged for diagnostics only.
        """
        from motor.common.resources.endpoint import EndpointStatus

        registered = {nm.pod_ip for nm in instance.get_node_managers()}
        by_id: dict[int, Any] = {}
        for ep in self._iter_raw_endpoints(instance):
            if ep.ip not in registered:
                continue
            by_id[int(ep.id)] = ep
        missing = [r for r in wanted_ranks if r not in by_id]
        not_normal = [r for r in wanted_ranks if r in by_id and by_id[r].status != EndpointStatus.NORMAL]
        return (not missing), missing, not_normal

    def _wait_joiner_ranks_present(
        self,
        instance_id: int,
        original_dp_ranks: list[int],
        survivor_rank_ids: set[int],
        config: Any,
        deadline: float,
    ) -> bool:
        """Gate 1: every joiner rank is on the roster, before the scale is sent.

        Registration is independent of the scale, so this cannot deadlock, and
        it keeps ``_wait_for_ready_ranks`` (300s, counted from prepare) from
        being spent on Pod recreation.

        Presence is the only joiner signal available before commit. A joiner
        launched with ``VLLM_ELASTIC_EP_SCALE_UP_LAUNCH`` skips warmup and
        parks in ``_wait_for_external_scale_commit``, so its ``/health`` stays
        refused by design and must never be used as a gate.
        """
        scale_down_config = config.dp_scale_down_config
        poll = float(getattr(scale_down_config, "poll_interval_sec", 1.0) or 1.0)
        original = [int(x) for x in original_dp_ranks]
        survivors = {int(x) for x in survivor_rank_ids}
        joiners = sorted(set(original) - survivors)
        if not joiners:
            logger.info("DP scale-up gate-1: no joiner ranks; skip presence wait")
            return True
        logger.info("DP scale-up gate-1: waiting for joiner ranks %s on the roster", joiners)

        while not self.event.is_set() and time.monotonic() < deadline:
            instance = _get_instance(instance_id)
            if instance is None:
                logger.error("instance disappeared while waiting for joiner present")
                return False
            fault = self._survivor_hard_fault(instance, survivors)
            if fault:
                logger.error("DP scale-up aborted: %s", fault)
                return False
            ok, missing, not_normal = self._roster_ranks_present(instance, original)
            if ok:
                logger.info(
                    "DP scale-up gate-1 satisfied: ranks %s present (not_normal=%s)",
                    original,
                    not_normal,
                )
                return True
            logger.info(
                "DP scale-up gate-1 pending present missing=%s not_normal=%s",
                missing,
                not_normal,
            )
            self.event.wait(poll)
        logger.error("DP scale-up gate-1 timed out waiting for joiner presence")
        return False

    def _poll_is_scaling_status(self, ep: Any, timeout: float) -> dict:
        import json as _json
        import urllib.request

        url = _engine_http_url(ep, "/is_scaling_elastic_ep")
        req = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=max(30.0, float(timeout))) as resp:
            return _json.loads(resp.read().decode("utf-8", "replace"))

    def _wait_elastic_preparing(
        self,
        instance: Any,
        survivor_rank_ids: set[int],
        config: Any,
        deadline: float,
        dispatch: _ScaleDispatch,
    ) -> str | None:
        """Gate 2: survivors published this round's scale bootstrap; return the epoch.

        ``prepare()`` writes phase=preparing together with the bootstrap that
        carries this round's coord store port, and only then binds the joiner
        handshake server. Reaching this phase is what makes the later gates
        meaningful, and it surfaces a rejected scale immediately instead of
        after a deadline.
        """
        scale_down_config = config.dp_scale_down_config
        poll = float(getattr(scale_down_config, "poll_interval_sec", 1.0) or 1.0)
        req_timeout = float(getattr(scale_down_config, "request_timeout_sec", 30.0) or 30.0)
        wanted = {int(x) for x in survivor_rank_ids}
        accepted = ("preparing", "paused", "committing")
        logger.info(
            "DP scale-up gate-2: waiting for survivors %s to publish scale bootstrap",
            sorted(wanted),
        )
        while not self.event.is_set() and time.monotonic() < deadline:
            fault = self._survivor_hard_fault(instance, wanted)
            if fault:
                logger.error("DP scale-up aborted: %s", fault)
                return None
            failed = self._scale_dispatch_error(dispatch)
            if failed:
                logger.error("DP scale-up gate-2 aborted, scale_elastic_ep failed: %s", failed)
                return None
            registered = {nm.pod_ip for nm in instance.get_node_managers()}
            candidates = sorted(
                (ep for ep in self._iter_raw_endpoints(instance) if int(ep.id) in wanted and ep.ip in registered),
                key=lambda ep: int(ep.id),
            )
            if candidates:
                ep = candidates[0]
                try:
                    status = self._poll_is_scaling_status(ep, req_timeout)
                    phase = str(status.get("phase") or "")
                    epoch = status.get("epoch")
                    if phase == "failed":
                        logger.error("DP scale-up gate-2: scale already failed: %s", status)
                        return None
                    if phase in accepted and epoch:
                        logger.info(
                            "DP scale-up gate-2 satisfied: rank=%s phase=%s epoch=%s",
                            ep.id,
                            phase,
                            epoch,
                        )
                        return str(epoch)
                    logger.info(
                        "DP scale-up gate-2 pending: rank=%s phase=%s epoch=%s",
                        ep.id,
                        phase or "-",
                        epoch,
                    )
                except Exception as e:
                    logger.info("is_scaling_elastic_ep poll failed on rank %s: %s", ep.id, e)
            self.event.wait(poll)
            instance = _get_instance(getattr(instance, "id", None) or -1) or instance
        logger.error("DP scale-up gate-2 timed out waiting for scale bootstrap")
        return None

    def _wait_elastic_paused(
        self,
        instance: Any,
        original_dp_ranks: list[int],
        config: Any,
        deadline: float,
        survivor_rank_ids: set[int] | None = None,
    ) -> str | None:
        """Gate 3: poll /is_scaling_elastic_ep until phase=paused; return epoch.

        Identity-preserving engines keep original DP ids, so the paused
        owner is min(survivors) (e.g. 16), not min(all present ranks).
        Joiners reuse 0..k and must not be polled here.
        """
        scale_down_config = config.dp_scale_down_config
        poll = float(getattr(scale_down_config, "poll_interval_sec", 1.0) or 1.0)
        req_timeout = float(getattr(scale_down_config, "request_timeout_sec", 30.0) or 30.0)
        if survivor_rank_ids:
            wanted = {int(x) for x in survivor_rank_ids}
        else:
            wanted = set(int(x) for x in original_dp_ranks)
        logger.info(
            "DP scale-up gate-3: waiting for phase=paused on ranks %s before resume_elastic_ep",
            sorted(wanted),
        )
        while not self.event.is_set() and time.monotonic() < deadline:
            if survivor_rank_ids is not None:
                fault = self._survivor_hard_fault(instance, survivor_rank_ids)
                if fault:
                    logger.error("DP scale-up aborted: %s", fault)
                    return None
            registered = {nm.pod_ip for nm in instance.get_node_managers()}
            candidates = sorted(
                (ep for ep in self._iter_raw_endpoints(instance) if int(ep.id) in wanted and ep.ip in registered),
                key=lambda ep: int(ep.id),
            )
            if not candidates:
                self.event.wait(poll)
                instance = _get_instance(getattr(instance, "id", None) or -1) or instance
                continue
            ep = candidates[0]
            try:
                status = self._poll_is_scaling_status(ep, req_timeout)
            except Exception as e:
                logger.info("is_scaling_elastic_ep poll failed on rank %s: %s", ep.id, e)
                self.event.wait(poll)
                instance = _get_instance(getattr(instance, "id", None) or -1) or instance
                continue
            phase = str(status.get("phase") or "")
            epoch = status.get("epoch")
            logger.info(
                "is_scaling_elastic_ep rank=%s phase=%s epoch=%s raw=%s",
                ep.id,
                phase,
                epoch,
                {k: status.get(k) for k in ("is_scaling_elastic_ep", "phase", "epoch", "error")},
            )
            if phase == "paused" and epoch:
                return str(epoch)
            if phase in ("failed", "completed") and status.get("error"):
                logger.error("elastic scale entered terminal phase=%s: %s", phase, status)
                return None
            self.event.wait(poll)
            instance = _get_instance(getattr(instance, "id", None) or -1) or instance
        return None

    def _resume_elastic_ep(
        self,
        instance: Any,
        epoch: str,
        timeout: float,
        rank_ids: set[int] | None = None,
    ) -> None:
        """POST /resume_elastic_ep to the lowest survivor (scale-status owner)."""
        import json as _json
        import urllib.request

        registered = {nm.pod_ip for nm in instance.get_node_managers()}
        wanted = {int(x) for x in rank_ids} if rank_ids else None
        candidates = sorted(
            (
                ep
                for ep in self._iter_raw_endpoints(instance)
                if ep.ip in registered and (wanted is None or int(ep.id) in wanted)
            ),
            key=lambda ep: int(ep.id),
        )
        if not candidates:
            raise RuntimeError("no endpoints available for resume_elastic_ep")
        ep = candidates[0]
        body = _json.dumps({"epoch": epoch}).encode()
        url = _engine_http_url(ep, "/resume_elastic_ep")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=max(60.0, float(timeout))) as resp:
            payload = resp.read()[:300]
            logger.info(
                "resume_elastic_ep ok: %s@%s:%s -> %s %r",
                ep.id,
                ep.ip,
                ep.business_port,
                resp.status,
                payload,
            )

    def _auto_scale_up(
        self,
        instance_id: int,
        original_dp_ranks: list[int],
        config: Any,
        survivor_rank_ids: set[int] | None = None,
    ) -> bool:
        """After empty-shell recycle: joiners present → scale survivors → bootstrap → resume.

        ``/scale_elastic_ep`` is posted to **survivors only**: a joiner drives
        its own scaling state from ``VLLM_ELASTIC_EP_SCALE_UP_LAUNCH`` startup
        and never receives the request. The POST does not answer until commit,
        and commit depends on the gates below, so it is dispatched without
        waiting.

        Joiners are left to start as soon as they register. They park in the
        startup handshake until survivor rank 0 binds the handshake server
        inside ``prepare()``, which absorbs their startup cost instead of
        spending the 300s ``_wait_for_ready_ranks`` budget on it.

        Only one scale-up round runs in the cluster at a time. The round
        restores every dead rank of this instance back to ``original_dp_ranks``.
        On warmup success the Controller issues one Coordinator SET of the
        full restored topology. On failure the caller still dispatches
        whole-instance reconfiguration.
        """
        with self._SCALE_UP_LOCK:
            return self._auto_scale_up_locked(instance_id, original_dp_ranks, config, survivor_rank_ids)

    def _auto_scale_up_locked(
        self,
        instance_id: int,
        original_dp_ranks: list[int],
        config: Any,
        survivor_rank_ids: set[int] | None,
    ) -> bool:
        """Run scale-up while ``_SCALE_UP_LOCK`` is held."""
        store = get_ft_runtime_store()
        scale_down_config = config.dp_scale_down_config
        wait_sec = float(getattr(scale_down_config, "execution_deadline_sec", 30.0) or 30.0)
        wait_sec = max(wait_sec, 600.0)
        deadline = time.monotonic() + wait_sec
        req_timeout = float(getattr(scale_down_config, "request_timeout_sec", 30.0) or 30.0)
        original = [int(x) for x in original_dp_ranks]
        survivors = {int(x) for x in survivor_rank_ids} if survivor_rank_ids is not None else set(original)
        if not survivors:
            survivors = set(original)
        existing_dp_ranks = sorted(int(x) for x in survivors)
        recorded = store.get(instance_id) or {}
        recorded_rank = recorded.get("dp_master_rank")
        if recorded_rank is not None and int(recorded_rank) in survivors:
            data_parallel_master_rank = int(recorded_rank)
        else:
            data_parallel_master_rank = min(existing_dp_ranks)
        # Mark in-progress so soft-fault collection cannot reconfig mid scale-up.
        # serving_dp_ranks is the roster Coordinator may keep using until
        # publish_restored(). Joiners are not in it, and must not be written
        # into dead_committed (heartbeat ignores that list during warmup).
        store.transition(
            instance_id,
            phase=FtPhase.WARMING_UP,
            serving_dp_ranks=sorted(survivors),
            last_error=None,
        )
        logger.info(
            "DP scale-up waiting up to %.0fs for ranks %s "
            "(gate-1 joiners present → scale survivors=%s → gate-2 bootstrap "
            "→ gate-3 paused → resume)",
            wait_sec,
            original,
            sorted(survivors),
        )

        if not self._wait_joiner_ranks_present(instance_id, original, survivors, config, deadline):
            logger.error("DP scale-up gate-1 (joiner presence) timed out")
            return False

        try:
            instance0 = _get_instance(instance_id)
            if instance0 is None:
                logger.error("instance disappeared before scale_elastic_ep")
                return False
            dispatch = self._trigger_scale_elastic_ep(
                instance0,
                target_dp_size=len(original),
                old_rank_ids=survivors,
                timeout=wait_sec,
                existing_dp_ranks=existing_dp_ranks,
                data_parallel_master_rank=data_parallel_master_rank,
            )
        except Exception as e:
            logger.error("DP scale-up scale_elastic_ep dispatch failed: %s", e)
            return False

        try:
            return self._run_scale_up_gates(
                instance_id,
                original,
                survivors,
                config,
                deadline,
                req_timeout,
                dispatch,
                store,
            )
        finally:
            self._drain_scale_elastic_ep(dispatch, timeout=max(1.0, deadline - time.monotonic()))

    def _run_scale_up_gates(
        self,
        instance_id: int,
        original: list[int],
        survivors: set[int],
        config: Any,
        deadline: float,
        req_timeout: float,
        dispatch: _ScaleDispatch,
        store: Any,
    ) -> bool:
        """Gate sequence after ``/scale_elastic_ep`` is in flight."""
        scale_down_config = config.dp_scale_down_config

        instance1 = _get_instance(instance_id)
        if instance1 is None:
            logger.error("instance disappeared after scale_elastic_ep dispatch")
            return False
        if not self._wait_elastic_preparing(instance1, survivors, config, deadline, dispatch):
            logger.error("DP scale-up gate-2 (scale bootstrap published) failed")
            return False

        instance1 = _get_instance(instance_id)
        if instance1 is None:
            logger.error("instance disappeared before gate-3")
            return False
        epoch = self._wait_elastic_paused(instance1, original, config, deadline, survivors)
        if not epoch:
            logger.error("DP scale-up gate-3 (phase=paused) timed out or failed")
            return False
        try:
            instance2 = _get_instance(instance_id)
            if instance2 is None:
                logger.error("instance disappeared before resume_elastic_ep")
                return False
            self._resume_elastic_ep(instance2, epoch, req_timeout, survivors)
        except Exception as e:
            logger.error("DP scale-up resume_elastic_ep failed: %s", e)
            return False

        while not self.event.is_set() and time.monotonic() < deadline:
            instance = _get_instance(instance_id)
            if instance is None:
                logger.error("instance disappeared during scale-up")
                return False
            fault = self._survivor_hard_fault(instance, survivors)
            if fault:
                logger.error("DP scale-up aborted: %s", fault)
                return False
            ready, statuses = self._scale_up_ready(instance, original, scale_down_config.request_timeout_sec)
            engine_fault = self._survivor_engine_failed(statuses, survivors)
            if engine_fault:
                logger.error("DP scale-up aborted: %s", engine_fault)
                return False
            if ready:
                logger.info(
                    "DP scale-up ranks healthy for instance %d: %s; publishing restored view",
                    instance_id,
                    sorted(original),
                )
                store.transition(instance_id, phase=FtPhase.WARMING_UP)
                try:
                    published = ServingOverlay.publish_restored(instance)
                except Exception as e:
                    logger.error("failed to publish restored serving view: %s", e)
                    store.transition(instance_id, phase=FtPhase.SCALED_DOWN_RUNNING)
                    return False
                if not published:
                    logger.error("failed to publish restored serving view")
                    store.transition(instance_id, phase=FtPhase.SCALED_DOWN_RUNNING)
                    return False
                return True
            self.event.wait(scale_down_config.poll_interval_sec)
        logger.error("DP scale-up recovery deadline exceeded for instance %d", instance_id)
        return False

    def run_problem_pod_recover(
        self,
        instance_id: int,
        original_dp_ranks: list[int],
        expanded_ranks: set[int],
        target_dp_size: int,
        config: Any,
    ) -> bool:
        """Kill problem-Pod workers, wait for native FT scale-down, recycle, scale-up.

        1. Withdraw the instance from Coordinator so new external requests stop.
        2. Drain requests already inside the engine.
        3. SIGKILL ``VLLM::Worker`` on the problem Pods (no soft-inject).
        4. POST one short completion directly at one surviving rank.
        5. Wait until native ``DpScaleDownStrategy`` commits those ranks
           (``SCALED_DOWN_RUNNING`` + ``dead_committed`` covers ``expanded``).
        6. Stop empty-shell Pods. Survivors keep original DP ids; K8s
           replacements register into the missing ranks.
        7. ``_auto_scale_up`` via ``/scale_elastic_ep`` back to original DP size.

        Does **not** call ``execute()`` itself — that slot stays free for the
        normal software-fault recovery plan.
        """
        original = sorted(set(original_dp_ranks))
        expanded = set(expanded_ranks)
        if target_dp_size < 1 or target_dp_size != len(original) - len(expanded):
            logger.error(
                "elastic pod recover invalid cut: target_dp=%s original=%s expanded=%s",
                target_dp_size,
                original,
                sorted(expanded),
            )
            return False
        if set(original) & expanded != expanded:
            logger.error(
                "problem-pod recover expanded=%s not subset of original=%s",
                sorted(expanded),
                original,
            )
            return False

        instance = _get_instance(instance_id)
        if instance is None:
            logger.error("instance %d disappeared before kill-worker", instance_id)
            return False

        ServingOverlay.hold(instance_id)
        try:
            if not self._withdraw_kill_and_wait_scale_down(instance_id, instance, original, expanded, config):
                return False
        finally:
            # Once native scale-down commits, the FT phase governs the serving view.
            ServingOverlay.release(instance_id)

        store = get_ft_runtime_store()
        instance = _get_instance(instance_id)
        if instance is None:
            logger.error("instance %d disappeared after native scale-down", instance_id)
            return False
        runtime = store.get(instance_id) or {}
        dead = set(runtime.get("dead_committed") or [])
        survivors = set(int(x) for x in original) - set(int(x) for x in expanded)
        recyclable = TopologyResolver.recyclable_pods(instance, dead)
        if recyclable:
            if not self._wait_survivors_healthy_before_recycle(
                instance_id,
                survivors,
                config,
                empty_shell_ips=list(recyclable),
            ):
                store.transition(
                    instance_id,
                    last_error="survivors not stably healthy before empty-shell recycle",
                )
                logger.error(
                    "Instance %d: survivors not stably healthy before recycling %s",
                    instance_id,
                    recyclable,
                )
                return False
            instance = _get_instance(instance_id)
            if instance is None:
                logger.error("instance %d disappeared before empty-shell recycle", instance_id)
                return False
            logger.info(
                "Instance %d: survivors healthy; stopping empty-shell pods %s after scale-down",
                instance_id,
                recyclable,
            )
            PodLifecycle.stop_recyclable_pods(instance, recyclable)
        else:
            logger.warning(
                "Instance %d: no recyclable pods for dead=%s; continue scale-up",
                instance_id,
                sorted(dead),
            )

        # Empty shells are gone: drop dead_committed so heartbeat no longer
        # ignores the ranks joiners are about to occupy. Serving view follows
        # the live roster (survivors only until joiners register).
        store.transition(
            instance_id,
            phase=FtPhase.SCALED_DOWN_RUNNING,
            dead_committed=[],
            pending_removed_ranks=[],
            last_error=None,
        )
        logger.info(
            "Instance %d: cleared dead_committed after empty-shell recycle; "
            "waiting for joiner present+/health before scale_elastic_ep (survivors=%s)",
            instance_id,
            sorted(survivors),
        )
        if not self._auto_scale_up(instance_id, original, config, survivor_rank_ids=survivors):
            # This worker does not own the fault-manager strategy slot, so
            # dispatch whole-instance restart directly.
            self._reconfigure_after_recover_failure(instance_id, "scale-up failed")
            return False
        logger.info(
            "Instance %d: kill → native scale-down → recycle → scale-up completed",
            instance_id,
        )
        return True

    def run_scale_up_recovery(
        self,
        instance_id: int,
        original_dp_ranks: list[int],
        dead_ranks: set[int],
        config: Any,
        survivor_rank_ids: set[int] | None = None,
    ) -> bool:
        """Wait survivors healthy → stop empty shells → ``_auto_scale_up``.

        When submitted by user recover this method owns the FaultManager
        strategy slot, so every exit must ``_finish()``; otherwise the slot
        stays occupied by a strategy that never reports itself finished and
        the instance can never be recovered again. ``_fail`` already finishes
        the scale-up-failure exit.
        """
        original = [int(x) for x in original_dp_ranks]
        dead = {int(x) for x in dead_ranks}
        survivors = {int(x) for x in survivor_rank_ids} if survivor_rank_ids is not None else set(original) - dead
        store = get_ft_runtime_store()
        instance = _get_instance(instance_id)
        if instance is None:
            logger.error("instance %d disappeared before empty-shell recycle", instance_id)
            self._finish(False)
            return False
        recyclable = TopologyResolver.recyclable_pods(instance, dead)
        if recyclable:
            if not self._wait_survivors_healthy_before_recycle(
                instance_id,
                survivors,
                config,
                empty_shell_ips=list(recyclable),
            ):
                store.transition(
                    instance_id,
                    last_error="survivors not stably healthy before empty-shell recycle",
                )
                self._finish(False)
                return False
            instance = _get_instance(instance_id)
            if instance is None:
                logger.error("instance %d disappeared before empty-shell recycle", instance_id)
                self._finish(False)
                return False
            logger.info(
                "Instance %d: survivors healthy; stopping empty-shell pods %s",
                instance_id,
                recyclable,
            )
            PodLifecycle.stop_recyclable_pods(instance, recyclable)
            store.transition(
                instance_id,
                dead_committed=[],
                pending_removed_ranks=[],
            )
        if self._auto_scale_up(instance_id, original, config, survivor_rank_ids=survivors):
            self._finish(False)
            return True
        self._fail(store.get_or_create(instance_id), "DP scale-up failed")
        return False

    def _reconfigure_after_recover_failure(self, instance_id: int, reason: str) -> None:
        """Restart the instance when this worker cannot leave the failure to the strategy center."""
        from motor.controller.fault_tolerance.strategy.reconfiguration import InstanceReconfigurationStrategy

        logger.error(
            "Instance %d: %s during elastic recover; falling back to whole-instance reconfiguration",
            instance_id,
            reason,
        )
        strategy = InstanceReconfigurationStrategy()
        strategy.bind_config(self._controller_config)
        strategy.execute(instance_id)

    def _wait_native_scale_down(self, instance_id: int, expanded_ranks: set[int], config: Any) -> bool:
        """Block until native FT has committed ``expanded_ranks`` as dead."""
        scale_down_config = config.dp_scale_down_config
        wait_sec = float(getattr(scale_down_config, "execution_deadline_sec", 30.0) or 30.0)
        wait_sec = max(wait_sec, 600.0)
        poll = float(getattr(scale_down_config, "poll_interval_sec", 1.0) or 1.0)
        deadline = time.monotonic() + wait_sec
        store = get_ft_runtime_store()
        logger.info(
            "Instance %d: waiting up to %.0fs for native scale-down of ranks=%s",
            instance_id,
            wait_sec,
            sorted(expanded_ranks),
        )
        while not self.event.is_set() and time.monotonic() < deadline:
            runtime = store.get(instance_id)
            if runtime is not None:
                phase = runtime.get("phase")
                dead = set(runtime.get("dead_committed") or [])
                if (
                    phase
                    in (
                        FtPhase.SCALED_DOWN_RUNNING.value,
                        FtPhase.WARMING_UP.value,
                    )
                    and expanded_ranks <= dead
                ):
                    logger.info(
                        "Instance %d: native scale-down committed ranks=%s phase=%s",
                        instance_id,
                        sorted(expanded_ranks),
                        phase,
                    )
                    return True
            self.event.wait(poll)
        return False

    def _probe_endpoint_ft_status(self, ep: Any, timeout: float) -> dict[str, Any] | None:
        """GET engine ``/v1/fault_tolerance/status`` for one endpoint."""
        import json
        import urllib.request

        url = _engine_http_url(ep, "/v1/fault_tolerance/status")
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=max(2.0, float(timeout))) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception:
            return None
        engines = payload.get("engines") if isinstance(payload, dict) else None
        if not isinstance(engines, list) or not engines:
            return None
        want = int(ep.id)
        chosen = None
        for eng in engines:
            if not isinstance(eng, dict):
                continue
            rank = eng.get("dp_rank", eng.get("id"))
            try:
                if int(rank) == want:
                    chosen = eng
                    break
            except (TypeError, ValueError):
                continue
        if chosen is None:
            return None
        return {
            "id": int(ep.id),
            "dp_rank": int(chosen.get("dp_rank", want)),
            "status": str(chosen.get("status") or ""),
            "ft_state": chosen.get("ft_state"),
        }

    def _query_survivor_ft_statuses_live(
        self,
        instance: Any,
        survivor_ranks: list[int],
        timeout: float,
    ) -> dict[int, dict[str, Any]]:
        """Fresh survivor FT statuses. NM first; on failure probe engines directly.

        Raises on total failure — caller must not treat stale store as healthy.
        """
        survivors = [int(x) for x in survivor_ranks]
        try:
            groups = self._build_node_manager_groups(instance, dead_ranks=set())
            query_groups = [
                group for group in groups if set(int(x) for x in group.survivor_endpoint_ids) & set(survivors)
            ]
            if query_groups:
                statuses = self._query_via_node_managers(query_groups, timeout)
                return {
                    int(rank): (info if isinstance(info, dict) else {})
                    for rank, info in statuses.items()
                    if int(rank) in set(survivors)
                }
        except Exception as e:
            logger.info(
                "pre-recycle NM FT query failed (%s); falling back to direct engine status",
                e,
            )

        by_id: dict[int, Any] = {}
        for ep in self._iter_raw_endpoints(instance):
            by_id[int(ep.id)] = ep
        statuses: dict[int, dict[str, Any]] = {}
        failed: list[str] = []
        for rank in survivors:
            ep = by_id.get(rank)
            if ep is None:
                failed.append("rank_%d=missing_endpoint" % rank)
                continue
            info = self._probe_endpoint_ft_status(ep, timeout)
            if info is None:
                failed.append("rank_%d=ft_status_unreachable" % rank)
                continue
            statuses[rank] = info
        if failed or set(statuses) != set(survivors):
            raise RuntimeError("live survivor FT status incomplete: %s" % (", ".join(failed) or "mismatch"))
        return statuses

    @staticmethod
    def _empty_shell_probe_targets(instance: Any, empty_shell_ips: list[str]) -> list[tuple[str, int]]:
        """Business and bootstrap ports of endpoints that still sit on empty-shell Pods."""
        shells = {ip for ip in empty_shell_ips if ip}
        targets: list[tuple[str, int]] = []
        seen: set[tuple[str, int]] = set()
        for endpoint in DpScaleDownStrategy._iter_raw_endpoints(instance):
            if endpoint.ip not in shells:
                continue
            for raw in (getattr(endpoint, "business_port", None), getattr(endpoint, "bootstrap_port", None)):
                if raw is None or raw == "":
                    continue
                try:
                    port = int(raw)
                except (TypeError, ValueError):
                    continue
                if not 1 <= port <= 65535:
                    continue
                key = (str(endpoint.ip), port)
                if key in seen:
                    continue
                seen.add(key)
                targets.append(key)
        return targets

    @staticmethod
    def _empty_shell_ports_closed(instance: Any, empty_shell_ips: list[str]) -> tuple[bool, str]:
        """True when every empty-shell endpoint port refuses connect (peers gone).

        Ports come from those Pods' ``business_port`` and ``bootstrap_port``.
        A fixed sample such as 10000/10002/9000 is not used: those ports are
        unrelated to the instance, so a refused connect there used to look
        like the peer was already gone.
        """
        import socket

        targets = DpScaleDownStrategy._empty_shell_probe_targets(instance, empty_shell_ips)
        if not targets:
            return False, "no endpoint ports"
        still_open: list[str] = []
        for ip, port in targets:
            try:
                with socket.create_connection((ip, port), timeout=1.0):
                    still_open.append("%s:%d" % (ip, port))
            except OSError:
                continue
        if still_open:
            return False, "still_open=%s" % still_open[:8]
        return True, "closed"

    def _wait_survivors_healthy_before_recycle(
        self,
        instance_id: int,
        survivor_rank_ids: set[int],
        config: Any,
        deadline: float | None = None,
        empty_shell_ips: list[str] | None = None,
    ) -> bool:
        """Wait until survivors are *live*-healthy before stopping empty shells.

        Kill/scale-down of the high Pod often wounds survivors via gloo
        (``Connection closed by peer``) while the Controller store still shows
        commit-time ``healthy``. Recycling then / scale-up prepare yields
        ``HcclCreateOpResCtx ret:24``.

        Rules:
        - Ignore commit-time store; require a successful live FT query each poll
          (NM, else direct ``/v1/fault_tolerance/status``).
        - Query failure does **not** count as healthy (stable counter resets).
        - Need ``_SURVIVOR_HEALTHY_STABLE_POLLS`` consecutive live-healthy polls.
        - Optionally require empty-shell endpoint business/bootstrap ports closed before return.
        """
        survivors = sorted({int(x) for x in survivor_rank_ids})
        if not survivors:
            return True
        scale_down_config = config.dp_scale_down_config
        poll = float(getattr(scale_down_config, "poll_interval_sec", 1.0) or 1.0)
        req_timeout = float(getattr(scale_down_config, "request_timeout_sec", 30.0) or 30.0)
        wait_sec = float(getattr(scale_down_config, "execution_deadline_sec", 30.0) or 30.0)
        wait_sec = max(wait_sec, 600.0)
        need = _SURVIVOR_HEALTHY_STABLE_POLLS
        if deadline is None:
            deadline = time.monotonic() + wait_sec
        stable = 0
        shell_ips = [str(x) for x in (empty_shell_ips or []) if x]
        logger.info(
            "Instance %d: waiting for LIVE survivor FT healthy %s (stable_polls=%d, empty_shells=%s) before recycling",
            instance_id,
            survivors,
            need,
            shell_ips,
        )
        while not self.event.is_set() and time.monotonic() < deadline:
            instance = _get_instance(instance_id)
            if instance is None:
                logger.error(
                    "instance %d disappeared while waiting for survivors healthy",
                    instance_id,
                )
                return False
            try:
                statuses = self._query_survivor_ft_statuses_live(instance, survivors, min(10.0, req_timeout))
            except Exception as e:
                stable = 0
                logger.info(
                    "Instance %d: pre-recycle live FT query pending (not healthy yet): %s",
                    instance_id,
                    e,
                )
                self.event.wait(poll)
                continue

            missing = [rank for rank in survivors if rank not in statuses]
            unhealthy = [
                rank
                for rank in survivors
                if (statuses.get(rank) or {}).get("status") != "healthy"
                or (statuses.get(rank) or {}).get("ft_state") in ("recovering", "failed")
            ]
            if missing or unhealthy:
                stable = 0
                logger.info(
                    "Instance %d: pre-recycle survivors not live-healthy yet missing=%s unhealthy=%s",
                    instance_id,
                    missing,
                    unhealthy,
                )
                self.event.wait(poll)
                continue
            stable += 1
            logger.info(
                "Instance %d: pre-recycle survivors live-healthy (%d/%d)",
                instance_id,
                stable,
                need,
            )
            if stable < need:
                self.event.wait(poll)
                continue
            if shell_ips:
                closed, detail = self._empty_shell_ports_closed(instance, shell_ips)
                if not closed:
                    # Keep requiring healthy, but do not finish until peers are gone.
                    stable = max(0, need - 1)
                    logger.info(
                        "Instance %d: survivors live-healthy but empty-shell ports still reachable (%s); delay recycle",
                        instance_id,
                        detail,
                    )
                    self.event.wait(poll)
                    continue
                logger.info(
                    "Instance %d: empty-shell ports closed (%s); recycle allowed",
                    instance_id,
                    detail,
                )
            return True
        logger.error(
            "Instance %d: timed out waiting for live survivors healthy before recycle",
            instance_id,
        )
        return False

    @staticmethod
    def _iter_raw_endpoints(instance: Any):
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

    def _withdraw_kill_and_wait_scale_down(
        self,
        instance_id: int,
        instance: Any,
        original: list[int],
        expanded: set[int],
        config: Any,
    ) -> bool:
        """Withdraw serving, drain, kill the problem-Pod workers, wait for native scale-down."""
        store = get_ft_runtime_store()
        try:
            withdrawn = ServingOverlay.withdraw(instance)
        except Exception as e:
            logger.error("Instance %d: serving withdraw failed: %s", instance_id, e)
            store.transition(instance_id, last_error="serving withdraw failed: %s" % e)
            return False
        if not withdrawn:
            logger.error("Instance %d: serving withdraw was not accepted", instance_id)
            store.transition(instance_id, last_error="serving withdraw was not accepted")
            return False
        logger.info(
            "Instance %d: serving withdrawn; waiting for in-flight requests to finish (timeout %.0fs)",
            instance_id,
            _USER_RECOVER_INFLIGHT_DRAIN_TIMEOUT_SEC,
        )
        if self._drain_inflight_requests(instance):
            logger.info("Instance %d: recover stopped during in-flight drain", instance_id)
            return False

        try:
            store.transition(
                instance_id,
                original_dp_ranks=original,
                pending_removed_ranks=sorted(expanded),
                last_error=None,
            )
            self._kill_problem_pod_workers(instance, expanded)
        except Exception as e:
            logger.error("kill-worker problem-pod recover failed for instance %d: %s", instance_id, e)
            store.transition(
                instance_id,
                last_error="kill-worker failed: %s" % e,
            )
            return False
        logger.info(
            "Instance %d: killed problem-pod workers ranks=%s",
            instance_id,
            sorted(expanded),
        )
        req_timeout = float(getattr(config.dp_scale_down_config, "request_timeout_sec", 30.0) or 30.0)
        self._probe_survivor_rank(instance, expanded, max(req_timeout, 10.0))
        logger.info(
            "Instance %d: survivor probe sent; waiting for native FT scale-down",
            instance_id,
        )

        if not self._wait_native_scale_down(instance_id, expanded, config):
            store.transition(
                instance_id,
                last_error="native FT scale-down did not finish after kill-worker",
            )
            logger.error(
                "Instance %d: timed out waiting for native scale-down of ranks=%s",
                instance_id,
                sorted(expanded),
            )
            return False
        return True

    def _drain_inflight_requests(self, instance: Any) -> bool:
        """Wait until no rank has running or waiting requests. True when recover was stopped.

        On timeout recovery continues anyway: serving is already withdrawn and
        cannot be republished outside ``WARMING_UP``.
        """
        endpoints = list(self._iter_raw_endpoints(instance))
        instance_id = getattr(instance, "id", -1)
        deadline = time.monotonic() + _USER_RECOVER_INFLIGHT_DRAIN_TIMEOUT_SEC
        next_log = 0.0
        idle_polls = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(len(endpoints), 32))) as pool:
            while True:
                counts = dict(
                    zip(
                        (int(ep.id) for ep in endpoints),
                        pool.map(self._read_inflight_requests, endpoints),
                    )
                )
                busy = {rank: n for rank, n in counts.items() if n != 0}
                idle_polls = idle_polls + 1 if not busy else 0
                if idle_polls >= _USER_RECOVER_INFLIGHT_IDLE_POLLS:
                    logger.info(
                        "Instance %d: all %d ranks idle; settling %.0fs for DP wave end",
                        instance_id,
                        len(endpoints),
                        _USER_RECOVER_IDLE_SETTLE_SEC,
                    )
                    return self.event.wait(_USER_RECOVER_IDLE_SETTLE_SEC)
                now = time.monotonic()
                if now >= deadline:
                    logger.warning(
                        "Instance %d: in-flight drain timed out after %.0fs; busy ranks "
                        "(-1 = metrics unreadable): %s; continuing recover",
                        instance_id,
                        _USER_RECOVER_INFLIGHT_DRAIN_TIMEOUT_SEC,
                        busy,
                    )
                    return False
                if busy and now >= next_log:
                    logger.info(
                        "Instance %d: waiting for in-flight requests; busy ranks (-1 = metrics unreadable): %s",
                        instance_id,
                        busy,
                    )
                    next_log = now + 10.0
                if self.event.wait(_USER_RECOVER_INFLIGHT_POLL_SEC):
                    return True

    @staticmethod
    def _read_inflight_requests(ep: Any) -> int:
        """Running + waiting requests from one rank's ``/metrics``; -1 if unreadable."""
        import urllib.request

        url = _engine_http_url(ep, "/metrics")
        try:
            with urllib.request.urlopen(url, timeout=_USER_RECOVER_INFLIGHT_METRICS_TIMEOUT_SEC) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception:
            return -1
        total = 0.0
        found = False
        for line in text.splitlines():
            name = line.split("{", 1)[0].split(" ", 1)[0]
            if name in _INFLIGHT_METRICS:
                try:
                    total += float(line.rsplit(" ", 1)[1])
                except (IndexError, ValueError):
                    return -1
                found = True
        return int(total) if found else -1

    def _probe_survivor_rank(
        self,
        instance: Any,
        expanded_ranks: set[int],
        timeout: float,
        prefer_rank: int | None = None,
    ) -> None:
        """Compatibility entry for expansion's temporary probe in common collection."""
        from motor.controller.fault_tolerance.engine_recovery.runner import probe_recovery_rank

        probe_recovery_rank(instance, expanded_ranks, timeout, prefer_rank)

    def _kill_problem_pod_workers(self, instance: Any, expanded_ranks: set[int]) -> None:
        """Fan out kill-workers to NodeManagers that own the problem-Pod ranks."""
        scale_down_config = self.ft_config.dp_scale_down_config
        timeout = float(getattr(scale_down_config, "request_timeout_sec", 30.0) or 30.0)
        by_ip: dict[str, list[int]] = {}
        for endpoint in self._iter_raw_endpoints(instance):
            if endpoint.id not in expanded_ranks:
                continue
            by_ip.setdefault(endpoint.ip, []).append(int(endpoint.id))
        if not by_ip:
            raise RuntimeError("no endpoints for expanded ranks %s" % sorted(expanded_ranks))
        managers = {nm.pod_ip: nm for nm in instance.get_node_managers()}
        missing = sorted(set(by_ip) - set(managers))
        if missing:
            raise RuntimeError("no NodeManager for problem-pod IPs %s" % missing)
        for pod_ip, endpoint_ids in sorted(by_ip.items()):
            result = NodeManagerApiClient.kill_engine_workers(
                managers[pod_ip],
                sorted(endpoint_ids),
                timeout,
            )
            logger.info(
                "kill-workers on %s endpoints=%s result=%s",
                pod_ip,
                sorted(endpoint_ids),
                result,
            )

    def _scale_up_ready(
        self, instance: Any, original_dp_ranks: list[int], timeout: float
    ) -> tuple[bool, dict[int, dict]]:
        """True when all original ranks are present and engine FT status is healthy."""
        registered = {nm.pod_ip for nm in instance.get_node_managers()}
        # get_all_endpoints() drops every rank except id 0 when multi-endpoints
        # is off, so a 16-rank scale-up would wait forever. Walk the raw roster.
        present = {
            int(endpoint.id)
            for endpoint in self._iter_raw_endpoints(instance)
            if endpoint.ip in registered and not getattr(endpoint, "headless", False)
        }
        missing = set(original_dp_ranks) - present
        if missing:
            return False, {}
        try:
            groups = self._build_node_manager_groups(instance, dead_ranks=set())
            query_groups = [group for group in groups if group.survivor_endpoint_ids]
            if not query_groups:
                return False, {}
            statuses = self._query_via_node_managers(query_groups, timeout)
        except Exception as e:
            logger.info("DP scale-up waiting for engine FT status: %s", e)
            return False, {}
        missing_status = set(original_dp_ranks) - set(statuses)
        if missing_status:
            return False, statuses
        for rank in original_dp_ranks:
            status = statuses.get(rank) or {}
            if status.get("status") != "healthy":
                return False, statuses
            if status.get("ft_state") in ("recovering", "failed"):
                return False, statuses
        return True, statuses

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
        if first_error is not None:
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
        # Keep the existing NodeManager 300s limit; longer transactions renew the same owner.
        return min(
            300.0,
            max(
                3 * (scale_down_config.request_timeout_sec + scale_down_config.poll_interval_sec),
                getattr(config, "fault_collection_timeout_seconds", 60.0)
                + scale_down_config.execution_deadline_sec
                + 4 * scale_down_config.request_timeout_sec
                + 5.0,
            ),
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
    def _scale_down_outcome(
        statuses: dict[int, dict],
        *,
        wait_for_store_migration: bool = False,
    ) -> tuple[str, dict | None]:
        """Classify the asynchronous scale-down result from survivors.

        With scale-up enabled, ``("healthy", "recovering")`` means EngineCore
        finished but the frontend is still migrating its coordination store
        after the DP master was removed. Ordinary scale-down treats that state
        as a failure, matching the previous classifier.
        """
        waiting_states = {("unhealthy", None), ("unhealthy", "recovering")}
        if wait_for_store_migration:
            waiting_states.add(("healthy", "recovering"))
        return DpScaleDownStrategy._classify_statuses(
            statuses,
            success_states={("healthy", None)},
            waiting_states=waiting_states,
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

    _gate = staticmethod(scale_down_gate)

    def _transition(self, instance_id: int, **changes: Any) -> FtRuntime:
        return get_ft_runtime_store().transition(instance_id, expected_recovery_id=self.recovery_id or None, **changes)

    def _renew_guard(self, groups, request_id, config, executor, *, deadline: float | None = None) -> None:
        if time.monotonic() < getattr(self, "_guard_renew_at", 0.0):
            return
        lease = self._guard_lease_sec(config)
        timeout = config.dp_scale_down_config.request_timeout_sec
        if deadline is not None:
            timeout = self._remaining_timeout(deadline, timeout)
        self._guard_node_managers(groups, request_id, lease, timeout, executor)
        self._guard_renew_at = time.monotonic() + lease / 3

    def _fail(self, runtime: FtRuntime, error: str) -> None:
        self._transition(
            runtime.instance_id,
            phase=FtPhase.RECONFIGURING if self.mutation_started else FtPhase.WAITING_ENGINE_FAULT,
            stage="failed",
            action="RECONFIGURE" if self.mutation_started else "SCALE_DOWN",
            reconfiguration_dispatched=False,
            last_error=error,
        )
        self._finish(True, error)
