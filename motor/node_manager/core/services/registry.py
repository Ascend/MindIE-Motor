# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
# MindIE is licensed under Mulan PSL v2.
# You can use this software according to the terms and conditions of the Mulan PSL v2.
# You may obtain a copy of Mulan PSL v2 at:
#         http://license.coscl.org.cn/MulanPSL2
# THIS SOFTWARE IS PROVIDED ON AN "AS IS" BASIS, WITHOUT WARRANTIES OF ANY KIND,
# EITHER EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO NON-INFRINGEMENT,
# MERCHANTABILITY OR FIT FOR A PARTICULAR PURPOSE.
# See the Mulan PSL v2 for more details.

"""Decorator-based service registry for the NodeManager daemon.

Usage::

    from motor.node_manager.core.services.registry import register_service

    @register_service("engine")
    class EngineService:
        ...

    @register_service("kv_store", backend="memcache", prepare_priority=10)
    class LocalService:
        ...

The Daemon discovers active services at init time via :func:`registry.get_active`
and instantiates them.  Non-matching backends are never instantiated.
"""

import importlib
import threading
from collections.abc import Callable
from typing import Protocol, runtime_checkable

from motor.common.logger import get_logger
from motor.common.utils.env import Env

logger = get_logger(__name__)

# --- Well-known service names ---
SERVICE_ENGINE: str = "engine"
SERVICE_KV_STORE: str = "kv_store"

# --- Module discovery: backend → modules to import for @register_service ---
# ``None`` key = always-active modules (imported unconditionally).
_MODULE_MAP: dict[str | None, list[str]] = {
    None: ["motor.node_manager.core.services.engine"],
    "memcache": ["motor.node_manager.core.services.local_service"],
    # Future: "mooncake": ["motor.node_manager.core.services.mooncake"],
    # Future: "yuanrong": ["motor.node_manager.core.services.yuanrong"],
}


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class DaemonService(Protocol):
    """Minimal interface every daemon-managed service must satisfy.

    ``stop()`` must be safe to call multiple times (idempotent).
    ``health_check()`` is called periodically by the Daemon process monitor;
    each service encapsulates its own failure detection and self-restart logic.
    """

    def stop(self) -> None:
        """Stop the service.  Must be idempotent."""
        ...

    def health_check(self) -> None:
        """Check service health and self-restart if needed.

        Called by the Daemon process monitor on every tick (~5 s).
        The implementation handles its own failure detection and recovery
        (e.g. ``os.kill`` for subprocess PIDs, ``thread.is_alive`` for threads).
        """
        ...


@runtime_checkable
class PreparableService(DaemonService, Protocol):
    """A service that needs pre-flight preparation before the engine starts."""

    def prepare(self, **kwargs) -> None:
        """Run before ``EngineService.pull()``.

        *kwargs* include ``endpoints_count`` (int) so the service can
        divide per-node DRAM across DP ranks.
        """
        ...


# ---------------------------------------------------------------------------
# Registration record
# ---------------------------------------------------------------------------


class _ServiceRegistration:
    """Internal record for one registered service."""

    __slots__ = (
        "name",
        "service_class",
        "backend",
        "prepare_priority",
        "factory",
        "instance",
    )

    def __init__(
        self,
        name: str,
        service_class: type,
        *,
        backend: str | None = None,
        prepare_priority: int | None = None,
        factory: Callable[..., DaemonService] | None = None,
    ):
        self.name = name
        self.service_class = service_class
        self.backend = backend  # None = always active
        self.prepare_priority = prepare_priority  # None = no prepare phase
        self.factory = factory  # None = use service_class(**kwargs)
        self.instance: DaemonService | None = None

    @property
    def is_active(self) -> bool:
        """True when the service's backend matches the current configuration."""
        if self.backend is None:
            return True
        return Env.kv_store_backend == self.backend

    def instantiate(self, **kwargs) -> DaemonService:
        """Create and store the service instance.

        Uses *factory* when provided, otherwise calls ``service_class(**kwargs)``.
        """
        if self.factory is not None:
            self.instance = self.factory(**kwargs)
        else:
            self.instance = self.service_class(**kwargs)
        logger.info("Instantiated service %r (backend=%s)", self.name, self.backend)
        return self.instance


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class _ServiceRegistry:
    """Thread-safe registry of daemon-managed services.

    Services declare themselves via the ``@register_service`` decorator.
    The Daemon queries active services at init time via :meth:`get_active`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._registrations: dict[str, _ServiceRegistration] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> None:
        """Import service modules so ``@register_service`` decorators fire.

        Always-active modules are imported unconditionally; backend-specific
        modules are imported only when ``Env.kv_store_backend`` matches.

        Called once by the Daemon before :meth:`get_active`.
        """
        backend = Env.kv_store_backend
        for mod_path in _MODULE_MAP.get(None, []):
            importlib.import_module(mod_path)
        if backend:
            for mod_path in _MODULE_MAP.get(backend, []):
                importlib.import_module(mod_path)
        logger.debug(
            "Service discovery complete (backend=%s, registered=%d)",
            backend or "<none>",
            len(self._registrations),
        )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        *,
        backend: str | None = None,
        prepare_priority: int | None = None,
        factory: Callable[..., DaemonService] | None = None,
    ):
        """Decorator: register *cls* as a daemon-managed service.

        Parameters
        ----------
        name:
            Unique service identifier (e.g. ``SERVICE_ENGINE``, ``SERVICE_KV_STORE``).
        backend:
            If set, the service is only active when
            ``Env.kv_store_backend == backend``.  ``None`` means always active.
        prepare_priority:
            If set, the Daemon calls ``svc.prepare()`` on this service before
            starting the engine.  Lower values run first.  ``None`` means the
            service does not participate in the prepare phase.
        factory:
            Optional callable ``(hardware_type, config) -> DaemonService``.
            When provided, :meth:`_ServiceRegistration.instantiate` delegates to
            *factory* instead of calling ``cls(**kwargs)`` directly.  This keeps
            constructor specifics out of the daemon.
        """

        def decorator(cls: type) -> type:
            with self._lock:
                existing = self._registrations.get(name)
                if existing is not None:
                    logger.warning(
                        "Service %r (from %s) is replacing %s",
                        name,
                        cls.__module__,
                        existing.service_class.__module__,
                    )
                self._registrations[name] = _ServiceRegistration(
                    name,
                    cls,
                    backend=backend,
                    prepare_priority=prepare_priority,
                    factory=factory,
                )
                logger.debug(
                    "Registered service %r from %s (backend=%s, prepare=%s, factory=%s)",
                    name,
                    cls.__module__,
                    backend,
                    prepare_priority,
                    factory is not None,
                )
            return cls

        return decorator

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_active(self) -> dict[str, _ServiceRegistration]:
        """Return registrations whose backend matches the current environment."""
        with self._lock:
            return {name: reg for name, reg in self._registrations.items() if reg.is_active}

    def get_preparable(self) -> list[_ServiceRegistration]:
        """Active, preparable registrations sorted by priority (ascending)."""
        active = self.get_active().values()
        preparable = [r for r in active if r.prepare_priority is not None]
        preparable.sort(key=lambda r: r.prepare_priority)  # type: ignore[arg-type,return-value]
        return preparable

    def get_instance(self, name: str) -> DaemonService | None:
        """Return the instantiated service by name, or *None*."""
        reg = self._registrations.get(name)
        return reg.instance if reg is not None else None


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

registry = _ServiceRegistry()
register_service = registry.register
