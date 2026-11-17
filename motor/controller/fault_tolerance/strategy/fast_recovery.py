# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Shared engine fast recovery with HBM UCE, token reinference and software policies."""

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum

from motor.common.constants import (
    ENGINE_STATUS_DEAD,
    ENGINE_STATUS_HEALTHY,
    ENGINE_STATUS_UNHEALTHY,
)
from motor.common.logger import get_logger
from motor.common.resources import Endpoint, InsStatus, NodeManagerInfo
from motor.controller.api_client.node_manager_api_client import NodeManagerApiClient
from motor.controller.fault_tolerance.dp_scale_down import get_ft_runtime_store
from motor.controller.fault_tolerance.fault_types import FaultLevel, RecoveryCause
from motor.config.controller import ControllerConfig
from motor.controller.fault_tolerance.serving_overlay import ServingOverlay
from motor.controller.fault_tolerance.strategy.base import StrategyBase

logger = get_logger(__name__)

_UNHEALTHY_STATUSES = frozenset({ENGINE_STATUS_UNHEALTHY, ENGINE_STATUS_DEAD, "unknown"})


@dataclass(frozen=True)
class FastRecoveryContext:
    """Fault facts carried by the legacy planner hook."""

    fault_level: FaultLevel
    fault_code: int
    source: str


class FastRecoveryState(str, Enum):
    """States of the engine fast-recovery workflow."""

    INIT = "init"
    DISPATCHING = "dispatching"
    WAITING = "waiting"
    WAITING_CLEARANCE = "waiting_clearance"
    SUCCESS = "success"
    FAILED = "failed"


@dataclass
class FastRecoveryTarget:
    """One vLLM engine target that should receive a retry instruction."""

    node_manager: NodeManagerInfo
    engine_id: int
    status: str = ENGINE_STATUS_UNHEALTHY


@dataclass
class FastRecoveryRunContext:
    """Mutable context carried through one engine fast-recovery run."""

    instance_id: int
    targets: list[FastRecoveryTarget] = field(default_factory=list)
    current_state: FastRecoveryState = FastRecoveryState.INIT
    last_error: str | None = None
    request_id: str = ""
    all_ranks_failed: bool = False
    attempt: int = 0
    completion_started: bool = False


