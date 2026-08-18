# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.
"""EngineRelaunchStrategy: fallback recovery that restarts engines, then containers.

The fallback of the recovery ladder: fast strategies (token reinference,
UCE, elastic scaling) try to recover without restarting the engine; when one
of them fails (``mark_failed``) — or an engine is reported DEAD outright —
this strategy takes over:

1. **Phase 1 (relaunch engines)**: dispatch ``/node-manager/engine-restart``
   to *every* NodeManager of the instance so all ranks restart in the same
   window (cross-machine collective-group consistency), then poll
   ``/node-manager/status`` until every NodeManager reports all endpoints
   NORMAL again.
2. **Phase 2 (restart containers)**: if relaunching engines fails (dispatch
   error, a NodeManager unreachable, or the completion timeout elapsed),
   unfreeze each reachable NodeManager's suicide counter (``abort``) so the
   heartbeat mechanism restarts the pod via k8s. The instance's NodeManagers
   either all relaunch their engines or all restart their containers — a
   partial mix would leave the collective group split.

The instance briefly goes INACTIVE while heartbeats report ABNORMAL and
returns to ACTIVE automatically once the engines report NORMAL again
(``INACTIVE + INSTANCE_NORMAL -> ACTIVE`` in the instance state machine).
"""

import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from motor.common.logger import get_logger
from motor.common.resources import InsStatus
from motor.controller.api_client.node_manager_api_client import NodeManagerApiClient
from motor.controller.fault_tolerance.strategy.base import StrategyBase

logger = get_logger(__name__)


class RelaunchState(str, Enum):
    """States of the engine relaunch workflow."""

    INIT = "init"
    RELAUNCH_ENGINE = "relaunch_engine"
    RESTART_CONTAINER = "restart_container"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class RelaunchContext:
    """Mutable context carried through a single relaunch run."""

    instance_id: int
    current_state: RelaunchState = RelaunchState.INIT
    node_managers: list = field(default_factory=list)
    #: Per-NodeManager restart-dispatch outcome (pod_ip -> True when the
    #: restart command was accepted). Phase 2 only aborts the ones that were
    #: NOT dispatched — a NodeManager that is already relaunching keeps its
    #: freeze (its deadline provides the fallback) instead of being torn down
    #: mid-relaunch.
    dispatch_results: dict = field(default_factory=dict)
    last_error: str | None = None
    start_time: float = field(default_factory=time.time)


