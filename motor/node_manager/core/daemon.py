# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

import threading
import time

from motor.common.resources.instance import PDRole
from motor.common.resources.endpoint import Endpoint
from motor.node_manager.core.services.native_engine.models import RuntimeState
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.logger import get_logger
from motor.config.node_manager import NodeManagerConfig
from motor.node_manager.api_client.controller_api_client import ControllerApiClient
from motor.node_manager.core.fault_reporter import FaultReporter
from motor.node_manager.core.heartbeat_manager import HeartbeatManager
from motor.node_manager.core.services.protocols import DaemonService, PreparableService
from motor.node_manager.core.services.registry import (
    SERVICE_ENGINE,
    SERVICE_KV_STORE,
    registry,
)

logger = get_logger(__name__)


class EngineRestartInProgressError(RuntimeError):
    """Raised when a second engine relaunch starts while one is in progress.

    The Controller's dispatch retries may resend a restart command whose
    response was lost; overlapping relaunches would kill/pull engines twice
    and collide on ports — the route maps this to 409 and the strategy
    escalates to container restart.
    """


class EngineRestartParamError(RuntimeError):
    """Raised when the relaunch cannot resolve its launch params.

    Covers "no engine start recorded" (the NodeManager never received a
    start command) and "instance id mismatch" (the NodeManager now serves a
    different instance) — the route maps this to 400.
    """