class EngineFastRecoveryStrategy(StrategyBase):
    """Orchestrate engine fast recovery; subclasses define rounds and completion."""

    abort_rounds: tuple[bool, ...] = ()
    failure_fallback: str = "InstanceReconfigurationStrategy"
    recovery_cause = RecoveryCause.SOFTWARE

    @classmethod
    def is_applicable(cls, instance_id: int, context: FastRecoveryContext, config: ControllerConfig) -> bool:
        """Legacy planner hook; concrete fast-recovery policies require explicit selection."""
        del instance_id, context, config
        return False

    def __init__(self) -> None:
        super().__init__()
        self.context: FastRecoveryRunContext | None = None
        self.round_timeout_sec = self._resolve_config("recovery_round_timeout_sec", 60)
        self.poll_interval_sec = self._resolve_config("recovery_poll_interval_sec", 1.0)

    @staticmethod
    def _resolve_config(attr: str, default: float) -> float:
        from motor.controller.fault_tolerance.fault_manager import (  # pylint: disable=cyclic-import
            FaultManager,
        )

        try:
            ft_config = FaultManager().config.fault_tolerance_config
            return getattr(ft_config, attr, default)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to resolve fast-recovery config %s, using default %s: %s", attr, default, e)
            return default

    def execute(self, instance_id: int) -> None:
        if self.event.is_set():
            return
        self.context = FastRecoveryRunContext(instance_id=instance_id)
        self.failure_fallback = "InstanceReconfigurationStrategy"
        try:
            if not self.abort_rounds:
                self._finish(FastRecoveryState.FAILED, "fast recovery requires a concrete recovery policy")
                return
            if not self._can_recover_instance(instance_id):
                return
            instance = self._get_instance_manager().get_instance(instance_id)
            if instance is None or not ServingOverlay.pause_for_recovery(instance):
                self._finish(FastRecoveryState.FAILED, "failed to pause new scheduling before fast recovery")
                return
            targets = self._check_unhealthy_targets(instance_id)
            if not targets:
                return
            if any(target.status != ENGINE_STATUS_UNHEALTHY for target in targets):
                self._finish(FastRecoveryState.FAILED, "fast recovery requires every DP endpoint to be UNHEALTHY")
                return

            logger.info(
                "All DP endpoints UNHEALTHY before FT fast recovery: instance=%d endpoints=%s",
                instance_id,
                sorted((target.node_manager.pod_ip, target.engine_id) for target in targets),
            )
            self.context.targets = targets
            for attempt, abort in enumerate(self.abort_rounds):
                self.context.attempt = attempt + 1
                self.context.all_ranks_failed = False
                self.context.current_state = FastRecoveryState.DISPATCHING
                if not self._dispatch_all(instance_id, targets, abort=abort):
                    self._complete_recovery(instance_id, self.context.last_error or "fast-recovery dispatch failed")
                    return
                self.context.current_state = FastRecoveryState.WAITING
                error = self._wait_until_recovered(instance_id, targets)
                if error is None:
                    self._complete_recovery(instance_id, None)
                    return
                if attempt == len(self.abort_rounds) - 1 or not self.context.all_ranks_failed or self.event.is_set():
                    self._complete_recovery(instance_id, error)
                    return
                logger.warning(
                    "FastRecovery round failed for instance %d: attempt=%d request_id=%s; escalating to the abort round: %s",
                    instance_id,
                    self.context.attempt,
                    self.context.request_id,
                    error,
                )
        except Exception as e:
            logger.exception("FastRecovery strategy failed for instance %d", instance_id)
            if self.context.attempt and not self.context.completion_started:
                try:
                    self._complete_recovery(instance_id, str(e))
                    return
                except Exception as completion_error:  # noqa: BLE001
                    self._finish(FastRecoveryState.FAILED, str(completion_error))
                    return
            self._finish(FastRecoveryState.FAILED, str(e))

    def stop(self) -> None:
        self.event.set()
        with self._lock:
            self._is_finished = True
        logger.info(
            "FastRecovery strategy stop requested for instance %s", self.context.instance_id if self.context else "?"
        )

    @staticmethod
    def _get_instance_manager():
        from motor.controller.core.instance_manager import (  # pylint: disable=cyclic-import
            InstanceManager,
        )

        return InstanceManager()

    def _can_recover_instance(self, instance_id: int) -> bool:
        instance = self._get_instance_manager().get_instance(instance_id)
        if (
            instance is None
            or instance.status != InsStatus.ACTIVE
            or get_ft_runtime_store().is_reconfiguration_dispatched(instance_id)
        ):
            self._finish(FastRecoveryState.FAILED, "instance is no longer eligible for in-place fast recovery")
            return False
        return True

    def _check_unhealthy_targets(self, instance_id: int) -> list[FastRecoveryTarget]:
        instance = self._get_instance_manager().get_instance(instance_id)
        if instance is None:
            self._finish(FastRecoveryState.FAILED, "instance not found")
            return []

        expected_keys = self._expected_endpoint_keys(instance)
        if not expected_keys:
            self._finish(FastRecoveryState.FAILED, "no recovery endpoint found for instance")
            return []

        active_ips = {ip for ip, _ in expected_keys}
        node_managers = [node for node in instance.get_node_managers() if node.pod_ip in active_ips]

        # Revalidate the collected ranks without restarting the collection window.
        if self.event.is_set():
            self._finish(FastRecoveryState.FAILED, "fast recovery interrupted")
            return []
        targets = [
            target
            for target in self._targets_from_node_managers(node_managers)
            if (target.node_manager.pod_ip, target.engine_id) in expected_keys
        ]
        if not self._targets_cover_expected(targets, expected_keys):
            self._finish(FastRecoveryState.FAILED, "DP status changed after recovery collection")
            return []
        return targets

    @staticmethod
    def _expected_endpoint_keys(instance) -> set[tuple[str, int]]:
        runtime = get_ft_runtime_store().get(instance.id)
        retired = set(runtime["dead_committed"]) if runtime else set()
        keys: set[tuple[str, int]] = set()
        for endpoint in _iter_recovery_endpoints(instance):
            if not endpoint.headless and endpoint.id not in retired:
                keys.add((endpoint.ip, int(endpoint.id)))
        return keys

    @staticmethod
    def _targets_cover_expected(targets: list[FastRecoveryTarget], expected_keys: set[tuple[str, int]]) -> bool:
        if not targets:
            return False
        target_keys = {(target.node_manager.pod_ip, target.engine_id) for target in targets}
        return expected_keys.issubset(target_keys)

    def _targets_from_node_managers(self, node_managers: list[NodeManagerInfo]) -> list[FastRecoveryTarget]:
        targets: list[FastRecoveryTarget] = []
        seen: set[tuple[str, int]] = set()
        for node_mgr in node_managers:
            payload = NodeManagerApiClient.query_fault_tolerance_status(node_mgr)
            for entry in payload.get("engines") or []:
                if not isinstance(entry, dict) or entry.get("engine_id") is None:
                    continue
                engine_id = int(entry["engine_id"])
                status = _endpoint_status(entry)
                if any(
                    engine.get("ft_state") == "recovering"
                    for engine in entry.get("engines", [])
                    if isinstance(engine, dict)
                ):
                    continue
                if status not in _UNHEALTHY_STATUSES and not entry.get("query_error"):
                    continue
                key = (node_mgr.pod_ip, engine_id)
                if key in seen:
                    continue
                seen.add(key)
                targets.append(
                    FastRecoveryTarget(
                        node_manager=node_mgr,
                        engine_id=engine_id,
                        status=status,
                    )
                )
        return targets

    def _dispatch_all(self, instance_id: int, targets: list[FastRecoveryTarget], *, abort: bool = False) -> bool:
        """POST apply once per NodeManager after the whole DP group is unhealthy."""
        if not self._can_recover_instance(instance_id):
            return False
        if not self._policy_evidence_valid(instance_id):
            self._finish(FastRecoveryState.FAILED, "hardware evidence changed before fast-recovery dispatch")
            return False
        node_managers = self._node_managers_from_targets(targets)

        payload = self._build_broadcast_payload(instance_id)
        payload["request_id"] = "recovery-" + uuid.uuid4().hex
        assert self.context is not None
        self.context.request_id = payload["request_id"]
        if abort:
            payload["params"]["abort"] = True

        logger.info(
            "Dispatching FT fast recovery to %d node managers for instance %d: "
            "attempt=%d request_id=%s abort=%s endpoint_count=%d endpoints=%s",
            len(node_managers),
            instance_id,
            self.context.attempt,
            self.context.request_id,
            "true" if abort else "omitted",
            len(targets),
            sorted((target.node_manager.pod_ip, target.engine_id) for target in targets),
        )

        def _apply(node_mgr: NodeManagerInfo) -> tuple[NodeManagerInfo, bool]:
            return node_mgr, NodeManagerApiClient.apply_fault_tolerance(node_mgr, payload)

        max_workers = min(16, len(node_managers))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(_apply, node_mgr) for node_mgr in node_managers]
            for future in as_completed(futures):
                if self.event.is_set():
                    self._finish(FastRecoveryState.FAILED, "fast recovery interrupted")
                    return False
                node_mgr, ok = future.result()
                if not ok:
                    self._finish(
                        FastRecoveryState.FAILED,
                        f"failed to dispatch fast recovery to node manager {node_mgr.pod_ip}",
                    )
                    return False
        return True

    def _wait_until_recovered(self, instance_id: int, targets: list[FastRecoveryTarget]) -> str | None:
        assert self.context is not None
        logger.info(
            "Waiting for all DP endpoints HEALTHY after FT fast recovery: "
            "instance=%d attempt=%d request_id=%s endpoint_count=%d timeout=%ss",
            instance_id,
            self.context.attempt,
            self.context.request_id,
            len(targets),
            self.round_timeout_sec,
        )
        deadline = time.monotonic() + float(self.round_timeout_sec)
        last_pending_count: int | None = None
        while time.monotonic() < deadline:
            if self.event.is_set():
                return "fast recovery interrupted"
            if not self._can_recover_instance(instance_id):
                return "instance is no longer eligible for in-place fast recovery"
            error, pending_count = self._poll_recovery(targets)
            if error == "":
                logger.info(
                    "All DP endpoints HEALTHY after FT fast recovery: instance=%d attempt=%d request_id=%s",
                    instance_id,
                    self.context.attempt,
                    self.context.request_id,
                )
                return None
            if error:
                return error
            if pending_count and pending_count != last_pending_count:
                logger.info(
                    "Waiting for %d engines to become HEALTHY after FT fast recovery: "
                    "instance=%d attempt=%d request_id=%s",
                    pending_count,
                    instance_id,
                    self.context.attempt,
                    self.context.request_id,
                )
            last_pending_count = pending_count
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self.event.wait(min(float(self.poll_interval_sec), remaining))
        return "fast recovery timed out waiting for all DP engines HEALTHY; pending: " + self._pending_engine_details(
            targets
        )

    def _policy_evidence_valid(self, instance_id: int) -> bool:
        """Guard against severe hardware without changing the current recovery branch."""
        from motor.controller.fault_tolerance.fault_manager import FaultManager  # pylint: disable=cyclic-import

        _, severe = FaultManager().get_recovery_hardware_evidence(instance_id)
        # Late recoverable codes belong to the next planning round. Only the
        # HBM policy waits for UCE clearance; other policies finish on engine health.
        return not severe

    def _complete_recovery(self, instance_id: int, error: str | None) -> None:
        """Fault-specific completion hook; common fast recovery only waits for engine health."""
        assert self.context is not None
        self.context.completion_started = True
        if not self._policy_evidence_valid(instance_id):
            self.failure_fallback = "InstanceReconfigurationStrategy"
            error = "hardware evidence changed during fast recovery; reconfiguration required"
        self._finish(FastRecoveryState.SUCCESS if error is None else FastRecoveryState.FAILED, error)

    @staticmethod
    def _pending_engine_details(targets: list[FastRecoveryTarget]) -> str:
        return "; ".join(
            f"pod={target.node_manager.pod_ip} engine_id={target.engine_id} status={target.status}"
            for target in targets
            if target.status != ENGINE_STATUS_HEALTHY
        )

    def _poll_recovery(self, targets: list[FastRecoveryTarget]) -> tuple[str | None, int]:
        """Return an error or completion marker with the number of pending engines."""
        assert self.context is not None
        snapshots: dict[tuple[str, int], dict] = {}
        for node_mgr in self._node_managers_from_targets(targets):
            payload = NodeManagerApiClient.query_fault_tolerance_status(node_mgr)
            for entry in payload.get("engines") or []:
                if isinstance(entry, dict) and entry.get("engine_id") is not None:
                    snapshots[(node_mgr.pod_ip, int(entry["engine_id"]))] = entry

        pending = 0
        failed = 0
        for target in targets:
            entry = snapshots.get((target.node_manager.pod_ip, target.engine_id), {})
            status = _endpoint_status(entry)
            target.status = status
            if status == ENGINE_STATUS_DEAD:
                return (
                    f"engine {target.engine_id} on {target.node_manager.pod_ip} became DEAD during fast recovery",
                    pending,
                )
            engines = entry.get("engines") or []
            current_round = bool(engines) and all(
                isinstance(engine, dict) and engine.get("last_ft_request_id") == self.context.request_id
                for engine in engines
            )
            # vLLM's HEALTHY push replaces the cached status and may omit the
            # request ID. Explicit IDs must still match this round.
            healthy = (
                status == ENGINE_STATUS_HEALTHY
                and bool(engines)
                and all(
                    engine.get("last_ft_request_id") in (None, self.context.request_id)
                    and engine.get("ft_state") != "recovering"
                    for engine in engines
                )
            )
            if not healthy:
                pending += 1
            if (
                current_round
                and status == ENGINE_STATUS_UNHEALTHY
                and all(engine.get("ft_state") == "failed" for engine in engines)
            ):
                failed += 1
        # Wait for every rank to leave the first collective before a second abort round.
        if failed == len(targets):
            self.context.all_ranks_failed = True
            return "all DP engines reported this fast-recovery round failed", pending
        if pending:
            return None, pending
        return "", 0

    @staticmethod
    def _build_broadcast_payload(instance_id: int) -> dict:
        return {"instruction": "retry", "params": {"instance_id": instance_id, "scope": "all_local_endpoints"}}

    @staticmethod
    def _node_managers_from_targets(targets: list[FastRecoveryTarget]) -> list[NodeManagerInfo]:
        node_managers: list[NodeManagerInfo] = []
        seen: set[tuple[str, str]] = set()
        for target in targets:
            key = (target.node_manager.pod_ip, target.node_manager.port)
            if key in seen:
                continue
            seen.add(key)
            node_managers.append(target.node_manager)
        return node_managers

    def _finish(self, state: FastRecoveryState, error: str | None = None) -> None:
        if self.context is not None:
            self.context.current_state = state
            self.context.last_error = error
        if state == FastRecoveryState.FAILED:
            self.mark_failed()
            logger.error(
                "FastRecovery strategy failed for instance %s: attempt=%s request_id=%s error=%s",
                self.context.instance_id if self.context else "?",
                self.context.attempt if self.context else "?",
                self.context.request_id if self.context else "?",
                error,
            )
        else:
            logger.info(
                "FastRecovery strategy succeeded for instance %s: attempt=%s request_id=%s policy completion criteria satisfied",
                self.context.instance_id if self.context else "?",
                self.context.attempt if self.context else "?",
                self.context.request_id if self.context else "?",
            )
        with self._lock:
            self._is_finished = True


