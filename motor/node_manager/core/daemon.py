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

from motor.common.resources.instance import PDRole
from motor.common.resources.endpoint import Endpoint
from motor.common.utils.singleton import ThreadSafeSingleton
from motor.common.logger import get_logger
from motor.config.node_manager import NodeManagerConfig
from motor.node_manager.core.services.registry import (
    SERVICE_ENGINE,
    SERVICE_KV_STORE,
    DaemonService,
    PreparableService,
    registry,
)

logger = get_logger(__name__)


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

        # --- Discover & instantiate all active services ---
        registry.discover()
        active = registry.get_active()
        self._services: dict[str, DaemonService] = {}

        hardware_type = str(config.basic_config.hardware_type)

        def _sort_key(name: str) -> tuple[bool, int]:
            reg = active[name]
            if reg.prepare_priority is not None:
                return (False, reg.prepare_priority)
            return (True, 0)

        for name in sorted(active, key=_sort_key):
            reg = active[name]
            self._services[name] = reg.instantiate(
                hardware_type=hardware_type,
                config=config,
            )

        # --- Process monitor ---
        self._monitor_thread: threading.Thread | None = None
        self._monitor_stop = threading.Event()
        self._monitor_interval = 5

        self._initialized = True
        self._start_process_monitor()

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

    def stop(self) -> None:
        self._monitor_stop.set()
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5.0)
        self._monitor_thread = None

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
                    svc.health_check()
                except Exception:
                    logger.exception("health_check failed for service %r", name)
            self._monitor_stop.wait(self._monitor_interval)