class Daemon(ThreadSafeSingleton):
    """Orchestrate engine subprocess and KV-store service lifecycle.

    Backend-agnostic — all services are discovered and instantiated via
    :mod:`motor.node_manager.core.services.registry`.  Adding a new
    backend only requires a service module with ``@register_service``
    and an entry in the registry's ``_MODULE_MAP``; this class stays
    unchanged.
    """

    def __init__(self, config: NodeManagerConfig | None = None):
        if hasattr(self, "_initialized"):
            return

        if config is None:
            config = NodeManagerConfig.from_json()

        # --- Determine which services to activate ---
        # Derived from kv_cache_store_config in user_config.json:
        #   mode="combined" (default): engine + KV in same pod
        #   mode="separated":         KV-only pod (no engine, no heartbeat)
        self._config = config
        self.config_lock = threading.RLock()

        #   no kv config:             engine-only pod
        kv_cfg = config.kv_cache_store_config
        if not kv_cfg.enable:
            services = "engine"
        elif kv_cfg.mode == "separated":
            services = kv_cfg.backend
        else:
            services = f"engine,{kv_cfg.backend}"

        # --- Discover & instantiate all active services ---
        registry.discover(services=services)
        self._services: dict[str, DaemonService] = {}

        hardware_type = str(config.basic_config.hardware_type)

        for name, reg in registry.get_active_sorted():
            self._services[name] = reg.instantiate(
                hardware_type=hardware_type,
                config=config,
            )

        # --- Process monitor ---
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._monitor_interval = 5

        # --- Suicide arbitration (single decision point for pod rescheduling) ---
        # The heartbeat module only reports endpoint-state facts; the Daemon
        # (process/service lifecycle owner) decides whether to kill the pod:
        # N consecutive abnormal-endpoint observations after the cold-start
        # grace period, unless the suicide counter is frozen (engine relaunch
        # window). A deadline (not a boolean) freeze keeps the fallback alive:
        # if the abort message is lost the freeze expires and counting resumes.
        self._suicide_abnormal_count = 0
        self._suicide_freeze_until = 0.0
        self._should_suicide = False
        self._suicide_lock = threading.Lock()
        self._last_endpoints_generation = -1
        # Consecutive abnormal observations that trigger suicide. Arbitrated
        # in a dedicated loop paced by the configured heartbeat interval
        # (matching the former heartbeat cadence, which counted per heartbeat
        # report): threshold x interval = ~15s of continuous unhealthiness
        # with the default 3s interval — same as before the move.
        self._suicide_threshold = 5
        self._suicide_interval = getattr(config.basic_config, "heartbeat_interval_seconds", 3.0)
        self._suicide_thread: threading.Thread | None = None
        # PIDs whose death was already reported to the Controller (dedup by
        # pid — a relaunched engine gets a fresh PID, so it can be reported
        # again on its next death).
        # Engine relaunch in progress (route serializes via this flag — a
        # second restart is rejected 409 instead of racing the pull). Owned
        # here because the Daemon is the process-lifecycle owner; the flag is
        # set/cleared inside restart_engine, so the lock only guards access.
        # Engine FT status reporter: the third monitoring source alongside the
        # process monitor and the suicide arbitration — owned by the Daemon
        # (the process-lifecycle owner), started when engines are pulled,
        # paused across an engine relaunch.
        self._fault_reporter = FaultReporter(config)
        self._endpoints_info: list[Endpoint] = []
        # Set once the pulled engines' mgmt ports accept connections — the
        # HeartbeatManager starts probing engine status only after this
        # handoff (no engine readiness logic of its own).
        self._engine_ready = threading.Event()
        self._engine_restart_in_progress = False
        self._engine_restart_lock = threading.Lock()
        self._reported_dead_pids: set[int] = set()
        # Endpoint ids whose ABNORMAL state was already reported (dedup —
        # cleared once all endpoints recover, so a re-failure is reported
        # again).
        self._reported_abnormal_ep_ids: set[int] = set()
        # Endpoint ids that have ever been NORMAL. Cold-starting engines
        # (model loading) are never NORMAL yet — their ABNORMAL observations
        # must not be reported as engine death (they would trigger a relaunch
        # that interrupts the loading).
        self._seen_normal_ep_ids: set[int] = set()

        self._initialized = True
        self._start_process_monitor()
        self._start_suicide_arbitration()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def pull_engine(
        self,
        pd_role_info: PDRole,
        endpoints_info: list[Endpoint],
        instance_id: int,
        master_dp_ip: str,
        d2d_peer_ips: list[str] | None = None,
        node_rank: int = 0,
    ) -> None:
        self._endpoints_info = endpoints_info

        # Phase 1: run PreparableService.prepare() before engines start
        for reg in registry.get_preparable():
            svc = self._services.get(reg.name)
            if svc is not None and isinstance(svc, PreparableService):
                svc.prepare(endpoints_count=len(endpoints_info))

        # Phase 2: launch engine subprocesses
        engine = self._services.get(SERVICE_ENGINE)
        if engine is not None:
            engine.pull(  # type: ignore[attr-defined]
                pd_role_info,
                endpoints_info,
                instance_id,
                master_dp_ip,
                d2d_peer_ips=d2d_peer_ips,
                node_rank=node_rank,
            )

        # Phase 3: signal engine readiness in the background (mgmt ports up),
        # then start the FT status reporter on the launched engines
        # (idempotent — a restart keeps the existing thread).
        self._engine_ready.clear()
        threading.Thread(
            target=self._wait_engines_ready,
            args=(endpoints_info,),
            daemon=True,
            name="engine_ready_wait",
        ).start()
        self._fault_reporter.start(endpoints_info)

    def restart_engine(self, instance_id: int | None = None) -> None:
        """Relaunch the engine service in place, owned entirely by the Daemon.

        Serializes relaunches (second call raises
        :class:`EngineRestartInProgressError`), resolves the launch params
        from the RegisterManager (raises :class:`EngineRestartParamError` when
        nothing was started or the instance id mismatches), freezes suicide
        arbitration for the relaunch window (unfrozen on failure so the
        container-restart fallback stays live), suspends the FaultReporter
        while the engines are down and resumes it afterwards. Used by the
        Controller-driven engine relaunch flow (``/node-manager/engine-restart``).
        """
        with self._engine_restart_lock:
            if self._engine_restart_in_progress:
                raise EngineRestartInProgressError()
            self._engine_restart_in_progress = True
        try:
            self._fault_reporter.pause()
            try:
                restart_params = self._get_register_manager().get_restart_params()
                if restart_params is None:
                    raise EngineRestartParamError("no engine start recorded")
                if instance_id is not None and instance_id != restart_params["instance_id"]:
                    raise EngineRestartParamError("instance id mismatch")
                with self.config_lock:
                    freeze_sec = self._config.fault_tolerance_config.engine_restart_freeze_sec
                self.freeze_suicide(freeze_sec)
                try:
                    # Phase 1: re-run PreparableService.prepare() (idempotent) before engines start
                    for reg in registry.get_preparable():
                        svc = self._services.get(reg.name)
                        if svc is not None and isinstance(svc, PreparableService):
                            svc.prepare(endpoints_count=len(restart_params["endpoints"]))

                    # Phase 2: engine service owns its own relaunch lifecycle.
                    engine = self._services.get(SERVICE_ENGINE)
                    if engine is not None:
                        engine.restart(  # type: ignore[attr-defined]
                            PDRole(restart_params["role"]),
                            restart_params["endpoints"],
                            restart_params["instance_id"],
                            restart_params["master_dp_ip"],
                            d2d_peer_ips=restart_params["d2d_peer_ips"],
                            node_rank=restart_params["node_rank"],
                        )
                except Exception:
                    self.unfreeze_suicide()
                    raise
            finally:
                self._fault_reporter.resume()
        finally:
            with self._engine_restart_lock:
                self._engine_restart_in_progress = False

    @staticmethod
    def _get_register_manager():
        """RegisterManager singleton (owns the engine launch params).

        Lazy import: register_manager pulls in controller_api_client which
        pulls in more of the module graph; keep it out of daemon import time.
        """
        from motor.node_manager.core.register_manager import RegisterManager  # pylint: disable=cyclic-import

        return RegisterManager()

    def update_config(self, config: NodeManagerConfig) -> None:
        """Apply a new config, (re)configuring the FT status reporter."""
        with self.config_lock:
            self._config = config
        self._fault_reporter.update_config(config, self._endpoints_info)
        logger.info("Daemon configuration updated")

    def pull_kv_store(self) -> None:
        """Start/restart the KV store service (if active)."""
        kv = self._services.get(SERVICE_KV_STORE)
        if kv is not None:
            kv.pull()  # type: ignore[attr-defined]

    @property
    def engine_pids(self) -> list[int]:
        """Return a snapshot of engine PIDs (thread-safe copy)."""
        engine = self._services.get(SERVICE_ENGINE)
        if engine is not None:
            return engine.pid_list()  # type: ignore[attr-defined]
        return []

    @property
    def has_engine(self) -> bool:
        """True when the engine service is active (i.e. this pod runs inference)."""
        return SERVICE_ENGINE in self._services

    def get_engine_runtime_state(self, endpoint: Endpoint, instance_id: int) -> RuntimeState:
        """Return the native runtime state for one locally managed endpoint."""
        engine = self._services.get(SERVICE_ENGINE)
        if engine is None:
            return RuntimeState.STOPPED
        return engine.runtime_state(endpoint, instance_id)  # type: ignore[attr-defined]

    def get_engine_metrics_target(self, endpoint: Endpoint) -> str | None:
        engine = self._services.get(SERVICE_ENGINE)
        if engine is None:
            return None
        return engine.metrics_target(endpoint)  # type: ignore[attr-defined]

    @property
    def engine_ready_event(self) -> threading.Event:
        """Set once the pulled engines' readiness probes succeed.

        Injected into the HeartbeatManager at start time: the status polling
        waits for this handoff instead of probing engine readiness itself.
        """
        return self._engine_ready

    def _wait_engines_ready(self, endpoints_info: list[Endpoint]) -> None:
        """Background wait for the engines' readiness probes, then flag ready.

        Runs detached so the start command returns immediately; the event is
        set even when the wait aborted (engine died or the load exceeds the
        timeout) so the HeartbeatManager never blocks on it — its probing
        then reports ABNORMAL (or keeps STARTING) and the arbitration handles
        the death.
        """
        engine = self._services.get(SERVICE_ENGINE)
        if engine is not None:
            try:
                engine.wait_ready(endpoints_info)  # type: ignore[attr-defined]
            except Exception:
                logger.exception("Failed to wait for engines ready")
        self._engine_ready.set()

    def is_engine_restart_in_progress(self) -> bool:
        with self._engine_restart_lock:
            return self._engine_restart_in_progress

    def stop(self) -> None:
        self._monitor_stop.set()
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5.0)
        self._monitor_thread = None
        if self._suicide_thread is not None and self._suicide_thread.is_alive():
            self._suicide_thread.join(timeout=5.0)
        self._suicide_thread = None
        self._fault_reporter.stop()

        # Stop services in reverse registration order
        for svc in reversed(list(self._services.values())):
            try:
                svc.stop()
            except Exception:
                logger.exception("Error stopping service")

    # ------------------------------------------------------------------
    # process monitor
    # ------------------------------------------------------------------

    def _start_process_monitor(self) -> None:
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self._process_monitor_loop,
            daemon=True,
            name="process_monitor",
        )
        self._monitor_thread.start()
        logger.info("Process monitor thread started (interval=%ss)", self._monitor_interval)

    def _process_monitor_loop(self) -> None:
        while not self._monitor_stop.is_set():
            for name, svc in self._services.items():
                try:
                    deaths = svc.health_check() or []
                    self._handle_engine_deaths(deaths)
                except Exception:
                    logger.exception("Health check failed for service %s", name)

            if self._monitor_stop.wait(self._monitor_interval):
                break

    def _handle_engine_deaths(self, deaths: list) -> None:
        """Handle engine subprocess deaths: report to Controller, freeze on success.

        The engine-relaunch flow is a base capability — it must work without
        fault-tolerance reporting enabled, so the Daemon (the process
        lifecycle owner) detects PID deaths via the monitor's health_check
        and reports them itself. Dedup by pid: a relaunched engine gets a
        fresh PID, so the next death is reported again.

        The suicide freeze is only applied after a successful report AND when
        the in-place relaunch is enabled: a failed report (Controller
        unreachable) or a disabled relaunch switch means no relaunch is
        coming, so the arbitration must keep counting and the
        container-restart fallback (k8s) stays live instead of being frozen.
        """
        if not deaths:
            return
        for pid, endpoint_id in deaths:
            if pid in self._reported_dead_pids:
                continue
            try:
                with self.config_lock:
                    pod_ip = self._config.api_config.pod_ip
                    enable_relaunch = self._config.fault_tolerance_config.enable_engine_relaunch
                    freeze_sec = self._config.fault_tolerance_config.engine_restart_wait_timeout_sec
                if self._report_engine_death(endpoint_id, pod_ip):
                    if enable_relaunch:
                        self.freeze_suicide(freeze_sec)
                    self._reported_dead_pids.add(pid)
                    logger.error(
                        "Engine death reported to Controller: endpoint_id=%s pid=%s (relaunch expected)",
                        endpoint_id,
                        pid,
                    )
            except Exception as e:
                logger.error("Failed to handle engine death pid=%s: %s", pid, e)

    @staticmethod
    def _report_engine_death(endpoint_id: int, pod_ip: str) -> bool:
        """Report a dead engine to the Controller via the shared software-fault channel."""
        fault_data = {
            "exception_type": "EngineDeadError",
            "exception_message": "Engine process died",
            "engine_id": endpoint_id,
            "engine_status": 1,
            "pod_ip": pod_ip,
        }
        try:
            return ControllerApiClient.report_software_fault(fault_data)
        except Exception as e:
            logger.error("Failed to report engine death to Controller: %s", e)
            return False

    def _start_suicide_arbitration(self) -> None:
        """Dedicated 3s suicide-arbitration loop.

        Kept separate from the 5s process monitor so the arbitration cadence
        (3s, matching the former heartbeat rhythm: 5 x 3s ~ 15s to suicide)
        does not change the process health-check cadence.
        """
        if self._suicide_thread is not None and self._suicide_thread.is_alive():
            return
        self._suicide_thread = threading.Thread(
            target=self._suicide_arbitration_loop,
            daemon=True,
            name="suicide_arbitration",
        )
        self._suicide_thread.start()
        logger.info("Suicide arbitration thread started (interval=%ss)", self._suicide_interval)

    def _suicide_arbitration_loop(self) -> None:
        while not self._monitor_stop.is_set():
            try:
                self._check_suicide_condition()
            except Exception:
                logger.exception("Suicide arbitration failed")

            if self._monitor_stop.wait(self._suicide_interval):
                break

    # ------------------------------------------------------------------
    # suicide arbitration (pod rescheduling decision)
    # ------------------------------------------------------------------

    def _check_suicide_condition(self) -> None:
        """One arbitration round: observe endpoint health and update the counter.

        State facts come from the HeartbeatManager (endpoint status, grace
        period, endpoint generation); the decision (consecutive abnormal
        count >= threshold -> should_suicide) lives here.
        """
        hb = HeartbeatManager()
        generation = hb.endpoints_generation()
        if generation != self._last_endpoints_generation:
            # Endpoints were (re)set — restart the counting window.
            self._last_endpoints_generation = generation
            with self._suicide_lock:
                self._suicide_abnormal_count = 0
                self._should_suicide = False
            return

        if self.is_suicide_frozen() or hb.is_within_grace_period():
            with self._suicide_lock:
                self._suicide_abnormal_count = 0
            return

        self._seen_normal_ep_ids.update(hb.normal_endpoint_ids())
        if hb.has_abnormal_endpoints():
            self._report_abnormal_engines(hb.abnormal_endpoint_ids())
            with self._suicide_lock:
                self._suicide_abnormal_count += 1
                if self._suicide_abnormal_count >= self._suicide_threshold:
                    logger.error(
                        "Reached %d consecutive abnormal endpoint observations, "
                        "setting suicide flag for main to handle (k8s pod restart)",
                        self._suicide_threshold,
                    )
                    self._should_suicide = True
        else:
            with self._suicide_lock:
                self._suicide_abnormal_count = 0
            # All endpoints recovered — allow re-reporting on the next failure.
            self._reported_abnormal_ep_ids.clear()

    def _report_abnormal_engines(self, abnormal_ep_ids: list[int]) -> None:
        """Report ABNORMAL endpoints as dead engines so the Controller relaunches them.

        Covers the case the PID monitor cannot see: the native engine alive
        but its internal executor (vLLM EngineCore) died — the heartbeat marks
        the endpoint ABNORMAL (business-level health failed). Dedup per
        endpoint: a recovered (and re-failed) endpoint is reported again.

        The suicide freeze is only applied after a successful report (see
        :meth:`_handle_engine_deaths`): a Controller that cannot be reached
        must not keep the fallback frozen.
        """
        for ep_id in abnormal_ep_ids:
            if ep_id in self._reported_abnormal_ep_ids:
                continue
            if ep_id not in self._seen_normal_ep_ids:
                # Never NORMAL yet = cold start (model loading) — not a death.
                continue
            try:
                with self.config_lock:
                    pod_ip = self._config.api_config.pod_ip
                    enable_relaunch = self._config.fault_tolerance_config.enable_engine_relaunch
                    freeze_sec = self._config.fault_tolerance_config.engine_restart_wait_timeout_sec
                if self._report_engine_death(ep_id, pod_ip):
                    if enable_relaunch:
                        self.freeze_suicide(freeze_sec)
                    self._reported_abnormal_ep_ids.add(ep_id)
                    logger.error(
                        "Engine death reported to Controller: endpoint_id=%s (ABNORMAL, relaunch expected)",
                        ep_id,
                    )
            except Exception as e:
                logger.error("Failed to report abnormal engine endpoint %s: %s", ep_id, e)

    def freeze_suicide(self, seconds: float) -> None:
        """Freeze suicide arbitration for ``seconds`` and reset counting state.

        Used by the engine relaunch flow: while engines are being re-pulled
        (or a potential engine fault is being reported to the Controller) the
        abnormal observations must not accumulate the threshold that would
        kill this pod before recovery completes.
        """
        with self._suicide_lock:
            self._suicide_freeze_until = time.monotonic() + seconds
            self._suicide_abnormal_count = 0
            self._should_suicide = False

    def unfreeze_suicide(self) -> None:
        """Resume suicide arbitration immediately (container-restart fallback)."""
        with self._suicide_lock:
            self._suicide_freeze_until = 0.0

    def is_suicide_frozen(self) -> bool:
        with self._suicide_lock:
            return time.monotonic() < self._suicide_freeze_until

    def should_suicide(self) -> bool:
        """True when the pod should reschedule (main loop handles shutdown)."""
        with self._suicide_lock:
            return self._should_suicide