class GenericFastRecoveryStrategy(EngineFastRecoveryStrategy):
    """Software fallback: resume first, then abort after unanimous failure."""

    abort_rounds = (False, True)

    def _complete_recovery(self, instance_id: int, error: str | None) -> None:
        if error is not None and not self.event.is_set() and self._policy_evidence_valid(instance_id):
            self.failure_fallback = "EngineRelaunchStrategy"
        super()._complete_recovery(instance_id, error)


class TokenReinferenceStrategy(GenericFastRecoveryStrategy):
    """Lingqu entry point sharing the no-fault-code fast-recovery and relaunch policy."""

    recovery_cause = RecoveryCause.TOKEN_REINFERENCE


class HbmUceRecoveryStrategy(EngineFastRecoveryStrategy):
    """HBM UCE: abort once, verify clearance even when engine recovery fails."""

    abort_rounds = (True,)
    recovery_cause = RecoveryCause.HBM

    def _complete_recovery(self, instance_id: int, error: str | None) -> None:
        assert self.context is not None
        self.context.completion_started = True
        self.context.current_state = FastRecoveryState.WAITING_CLEARANCE
        if self.event.is_set():
            self._finish(FastRecoveryState.FAILED, error or "fast recovery interrupted")
            return
        clearance_error = self._wait_for_hbm_clearance(instance_id)
        if clearance_error:
            self._finish(FastRecoveryState.FAILED, clearance_error)
            return
        if not self._policy_evidence_valid(instance_id):
            self._finish(FastRecoveryState.FAILED, "hardware evidence changed during HBM UCE recovery")
            return
        if error is None:
            health, _ = self._poll_recovery(self.context.targets)
            if health != "":
                error = health or "engine health changed while waiting for HBM UCE clearance"
        if error is not None:
            self.failure_fallback = "EngineRelaunchStrategy"
        self._finish(FastRecoveryState.SUCCESS if error is None else FastRecoveryState.FAILED, error)

    def _wait_for_hbm_clearance(self, instance_id: int) -> str | None:
        """Wait on an independent deadline, even when the engine recovery round failed."""
        timeout = self._resolve_config("hbm_uce_clearance_timeout_sec", 60)
        logger.info("Waiting for HBM UCE clearance: instance=%d timeout=%ss", instance_id, timeout)
        deadline = time.monotonic() + float(timeout)
        previous: list[str] | None = None
        while not self.event.is_set():
            pending = self._check_hbm_clearance(instance_id, previous)
            if not pending:
                return None
            previous = pending
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "fast recovery timed out waiting for HBM UCE 0x80e01801 clearance; pending: " + "; ".join(
                    pending
                )
            self.event.wait(min(float(self.poll_interval_sec), remaining))
        return "fast recovery interrupted during HBM UCE clearance"

    @staticmethod
    def _pending_hbm_uce_faults(instance_id: int) -> list[str]:
        from motor.controller.fault_tolerance.fault_manager import (  # pylint: disable=cyclic-import
            FaultManager,
        )

        return FaultManager().get_pending_hbm_uce_faults(instance_id)

    def _check_hbm_clearance(self, instance_id: int, previous_faults: list[str] | None) -> list[str]:
        """Check 1801 clearance and log changes within the independent stage deadline."""
        assert self.context is not None
        pending = self._pending_hbm_uce_faults(instance_id)
        if not pending:
            logger.info(
                "HBM UCE clearance check passed: instance=%d attempt=%d request_id=%s "
                "no pending 0x80e01801 on instance hosting nodes",
                instance_id,
                self.context.attempt,
                self.context.request_id,
            )
        elif pending != previous_faults:
            logger.info(
                "Waiting for HBM UCE 0x80e01801 clearance: instance=%d attempt=%d request_id=%s pending=%s",
                instance_id,
                self.context.attempt,
                self.context.request_id,
                pending,
            )
        return pending