class EngineRelaunchStrategy(StrategyBase):
    """Fallback recovery: relaunch all engines in place, then restart containers.

    The strategy center escalates here when (a) an engine is reported DEAD
    (ENGINE_DEAD L2 fault) or (b) any previous strategy finished with
    ``mark_failed()`` (see ``fault_manager._process_instance_strategy``).
    """

    DISPATCH_RETRY_BACKOFF_SEC = 2.0

    def __init__(self) -> None:
        super().__init__()
        self.context: RelaunchContext | None = None
        self.engine_relaunch_poll_interval_sec = self._resolve_config("engine_relaunch_poll_interval_sec", 5.0)
        self.engine_relaunch_complete_timeout_sec = self._resolve_config("engine_relaunch_complete_timeout_sec", 600)
        self.engine_relaunch_dispatch_retries = self._resolve_config("engine_relaunch_dispatch_retries", 3)
        self.engine_relaunch_nm_unreachable_threshold = self._resolve_config(
            "engine_relaunch_nm_unreachable_threshold", 3
        )

    @staticmethod
    def _resolve_config(attr: str, default: float) -> float:
        """Read a relaunch knob from the FaultManager config; fall back on errors."""
        # Local import to avoid a circular dependency:
        # fault_manager → strategy/__init__ → engine_relaunch → fault_manager
        from motor.controller.fault_tolerance.fault_manager import FaultManager  # pylint: disable=cyclic-import

        try:
            ft_config = FaultManager().config.fault_tolerance_config
            return getattr(ft_config, attr, default)
        except Exception as e:
            logger.warning("Failed to resolve relaunch config %s, using default %s: %s", attr, default, e)
            return default

    # ------------------------------------------------------------------
    # execute: two-phase recovery
    # ------------------------------------------------------------------

    def execute(self, instance_id: int) -> None:
        self.context = RelaunchContext(instance_id=instance_id)
        try:
            instance = self._get_instance(instance_id)
            if instance is None:
                logger.error("Engine relaunch aborted: instance %d not found", instance_id)
                self._finish(RelaunchState.FAILED, "instance not found")
                return

            self.context.node_managers = list(instance.get_node_managers())
            if not self.context.node_managers:
                logger.error("Engine relaunch aborted: instance %d has no node managers", instance_id)
                self._finish(RelaunchState.FAILED, "no node managers")
                return

            if not self._phase_relaunch_engine(instance_id):
                self._phase_restart_container()
                return

            self._finish(RelaunchState.SUCCESS)
        except Exception as e:
            logger.exception("Engine relaunch failed for instance %d", instance_id)
            self._finish(RelaunchState.FAILED, str(e))

    def _phase_relaunch_engine(self, instance_id: int) -> bool:
        """Phase 1: probe all NodeManagers, dispatch restart, poll recovery.

        Returns True when every NodeManager reports all endpoints NORMAL
        within the completion timeout; False escalates to Phase 2.
        """
        self.context.current_state = RelaunchState.RELAUNCH_ENGINE
        if self._any_event_set():
            return False

        # 1. Probe every NodeManager first: dispatching a restart to a dead
        #    NodeManager is pointless, and its pod restart would split the
        #    collective group — escalate to container restart instead.
        self.context.dispatch_results = self._dispatch_to_all("restart", probe_first=True)
        if self.context.dispatch_results is None:
            return False

        # 2. Poll until all NodeManagers report all endpoints NORMAL.
        unreachable_counts: dict = {}
        while not self._any_event_set():
            if time.time() - self.context.start_time > self.engine_relaunch_complete_timeout_sec:
                logger.error(
                    "Engine relaunch timeout after %ss for instance %d",
                    self.engine_relaunch_complete_timeout_sec,
                    instance_id,
                )
                return False

            instance = self._get_instance(instance_id)
            if instance is None or instance.status == InsStatus.DELETED:
                logger.info("Instance %d gone during engine relaunch, finishing", instance_id)
                return True

            all_normal = True
            for node_mgr in list(instance.get_node_managers()):
                try:
                    # relaxed: "no endpoint ABNORMAL" — a freshly relaunched
                    # engine reports INITIAL while loading its model, which
                    # counts as recovering. Waiting for full NORMAL here would
                    # make the completion timeout fight the model load time.
                    response = NodeManagerApiClient.query_status(node_mgr, relaxed=True)
                    unreachable_counts.pop(node_mgr.pod_ip, None)
                    if not response.get("status"):
                        all_normal = False
                except Exception as e:
                    unreachable_counts[node_mgr.pod_ip] = unreachable_counts.get(node_mgr.pod_ip, 0) + 1
                    all_normal = False
                    if unreachable_counts[node_mgr.pod_ip] >= self.engine_relaunch_nm_unreachable_threshold:
                        logger.error(
                            "NodeManager %s unreachable for %d consecutive polls during engine relaunch",
                            node_mgr.pod_ip,
                            unreachable_counts[node_mgr.pod_ip],
                        )
                        return False
                    logger.debug("NodeManager %s not ready yet: %s", node_mgr.pod_ip, e)

            if all_normal:
                logger.info(
                    "Engine relaunch finished: all endpoints of instance %d recovering (no ABNORMAL)", instance_id
                )
                return True

            if self.event.wait(self.engine_relaunch_poll_interval_sec):
                return False

        return False

    def _phase_restart_container(self) -> None:
        """Phase 2: restart containers — abort the NodeManagers that were not
        relaunching.

        Only the NodeManagers whose restart dispatch FAILED (or that were
        never reached) get an abort: their engines were never killed, so the
        heartbeat mechanism must restart the container via k8s. A NodeManager
        that already accepted the restart keeps its suicide freeze — it is
        relaunching in place and its freeze deadline provides the eventual
        fallback. Aborting it mid-relaunch would tear down an engine that is
        actually recovering and split the collective group.
        """
        self.context.current_state = RelaunchState.RESTART_CONTAINER
        logger.warning(
            "Engine relaunch failed for instance %d, escalating to container restart",
            self.context.instance_id,
        )
        for node_mgr in self.context.node_managers:
            if (self.context.dispatch_results or {}).get(node_mgr.pod_ip, False):
                logger.info(
                    "Node manager %s already relaunching (restart dispatched), keeping its freeze", node_mgr.pod_ip
                )
                continue
            if NodeManagerApiClient.restart_engine(node_mgr, action="abort", instance_id=self.context.instance_id):
                logger.info("Abort sent to node manager %s: suicide counter unfrozen", node_mgr.pod_ip)
            else:
                logger.error(
                    "Abort to node manager %s failed: its container-restart fallback may not kick in",
                    node_mgr.pod_ip,
                )
        self._finish(RelaunchState.FAILED, "engine relaunch failed, escalated to container restart")

    def _dispatch_to_all(self, action: str, probe_first: bool = False) -> dict | None:
        """Send the restart (or abort) command to every NodeManager.

        Returns the per-NodeManager dispatch outcome (``{pod_ip: bool}``),
        or None when the probe found a NodeManager unreachable (nothing was
        dispatched — the whole instance escalates for consistency).
        """
        node_managers = list(self.context.node_managers)
        if probe_first:
            node_managers = self._probe_reachable(node_managers)
            if node_managers is None:
                return None

        results: dict = {}
        for node_mgr in node_managers:
            success = False
            for attempt in range(1, self.engine_relaunch_dispatch_retries + 1):
                if self._any_event_set():
                    return results
                success = NodeManagerApiClient.restart_engine(
                    node_mgr, action=action, instance_id=self.context.instance_id
                )
                if success:
                    break
                logger.warning(
                    "Dispatch %s to node manager %s failed (attempt %d/%d)",
                    action,
                    node_mgr.pod_ip,
                    attempt,
                    self.engine_relaunch_dispatch_retries,
                )
                if attempt < self.engine_relaunch_dispatch_retries:
                    if self.event.wait(self.DISPATCH_RETRY_BACKOFF_SEC):
                        return results
            results[node_mgr.pod_ip] = success
        return results

    def _probe_reachable(self, node_managers: list) -> list | None:
        """Return the reachable subset after retries; None when any is unreachable."""
        reachable = []
        for node_mgr in node_managers:
            probed = False
            for attempt in range(1, self.engine_relaunch_dispatch_retries + 1):
                if self._any_event_set():
                    return None
                try:
                    NodeManagerApiClient.query_status(node_mgr)
                    probed = True
                    break
                except Exception as e:
                    logger.warning(
                        "NodeManager %s unreachable during probe (attempt %d/%d): %s",
                        node_mgr.pod_ip,
                        attempt,
                        self.engine_relaunch_dispatch_retries,
                        e,
                    )
                    if attempt < self.engine_relaunch_dispatch_retries:
                        if self.event.wait(self.DISPATCH_RETRY_BACKOFF_SEC):
                            return None
            if not probed:
                logger.error(
                    "NodeManager %s unreachable — escalating instance %d to container restart",
                    node_mgr.pod_ip,
                    self.context.instance_id,
                )
                return None
            reachable.append(node_mgr)
        return reachable

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _any_event_set(self) -> bool:
        return self.event.is_set()

    @staticmethod
    def _get_instance(instance_id: int):
        """InstanceManager lookup; lazy import to keep the import graph acyclic."""
        from motor.controller.core.instance_manager import InstanceManager

        return InstanceManager().get_instance(instance_id)

    def _finish(self, state: RelaunchState, error: str | None = None) -> None:
        if self.context is not None:
            self.context.current_state = state
            self.context.last_error = error
        if state == RelaunchState.FAILED:
            self.mark_failed()
            logger.error(
                "Engine relaunch failed for instance %s after %.1fs: %s",
                self.context.instance_id if self.context else "?",
                time.time() - self.context.start_time if self.context else 0.0,
                error,
            )
        else:
            logger.info(
                "Engine relaunch succeeded for instance %s after %.1fs",
                self.context.instance_id if self.context else "?",
                time.time() - self.context.start_time if self.context else 0.0,
            )
        with self._lock:
            self._is_finished = True

    def stop(self) -> None:
        """Interrupt the strategy (upgrade/switch); unfreeze the NodeManagers.

        The suicide counters were frozen for up to ``engine_restart_freeze_sec``
        on every NodeManager — unfreeze them so the container-restart
        fallback is not delayed for the whole freeze window.
        """
        logger.info("Engine relaunch strategy stopped.")
        self.event.set()
        if self.context is None:
            return
        node_managers = list(self.context.node_managers)

        def _unfreeze_async() -> None:
            for node_mgr in node_managers:
                try:
                    NodeManagerApiClient.restart_engine(node_mgr, action="abort", instance_id=self.context.instance_id)
                except Exception as e:
                    logger.warning("Failed to abort relaunch on %s: %s", node_mgr.pod_ip, e)

        threading.Thread(target=_unfreeze_async, daemon=True, name="engine_relaunch_abort").start()