def _endpoint_status(entry: dict) -> str:
    """Reduce local engine states, treating missing evidence as unknown."""
    if entry.get("query_error"):
        return "unknown"
    inner = entry.get("engines") or []
    if not inner or any(not isinstance(engine, dict) for engine in inner):
        return "unknown"
    worst = ENGINE_STATUS_HEALTHY
    for engine in inner:
        status = str(engine.get("status") or "unknown").lower()
        if status == ENGINE_STATUS_DEAD:
            worst = ENGINE_STATUS_DEAD
        elif status != ENGINE_STATUS_HEALTHY and worst != ENGINE_STATUS_DEAD:
            worst = status
    return worst


def _iter_recovery_endpoints(instance) -> tuple[Endpoint, ...]:
    """Return the routable endpoints that must enter the DP fast-recovery barrier."""
    if hasattr(instance, "get_all_endpoints"):
        endpoints = instance.get_all_endpoints()
        if endpoints:
            return tuple(endpoints)

    endpoints_by_pod = getattr(instance, "endpoints", {}) or {}
    endpoints: list[Endpoint] = []
    for pod_endpoints in endpoints_by_pod.values():
        if not isinstance(pod_endpoints, dict):
            continue
        for endpoint in pod_endpoints.values():
            if getattr(endpoint, "headless", False):
                continue
            endpoints.append(endpoint)
    return tuple(endpoints)
